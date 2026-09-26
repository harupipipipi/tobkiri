"""Deterministic adapter routing with persistent idempotency protection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

ADAPTER = "rumi.action.job.adapter.v1"
SERVICE_PACK_ID = "rumi_job_action_broker_pack"
VERSION = "rumi.job-dispatch-ledger.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_FORBIDDEN = {
    "approved",
    "approval_token",
    "authority_token",
    "authority_receipt",
    "viewer_host_approved",
    "yolo_mode",
}


class JobActionBroker:
    """Route one action ID to one selected adapter and suppress replay."""

    def __init__(self, client: Any, profile_id: str) -> None:
        self.client = client
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(USER_DATA_DIR)
            / "packs"
            / SERVICE_PACK_ID
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "dispatch-ledger.json"
        self.lock_root = self.root / "locks"

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch or cancel one idempotent job action."""

        if name == "dispatch":
            return self._dispatch(payload)
        if name == "cancel":
            return self._cancel(payload)
        if name == "status":
            return self._status(payload)
        raise ValueError(f"unknown job action operation: {name}")

    def _dispatch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _reject_authority_material(payload)
        action_id = _identifier(payload.get("action_id"), "action_id")
        key = _identifier(payload.get("idempotency_key"), "idempotency_key")
        arguments = dict(_mapping(payload.get("payload")))
        digest = _digest({"action_id": action_id, "payload": arguments})
        with NamedLock(self.lock_root, "dispatch"):
            state = self._read()
            current = state["entries"].get(key)
            if current is not None:
                if current["payload_hash"] != digest:
                    raise PermissionError("idempotency key payload does not match")
                return {
                    "status": current["status"],
                    "deduplicated": True,
                    "idempotency_key": key,
                    "dispatch": _copy(current),
                }
            entry = {
                "idempotency_key": key,
                "action_id": action_id,
                "payload_hash": digest,
                "status": "running",
                "provider_instance_id": "",
                "result": None,
                "error": "",
                "created_at_ms": _now_ms(),
                "updated_at_ms": _now_ms(),
            }
            state["entries"][key] = entry
            self._write(state)
        provider = self._provider(action_id)
        entry["provider_instance_id"] = str(provider["provider_instance_id"])
        try:
            result = self.client.invoke(
                ADAPTER,
                "dispatch",
                {
                    "action_id": action_id,
                    "payload": arguments,
                    "idempotency_key": key,
                    "schedule_id": str(payload.get("schedule_id") or ""),
                    "lease_id": str(payload.get("lease_id") or ""),
                    "profile_id": self.profile_id,
                },
                provider_instance_id=entry["provider_instance_id"],
            )
            entry["status"] = _status(result)
            entry["result"] = _bounded(result)
        except Exception as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)[:1000]
            self._finish(key, entry)
            raise
        self._finish(key, entry)
        return {
            "status": entry["status"],
            "idempotency_key": key,
            "result": result,
            "provider_instance_id": entry["provider_instance_id"],
        }

    def _cancel(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        key = _identifier(payload.get("idempotency_key"), "idempotency_key")
        state = self._read()
        entry = state["entries"].get(key)
        if entry is None:
            return {"status": "unknown", "idempotency_key": key}
        provider_id = str(entry.get("provider_instance_id") or "")
        if not provider_id:
            return {"status": "cancellation_pending", "idempotency_key": key}
        result = self.client.invoke(
            ADAPTER,
            "cancel",
            {
                "action_id": entry["action_id"],
                "idempotency_key": key,
                "profile_id": self.profile_id,
            },
            provider_instance_id=provider_id,
        )
        entry["status"] = "cancelled"
        entry["result"] = _bounded(result)
        self._finish(key, entry)
        return {"status": "cancelled", "idempotency_key": key, "result": result}

    def _status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        key = _identifier(payload.get("idempotency_key"), "idempotency_key")
        value = self._read()["entries"].get(key)
        return _copy(value) if value is not None else {"status": "unknown"}

    def _provider(self, action_id: str) -> Mapping[str, Any]:
        providers = self.client.providers(ADAPTER)
        matches = [
            item for item in providers if str(item.get("instance_key") or "") == action_id
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected one selected job adapter for {action_id}; found {len(matches)}"
            )
        return matches[0]

    def _finish(self, key: str, entry: dict[str, Any]) -> None:
        with NamedLock(self.lock_root, "dispatch"):
            state = self._read()
            current = state["entries"].get(key)
            if current is None or current["payload_hash"] != entry["payload_hash"]:
                raise RuntimeError("job dispatch ledger changed during execution")
            entry["updated_at_ms"] = _now_ms()
            state["entries"][key] = _copy(entry)
            self._write(state)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": VERSION, "entries": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("version") != VERSION:
            raise ValueError("job dispatch ledger is invalid")
        entries = value.get("entries")
        if not isinstance(entries, Mapping):
            raise ValueError("job dispatch entries are invalid")
        return {"version": VERSION, "entries": _copy(entries)}

    def _write(self, value: Mapping[str, Any]) -> None:
        _atomic_json(self.path, value)


def create_job_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the selected global job action broker."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return JobActionBroker(
            client,
            str(payload.get("profile_id") or "default"),
        ).invoke(name, payload)

    return operation


def _reject_authority_material(payload: Mapping[str, Any]) -> None:
    found = sorted(_authority_keys(payload))
    if found:
        raise PermissionError("job payload contains forbidden authority material")


def _authority_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        found = {str(key) for key in value if str(key) in _FORBIDDEN}
        for item in value.values():
            found.update(_authority_keys(item))
        return found
    if isinstance(value, (list, tuple)):
        found: set[str] = set()
        for item in value:
            found.update(_authority_keys(item))
        return found
    return set()


def _identifier(value: Any, name: str) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError(f"{name} is invalid")
    return identifier


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("job action payload must be an object")
    return value


def _status(result: Any) -> str:
    if not isinstance(result, Mapping):
        return "failed"
    value = str(result.get("status") or "")
    return value if value in {"ok", "accepted", "completed", "failed"} else "failed"


def _bounded(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        return {"status": "truncated", "sha256": hashlib.sha256(encoded.encode()).hexdigest()}
    return json.loads(encoded)


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".job-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

