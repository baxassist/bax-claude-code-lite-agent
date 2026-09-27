"""Связь с Баксом после сна компьютера (27.09).

Mac уснул — релей закрыл соединение, пока тот спал. Проснувшись, плагин должен заметить
это сам и подключиться сразу: свой websocket-клиент ping не шлёт, а таймеры asyncio во сне
на Mac стоят. Сон здесь — скачок часов на стене, релей — настоящий сервер websockets,
который проводит рукопожатие и дальше молчит, как соединение, умершее во сне.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
import websockets

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "bax"
sys.path.insert(0, str(PLUGIN))

from bax_link import connection  # noqa: E402
from bax_link.connection import Link  # noqa: E402


class WallClock:
    """Часы на стене, которые тест переводит вперёд: для процесса так и выглядит сон."""

    def __init__(self) -> None:
        self.offset = 0.0
        self.real = time.time

    def __call__(self) -> float:
        return self.real() + self.offset


@pytest.fixture
def clock(monkeypatch):
    made = WallClock()
    monkeypatch.setattr(time, "time", made)
    # сторож — в долях секунды, чтобы тесты шли быстро
    monkeypatch.setattr(connection, "WATCH_EVERY", 0.05)
    monkeypatch.setattr(connection, "SLEEP_GAP", 0.5)
    return made


@pytest.fixture
async def relay():
    """Адрес релея и его соединения: пускает, отвечает `ready` и больше ничего не шлёт."""
    seen = []

    async def handler(conn):
        seen.append(conn)
        await conn.recv()  # hello
        await conn.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await conn.recv()  # auth
        await conn.send(json.dumps({"v": 1, "type": "ready", "user": "u", "agent": "a"}))
        await conn.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}/agent", seen


async def until(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "не дождались"
        await asyncio.sleep(0.02)


def start(url: str) -> tuple[asyncio.Task, list]:
    """Запущенная связь и список её подключений (по одному `ready` на каждое)."""
    readies: list = []

    async def on_ready(frame: dict) -> None:
        readies.append(frame)

    async def on_frame(_: dict) -> None:
        pass

    link = Link(url, "key", "secret", "0", engine="claude_code_lite", install_id="i", session_id="s")
    return asyncio.create_task(link.run(on_ready, on_frame)), readies


async def test_after_sleep_it_reconnects_at_once(clock, relay, monkeypatch):
    url, seen = relay
    # обычная пауза — пять минут: подключение без неё и есть проверка
    monkeypatch.setattr(connection.protocol, "backoff", lambda attempt: 300)
    task, readies = start(url)
    try:
        await until(lambda: len(readies) == 1)
        clock.offset += 3600  # проспал час
        await until(lambda: len(readies) == 2)
        assert len(seen) == 2
    finally:
        task.cancel()


async def test_silent_server_means_dead_connection(clock, relay, monkeypatch):
    """Сервер молчит дольше предела (релей шлёт ping раз в 20 с) — соединение рвём сами."""
    url, seen = relay
    monkeypatch.setattr(connection, "SILENCE_LIMIT", 0.3)
    monkeypatch.setattr(connection.protocol, "backoff", lambda attempt: 0)
    task, readies = start(url)
    try:
        await until(lambda: len(readies) == 2)
        assert len(seen) == 2
    finally:
        task.cancel()


async def test_pause_is_counted_by_the_wall_clock(clock):
    """Пять минут паузы, начатые перед сном, кончаются с пробуждением, а не через пять минут."""
    link = Link("ws://127.0.0.1:9/agent", "key", "secret", "0")
    assert await link._pause(0.1) is False
    pause = asyncio.create_task(link._pause(300))
    await asyncio.sleep(0.1)
    clock.offset += 3600
    assert await asyncio.wait_for(pause, 2) is True
