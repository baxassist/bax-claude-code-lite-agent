"""Полное состояние управления сессией; правила актуальности принадлежат источнику."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4


class SessionView:
    def __init__(self):
        self.epoch = str(uuid4())
        self.revision = 0
        self.failures: dict[str, dict] = {}
        self.failure_versions: dict[str, int] = {}
        self.positions: dict[str, dict] = {}
        self.enabled = False
        self._last: dict | None = None

    def observe(self, kind: str, fields: dict) -> None:
        if kind not in {
            "message",
            "delta",
            "done",
            "status",
            "question",
            "question.resolved",
            "error",
            "error.resolved",
            "history.done",
        }:
            return
        self.revision += 1
        if kind == "message" and fields.get("turn_id"):
            self.positions[fields["turn_id"]] = {
                "after_message_id": fields.get("message_id"),
                "after_entry": fields.get("id"),
            }
        if kind == "message" and fields.get("kind") == "error":
            kind = "error"
        if kind == "error" and fields.get("scope", "session") == "session":
            if fields.get("operation") in {"subscribe", "history"}:
                return
            identity = (
                fields.get("error_id")
                or fields.get("rid")
                or ":".join(str(fields.get(key, "")) for key in ("operation", "turn_id", "code"))
            )
            self.failures[identity] = {**deepcopy(fields), "error_id": identity}
            self.failure_versions[identity] = self.revision
        elif kind == "error.resolved":
            for identity, failure in list(self.failures.items()):
                turn_resolved = (
                    fields.get("turn_id")
                    and failure.get("turn_id") == fields["turn_id"]
                    and failure.get("will_retry")
                )
                delivery_resolved = (
                    fields.get("delivery") == "accepted"
                    and (
                        (fields.get("error_id") and identity == fields["error_id"])
                        or (fields.get("rid") and failure.get("rid") == fields["rid"])
                    )
                    and (failure.get("delivery") in {"unknown", "rejected", "queued"})
                )
                if turn_resolved or delivery_resolved:
                    del self.failures[identity]
                    self.failure_versions.pop(identity, None)
        if kind == "message" and fields.get("kind") == "user" and fields.get("rid"):
            # История с точным RID является квитанцией даже после потери live ACK.
            self.observe("error.resolved", {"rid": fields["rid"], "delivery": "accepted"})

    def reconcile_turns(self, turns: list[dict], baseline: int) -> set[str]:
        completed = {
            turn["id"]
            for turn in turns
            if turn.get("status") in {"completed", "interrupted"} and not turn.get("error")
        }
        resolved = {
            identity
            for identity, failure in self.failures.items()
            if failure.get("turn_id") in completed and self.failure_versions.get(identity, 0) <= baseline
        }
        for identity in resolved:
            del self.failures[identity]
            self.failure_versions.pop(identity, None)
        if resolved:
            self.revision += 1
        return resolved

    def snapshot(self, *, state: str, question: dict | None, supports: list[str], force=False) -> dict | None:
        actions = set(supports) - {"session.view", "history.done", "attachments"}
        if state not in {"ready", "busy", "waiting"}:
            actions = set()
        if state != "ready":
            actions.discard("session.compact")
        if state not in {"busy", "waiting"}:
            actions.discard("cancel")
        if question is None:
            actions.discard("answer")
        failures = [
            {**deepcopy(failure), **self.positions.get(failure.get("turn_id", ""), {})}
            for failure in self.failures.values()
        ]
        value = {
            "state": state,
            "question": deepcopy(question),
            "failures": failures,
            "actions": sorted(actions),
        }
        if not force and value == self._last:
            return None
        self.revision += 1
        self._last = deepcopy(value)
        return {"epoch": self.epoch, "revision": self.revision, **value}
