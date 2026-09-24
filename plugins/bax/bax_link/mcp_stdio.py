"""MCP-сервер через stdin/stdout на стандартной библиотеке — только то, что нужно каналу.

Claude Code говорит с сервером построчным JSON-RPC 2.0: `initialize`, `tools/list`,
`tools/call`, `ping` и уведомления. Канал сверх этого шлёт свои уведомления
(`notifications/claude/channel`) и принимает запросы разрешений. Пакет `mcp` для этого
не нужен — без него плагин запускается системным `python3` без `uv` и `pip` (заказчик 24.09).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable
from typing import Any, Callable

logger = logging.getLogger("bax.mcp")

#: Какую версию протокола отвечаем, если клиент свою не назвал
DEFAULT_PROTOCOL = "2025-06-18"

#: Вызов инструмента: имя и аргументы → (текст ответа, ошибка ли это)
ToolHandler = Callable[[str, dict], Awaitable[tuple]]
NotificationHandler = Callable[[dict], Awaitable[None]]


class StdioServer:
    def __init__(self, name: str, version: str, instructions: str, tools: list[dict],
                 call_tool: ToolHandler, experimental: dict | None = None) -> None:
        self.name = name
        self.version = version
        self.instructions = instructions
        self.tools = tools
        self.call_tool = call_tool
        self.experimental = experimental or {}
        self.notification_handlers: dict[str, NotificationHandler] = {}
        #: вызывается один раз, когда клиент сказал `notifications/initialized`
        self.on_initialized: Callable[[], Awaitable[None]] | None = None
        self._write_lock = asyncio.Lock()
        self._out = sys.stdout.buffer
        self._tasks: set = set()

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self.notification_handlers[method] = handler

    # --- вывод ---------------------------------------------------------------

    async def _write(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self._out.write(data)
            self._out.flush()

    async def notify(self, method: str, params: dict) -> None:
        """Уведомление клиенту — без запроса с его стороны: в этом и есть канал."""
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # --- разбор входящего ----------------------------------------------------

    async def _answer(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                    "capabilities": {"tools": {}, "experimental": self.experimental},
                    "serverInfo": {"name": self.name, "version": self.version},
                    "instructions": self.instructions,
                }
            elif method == "tools/list":
                result = {"tools": self.tools}
            elif method == "tools/call":
                text, is_error = await self.call_tool(str(params.get("name") or ""),
                                                      dict(params.get("arguments") or {}))
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            elif method == "ping":
                result = {}
            else:
                await self._write({"jsonrpc": "2.0", "id": request_id,
                                   "error": {"code": -32601, "message": f"нет метода {method}"}})
                return
        except Exception as error:  # noqa: BLE001 — ответ клиенту важнее падения сервера
            logger.exception("запрос %s не выполнился", method)
            await self._write({"jsonrpc": "2.0", "id": request_id,
                               "error": {"code": -32603, "message": str(error)}})
            return
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _notification(self, message: dict) -> None:
        method = str(message.get("method") or "")
        if method == "notifications/initialized":
            if self.on_initialized is not None:
                callback, self.on_initialized = self.on_initialized, None
                await callback()
            return
        handler = self.notification_handlers.get(method)
        if handler is not None:
            try:
                await handler(dict(message.get("params") or {}))
            except Exception:  # noqa: BLE001
                logger.exception("уведомление %s не обработалось", method)

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle_line(self, line: bytes) -> None:
        line = line.strip()
        if not line:
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("строка от клиента не разобралась")
            return
        if not isinstance(message, dict) or "method" not in message:
            return  # ответы на наши запросы — мы их не шлём
        if "id" in message:
            # запросы — параллельно: долгий инструмент не держит чтение
            self._spawn(self._answer(message))
        else:
            self._spawn(self._notification(message))

    async def run(self) -> None:
        """Читать stdin, пока клиент его не закроет — тогда и сессии Claude Code конец."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while True:
            line = await reader.readline()
            if not line:
                break
            await self.handle_line(line)
