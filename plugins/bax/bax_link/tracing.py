"""Контекст непосредственного ответа; не связывает фоновые задачи по RID или сессии."""

from __future__ import annotations

import asyncio
import re
import time
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
    token = _current.set((asyncio.current_task(), parent, [], [0]))
    try:
        yield
    finally:
        _current.reset(token)


def _owned_context():
    context = _current.get()
    try:
        return context if context and context[0] is asyncio.current_task() and context[1] else None
    except RuntimeError:
        return None  # Синхронное чтение вне event loop не привязывается к чужой команде.


_RPC_METHODS = frozenset({
    "initialize", "thread/read", "thread/list", "thread/start", "thread/resume", "thread/fork",
    "thread/archive", "thread/name/set", "thread/compact/start", "thread/loaded/list",
    "thread/turns/list", "turn/start", "turn/steer", "turn/interrupt", "model/list", "skills/list",
    "configRequirements/read", "thread/backgroundTerminals/list", "thread/backgroundTerminals/terminate",
    "thread/unarchive",
    "account/read", "account/rateLimits/read", "config/read", "thread/items/list",
})


@contextmanager
def dependency(system, operation):
    """Время на границе зависимости; аргументы, пути и ответы не попадают в метаданные."""
    context = _owned_context()
    if system not in {"codex", "filesystem", "mcp"}:
        raise ValueError("Неизвестная зависимость")
    allowed = (_RPC_METHODS if system == "codex" else
               {"read", "open"} if system == "filesystem" else {"notify"})
    operation = operation if operation in allowed else "other"
    began, wall = time.monotonic_ns(), time.time_ns()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        if context and _owned_context() is context and len(context[2]) >= 32:
            context[3][0] += 1
        elif context and _owned_context() is context:
            elapsed = min(300_000.0, max(0.0, (time.monotonic_ns() - began) / 1_000_000))
            context[2].append({"system": system, "operation": operation,
                               "started_at_ms": wall // 1_000_000, "elapsed_ms": elapsed, "outcome": outcome})


def response_fields(fields):
    context = _owned_context()
    if not context:
        return fields
    parent = fields.get("traceparent", context[1])
    result = {**fields, "traceparent": parent}
    # Явный чужой parent не получает измерения текущей команды.
    if parent == context[1] and context[2]:
        result["dependency_timings"] = list(context[2])
        context[2].clear()
    if parent == context[1] and context[3][0]:
        result["dependency_timings_dropped"] = min(context[3][0], 1_000_000)
        context[3][0] = 0
    return result
