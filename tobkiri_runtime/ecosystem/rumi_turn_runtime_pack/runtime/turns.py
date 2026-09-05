"""Profile-bound turn lifecycle owner without conversation persistence."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Callable, Mapping

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TERMINAL = {"completed", "failed", "cancelled"}
_ALLOWED = {
    "queued": {"running", "cancelled"},
    "running": {"waiting", "completed", "failed", "cancelled"},
    "waiting": {"running", "failed", "cancelled"},
}


class TurnConflict(RuntimeError):
    """Raised when a turn lifecycle mutation is stale or invalid."""


class TurnRuntime:
    """Own bounded in-process turn state and emitted lifecycle events."""

    def __init__(self, *, max_terminal_turns: int = 128) -> None:
        self.max_terminal_turns = max(1, max_terminal_turns)
        self._lock = threading.RLock()
        self._turns: dict[str, dict[str, Any]] = {}
        self._request_ids: dict[str, str] = {}

    def begin(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Begin one idempotent turn bound to a conversation revision."""
        request_id = _identifier(payload.get("request_id") or uuid.uuid4())
        with self._lock:
            known = self._request_ids.get(request_id)
            if known is not None:
                return _copy(self._turns[known])
            turn_id = _identifier(payload.get("turn_id") or uuid.uuid4())
            if turn_id in self._turns:
                raise TurnConflict("turn already exists")
            now = _now_ms()
            turn = {
                "id": turn_id,
                "request_id": request_id,
                "profile_id": str(payload.get("profile_id") or "default"),
                "conversation_id": _identifier(payload.get("conversation_id")),
                "conversation_revision": max(
                    1, int(payload.get("conversation_revision") or 0)
                ),
                "status": "queued",
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "guidance": [],
                "handoff": None,
                "result_reference": None,
                "error": None,
                "events": [],
            }
            self._event(turn, "turn.queued", {})
            self._turns[turn_id] = turn
            self._request_ids[request_id] = turn_id
            return _copy(turn)

    def get(self, turn_id: str) -> dict[str, Any] | None:
        """Return one ephemeral turn snapshot."""
        with self._lock:
            turn = self._turns.get(_identifier(turn_id))
            return _copy(turn) if turn is not None else None

    def list(self, *, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """List bounded turn snapshots, optionally for one conversation."""
        with self._lock:
            values = [
                _copy(turn)
                for turn in self._turns.values()
                if conversation_id is None
                or turn.get("conversation_id") == conversation_id
            ]
        values.sort(key=lambda turn: int(turn.get("updated_at") or 0), reverse=True)
        return values

    def transition(
        self,
        turn_id: str,
        status: str,
        *,
        expected_revision: int,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply an allowed lifecycle transition at an exact revision."""
        status = str(status).strip().lower()
        with self._lock:
            turn = self._required(turn_id)
            self._assert_revision(turn, expected_revision)
            allowed = _ALLOWED.get(turn["status"], set())
            if status not in allowed:
                raise TurnConflict("turn lifecycle transition is invalid")
            safe_details = _copy(details or {})
            turn["status"] = status
            turn["revision"] += 1
            turn["updated_at"] = _now_ms()
            if status == "completed":
                turn["result_reference"] = safe_details.get("result_reference")
            if status == "failed":
                turn["error"] = safe_details.get("error")
            self._event(turn, f"turn.{status}", safe_details)
            self._prune()
            return _copy(turn)

    def steer(
        self,
        turn_id: str,
        guidance: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Attach guidance to a nonterminal turn without storing messages."""
        with self._lock:
            turn = self._required(turn_id)
            self._assert_revision(turn, expected_revision)
            if turn["status"] in _TERMINAL:
                raise TurnConflict("terminal turn cannot be steered")
            item = {"id": str(uuid.uuid4()), "value": _copy(guidance)}
            item["status"] = "queued"
            turn["guidance"].append(item)
            turn["revision"] += 1
            turn["updated_at"] = _now_ms()
            self._event(turn, "turn.steered", {"guidance_id": item["id"]})
            return _copy(turn)

    def consume_guidance(
        self,
        turn_id: str,
        *,
        expected_revision: int,
        guidance_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Atomically mark all queued guidance consumed and return those items."""
        with self._lock:
            turn = self._required(turn_id)
            self._assert_revision(turn, expected_revision)
            selected_ids = (
                {str(value) for value in guidance_ids if str(value)}
                if guidance_ids is not None
                else None
            )
            queued = [
                item
                for item in turn["guidance"]
                if item.get("status") == "queued"
                and (
                    selected_ids is None
                    or str(item.get("id") or "") in selected_ids
                )
            ]
            for item in queued:
                item["status"] = "consumed"
                item["consumed_at"] = _now_ms()
            if queued:
                turn["revision"] += 1
                turn["updated_at"] = _now_ms()
                self._event(
                    turn,
                    "turn.guidance_consumed",
                    {"guidance_ids": [item["id"] for item in queued]},
                )
            return {"turn": _copy(turn), "items": _copy(queued)}

    def cancel_guidance(
        self, turn_id: str, guidance_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        """Cancel one queued guidance item at an exact turn revision."""
        with self._lock:
            turn = self._required(turn_id)
            self._assert_revision(turn, expected_revision)
            item = next(
                (value for value in turn["guidance"] if value["id"] == guidance_id),
                None,
            )
            if item is None:
                raise KeyError("turn guidance is unknown")
            if item.get("status") != "queued":
                raise TurnConflict("only queued guidance can be cancelled")
            item["status"] = "cancelled"
            item["cancelled_at"] = _now_ms()
            turn["revision"] += 1
            turn["updated_at"] = _now_ms()
            self._event(turn, "turn.guidance_cancelled", {"guidance_id": guidance_id})
            return {"turn": _copy(turn), "item": _copy(item)}

    def handoff(
        self,
        turn_id: str,
        target: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Record an explicit handoff reference for a nonterminal turn."""
        with self._lock:
            turn = self._required(turn_id)
            self._assert_revision(turn, expected_revision)
            if turn["status"] in _TERMINAL:
                raise TurnConflict("terminal turn cannot be handed off")
            turn["handoff"] = _copy(target)
            turn["revision"] += 1
            turn["updated_at"] = _now_ms()
            self._event(turn, "turn.handoff", {"target": turn["handoff"]})
            return _copy(turn)

    def _required(self, turn_id: str) -> dict[str, Any]:
        turn = self._turns.get(_identifier(turn_id))
        if turn is None:
            raise KeyError("turn is unknown")
        return turn

    @staticmethod
    def _assert_revision(turn: Mapping[str, Any], expected: int) -> None:
        if int(turn["revision"]) != expected:
            raise TurnConflict("turn revision is stale")

    @staticmethod
    def _event(
        turn: dict[str, Any], name: str, details: Mapping[str, Any]
    ) -> None:
        turn["events"].append(
            {
                "sequence": len(turn["events"]),
                "name": name,
                "at": _now_ms(),
                "details": _copy(details),
            }
        )

    def _prune(self) -> None:
        terminal = sorted(
            (
                turn for turn in self._turns.values()
                if turn["status"] in _TERMINAL
            ),
            key=lambda item: item["updated_at"],
        )
        for turn in terminal[:-self.max_terminal_turns]:
            self._turns.pop(turn["id"], None)
            self._request_ids.pop(turn["request_id"], None)


_RUNTIMES: dict[str, TurnRuntime] = {}
_RUNTIMES_LOCK = threading.Lock()


def create_turn_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create read operations for the profile turn owner."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name == "get":
            return _runtime(payload).get(str(payload.get("turn_id") or ""))
        if name == "list":
            conversation_id = payload.get("conversation_id")
            return {
                "turns": _runtime(payload).list(
                    conversation_id=str(conversation_id) if conversation_id else None
                )
            }
        raise ValueError(f"unknown turn resource operation: {name}")

    return operation


def create_turn_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create turn lifecycle mutation operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        runtime = _runtime(payload)
        if name == "begin":
            return runtime.begin(payload)
        turn_id = str(payload.get("turn_id") or "")
        expected = int(payload.get("expected_revision") or 0)
        if name == "transition":
            return runtime.transition(
                turn_id,
                str(payload.get("status") or ""),
                expected_revision=expected,
                details=_mapping(payload.get("details")),
            )
        if name == "steer":
            return runtime.steer(
                turn_id,
                _mapping(payload.get("guidance")),
                expected_revision=expected,
            )
        if name == "handoff":
            return runtime.handoff(
                turn_id,
                _mapping(payload.get("target")),
                expected_revision=expected,
            )
        if name == "consume_guidance":
            return runtime.consume_guidance(
                turn_id,
                expected_revision=expected,
                guidance_ids=_optional_identifier_list(
                    payload,
                    "guidance_ids",
                ),
            )
        if name == "cancel_guidance":
            return runtime.cancel_guidance(
                turn_id,
                str(payload.get("guidance_id") or ""),
                expected_revision=expected,
            )
        raise ValueError(f"unknown turn action: {name}")

    return operation


def _runtime(payload: Mapping[str, Any]) -> TurnRuntime:
    profile_id = str(payload.get("profile_id") or "default")
    with _RUNTIMES_LOCK:
        return _RUNTIMES.setdefault(profile_id, TurnRuntime())


def _identifier(value: Any) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError("turn identifier is invalid")
    return identifier


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _optional_identifier_list(
    payload: Mapping[str, Any],
    key: str,
) -> list[str] | None:
    if key not in payload:
        return None
    values = payload.get(key)
    if not isinstance(values, list):
        raise ValueError(f"{key} must be an array")
    return [_identifier(value) for value in values]


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _now_ms() -> int:
    return int(time.time() * 1000)
