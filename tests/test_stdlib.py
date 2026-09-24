"""Плагин без сторонних пакетов (заказчик 24.09): свой websocket-клиент, свой MCP-сервер
и запуск системным `python3` — как на Mac, где кроме инструментов разработчика ничего нет."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import websockets

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "bax"
sys.path.insert(0, str(PLUGIN))

from bax_link import ws  # noqa: E402
from bax_link.mcp_stdio import StdioServer  # noqa: E402


async def test_websocket_client_talks_to_a_real_server():
    """Текст туда и обратно, большой кадр (картинка из приложения), ping и закрытие сервером."""
    async def echo(connection):
        async for message in connection:
            if message == "закрой":
                await connection.close()
                return
            await connection.ping()
            await connection.send("эхо: " + message)

    async with websockets.serve(echo, "127.0.0.1", 0, max_size=None) as server:
        port = server.sockets[0].getsockname()[1]
        client = await ws.connect(f"ws://127.0.0.1:{port}/agent", max_size=8 * 1024 * 1024)
        await client.send("привет")
        assert await client.recv() == "эхо: привет"
        big = "ж" * 300_000  # 600 КБ — больше одного кадра по 64 КБ
        await client.send(big)
        assert await client.recv() == "эхо: " + big
        await client.send("закрой")
        with pytest.raises(ws.ConnectionClosed):
            await client.recv()
        await client.close()


async def test_websocket_refuses_non_websocket_answer():
    async def http(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(http, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    with pytest.raises(ws.WebSocketError):
        await ws.connect(f"ws://127.0.0.1:{port}/agent")
    server.close()


async def test_mcp_server_answers_like_claude_code_expects():
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        return "Доставлено в Бакс.", False

    server = StdioServer("bax", "0.4.0", "инструкции", [{"name": "reply"}], call_tool,
                         experimental={"claude/channel": {}})
    out = io.BytesIO()
    server._out = out
    started, permissions = [], []

    async def start():
        started.append(True)

    async def permission(params):
        permissions.append(params)

    server.on_initialized = start
    server.on_notification("notifications/claude/channel/permission_request", permission)

    for message in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "reply", "arguments": {"text": "готово"}}},
        {"jsonrpc": "2.0", "method": "notifications/claude/channel/permission_request",
         "params": {"request_id": "r1", "tool_name": "Bash"}},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
    ):
        await server.handle_line(json.dumps(message).encode())
    await asyncio.sleep(0.05)
    await server.notify("notifications/claude/channel", {"content": "задача"})

    answers = [json.loads(line) for line in out.getvalue().decode().splitlines()]
    by_id = {a["id"]: a for a in answers if "id" in a}
    init = by_id[1]["result"]
    assert init["protocolVersion"] == "2025-11-25"
    assert init["capabilities"]["experimental"] == {"claude/channel": {}}
    assert init["instructions"] == "инструкции"
    assert by_id[2]["result"]["tools"] == [{"name": "reply"}]
    assert by_id[3]["result"] == {"content": [{"type": "text", "text": "Доставлено в Бакс."}], "isError": False}
    assert by_id[4]["error"]["code"] == -32601
    assert calls == [("reply", {"text": "готово"})]
    assert started == [True] and permissions == [{"request_id": "r1", "tool_name": "Bash"}]
    assert answers[-1] == {"jsonrpc": "2.0", "method": "notifications/claude/channel",
                           "params": {"content": "задача"}}


def system_python() -> str:
    """Системный python3 Mac (3.9, без пакетов), если он есть; иначе тот, что гоняет тесты."""
    return "/usr/bin/python3" if Path("/usr/bin/python3").exists() else (shutil.which("python3") or sys.executable)


def test_plugin_starts_with_bare_python3(tmp_path):
    """Плагин целиком — сервером, как его запускает Claude Code: системный python3, без uv
    и пакетов; в чужом домашнем каталоге, чтобы не тронуть настоящие регистрации."""
    env = {**os.environ, "HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path)}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    requests = "\n".join(json.dumps(m) for m in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )) + "\n"
    process = subprocess.run([system_python(), "-S", str(PLUGIN / "server.py")], input=requests.encode(),
                             capture_output=True, env=env, timeout=30)
    answers = [json.loads(line) for line in process.stdout.decode().splitlines() if line.strip()]
    by_id = {a["id"]: a for a in answers if "id" in a}
    assert by_id[1]["result"]["serverInfo"]["name"] == "bax", process.stderr.decode()[-2000:]
    assert {tool["name"] for tool in by_id[2]["result"]["tools"]} == {"reply", "connect"}


@pytest.mark.skipif(not any(Path(p).is_file() for p in ws.SYSTEM_CA_FILES), reason="нет системного набора")
def test_ssl_uses_system_certificates_when_python_has_none(monkeypatch):
    """Python с python.org без «Install Certificates» не видит корневых сертификатов —
    плагин берёт набор системы, и `wss://` проверяется (24.09)."""
    import ssl

    monkeypatch.setattr(ssl, "create_default_context", lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    context = ws._ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats()["x509_ca"] > 0
