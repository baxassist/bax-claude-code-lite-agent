"""Кадры обмена с Баксом — те же, что у движка bax-claude-code-agent.

Кадр — один JSON-объект в одном сообщении websocket. Поле `v` — версия протокола:
сервер с другой версией честно скажет `unsupported_version`, а не будет догадываться.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

VERSION = 1

# Паузы переподключения, секунды: растут до пяти минут и дальше не увеличиваются.
# Пять минут — предел: агент не должен «уснуть» надолго, когда сервер вернётся.
BACKOFF = (1, 2, 5, 10, 30, 60, 120, 300)


def sign(secret: str, nonce: str, ts: int, key_id: str) -> str:
    """HMAC-SHA256 рукопожатия. Секрет по сети не уходит — уходит только подпись."""
    return hmac.new(secret.encode(), f"{nonce}{ts}{key_id}".encode(), hashlib.sha256).hexdigest()


def frame(type_: str, **fields: Any) -> str:
    """Готовый к отправке кадр. Пустые значения не выкидываем: приложение отличает «» от «нет поля»."""
    return json.dumps({"v": VERSION, "type": type_, **fields}, ensure_ascii=False)


def parse(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("кадр должен быть объектом")
    return data


def backoff(attempt: int) -> int:
    """Пауза перед попыткой номер attempt (с нуля)."""
    return BACKOFF[min(attempt, len(BACKOFF) - 1)]
