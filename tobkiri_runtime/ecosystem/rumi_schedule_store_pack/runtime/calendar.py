"""Captured Calendar facade over the canonical schedule and scheduler owners."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import json
import re
import time
from typing import Any, Callable, Mapping
import uuid

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.rumi_schedule_store_pack.runtime.recurrence import (
    normalize_recurrence,
    next_cron_ms,
)

PACK = "rumi_schedule_store_pack"
RESOURCE = "tobkiri.resource.schedule.v1"
RESOURCE_OPERATION = "rumi_schedule_store_pack.schedule-resource"
ACTION = "tobkiri.action.schedule.v1"
ACTION_OPERATION = "rumi_schedule_store_pack.schedule-action"
CONTROL = "tobkiri.action.scheduler.v1"
CONTROL_OPERATION = "rumi_scheduler_runtime_pack.scheduler-control"
CLOCK = "tobkiri.action.scheduler.clock.v1"
CLOCK_OPERATION = "rumi_scheduler_runtime_pack.scheduler-clock-control"
READ_FUNCTION = "rumi_schedule_store_pack.calendar.resource"
WRITE_FUNCTION = "rumi_schedule_store_pack.calendar.manage"
READ_CONTRACT = "tobkiri.resource.calendar.schedule.v1"
WRITE_CONTRACT = "tobkiri.action.calendar.schedule.v1"
READ_OPERATION = "rumi_schedule_store_pack.calendar-read"
WRITE_OPERATION = "rumi_schedule_store_pack.calendar-manage"


def _iso_ms(value: Any) -> int:
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("Calendar run_at is invalid")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Compatibility: legacy naive run_at means UTC. Calendar UI sends offset/Z.
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    result = int(instant.timestamp() * 1000)
    if result < 0:
        raise ValueError("Calendar run_at precedes the supported epoch")
    return result


def _timing(kind: Any, config: Any, now_ms: int) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise ValueError("Calendar schedule_config must be an object")
    if kind == "once":
        if set(config) != {"run_at"}:
            raise ValueError("Calendar once configuration is invalid")
        return {
            "next_run_at_ms": _iso_ms(config["run_at"]),
            "interval_ms": 0,
            "recurrence": None,
        }
    if kind == "interval":
        if set(config) - {"value", "unit"} or "value" not in config:
            raise ValueError("Calendar interval configuration is invalid")
        value, unit = config["value"], config.get("unit", "minutes")
        if (
            type(value) not in {int, float}
            or not math.isfinite(value)
            or value <= 0
            or unit not in {"seconds", "minutes", "hours"}
        ):
            raise ValueError("Calendar interval is invalid")
        interval = int(
            value * {"seconds": 1000, "minutes": 60000, "hours": 3600000}[unit]
        )
        if not 1000 <= interval <= 366 * 86400000:
            raise ValueError("Calendar interval exceeds its bounds")
        return {
            "next_run_at_ms": now_ms + interval,
            "interval_ms": interval,
            "recurrence": None,
        }
    if kind == "cron":
        if set(config) - {"expression", "timezone"} or "expression" not in config:
            raise ValueError("Calendar cron configuration is invalid")
        recurrence = normalize_recurrence(
            {
                "kind": "cron",
                "expression": config["expression"],
                "timezone": config.get("timezone", "UTC"),
            }
        )
        return {
            "next_run_at_ms": next_cron_ms(recurrence, now_ms),
            "interval_ms": 0,
            "recurrence": recurrence,
        }
    raise ValueError("Calendar schedule_type must be once, interval or cron")


def _normalize_task(
    task: Any, profile_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    from ecosystem.rumi_turn_runtime_pack.runtime.scheduled_saved import (
        normalize_scheduled_saved_task,
    )

    if not isinstance(task, Mapping) or set(task) - {
        "message",
        "profile_id",
        "conversation_id",
        "chat_references",
        "tool_selection",
        "model",
        "metadata",
        "timeout",
        "action_approval_mode",
        "workspace_id",
    }:
        raise ValueError("Calendar task fields are invalid")
    # Normal Saved currently admits 60 KiB, below the adapter's 64 KiB cap.
    # Validate the lower downstream limit before any schedule can be saved.
    message = task.get("message")
    if (not isinstance(message, str) or not message.strip()
            or len(message.encode("utf-8")) > 60 * 1024):
        raise ValueError("Calendar task message exceeds the Saved UTF-8 bound")
    if len(json.dumps(dict(task), ensure_ascii=False).encode("utf-8")) > 100_000:
        raise ValueError("Calendar task exceeds its bounds")
    if "metadata" in task and not isinstance(task["metadata"], Mapping):
        raise ValueError("Calendar task metadata must be an inert object")
    if "timeout" in task and (
        type(task["timeout"]) is not int or task["timeout"] != 300
    ):
        raise ValueError("Only the Saved default timeout of 300 seconds is supported")
    canonical = {
        key: task[key]
        for key in ("message", "conversation_id", "chat_references", "tool_selection")
        if key in task
    }
    if "profile_id" in task and task["profile_id"] != profile_id:
        raise PermissionError("Calendar task Profile cannot be selected")
    canonical["profile_id"] = profile_id
    # These legacy display hints never enter the saved execution payload.
    presentation = {key: task[key] for key in ("metadata", "timeout") if key in task}
    if canonical.get("conversation_id") is None:
        from tobkiri_protocol.saved_conversation import (
            MAX_SAVED_TEXT_BYTES,
            saved_guidance_context,
        )

        message = canonical.get("message")
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message.encode("utf-8")) > MAX_SAVED_TEXT_BYTES
        ):
            raise ValueError("Calendar task message is invalid")
        normalized = {
            "message": message,
            "profile_id": profile_id,
            "conversation_id": None,
            **saved_guidance_context(canonical, profile_id=profile_id),
        }
    else:
        normalized = normalize_scheduled_saved_task(canonical, profile_id=profile_id)
    if "workspace_id" in task:
        workspace_id = task["workspace_id"]
        if (not isinstance(workspace_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", workspace_id)
                is None):
            raise ValueError("Calendar workspace preference is invalid")
        # Registered identity preference only; execution validates its fresh
        # owner association. No path, grant or authority is persisted here.
        normalized["workspace_id"] = workspace_id
    if "action_approval_mode" in task:
        preference = task["action_approval_mode"]
        if not isinstance(preference, str) or preference not in {"ask", "agent", "full"}:
            raise ValueError("Calendar action approval preference is invalid")
        # Preference data is not an approval receipt or a capability grant.
        normalized["action_approval_mode"] = preference
    if "model" in task:
        if (
            not isinstance(task["model"], str)
            or not task["model"].strip()
            or len(task["model"]) > 256
        ):
            raise ValueError("Calendar configured model is invalid")
        normalized["model"] = task["model"]
    return normalized, presentation


def _project(record: Mapping[str, Any]) -> dict[str, Any]:
    presentation = record.get("presentation") or {}
    status = record["status"]
    return {
        "id": record["id"],
        "name": record["name"],
        "description": presentation.get("description", ""),
        "type": presentation.get("schedule_type", "once"),
        "config": presentation.get("schedule_config", {}),
        "task": {**presentation.get("task", {}), **dict(record["payload"])},
        "status": "active" if status == "scheduled" else status,
        "next_run_at": record["next_run_at_ms"] / 1000,
        "next_execution_at": datetime.fromtimestamp(
            record["next_run_at_ms"] / 1000, timezone.utc
        ).isoformat()
        if record["status"] not in {"completed", "failed", "cancelled"}
        else None,
        "execution_count": record.get("execution_count", 0),
        "last_executed_at": datetime.fromtimestamp(
            record["history"][-1]["started_at_ms"] / 1000, timezone.utc
        ).isoformat()
        if record.get("history")
        else None,
        "next_run_at_ms": record["next_run_at_ms"],
        "created_at": datetime.fromtimestamp(
            record["created_at_ms"] / 1000, timezone.utc
        ).isoformat(),
        "updated_at": datetime.fromtimestamp(
            record["updated_at_ms"] / 1000, timezone.utc
        ).isoformat(),
        "last_error": record["last_error"],
    }


class CalendarHostFactoryV4:
    """Expose finite authenticated Calendar methods without retaining a client."""

    def __init__(
        self, function_id: str, *, clock: Callable[[], int] | None = None
    ) -> None:
        self.function_id = function_id
        self._clock = clock or (lambda: int(time.time() * 1000))

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture only exact Function/Profile bindings; all clients are per-call."""
        expected = {
            READ_FUNCTION: (READ_CONTRACT, READ_OPERATION),
            WRITE_FUNCTION: (WRITE_CONTRACT, WRITE_OPERATION),
        }[self.function_id]
        if not context.profile_id or len(context.provider_bindings) != 1:
            raise PermissionError("Calendar capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != self.function_id
            or (operation.contract_id, operation.operation_id) != expected
            or operation.contract_version != "1.0.0"
        ):
            raise PermissionError("Calendar binding is invalid")
        domain_id = context.domain_ids.get((*expected, binding.principal_ref.value))
        if domain_id is None:
            raise PermissionError("Calendar domain is unavailable")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            if (
                operation_id != expected[1]
                or payload.get("profile_id") != context.profile_id
                or not invocation.presentation_owner_principal_id
                or not invocation.presentation_owner_session_id
            ):
                raise PermissionError("Calendar caller Profile/owner is invalid")
            name = payload.get("operation")
            allowed = (
                {"list", "get", "history"}
                if self.function_id == READ_FUNCTION
                else {"create", "update", "pause", "resume", "delete", "trigger"}
            )
            fields = {
                "list": {"status"},
                "get": {"schedule_id"},
                "history": {"schedule_id", "limit", "offset"},
                "create": {
                    "name",
                    "description",
                    "schedule_type",
                    "schedule_config",
                    "task",
                },
                "update": {
                    "schedule_id",
                    "name",
                    "description",
                    "schedule_type",
                    "schedule_config",
                    "task",
                },
                "pause": {"schedule_id"},
                "resume": {"schedule_id"},
                "delete": {"schedule_id"},
                "trigger": {"schedule_id"},
            }
            if (
                not isinstance(name, str)
                or name not in allowed
                or set(payload)
                - fields[name]
                - {"operation", "profile_id", "_session_id"}
            ):
                raise PermissionError("Calendar operation fields are invalid")
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(
                    {RESOURCE}
                    if self.function_id == READ_FUNCTION
                    else {RESOURCE, ACTION, CONTROL, CLOCK}
                ),
                consumer_pack_id=PACK,
                include_credentials=False,
            )

            def call(
                contract: str, operation: str, values: Mapping[str, Any]
            ) -> Mapping[str, Any]:
                invocation.assert_current()
                response = client.invoke(
                    contract, operation, {"profile_id": context.profile_id, **values}
                )
                invocation.assert_current()
                if not isinstance(response, Mapping):
                    raise ValueError("Calendar owner response is invalid")
                return response

            state = call(RESOURCE, RESOURCE_OPERATION, {"operation": "list"})
            records = [
                item for item in state["schedules"] if item["action_id"] == "chat.saved"
            ]
            current = next(
                (item for item in records if item["id"] == payload.get("schedule_id")),
                None,
            )
            if name == "list":
                items = [_project(item) for item in records]
                if payload.get("status") is not None:
                    items = [
                        item for item in items if item["status"] == payload["status"]
                    ]
                return {
                    "status": "ok",
                    "data": {"schedules": items, "total": len(items)},
                }
            if name != "create" and current is None:
                raise KeyError("Calendar schedule is unavailable")
            if name == "history":
                limit, offset = payload.get("limit", 50), payload.get("offset", 0)
                if (
                    type(limit) is not int
                    or not 1 <= limit <= 200
                    or type(offset) is not int
                    or offset < 0
                ):
                    raise ValueError("Calendar history bounds are invalid")
                history = list(reversed(current.get("history") or []))
                return {
                    "status": "ok",
                    "data": {
                        "schedule_id": current["id"],
                        "entries": history[offset : offset + limit],
                        "total": len(history),
                        "limit": limit,
                        "offset": offset,
                    },
                }
            if name == "get":
                return {"status": "ok", "data": _project(current)}
            if name == "trigger":
                return {
                    "status": "ok",
                    "data": call(
                        CONTROL,
                        CONTROL_OPERATION,
                        {"operation": "trigger", "schedule_id": current["id"]},
                    ),
                }
            action = {
                "operation": name,
                "expected_revision": state["revision"],
                "schedule_id": current["id"] if current else f"sched_{uuid.uuid4()}",
            }
            if name in {"create", "update"}:
                previous = current.get("presentation", {}) if current else {}
                kind = payload.get("schedule_type", previous.get("schedule_type"))
                config = payload.get("schedule_config", previous.get("schedule_config"))
                raw_task = payload.get("task")
                if name == "update" and raw_task is not None:
                    raw_task = {
                        **current["payload"],
                        **previous.get("task", {}),
                        **dict(raw_task),
                    }
                elif raw_task is None and current:
                    raw_task = {**current["payload"], **previous.get("task", {})}
                task, task_presentation = _normalize_task(raw_task, context.profile_id)
                description = payload.get(
                    "description", previous.get("description", "")
                )
                title = payload.get(
                    "name", current["name"] if current else "Tobkiri Calendar"
                )
                if (
                    not isinstance(description, str)
                    or len(description) > 2000
                    or not isinstance(title, str)
                    or not 1 <= len(title) <= 120
                ):
                    raise ValueError("Calendar name/description exceeds its bounds")
                presentation = {
                    "description": description,
                    "schedule_type": kind,
                    "schedule_config": dict(config),
                    "task": task_presentation,
                }
                updates = {
                    "name": title,
                    "action_id": "chat.saved",
                    "payload": task,
                    "presentation": presentation,
                }
                if (
                    name == "create"
                    or "schedule_config" in payload
                    or "schedule_type" in payload
                ):
                    updates.update(_timing(kind, config, self._clock()))
                if name == "create":
                    action.update(updates, max_attempts=3)
                else:
                    action["updates"] = updates
            # Validate the complete finite body before touching wake intent. A
            # schedule must not become active without an admitted finite driver.
            if name in {"create", "resume"} or (
                name == "update"
                and current.get("enabled")
                and current.get("status") in {"scheduled", "running"}
            ):
                status = call(CLOCK, CLOCK_OPERATION, {"operation": "status"})
                if not status.get("available"):
                    raise PermissionError("Calendar wake driver is unavailable")
                deadline = status.get("expires_at_ms")
                if (
                    not status.get("armed")
                    or type(deadline) is not int
                    or deadline - self._clock() <= 120000
                ):
                    armed = call(
                        CLOCK,
                        CLOCK_OPERATION,
                        {
                            "operation": "renew" if status.get("armed") else "arm",
                            "duration_ms": 86400000,
                        },
                    )
                    if not armed.get("armed") or not armed.get("available"):
                        raise PermissionError("Calendar wake registration failed")
            changed = call(ACTION, ACTION_OPERATION, action)
            return {
                "status": "ok",
                "data": {"schedule_id": action["schedule_id"], "deleted": True}
                if name == "delete"
                else _project(changed["schedule"]),
            }

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=expected[0],
                    contract_version=operation.contract_version,
                    operation_id=expected[1],
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {
    function: CalendarHostFactoryV4(function)
    for function in (READ_FUNCTION, WRITE_FUNCTION)
}
