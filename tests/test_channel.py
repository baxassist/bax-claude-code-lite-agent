"""Канал Бакса внутри сессии Claude Code (плагин `plugins/bax/server.py`).

Настоящий Claude Code здесь не нужен: сессию и связь с Баксом подменяем, а проверяем то,
ради чего канал и написан — задача с телефона попадает в сессию, ответ уходит обратно,
а чего этот режим не умеет, он честно говорит.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "bax" / "server.py"


def load_module():
    spec = importlib.util.spec_from_file_location("bax_channel", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


channel_module = load_module()


class FakeOutbound:
    """Куда канал пишет уведомления сессии: тот же вызов, что у настоящего соединения MCP."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def notify(self, method: str, params: dict) -> None:
        self.sent.append((method, params))


class FakeSession:
    def __init__(self) -> None:
        self._connection = type("C", (), {"outbound": FakeOutbound()})()


class FakeLink:
    """Сторона Бакса: складываем кадры, как это делает сервер."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send(self, type_: str, **fields) -> None:
        self.frames.append({"type": type_, **fields})

    def of(self, type_: str) -> list[dict]:
        return [f for f in self.frames if f["type"] == type_]


@pytest.fixture
def channel(tmp_path, monkeypatch):
    monkeypatch.setattr(channel_module, "REGISTRY", tmp_path / ".bax" / "lite.json")
    made = channel_module.Channel(tmp_path / "проект")
    made.session = FakeSession()
    made.link = FakeLink()
    return made


async def test_task_from_the_phone_reaches_the_session(channel):
    """Задача с телефона уходит в сессию уведомлением канала — модель видит её сама,
    без единого нажатия в терминале."""
    await channel.on_frame({"type": "run", "text": "почини тест"})

    method, params = channel.session._connection.outbound.sent[0]
    assert method == "notifications/claude/channel"
    assert params["content"] == "почини тест"
    assert params["meta"]["source"] == "bax"

    assert channel.link.of("message")[0]["kind"] == "user", "задача видна в ленте приложения"
    assert channel.link.of("status")[-1]["state"] == "busy"


async def test_reply_goes_back_to_the_phone(channel):
    """Ответ модели доходит до телефона только через `reply`: написанное в терминал
    человек не видит. После ответа ход закончен."""
    await channel.reply("починил")

    assert channel.link.of("message")[-1] == {
        "type": "message", "id": 1, "kind": "assistant", "text": "починил",
    }
    assert channel.link.of("done")[-1]["id"] == 1
    assert channel.link.of("status")[-1]["state"] == "ready"


async def test_permission_card_and_answer(channel):
    """Разрешение: карточка уходит в телефон, ответ — обратно в сессию. Пока ждём человека,
    агент в состоянии «жду ответа»."""
    await channel.ask("req-1", "Bash", "Выполнить ls", "ls -la")

    question = channel.link.of("question")[0]
    assert question["kind"] == "permission" and question["tool"] == "Bash"
    assert question["rule"] == "", "«больше не спрашивать» в этом режиме нет"
    assert channel.link.of("status")[-1]["state"] == "waiting"

    await channel.on_frame({"type": "answer", "question_id": question["question_id"],
                            "verdict": "allow"})
    method, params = channel.session._connection.outbound.sent[-1]
    assert method == "notifications/claude/channel/permission"
    assert params == {"request_id": "req-1", "behavior": "allow"}


async def test_what_this_mode_cannot_do_is_said_plainly(channel):
    """«Стоп», смена модели и сессий здесь некому исполнять — приложение прячет эти кнопки
    по `caps`, а если кадр всё же придёт, канал отвечает понятной ошибкой."""
    await channel.on_frame({"type": "cancel", "scope": "turn"})
    assert channel.link.of("error")[-1]["code"] == "unsupported"

    await channel.on_ready({})
    caps = channel.link.of("caps")[-1]
    assert caps["mode"] == "lite" and caps["remember"] is False
    assert "run" in caps["supports"] and "cancel" not in caps["supports"]


async def test_session_closed_means_agent_offline(channel):
    """Сессию закрыли — задача не уходит в никуда: телефон получает честное «не запущен»."""
    channel.session = None
    await channel.on_frame({"type": "run", "text": "собери проект"})
    assert channel.link.of("error")[-1]["code"] == "agent_offline"


def test_registration_is_bound_to_the_project(tmp_path, monkeypatch):
    """Один агент — один каталог: так задача с телефона попадает в тот проект, которому
    её назначили, даже когда открыто несколько сессий."""
    registry = tmp_path / ".bax" / "lite.json"
    monkeypatch.setattr(channel_module, "REGISTRY", registry)

    first = channel_module.save_registration(
        tmp_path / "апи", "agent-1:key-1:секрет-1", "wss://bax.local/agent")
    channel_module.save_registration(
        tmp_path / "сайт", "agent-2:key-2:секрет-2", "wss://bax.local/agent")

    assert first["agent"] == "agent-1"
    assert channel_module.load_registration(tmp_path / "апи")["key_id"] == "key-1"
    assert channel_module.load_registration(tmp_path / "сайт")["key_id"] == "key-2"
    assert channel_module.load_registration(tmp_path / "чужой") is None
    assert oct(registry.stat().st_mode)[-3:] == "600", "в файле секреты агентов"

    saved = json.loads(registry.read_text(encoding="utf-8"))
    assert len(saved) == 2, "регистрации не затирают друг друга"


def test_broken_registration_string(tmp_path, monkeypatch):
    monkeypatch.setattr(channel_module, "REGISTRY", tmp_path / "lite.json")
    with pytest.raises(ValueError):
        channel_module.save_registration(tmp_path / "п", "не-строка", "wss://bax.local/agent")
