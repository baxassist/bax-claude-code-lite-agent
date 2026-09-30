"""Связь с сервером Бакса: рукопожатие, кадры, переподключение.

Обрыв связи — обычное дело (ноутбук закрыли, вайфай моргнул), поэтому соединение
восстанавливается само с нарастающей паузой, а после сна компьютера — сразу.

Копия соединения движка bax-claude-code-agent: рукопожатие и кадры у Lite ровно те же.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
#: в том же проекте. Закроют её — подключимся сами, поэтому ждём и пробуем снова.
#: `agent_taken` — то же для сессии без канала: агент у сессии с каналом, читать нечего
BUSY = "agent_busy"
TAKEN = "agent_taken"

#: раз в столько секунд сторож смотрит на соединение
WATCH_EVERY = 5
#: столько секунд без единого кадра от сервера — соединение мёртвое: релей шлёт ping
#: раз в 20 секунд, то есть это три пропущенных подряд
SILENCE_LIMIT = 60
#: часы на стене ушли вперёд настолько больше ожидаемого — процесс стоял, компьютер спал
SLEEP_GAP = 15
#: почему сторож оборвал соединение: после сна подключаемся сразу, без паузы
SLEPT = "компьютер спал"


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
                 path: str = "", session_id: str = "", role: str = "") -> None:
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
        #: `observer` — сессия без канала: телефон только читает её переписку (0.6.0). Такое
        #: соединение не занимает агента у сессии с каналом и уступает ей место
        self.role = role
        #: id агента, который сервер назвал в `ready` — для логов
        self.agent_id = ""
        #: начальные модель и усилие агента — приходят в `ready`, пока их нет
        self.settings: dict = {}
        self._ws: websocket.WebSocket | None = None
        self.user: str | None = None
        #: связь больше не нужна: агента отпустили (разговор продолжен в другой сессии)
        self.stopped = False
        #: почему соединение оборвал сторож; пусто — не он
        self.dropped = ""

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
        if self.role:
            hello["role"] = self.role
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

    async def _watch(self, ws: websocket.WebSocket) -> None:
        """Сторож: рвёт соединение, если компьютер спал или сервер давно молчит.

        Свой клиент (0.4.0) сам ping не шлёт, а мёртвое соединение по сокету не видно.
        Mac уснул, релей закрыл соединение, пока он спал, — а плагин после пробуждения
        ждал кадров из сокета, которого на той стороне уже нет: агент был «не в сети»
        при открытом Маке (27.09). Прежняя библиотека websockets ловила это своим ping.
        """
        last = time.time()
        while True:
            await asyncio.sleep(WATCH_EVERY)
            now = time.time()
            if now - last > WATCH_EVERY + SLEEP_GAP:
                self.dropped = SLEPT
            elif now - ws.last_frame > SILENCE_LIMIT:
                self.dropped = f"сервер молчит дольше {SILENCE_LIMIT} с"
            last = now
            if self.dropped:
                logger.info("рву соединение: %s", self.dropped)
                ws.abort()
                return

    async def _session(self, on_ready: Handler, on_frame: Handler) -> None:
        async with await websocket.connect(self.url, max_size=MAX_FRAME) as ws:
            # сторож — с первой секунды: на мёртвом соединении зависает и само рукопожатие
            watchdog = asyncio.create_task(self._watch(ws))
            try:
                await self._handshake(ws)
                self._ws = ws
                logger.info("на связи: %s", self.url)
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
                watchdog.cancel()

    async def _pause(self, seconds: float) -> bool:
        """Пауза перед переподключением — по часам на стене. True — за паузу компьютер спал.

        Таймеры asyncio идут по монотонным часам, а у системного python3 на Mac они во сне
        стоят (mach_absolute_time): пять минут паузы, начатые перед сном, после пробуждения
        тянулись бы ещё пять минут, и всё это время агент был бы «не в сети».
        """
        deadline = time.time() + seconds
        while True:
            left = deadline - time.time()
            if left <= 0:
                return False
            step = min(WATCH_EVERY, left)
            before = time.time()
            await asyncio.sleep(step)  # остановили агента — отмена уходит наружу, не глушим
            if time.time() - before > step + SLEEP_GAP:
                return True

    @property
    def connected(self) -> bool:
        """Есть ли связь прямо сейчас: кадр, отправленный без неё, до телефона не дойдёт."""
        return self._ws is not None

    def stop(self) -> None:
        """Отпустить агента: закрыть соединение и больше не переподключаться."""
        self.stopped = True
        self.dropped = "агента отпустили"
        if self._ws is not None:
            self._ws.abort()

    async def run(self, on_ready: Handler, on_frame: Handler) -> None:
        """Держит связь, пока агента не остановят или не отпустят (`stop`). Исключение —
        только при фатальной ошибке ключа."""
        attempt = 0
        while True:
            if self.stopped:
                return
            self.dropped = ""
            try:
                await self._session(on_ready, on_frame)
                attempt = 0  # соединение жило — считаем паузы заново
                logger.info("соединение закрыто: %s", self.dropped or "его закрыл сервер")
            except HandshakeError as error:
                if error.fatal:
                    logger.error("агент %s остановлен: %s", self.agent_id or self.key_id[:8], error)
                    raise
                logger.warning("рукопожатие не вышло: %s", error)
            except asyncio.CancelledError:
                raise
            except (OSError, websocket.WebSocketError) as error:
                logger.warning("связи нет (%s)", self.dropped or error)

            if self.stopped:
                logger.info("агента отпустили — на связь больше не выходим")
                return
            if self.dropped == SLEPT:
                # после сна — сразу: не поднялась ещё сеть — дальше пойдут обычные паузы
                attempt = 0
                continue
            pause = protocol.backoff(attempt)
            attempt += 1
            logger.info("переподключение через %s с", pause)
            if await self._pause(pause):
                logger.info("компьютер спал — переподключаюсь сейчас")
                attempt = 0  # прежние неудачи — про сеть до сна
