"""Bounded IDE bridge sessions independent of chat and UI implementations."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
WORKSPACE = "rumi.resource.workspace.v1"
SERVICE_PACK_ID = "rumi_ide_bridge_service_pack"
_MAX_SESSIONS = 64
_MAX_EVENTS = 512


class IdeBridgeRuntime:
    """Own bounded process-lifetime IDE bridge session envelopes."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.lock = threading.RLock()
        self.sessions: dict[str, dict[str, Any]] = {}

    def observe(self, name: str, payload: Mapping[str, Any]) -> Any:
        """Observe session metadata and events without control authority."""
        if name == "get":
            session = self._get(str(payload.get("ide_session_id") or ""))
            return _copy(session) if session is not None else None
        if name == "list":
            with self.lock:
                return {"sessions": [_copy(item) for item in self.sessions.values()]}
        raise ValueError(f"unknown IDE observe operation: {name}")

    def control(self, name: str, payload: Mapping[str, Any]) -> Any:
        """Apply one receipt-gated IDE session control mutation."""
        if name == "create":
            arguments = {
                "workspace_id": str(payload.get("workspace_id") or ""),
                "bridge_kind": str(payload.get("bridge_kind") or "generic"),
                "profile": _safe_object(payload.get("ide_profile")),
            }
        elif name == "input":
            arguments = {
                "ide_session_id": str(payload.get("ide_session_id") or ""),
                "message": str(payload.get("message") or ""),
                "attachment_refs": _safe_list(payload.get("attachment_refs")),
            }
        elif name == "event":
            arguments = {
                "ide_session_id": str(payload.get("ide_session_id") or ""),
                "event": _safe_object(payload.get("event")),
            }
        elif name == "close":
            arguments = {
                "ide_session_id": str(payload.get("ide_session_id") or "")
            }
        else:
            raise ValueError(f"unknown IDE control operation: {name}")
        if name != "create":
            session = self._required(str(arguments["ide_session_id"]))
            if session["profile_id"] != _profile(payload):
                raise PermissionError("IDE session profile mismatch")
            arguments["workspace_id"] = session["workspace_id"]
        self._redeem(name, payload, arguments)
        if name == "create":
            return self._create(payload, arguments)
        if name in {"input", "event"}:
            return self._append(name, arguments)
        return self._close(arguments["ide_session_id"])

    def _create(
        self, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        workspace_id = str(arguments["workspace_id"])
        mount = self.client.invoke(
            WORKSPACE,
            "get",
            {"profile_id": _profile(payload), "workspace_id": workspace_id},
        )
        if not isinstance(mount, Mapping):
            raise KeyError("workspace mount is unknown")
        now = time.time()
        session = {
            "id": str(uuid.uuid4()),
            "profile_id": _profile(payload),
            "workspace_id": workspace_id,
            "bridge_kind": arguments["bridge_kind"],
            "ide_profile": arguments["profile"],
            "status": "open",
            "events": [],
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "bounded": True,
        }
        with self.lock:
            self._prune()
            if len(self.sessions) >= _MAX_SESSIONS:
                raise RuntimeError("IDE bridge session limit reached")
            self.sessions[session["id"]] = session
        return _copy(session)

    def _append(self, kind: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        session = self._required(str(arguments["ide_session_id"]))
        if session["status"] != "open":
            raise RuntimeError("IDE bridge session is closed")
        value = (
            {
                "message": arguments["message"],
                "attachment_refs": arguments["attachment_refs"],
            }
            if kind == "input"
            else arguments["event"]
        )
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("IDE bridge event exceeds size limit")
        event = {
            "id": str(uuid.uuid4()),
            "sequence": len(session["events"]),
            "kind": kind,
            "value": value,
            "created_at": time.time(),
        }
        session["events"].append(event)
        if len(session["events"]) > _MAX_EVENTS:
            session["events"] = session["events"][-_MAX_EVENTS:]
        session["revision"] += 1
        session["updated_at"] = time.time()
        return {"session": _copy(session), "event": _copy(event)}

    def _close(self, session_id: str) -> dict[str, Any]:
        session = self._required(session_id)
        session["status"] = "closed"
        session["revision"] += 1
        session["updated_at"] = time.time()
        return _copy(session)

    def _redeem(
        self, name: str, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": f"ide.session.{name}",
                "authority": "ide.session.control",
                "caller_id": str(payload.get("caller_id") or ""),
                "caller_pack_id": str(payload.get("caller_pack_id") or ""),
                "caller_function_id": str(payload.get("caller_function_id") or ""),
                "profile_id": _profile(payload),
                "workspace_id": str(arguments.get("workspace_id") or ""),
                "session_id": str(payload.get("session_id") or ""),
                "arguments": dict(arguments),
            },
        )
        if not result.get("authorized"):
            raise PermissionError(str(result.get("reason") or "IDE control denied"))

    def _get(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self.sessions.get(session_id)

    def _required(self, session_id: str) -> dict[str, Any]:
        session = self._get(session_id)
        if session is None:
            raise KeyError("IDE bridge session is unknown")
        return session

    def _prune(self) -> None:
        closed = sorted(
            (item for item in self.sessions.values() if item["status"] == "closed"),
            key=lambda item: float(item["updated_at"]),
        )
        for item in closed[: max(0, len(self.sessions) - _MAX_SESSIONS + 1)]:
            del self.sessions[item["id"]]


_RUNTIMES: dict[str, IdeBridgeRuntime] = {}
_LOCK = threading.Lock()


def create_ide_observe(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create IDE session observe operations."""
    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return _runtime(client, payload).observe(name, payload)
    return operation


def create_ide_control(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated IDE session control operations."""
    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return _runtime(client, payload).control(name, payload)
    return operation


def _runtime(client: Any, payload: Mapping[str, Any]) -> IdeBridgeRuntime:
    profile_id = _profile(payload)
    with _LOCK:
        return _RUNTIMES.setdefault(profile_id, IdeBridgeRuntime(client))


def _safe_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("IDE object payload is invalid")
    return _copy(value)


def _safe_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("IDE attachment refs must be a list")
    return _copy(value)


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")

