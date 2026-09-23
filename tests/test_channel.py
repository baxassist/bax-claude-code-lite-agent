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
    # ни настоящей сессии, ни настоящих файлов Claude Code: тесты гоняют и из-под Claude Code,
    # где CLAUDE_CODE_SESSION_ID задан, — иначе плагин читал бы журнал этой самой сессии
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(channel_module.history, "projects_root", lambda: tmp_path / "claude-projects")
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


async def test_question_is_shown_again_when_the_app_opens(channel):
    """Вопрос о разрешении пришёл, пока приложение было закрыто, — открыли агента, и карточка
    снова на экране (23.09). Ответили — больше не показывается."""
    await channel.ask("req-1", "Bash", "Запустить тесты", "pytest -q")
    channel.link.frames.clear()

    await channel.on_frame({"type": "subscribe"})
    again = channel.link.of("question")
    assert len(again) == 1 and again[0]["tool"] == "Bash" and again[0]["text"] == "Запустить тесты"
    assert channel.link.of("status")[-1]["state"] == "waiting"

    await channel.answer({"question_id": again[0]["question_id"], "verdict": "allow"})
    channel.link.frames.clear()
    await channel.on_frame({"type": "subscribe"})
    assert channel.link.of("question") == [], "отвеченный вопрос показался снова"


async def test_finished_turn_forgets_its_questions(channel):
    """Ответ модели закрывает ход: вопрос, на который ответили в терминале, не всплывает."""
    await channel.ask("req-2", "Edit", "Поправить файл", "app.py")
    await channel.reply("готово")
    channel.link.frames.clear()
    await channel.on_frame({"type": "subscribe"})
    assert channel.link.of("question") == []


# --- история из файла сессии Claude Code (23.09: сервер её не хранит) -------------------

SESSION = "11112222-3333-4444-5555-666677778888"
TRANSCRIPT = [
    {"type": "user", "message": {"content": "сделай ревью"}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Смотрю код"}]}},
    {"type": "user", "isMeta": True, "message": {"content": "Base directory for this skill: /x"}},
    {"type": "user", "isMeta": True, "message": {"content":
        '<channel source="plugin:bax:bax" source="bax" chat_id="c1">\nсобери проект\n</channel>'}},
    {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "думаю"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "make"}},
    ]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__plugin_bax_bax__reply", "input": {"chat_id": "c1", "text": "собрал"}},
    ]}},
    {"type": "user", "message": {"content": "<command-name>/bax:connect</command-name>"}},
]


@pytest.fixture
def transcript(channel, tmp_path, monkeypatch):
    """Файл сессии там, где его пишет Claude Code, плюс недописанная последняя строка."""
    folder = channel_module.history.projects_root() / str(channel.project).replace("/", "-")
    folder.mkdir(parents=True)
    file = folder / f"{SESSION}.jsonl"
    file.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in TRANSCRIPT)
                    + '\n{"type": "assistant", "mess', encoding="utf-8")
    channel.session_id = SESSION
    return file


async def test_history_comes_from_the_session_file(channel, transcript):
    """Открыли агента — история из файла сессии: задачи с телефона, то, что писали в терминале,
    ответы через reply. Служебные вставки, эхо команд и вывод инструментов — не реплики."""
    await channel.on_frame({"type": "subscribe"})
    shown = [(f["id"], f["kind"], f["text"]) for f in channel.link.of("message")]
    assert shown == [
        (0, "user", "сделай ревью"),
        (1, "assistant", "Смотрю код"),
        (3, "user", "собери проект"),
        (4, "thinking", "думаю"),
        (4, "tool", "Bash: make"),
        (6, "assistant", "собрал"),
    ]


async def test_history_pages_back(channel, transcript):
    """Листание вверх — строки раньше `before`, по порядку."""
    await channel.on_frame({"type": "history", "before": 4, "limit": 2})
    assert [(f["id"], f["text"]) for f in channel.link.of("message")] == [
        (1, "Смотрю код"), (3, "собери проект"),
    ]


async def test_live_messages_go_after_the_history(channel, transcript):
    """Живой ответ нумеруется после строк файла: приложение ставит его ниже истории."""
    await channel.reply("готово")
    assert channel.link.of("message")[-1]["id"] >= 9


def test_file_is_read_from_the_end(tmp_path):
    """Номера строк верны с конца файла — с переводом строки в конце и без него."""
    for text in ("a\nb\nc\n", "a\nb\nc"):
        file = tmp_path / "f.jsonl"
        file.write_bytes(text.encode())
        assert list(channel_module.history._lines_backwards(file, chunk=2)) == [(2, b"c"), (1, b"b"), (0, b"a")]
        assert channel_module.history.next_id(file) == 3


def test_no_session_file_means_no_history(tmp_path, monkeypatch):
    monkeypatch.setattr(channel_module.history, "projects_root", lambda: tmp_path / "нет")
    assert channel_module.history.tail(channel_module.history.session_file(tmp_path, "x")) == []


# --- на связь — только из сессии с каналом Бакса (23.09) ------------------------------

@pytest.mark.parametrize("args, expected", [
    ("claude --resume", False),
    ("claude", False),
    ("claude --dangerously-load-development-channels plugin:bax@baxassist", True),
    ("claude --channels plugin:bax@baxassist", True),
    ("claude --dangerously-load-development-channels=plugin:bax@baxassist", True),
    ("claude --channels plugin:telegram@official,plugin:bax@baxassist", True),
    ("claude --dangerously-load-development-channels plugin:other@x", False),
    ("/Users/me/.local/share/claude/versions/2.1.280 --dangerously-load-development-channels plugin:bax@baxassist", True),
])
def test_channel_flag_is_read_from_the_command_line(args, expected):
    """Плагин загружается в каждую сессию, а выходить на связь должен только там, где Claude
    Code запущен с каналом Бакса — иначе агента забирала сессия без канала."""
    assert channel_module.is_claude(args)
    assert channel_module.launched_with_channel(args) is expected


async def test_session_without_channel_does_not_take_the_agent(channel, monkeypatch):
    """Привязка есть, но сессия без канала — на связь не выходим, агент остаётся свободным."""
    channel_module.save_registration(channel.project, "a:k:s", "wss://relay.example/agent")
    monkeypatch.setattr(channel_module, "channel_enabled", lambda: False)
    channel.link = None
    await channel_module.connect(channel)
    assert channel.link is None


def test_follower_sends_only_what_was_appended(tmp_path):
    """Дописанное в файл сессии уходит на телефон сразу (заказчик 23.09: новые сообщения
    появлялись, только если выйти из агента и зайти). Задачу с телефона и ответ `reply`
    плагин уже отправил сам — второй раз их нет; недописанная строка ждёт конца."""
    history = channel_module.history
    file = tmp_path / "s.jsonl"

    def line(entry: dict) -> bytes:
        return (json.dumps(entry, ensure_ascii=False) + "\n").encode()

    file.write_bytes(line({"type": "user", "message": {"content": "старое"}}))
    follower = history.Follower.at_end(file)
    assert follower.poll() == []

    task = '<channel source="plugin:bax:bax" source="bax" chat_id="1">\nзадача\n</channel>'
    reply = {"type": "tool_use", "name": history.REPLY_TOOL, "input": {"text": "ответ"}}
    with file.open("ab") as fh:
        fh.write(line({"type": "user", "isMeta": True, "message": {"content": task}}))
        fh.write(line({"type": "assistant", "message": {"content": [{"type": "text", "text": "делаю"}, reply]}}))
        fh.write(line({"type": "user", "message": {"content": "из терминала"}}))
        fh.write(b'{"type": "assistant", "mess')  # строка ещё пишется
    got = follower.poll()
    assert [(m.id, m.kind, m.text) for m in got] == [(2, "assistant", "делаю"), (3, "user", "из терминала")]

    with file.open("ab") as fh:
        fh.write(b'age": {"content": [{"type": "text", "text": "\xd0\xb3\xd0\xbe\xd1\x82\xd0\xbe\xd0\xb2\xd0\xbe"}]}}\n')
    assert [(m.id, m.text) for m in follower.poll()] == [(4, "готово")]
    # в истории задача и ответ на месте — они есть в файле
    assert [m.text for m in history.tail(file, 10)] == ["старое", "задача", "делаю", "ответ", "из терминала", "готово"]


def test_turn_state_follows_the_session_file(tmp_path):
    """«Работает» / «ждёт задачу» — по файлу сессии: конец хода — `system/turn_duration`
    (заказчик 23.09: агент работал, а в телефоне «ждёт задачу»)."""
    history = channel_module.history
    file = tmp_path / "s.jsonl"
    file.write_bytes(b"")
    follower = history.Follower.at_end(file)
    with file.open("ab") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}).encode() + b"\n")
    follower.poll()
    assert follower.turn == "busy"
    with file.open("ab") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "turn_duration"}).encode() + b"\n")
    follower.poll()
    assert follower.turn == "ready"
