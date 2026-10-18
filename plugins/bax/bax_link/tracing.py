"""Контекст непосредственного ответа; не связывает фоновые задачи по RID или сессии."""

from __future__ import annotations

import asyncio
import re
from contextlib import contextmanager
from contextvars import ContextVar

_current = ContextVar("bax_command_context", default=None)
_PATTERN = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}\Z")


def valid_parent(value):
    return (
        isinstance(value, str)
        and _PATTERN.fullmatch(value) is not None
        and value[3:35] != "0" * 32
        and value[36:52] != "0" * 16
    )


@contextmanager
def command_context(frame=None):
    parent = frame.get("traceparent") if frame is not None else None
    if parent is not None and not valid_parent(parent):
        raise ValueError("Некорректный контекст трассировки команды")
    token = _current.set((asyncio.current_task(), parent))
    try:
        yield
    finally:
        _current.reset(token)


def response_fields(fields):
    context = _current.get()
    # Созданные обработчиком фоновые задачи не наследуют случайную команду.
    if "traceparent" not in fields and context and context[0] is asyncio.current_task() and context[1]:
        return {**fields, "traceparent": context[1]}
    return fields
