"""Lease-aware clock and global job-action dispatcher."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
SCHEDULE_RESOURCE = "rumi.resource.schedule.v1"
SCHEDULE_ACTION = "rumi.action.schedule.v1"
JOB_ACTION = "rumi.action.job.v1"
SERVICE_PACK_ID = "rumi_scheduler_runtime_pack"
STORE_PACK_ID = "rumi_schedule_store_pack"


class SchedulerRuntime:
    """Dispatch due schedules without importing any target implementation."""

    def __init__(self, client: Any, profile_id: str) -> None:
        self.client = client
        self.profile_id = profile_id
        self.lock = threading.RLock()
        self.stopping = False
        self.active: dict[str, str] = {}
        self.last_tick_at_ms = 0
        self.last_error = ""

    def status(self) -> dict[str, Any]:
        """Return process-lifetime clock state without schedule ownership."""

        with self.lock:
            return {
                "profile_id": self.profile_id,
                "stopping": self.stopping,
                "active": dict(self.active),
                "last_tick_at_ms": self.last_tick_at_ms,
                "last_error": self.last_error,
                "schedule_owner": STORE_PACK_ID,
                "dispatch_contract": JOB_ACTION,
            }

    def control(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Apply receipt-gated tick, trigger, or stop control."""

        if name == "tick":
            arguments = {
                "now_ms": max(0, int(payload.get("now_ms") or _now_ms())),
                "limit": max(1, min(100, int(payload.get("limit") or 20))),
            }
        elif name == "trigger":
            arguments = {"schedule_id": str(payload.get("schedule_id") or "")}
        elif name == "stop":
            arguments = {"stop": True}
        else:
            raise ValueError(f"unknown scheduler control operation: {name}")
        self._redeem(payload, name, arguments)
        if name == "tick":
            return self._tick(arguments["now_ms"], arguments["limit"])
        if name == "trigger":
            return self._trigger(arguments["schedule_id"])
        with self.lock:
            self.stopping = True
            active = dict(self.active)
        cancellations = []
        for schedule_id, lease_id in active.items():
            try:
                cancellations.append(
                    self.client.invoke(
                        JOB_ACTION,
                        "cancel",
                        {
                            "action_id": "scheduler.dispatch",
                            "idempotency_key": f"{schedule_id}:{lease_id}",
                            "schedule_id": schedule_id,
                            "lease_id": lease_id,
                            "profile_id": self.profile_id,
                        },
                    )
                )
            except Exception as exc:
                cancellations.append({"status": "error", "error": str(exc)})
        return {"stopping": True, "cancellations": cancellations}

    def _tick(
        self,
        now_ms: int,
        limit: int,
        target_schedule_id: str = "",
    ) -> dict[str, Any]:
        with self.lock:
            if self.stopping:
                return {"status": "stopped", "dispatched": [], "count": 0}
            self.last_tick_at_ms = now_ms
        due = self.client.invoke(
            SCHEDULE_RESOURCE,
            "due",
            {
                "profile_id": self.profile_id,
                "now_ms": now_ms,
                "limit": limit,
                "schedule_id": target_schedule_id,
            },
        )
        revision = int(due.get("revision") or 0)
        dispatched = []
        for schedule in due.get("schedules") or []:
            if self.stopping:
                break
            lease_id = str(uuid.uuid4())
            lease_expires = now_ms + 5 * 60 * 1000
            claim_args = {
                "schedule_id": str(schedule.get("id") or ""),
                "expected_revision": revision,
                "lease_id": lease_id,
                "lease_expires_at_ms": lease_expires,
            }
            claim = self._store_action("claim", claim_args)
            revision = int(claim.get("revision") or revision)
            current = claim["schedule"]
            schedule_id = str(current["id"])
            with self.lock:
                self.active[schedule_id] = lease_id
            try:
                result = self.client.invoke(
                    JOB_ACTION,
                    "dispatch",
                    {
                        "action_id": str(current["action_id"]),
                        "payload": dict(current.get("payload") or {}),
                        "idempotency_key": f"{schedule_id}:{lease_id}",
                        "schedule_id": schedule_id,
                        "lease_id": lease_id,
                        "profile_id": self.profile_id,
                    },
                )
                succeeded = _succeeded(result)
                finish_args = {
                    "schedule_id": schedule_id,
                    "expected_revision": revision,
                    "lease_id": lease_id,
                    "error": "" if succeeded else _safe_error(result),
                }
                finish = self._store_action(
                    "complete" if succeeded else "fail",
                    finish_args,
                )
                revision = int(finish.get("revision") or revision)
                dispatched.append(
                    {
                        "schedule_id": schedule_id,
                        "lease_id": lease_id,
                        "result": result,
                        "schedule": finish["schedule"],
                    }
                )
            except Exception as exc:
                failure = self._store_action(
                    "fail",
                    {
                        "schedule_id": schedule_id,
                        "expected_revision": revision,
                        "lease_id": lease_id,
                        "error": str(exc)[:1000],
                    },
                )
                revision = int(failure.get("revision") or revision)
                dispatched.append(
                    {
                        "schedule_id": schedule_id,
                        "lease_id": lease_id,
                        "error": str(exc),
                        "schedule": failure["schedule"],
                    }
                )
                with self.lock:
                    self.last_error = str(exc)
            finally:
                with self.lock:
                    self.active.pop(schedule_id, None)
        return {"status": "ok", "dispatched": dispatched, "count": len(dispatched)}

    def _trigger(self, schedule_id: str) -> dict[str, Any]:
        current = self.client.invoke(
            SCHEDULE_RESOURCE,
            "get",
            {"profile_id": self.profile_id, "schedule_id": schedule_id},
        )
        if not isinstance(current, Mapping):
            raise KeyError("schedule is unknown")
        state = self.client.invoke(
            SCHEDULE_RESOURCE,
            "list",
            {"profile_id": self.profile_id},
        )
        updated = self._store_action(
            "update",
            {
                "schedule_id": schedule_id,
                "expected_revision": int(state.get("revision") or 0),
                "updates": {"next_run_at_ms": _now_ms()},
            },
        )
        return self._tick(
            _now_ms(),
            1,
            target_schedule_id=schedule_id,
        ) | {"triggered": updated["schedule"]}

    def _store_action(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        scope = {
            "service_pack_id": STORE_PACK_ID,
            "operation": f"schedule.{name}",
            "authority": "schedule.manage",
            "caller_id": "scheduler.runtime",
            "caller_pack_id": SERVICE_PACK_ID,
            "caller_function_id": f"scheduler.{name}",
            "profile_id": self.profile_id,
            "workspace_id": "",
            "session_id": "",
            "arguments": dict(arguments),
            "approval_required": False,
        }
        issued = self.client.invoke(AUTHORITY, "authorize", scope)
        if not issued.get("authorized"):
            raise PermissionError(str(issued.get("reason") or "schedule action denied"))
        return self.client.invoke(
            SCHEDULE_ACTION,
            name,
            {
                **dict(arguments),
                "profile_id": self.profile_id,
                "authority_receipt": str(issued.get("receipt") or ""),
                "caller_id": scope["caller_id"],
                "caller_pack_id": SERVICE_PACK_ID,
                "caller_function_id": scope["caller_function_id"],
                "session_id": "",
            },
        )

    def _redeem(
        self,
        payload: Mapping[str, Any],
        name: str,
        arguments: Mapping[str, Any],
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": f"scheduler.{name}",
                "authority": "scheduler.control",
                "caller_id": str(payload.get("caller_id") or ""),
                "caller_pack_id": str(payload.get("caller_pack_id") or ""),
                "caller_function_id": str(payload.get("caller_function_id") or ""),
                "profile_id": self.profile_id,
                "workspace_id": "",
                "session_id": str(payload.get("session_id") or ""),
                "arguments": dict(arguments),
            },
        )
        if not result.get("authorized"):
            raise PermissionError(str(result.get("reason") or "scheduler control denied"))


_RUNTIMES: dict[str, SchedulerRuntime] = {}
_LOCK = threading.Lock()


def create_scheduler_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create scheduler status observation."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "status":
            raise ValueError(f"unknown scheduler resource operation: {name}")
        return _runtime(client, payload).status()

    return operation


def create_scheduler_control(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create scheduler clock control."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return _runtime(client, payload).control(name, payload)

    return operation


def _runtime(client: Any, payload: Mapping[str, Any]) -> SchedulerRuntime:
    profile_id = str(payload.get("profile_id") or "default")
    with _LOCK:
        return _RUNTIMES.setdefault(profile_id, SchedulerRuntime(client, profile_id))


def _succeeded(result: Any) -> bool:
    if not isinstance(result, Mapping):
        return False
    return result.get("status") in {"ok", "completed", "accepted"}


def _safe_error(result: Any) -> str:
    if not isinstance(result, Mapping):
        return "job action returned an invalid result"
    return str(result.get("error") or result.get("message") or "job action failed")[:1000]


def _now_ms() -> int:
    return int(time.time() * 1000)

