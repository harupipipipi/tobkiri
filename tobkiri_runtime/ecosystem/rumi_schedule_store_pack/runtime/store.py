"""Profile-scoped schedule state with lease and retry ownership."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

AUTHORITY = "rumi.service.host.authorize.v1"
SERVICE_PACK_ID = "rumi_schedule_store_pack"
VERSION = "rumi.schedules.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TERMINAL = {"cancelled", "completed", "failed"}


class ScheduleConflict(RuntimeError):
    """Raised when a revision or lease no longer matches."""


class ScheduleStore:
    """Own canonical schedules and their dispatch lifecycle."""

    def __init__(
        self,
        profile_id: str,
        *,
        root: Path | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(root or USER_DATA_DIR) / "packs" / SERVICE_PACK_ID / "profiles" / self.profile_id
        )
        self.path = self.root / "schedules.json"
        self.lock_root = self.root / "locks"
        self.clock = clock or (lambda: int(time.time() * 1000))

    def snapshot(self) -> dict[str, Any]:
        """Return a copy of all schedules."""

        state = self._read()
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "schedules": [state["schedules"][key] for key in sorted(state["schedules"])],
        }

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        """Return one exact schedule."""

        value = self._read()["schedules"].get(_identifier(schedule_id))
        return _copy(value) if isinstance(value, Mapping) else None

    def due(
        self,
        now_ms: int,
        limit: int,
        schedule_id: str = "",
    ) -> dict[str, Any]:
        """Return due, enabled, unleased schedules without claiming them."""

        state = self._read()
        values = [
            item
            for item in state["schedules"].values()
            if item["enabled"]
            and item["status"] not in _TERMINAL
            and int(item["next_run_at_ms"]) <= now_ms
            and int(item.get("lease_expires_at_ms") or 0) <= now_ms
            and (not schedule_id or item["id"] == schedule_id)
        ]
        values.sort(key=lambda item: (item["next_run_at_ms"], item["id"]))
        return {"revision": state["revision"], "schedules": _copy(values[:limit])}

    def apply(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one exact revision-bound state transition."""

        with NamedLock(self.lock_root, "schedules"):
            state = self._read()
            _assert_revision(state, int(arguments["expected_revision"]))
            result = self._transition(state, name, arguments)
            state["revision"] += 1
            self._write(state)
            return {**result, "revision": state["revision"]}

    def _transition(
        self,
        state: dict[str, Any],
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        schedule_id = _identifier(arguments["schedule_id"])
        now_ms = self.clock()
        if name == "create":
            if schedule_id in state["schedules"]:
                raise ValueError("schedule already exists")
            record = {
                "id": schedule_id,
                "name": str(arguments["name"]),
                "action_id": str(arguments["action_id"]),
                "payload": _copy(arguments["payload"]),
                "next_run_at_ms": int(arguments["next_run_at_ms"]),
                "interval_ms": int(arguments["interval_ms"]),
                "max_attempts": int(arguments["max_attempts"]),
                "attempt": 0,
                "enabled": True,
                "status": "scheduled",
                "lease_id": "",
                "lease_expires_at_ms": 0,
                "last_error": "",
                "created_at_ms": now_ms,
                "updated_at_ms": now_ms,
            }
            state["schedules"][schedule_id] = record
            return {"schedule": _copy(record)}
        record = state["schedules"].get(schedule_id)
        if record is None:
            raise KeyError("schedule is unknown")
        if name == "delete":
            if record["status"] == "running":
                raise ScheduleConflict("running schedule must be cancelled first")
            del state["schedules"][schedule_id]
            return {"deleted": schedule_id}
        if name == "update":
            if record["status"] == "running":
                raise ScheduleConflict("running schedule cannot be edited")
            for key in (
                "name",
                "action_id",
                "payload",
                "next_run_at_ms",
                "interval_ms",
                "max_attempts",
            ):
                if key in arguments["updates"]:
                    record[key] = _copy(arguments["updates"][key])
            record["updated_at_ms"] = now_ms
            return {"schedule": _copy(record)}
        if name in {"pause", "resume", "cancel"}:
            if record["status"] == "running" and name != "cancel":
                raise ScheduleConflict("running schedule must be cancelled first")
            record["enabled"] = name == "resume"
            record["status"] = "scheduled" if name == "resume" else name + "d"
            if name == "cancel":
                record["status"] = "cancelled"
            record["lease_id"] = ""
            record["lease_expires_at_ms"] = 0
            record["updated_at_ms"] = now_ms
            return {"schedule": _copy(record)}
        if name == "claim":
            if not record["enabled"] or record["status"] in _TERMINAL:
                raise ScheduleConflict("schedule is not claimable")
            if int(record.get("lease_expires_at_ms") or 0) > now_ms:
                raise ScheduleConflict("schedule already has an active lease")
            if int(record["next_run_at_ms"]) > now_ms:
                raise ScheduleConflict("schedule is not due")
            if int(arguments["lease_expires_at_ms"]) <= now_ms:
                raise ScheduleConflict("schedule lease is already expired")
            record["lease_id"] = str(arguments["lease_id"])
            record["lease_expires_at_ms"] = int(arguments["lease_expires_at_ms"])
            record["status"] = "running"
            record["attempt"] = int(record["attempt"]) + 1
            record["updated_at_ms"] = now_ms
            return {"schedule": _copy(record)}
        if record["status"] != "running" or str(record.get("lease_id") or "") != str(
            arguments["lease_id"]
        ):
            raise ScheduleConflict("schedule lease does not match")
        succeeded = name == "complete"
        record["lease_id"] = ""
        record["lease_expires_at_ms"] = 0
        record["last_error"] = str(arguments.get("error") or "")
        if succeeded and int(record["interval_ms"]) > 0:
            record["status"] = "scheduled"
            record["attempt"] = 0
            record["next_run_at_ms"] = now_ms + int(record["interval_ms"])
        elif not succeeded and int(record["attempt"]) < int(record["max_attempts"]):
            record["status"] = "scheduled"
            record["next_run_at_ms"] = now_ms + _retry_delay(record["attempt"])
        else:
            record["status"] = "completed" if succeeded else "failed"
            record["enabled"] = False
        record["updated_at_ms"] = now_ms
        return {"schedule": _copy(record)}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": VERSION, "revision": 0, "schedules": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("version") != VERSION:
            raise ValueError("schedule state is invalid")
        schedules = value.get("schedules")
        if not isinstance(schedules, Mapping):
            raise ValueError("schedule records are invalid")
        return {
            "version": VERSION,
            "revision": max(0, int(value.get("revision") or 0)),
            "schedules": {str(key): _copy(item) for key, item in schedules.items()},
        }

    def _write(self, state: Mapping[str, Any]) -> None:
        _atomic_json(self.path, state)


def create_schedule_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create read-only schedule resource operations."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = ScheduleStore(_profile(payload))
        if name == "list":
            return store.snapshot()
        if name == "get":
            return store.get(str(payload.get("schedule_id") or ""))
        if name == "due":
            return store.due(
                max(0, int(payload.get("now_ms") or 0)),
                max(1, min(100, int(payload.get("limit") or 20))),
                str(payload.get("schedule_id") or ""),
            )
        raise ValueError(f"unknown schedule resource operation: {name}")

    return operation


def create_schedule_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated schedule state transitions."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        arguments = _arguments(name, payload)
        _redeem(client, payload, name, arguments)
        return ScheduleStore(_profile(payload)).apply(name, arguments)

    return operation


def _arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if name not in {
        "create",
        "update",
        "delete",
        "pause",
        "resume",
        "cancel",
        "claim",
        "complete",
        "fail",
    }:
        raise ValueError(f"unknown schedule action: {name}")
    revision = payload.get("expected_revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("schedule expected_revision must be a nonnegative integer")
    arguments: dict[str, Any] = {
        "schedule_id": str(payload.get("schedule_id") or ""),
        "expected_revision": revision,
    }
    if name == "create":
        arguments.update(
            {
                "name": str(payload.get("name") or "Scheduled action")[:120],
                "action_id": _identifier(payload.get("action_id")),
                "payload": dict(_mapping(payload.get("payload"))),
                "next_run_at_ms": max(0, int(payload.get("next_run_at_ms") or 0)),
                "interval_ms": max(0, int(payload.get("interval_ms") or 0)),
                "max_attempts": max(1, min(20, int(payload.get("max_attempts") or 3))),
            }
        )
    elif name == "update":
        arguments["updates"] = _updates(payload.get("updates"))
    elif name == "claim":
        arguments["lease_id"] = _identifier(payload.get("lease_id") or uuid.uuid4())
        arguments["lease_expires_at_ms"] = max(0, int(payload.get("lease_expires_at_ms") or 0))
    elif name in {"complete", "fail"}:
        arguments["lease_id"] = _identifier(payload.get("lease_id"))
        arguments["error"] = str(payload.get("error") or "")[:1000]
    return arguments


def _redeem(
    client: Any,
    payload: Mapping[str, Any],
    name: str,
    arguments: Mapping[str, Any],
) -> None:
    result = client.invoke(
        AUTHORITY,
        "redeem",
        {
            "receipt": str(payload.get("authority_receipt") or ""),
            "service_pack_id": SERVICE_PACK_ID,
            "operation": f"schedule.{name}",
            "authority": "schedule.manage",
            "caller_id": str(payload.get("caller_id") or ""),
            "caller_pack_id": str(payload.get("caller_pack_id") or ""),
            "caller_function_id": str(payload.get("caller_function_id") or ""),
            "profile_id": _profile(payload),
            "workspace_id": "",
            "session_id": str(payload.get("session_id") or ""),
            "arguments": dict(arguments),
        },
    )
    if not result.get("authorized"):
        raise PermissionError(str(result.get("reason") or "schedule authority denied"))


def _updates(value: Any) -> dict[str, Any]:
    updates = dict(_mapping(value))
    allowed = {
        "name",
        "action_id",
        "payload",
        "next_run_at_ms",
        "interval_ms",
        "max_attempts",
    }
    if set(updates) - allowed:
        raise ValueError("schedule update contains unsupported fields")
    if "action_id" in updates:
        updates["action_id"] = _identifier(updates["action_id"])
    if "name" in updates:
        updates["name"] = str(updates["name"])[:120]
    if "payload" in updates:
        updates["payload"] = dict(_mapping(updates["payload"]))
    for key in ("next_run_at_ms", "interval_ms"):
        if key in updates:
            updates[key] = max(0, int(updates[key]))
    if "max_attempts" in updates:
        updates["max_attempts"] = max(1, min(20, int(updates["max_attempts"])))
    return updates


def _identifier(value: Any) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError("schedule identifier is invalid")
    return identifier


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("schedule payload must be an object")
    return value


def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
    if int(state.get("revision") or 0) != expected:
        raise ScheduleConflict("schedule revision is stale")


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _retry_delay(attempt: int) -> int:
    return min(3_600_000, 1_000 * (2 ** max(0, min(12, attempt - 1))))


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".schedule-", suffix=".tmp")
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


_V4_ROUTES = {
    "rumi_schedule_store_pack.schedule-store.action": (
        "tobkiri.action.schedule.v1",
        "rumi_schedule_store_pack.schedule-action",
    ),
    "rumi_schedule_store_pack.schedule-store.resource": (
        "tobkiri.resource.schedule.v1",
        "rumi_schedule_store_pack.schedule-resource",
    ),
}


def _invoke_v4_owner(
    function_id: str, context: Any, payload: Mapping[str, Any], invocation: Any
) -> Mapping[str, Any]:
    operation = str(payload.get("operation") or "")
    store = ScheduleStore(context.profile_id, root=context.user_data_root)
    if function_id.endswith(".resource"):
        if set(payload) - {"operation", "profile_id", "schedule_id", "now_ms", "limit"}:
            raise ValueError("schedule resource payload is invalid")
        if operation == "list":
            return store.snapshot()
        if operation == "get":
            return {"schedule": store.get(str(payload.get("schedule_id") or ""))}
        if operation == "due":
            return store.due(
                int(payload.get("now_ms", store.clock())),
                int(payload.get("limit", 20)),
                str(payload.get("schedule_id") or ""),
            )
        raise ValueError("schedule resource operation is invalid")
    arguments = _arguments(operation, payload)
    if set(payload) - set(arguments) - {"operation", "profile_id"}:
        raise PermissionError("schedule action payload is invalid")
    invocation.assert_current()
    return store.apply(operation, arguments)


# Captured v4 entrypoints retain Broker-owned authority and profile scope.


class _OwnerHostFactoryV4:
    """Capture only exact, verified operations for this owner Function."""

    def __init__(self, function_id: str) -> None:
        self.function_id = function_id

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Bind operations and persistence to the selected Profile activation."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or not context.provider_bindings
            or any(
                binding.function.function_id != self.function_id
                for binding in context.provider_bindings
            )
        ):
            raise PermissionError("owner capture scope is incomplete")
        expected_contract, expected_operation = _V4_ROUTES[self.function_id]
        if any(
            binding.operation.contract_id != expected_contract
            or binding.operation.operation_id != expected_operation
            for binding in context.provider_bindings
        ):
            raise PermissionError("owner operation binding is invalid")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if operation_id != expected_operation or (
                "profile_id" in payload and payload["profile_id"] != context.profile_id
            ):
                raise PermissionError("owner invocation scope is invalid")
            invocation.assert_current()
            result = _invoke_v4_owner(self.function_id, context, payload, invocation)
            invocation.assert_current()
            return result

        contributions = []
        for binding in context.provider_bindings:
            key = (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            domain_id = context.domain_ids.get(key)
            if domain_id is None:
                raise PermissionError("owner domain binding is unavailable")
            contributions.append(
                HostProviderContributionV4(
                    contract_id=binding.operation.contract_id,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                )
            )
        return CapturedHostProviderV4(tuple(contributions), lambda: None)


HOST_PROVIDER_FACTORY = {
    function_id: _OwnerHostFactoryV4(function_id) for function_id in _V4_ROUTES
}
