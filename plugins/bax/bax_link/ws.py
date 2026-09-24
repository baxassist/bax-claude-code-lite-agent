"""Websocket-клиент на стандартной библиотеке (RFC 6455) — только то, что нужно плагину.

Плагин запускается системным `python3` без сторонних пакетов (заказчик 24.09: чтобы для
установки хватало команд `/plugin`, без `uv` и `pip`). Поэтому своё: рукопожатие HTTP/1.1,
текстовые кадры с маской, ping/pong, закрытие. Работает и на Python 3.9 — таком, какой
стоит на Mac вместе с инструментами разработчика.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
from pathlib import Path
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA


class WebSocketError(Exception):
    """Любая ошибка соединения: рукопожатие не прошло, кадр битый, сервер закрыл."""


class ConnectionClosed(WebSocketError):
    """Соединение закрыто — той или другой стороной."""


#: Системные наборы корневых сертификатов: Mac, Debian/Ubuntu, Fedora/RHEL
SYSTEM_CA_FILES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
)


def _ssl_context() -> ssl.SSLContext:
    """Проверка сертификата — всегда. Python с python.org на Mac своих корневых сертификатов
    не видит, пока не запустить у него «Install Certificates» (24.09: `wss://` падал с
    CERTIFICATE_VERIFY_FAILED) — тогда берём набор самой системы или certifi, если он есть."""
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0):
        return context
    for path in SYSTEM_CA_FILES:
        if Path(path).is_file():
            context.load_verify_locations(cafile=path)
            return context
    try:
        import certifi  # noqa: PLC0415 — необязательный пакет

        context.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return context


def _mask(payload: bytes, mask: bytes) -> bytes:
    """XOR с маской целым числом: побайтовый цикл на картинке в мегабайты заметно медленнее."""
    if not payload:
        return payload
    size = len(payload)
    key = (mask * (size // 4 + 1))[:size]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(size, "big")


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, max_size: int) -> None:
        self._reader = reader
        self._writer = writer
        self._max_size = max_size
        self._closed = False
        self._lock = asyncio.Lock()

    # --- отправка ------------------------------------------------------------

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise ConnectionClosed("соединение закрыто")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)  # кадры клиента обязаны быть замаскированы
        masked = _mask(payload, mask)
        async with self._lock:
            try:
                self._writer.write(bytes(header) + mask + masked)
                await self._writer.drain()
            except (ConnectionError, OSError) as error:
                self._closed = True
                raise ConnectionClosed(str(error)) from error

    async def send(self, text: str) -> None:
        await self._send_frame(OP_TEXT, text.encode("utf-8"))

    # --- приём ---------------------------------------------------------------

    async def _read_frame(self):
        try:
            first, second = await self._reader.readexactly(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", await self._reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
            if length > self._max_size:
                raise WebSocketError(f"кадр {length} байт больше предела {self._max_size}")
            mask = await self._reader.readexactly(4) if second & 0x80 else b""
            payload = await self._reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as error:
            self._closed = True
            raise ConnectionClosed("соединение оборвалось") from error
        if mask:
            payload = _mask(payload, mask)
        return bool(first & 0x80), first & 0x0F, payload

    async def recv(self) -> str:
        """Следующее текстовое сообщение. Ping отвечаем сами, закрытие — ConnectionClosed."""
        parts: list = []
        while True:
            fin, opcode, payload = await self._read_frame()
            if opcode == OP_PING:
                await self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                if not self._closed:
                    try:
                        await self._send_frame(OP_CLOSE, payload[:2])
                    except ConnectionClosed:
                        pass
                self._closed = True
                raise ConnectionClosed("сервер закрыл соединение")
            parts.append(payload)
            if sum(len(part) for part in parts) > self._max_size:
                raise WebSocketError("сообщение больше предела")
            if fin:
                return b"".join(parts).decode("utf-8")

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            return await self.recv()
        except ConnectionClosed:
            raise StopAsyncIteration from None

    # --- закрытие ------------------------------------------------------------

    async def close(self) -> None:
        if not self._closed:
            try:
                await self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
            except ConnectionClosed:
                pass
        self._closed = True
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def __aenter__(self) -> WebSocket:
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()


async def connect(url: str, max_size: int = 1 << 20, timeout: float | None = 20) -> WebSocket:
    """Открыть соединение `ws://` или `wss://`."""
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise WebSocketError(f"адрес должен начинаться с ws:// или wss://: {url}")
    secure = parts.scheme == "wss"
    host = parts.hostname or ""
    port = parts.port or (443 if secure else 80)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

    async def open_and_handshake() -> WebSocket:
        reader, writer = await asyncio.open_connection(
            host, port, ssl=_ssl_context() if secure else None,
            server_hostname=host if secure else None, limit=max_size + 1024,
        )
        key = base64.b64encode(os.urandom(16)).decode()
        host_header = host if parts.port is None else f"{host}:{port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: bax-claude-code-lite\r\n"
            "\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ")
        if len(status) < 2 or status[1] != "101":
            writer.close()
            raise WebSocketError(f"сервер не открыл websocket: {lines[0]}")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            writer.close()
            raise WebSocketError("сервер ответил неверным ключом рукопожатия")
        return WebSocket(reader, writer, max_size)

    try:
        return await asyncio.wait_for(open_and_handshake(), timeout)
    except asyncio.TimeoutError as error:
        raise WebSocketError("сервер не ответил на рукопожатие") from error
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
        raise WebSocketError("рукопожатие оборвалось") from error
