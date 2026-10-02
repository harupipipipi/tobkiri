"""Lease-aware clock and global job-action dispatcher."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

AUTHORITY = "rumi.service.host.authorize.v1"
SCHEDULE_RESOURCE = "rumi.resource.schedule.v1"
SCHEDULE_ACTION = "rumi.action.schedule.v1"
JOB_ACTION = "rumi.action.job.v1"
SERVICE_PACK_ID = "rumi_scheduler_runtime_pack"
STORE_PACK_ID = "rumi_schedule_store_pack"


class SchedulerRuntime:
    """Dispatch due schedules without importing any target implementation."""

    def __init__(
        self,
        client: Any,
        profile_id: str,
        *,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
        canonical: bool = False,
    ) -> None:
        self.client = client
        self.profile_id = profile_id
        self.lock = threading.RLock()
        self.stopping = False
        self.active: dict[str, str] = {}
        self.last_tick_at_ms = 0
        self.last_error = ""
        self.clock = clock
        self.canonical = canonical

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

        arguments: dict[str, Any]
        if name == "tick":
            arguments = {
                "now_ms": max(0, int(payload.get("now_ms", self.clock()))),
                "limit": max(1, min(100, int(payload.get("limit") or 20))),
            }
        elif name == "trigger":
            arguments = {"schedule_id": str(payload.get("schedule_id") or "")}
        elif name == "stop":
            arguments = {"stop": True}
        else:
            raise ValueError(f"unknown scheduler control operation: {name}")
        if not self.canonical:
            self._redeem(payload, name, arguments)
        if name == "tick":
            return self._tick(arguments["now_ms"], arguments["limit"])
        if name == "trigger":
            return self._trigger(arguments["schedule_id"])
        with self.lock:
            self.stopping = True
            active = dict(self.active)
        revision = None
        if self.canonical:
            # Process-local active handles disappear after an accepted dispatch.
            # The public schedule owner retains the exact outstanding lease.
            state = self._invoke(SCHEDULE_RESOURCE, "list", {"profile_id": self.profile_id})
            revision = state["revision"]
            active.update(
                {
                    item["id"]: item["lease_id"]
                    for item in state["schedules"]
                    if item["status"] == "running" and item.get("lease_id")
                }
            )
        cancellations = []
        for schedule_id, lease_id in active.items():
            try:
                result = self._invoke(
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
                if self.canonical and result.get("status") == "cancelled":
                    changed = self._store_action(
                        "cancel",
                        {
                            "schedule_id": schedule_id,
                            "expected_revision": revision,
                        },
                    )
                    revision = changed["revision"]
                cancellations.append({"schedule_id": schedule_id, **result})
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
        due = self._invoke(
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
        pending = []
        for schedule in due.get("schedules") or []:
            if self.stopping:
                break
            if schedule.get("status") == "running" and schedule.get("lease_id"):
                previous = self._invoke(
                    JOB_ACTION,
                    "status",
                    {
                        "idempotency_key": f"{schedule['id']}:{schedule['lease_id']}",
                        "profile_id": self.profile_id,
                    },
                )
                if _pending(previous):
                    pending.append({"schedule_id": schedule["id"], "result": previous})
                    continue
                finished = self._store_action(
                    "complete"
                    if _succeeded(previous)
                    else "cancel"
                    if previous.get("status") == "cancelled"
                    else "fail",
                    {
                        "schedule_id": schedule["id"],
                        "expected_revision": revision,
                        "lease_id": schedule["lease_id"],
                        "error": "" if _succeeded(previous) else _safe_error(previous),
                    },
                )
                revision = int(finished["revision"])
                dispatched.append(
                    {
                        "schedule_id": schedule["id"],
                        "schedule": finished["schedule"],
                        "recovered": True,
                    }
                )
                continue
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
                result = self._invoke(
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
                if _pending(result):
                    dispatched.append(
                        {
                            "schedule_id": schedule_id,
                            "lease_id": lease_id,
                            "result": result,
                            "schedule": current,
                        }
                    )
                    continue
                succeeded = _succeeded(result)
                finish_args = {
                    "schedule_id": schedule_id,
                    "expected_revision": revision,
                    "lease_id": lease_id,
                    "error": "" if succeeded else _safe_error(result),
                }
                finish = self._store_action(
                    "complete"
                    if succeeded
                    else "cancel"
                    if result.get("status") == "cancelled"
                    else "fail",
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
                safe_error = f"Job execution failed: {type(exc).__name__}"
                if self.canonical:
                    pending.append(
                        {
                            "schedule_id": schedule_id,
                            "result": {"status": "reconciliation_required", "reason": safe_error},
                        }
                    )
                    continue
                failure = self._store_action(
                    "fail",
                    {
                        "schedule_id": schedule_id,
                        "expected_revision": revision,
                        "lease_id": lease_id,
                        "error": safe_error,
                    },
                )
                revision = int(failure.get("revision") or revision)
                dispatched.append(
                    {
                        "schedule_id": schedule_id,
                        "lease_id": lease_id,
                        "error": safe_error,
                        "schedule": failure["schedule"],
                    }
                )
                with self.lock:
                    self.last_error = safe_error
            finally:
                with self.lock:
                    self.active.pop(schedule_id, None)
        return {
            "status": "ok",
            "dispatched": dispatched,
            "pending": pending,
            "count": len(dispatched),
        }

    def _trigger(self, schedule_id: str) -> dict[str, Any]:
        current = self._invoke(
            SCHEDULE_RESOURCE,
            "get",
            {"profile_id": self.profile_id, "schedule_id": schedule_id},
        )
        if self.canonical and isinstance(current, Mapping):
            current = current.get("schedule")
        if not isinstance(current, Mapping):
            raise KeyError("schedule is unknown")
        state = self._invoke(
            SCHEDULE_RESOURCE,
            "list",
            {"profile_id": self.profile_id},
        )
        updated = self._store_action(
            "update",
            {
                "schedule_id": schedule_id,
                "expected_revision": int(state.get("revision") or 0),
                "updates": {"next_run_at_ms": self.clock()},
            },
        )
        return self._tick(
            self.clock(),
            1,
            target_schedule_id=schedule_id,
        ) | {"triggered": updated["schedule"]}

    def _store_action(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if self.canonical:
            return self._invoke(
                SCHEDULE_ACTION,
                name,
                {**dict(arguments), "profile_id": self.profile_id},
            )
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

    def _invoke(self, contract: str, operation: str, payload: Mapping[str, Any]) -> Any:
        if not self.canonical:
            return self.client.invoke(contract, operation, dict(payload))
        routes = {
            SCHEDULE_RESOURCE: (
                "tobkiri.resource.schedule.v1",
                "rumi_schedule_store_pack.schedule-resource",
            ),
            SCHEDULE_ACTION: (
                "tobkiri.action.schedule.v1",
                "rumi_schedule_store_pack.schedule-action",
            ),
            JOB_ACTION: ("tobkiri.action.job.v1", "rumi_job_action_broker_pack.job-action-broker"),
        }
        target, exact_operation = routes[contract]
        return self.client.invoke(
            target, exact_operation, {**dict(payload), "operation": operation}
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
    return result.get("status") in {"ok", "completed"}


def _pending(result: Any) -> bool:
    """Only confirmed terminal owner outcomes may finish or retry a lease."""
    return not isinstance(result, Mapping) or result.get("status") not in {
        "ok",
        "completed",
        "failed",
        "cancelled",
    }


def _safe_error(result: Any) -> str:
    if not isinstance(result, Mapping):
        return "job action returned an invalid result"
    return str(result.get("error") or result.get("message") or "job action failed")[:1000]


def _now_ms() -> int:
    return int(time.time() * 1000)


_V4_ROUTES = {
    "rumi_scheduler_runtime_pack.scheduler.control": (
        "tobkiri.action.scheduler.v1",
        "rumi_scheduler_runtime_pack.scheduler-control",
    ),
    "rumi_scheduler_runtime_pack.scheduler.status": (
        "tobkiri.resource.scheduler.v1",
        "rumi_scheduler_runtime_pack.scheduler-resource",
    ),
}


def _invoke_v4_owner(
    function_id: str, context: Any, payload: Mapping[str, Any], invocation: Any
) -> Mapping[str, Any]:
    operation = str(payload.get("operation") or "")
    if set(payload) - {"operation", "profile_id", "now_ms", "limit", "schedule_id"}:
        raise PermissionError("scheduler payload is invalid")
    client = invocation.contract_client(
        allowed_contract_ids=frozenset(
            {
                "tobkiri.resource.schedule.v1",
                "tobkiri.action.schedule.v1",
                "tobkiri.action.job.v1",
            }
        ),
        consumer_pack_id=SERVICE_PACK_ID,
        include_credentials=False,
    )
    # Runtime state belongs to an exact activation, never merely a Profile ID.
    key = f"{context.profile_id}:{context.plan_digest}:{context.security_epoch}"
    with _LOCK:
        runtime = _RUNTIMES.setdefault(
            key, SchedulerRuntime(client, context.profile_id, canonical=True)
        )
        runtime.client = client
    if function_id.endswith(".status"):
        if operation != "status":
            raise ValueError("scheduler resource operation is invalid")
        return runtime.status()
    return runtime.control(operation, payload)


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
