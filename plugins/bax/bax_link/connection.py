"""Связь с сервером Бакса: рукопожатие, кадры, переподключение.

Обрыв связи — обычное дело (ноутбук закрыли, вайфай моргнул), поэтому соединение
восстанавливается само с нарастающей паузой.

Копия соединения движка bax-claude-code-agent: рукопожатие и кадры у Lite ровно те же.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from . import protocol
from . import ws as websocket
from .protocol import frame, parse

logger = logging.getLogger("bax.link")

# Картинка из приложения приезжает одним кадром: 1 МБ для этого мал, поэтому 8 МБ.
# Файлы обратно уходят кусками по 48 КБ.
MAX_FRAME = 8 * 1024 * 1024

Handler = Callable[[dict], Awaitable[None]]


#: окончательные ошибки: ключ сам собой верным не станет, переподключаться бессмысленно.
#: Гасят только своего агента — остальные агенты этой установки работают дальше
FATAL = ("unauthorized", "key_claimed", "wrong_engine", "unsupported_version")

#: «агента уже занял кто-то другой» — не приговор: у Claude Code Lite это вторая сессия
#: в том же проекте. Закроют её — подключимся сами, поэтому ждём и пробуем снова
BUSY = "agent_busy"


class HandshakeError(Exception):
    """Сервер не пустил: ключ перевыпущен, занят другой установкой, чужой движок, старый протокол."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @property
    def fatal(self) -> bool:
        return self.code in FATAL


class Link:
    """Одно соединение с сервером. Живёт до обрыва; переподключением занимается `run`."""

    def __init__(self, url: str, key_id: str, secret: str, agent_version: str,
                 engine: str = "claude_code", install_id: str = "", install_name: str = "",
                 path: str = "", session_id: str = "") -> None:
        self.url = url
        self.key_id = key_id
        self.secret = secret
        self.agent_version = agent_version
        # что движок сообщает о себе: чем он является, id и имя этой установки и каталог
        # проекта — по ним в приложении видно, где агент работает
        self.engine = engine
        self.install_id = install_id
        self.install_name = install_name
        self.path = path
        #: id сессии терминала — только у Claude Code Lite: по нему сервер отличает
        #: «та же сессия вернулась после обрыва» от «пришла вторая»
        self.session_id = session_id
        #: id агента, который сервер назвал в `ready` — для логов
        self.agent_id = ""
        #: начальные модель и усилие агента — приходят в `ready`, пока их нет
        self.settings: dict = {}
        self._ws: websocket.WebSocket | None = None
        self.user: str | None = None

    async def send(self, type_: str, **fields) -> None:
        """Кадр серверу. Соединения нет — молча пропускаем: телефон переспросит сам."""
        ws = self._ws
        if ws is None:
            logger.debug("кадр %s не ушёл: нет соединения", type_)
            return
        try:
            await ws.send(frame(type_, **fields))
        except websocket.ConnectionClosed:
            logger.debug("кадр %s не ушёл: соединение закрылось", type_)

    async def _handshake(self, ws: websocket.WebSocket) -> None:
        hello = {"key_id": self.key_id, "agent_version": self.agent_version,
                 "engine": self.engine, "install_id": self.install_id,
                 "install_name": self.install_name, "path": self.path}
        if self.session_id:
            hello["session_id"] = self.session_id
        await ws.send(frame("hello", **hello))
        answer = parse(await ws.recv())
        if answer.get("type") == "error":
            raise HandshakeError(answer.get("code", "internal"), answer.get("message", ""))
        if answer.get("type") != "challenge":
            raise HandshakeError("internal", f"ждали challenge, пришло {answer.get('type')!r}")

        await ws.send(frame("auth", sign=protocol.sign(
            self.secret, answer["nonce"], answer["ts"], self.key_id,
        )))
        ready = parse(await ws.recv())
        if ready.get("type") == "error":
            raise HandshakeError(ready.get("code", "internal"), ready.get("message", ""))
        if ready.get("type") != "ready":
            raise HandshakeError("internal", f"ждали ready, пришло {ready.get('type')!r}")
        self.user = ready.get("user")
        self.agent_id = str(ready.get("agent") or "")
        #: начальные модель и усилие агента — со слов сервера
        self.settings = dict(ready.get("settings") or {})

    async def _session(self, on_ready: Handler, on_frame: Handler) -> None:
        async with await websocket.connect(self.url, max_size=MAX_FRAME) as ws:
            await self._handshake(ws)
            self._ws = ws
            logger.info("на связи: %s", self.url)
            try:
                await on_ready({"type": "ready", "user": self.user, "settings": self.settings})
                async for message in ws:
                    try:
                        incoming = parse(message)
                    except ValueError:
                        logger.warning("кадр не разобрался, пропускаем")
                        continue
                    if incoming.get("type") == "ping":
                        await self.send("pong")
                        continue
                    if incoming.get("type") == "error" and incoming.get("code") in FATAL:
                        # сервер прервал соединение: ключ перевыпустили, агента удалили
                        raise HandshakeError(incoming["code"], incoming.get("message", ""))
                    await on_frame(incoming)
            finally:
                self._ws = None

    async def run(self, on_ready: Handler, on_frame: Handler) -> None:
        """Держит связь, пока агента не остановят. Возвращается только при фатальной ошибке ключа."""
        attempt = 0
        while True:
            try:
                await self._session(on_ready, on_frame)
                attempt = 0  # соединение жило — считаем паузы заново
                logger.info("сервер закрыл соединение")
            except HandshakeError as error:
                if error.fatal:
                    logger.error("агент %s остановлен: %s", self.agent_id or self.key_id[:8], error)
                    raise
                logger.warning("рукопожатие не вышло: %s", error)
            except asyncio.CancelledError:
                raise
            except (OSError, websocket.WebSocketError) as error:
                logger.warning("связи нет (%s)", error)

            pause = protocol.backoff(attempt)
            attempt += 1
            logger.info("переподключение через %s с", pause)
            await asyncio.sleep(pause)  # остановили агента — отмена уходит наружу, не глушим
