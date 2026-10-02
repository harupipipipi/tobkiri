"""Receipt-aware scheduler tool definitions and contract projections."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
SCHEDULE_RESOURCE = "rumi.resource.schedule.v1"
SCHEDULE_ACTION = "rumi.action.schedule.v1"
SCHEDULER_ACTION = "rumi.action.scheduler.v1"
CONTRIBUTION = "rumi.resource.tool.definition.contribution.v1"
LOCAL_OPERATION = "rumi.service.tool.local.operation.v1"
LOCAL_PROVIDER = "tool-adapter.scheduler"
SERVICE_PACK_ID = "rumi_scheduler_tool_adapter_pack"
STORE_PACK_ID = "rumi_schedule_store_pack"
RUNTIME_PACK_ID = "rumi_scheduler_runtime_pack"
EXPECTED_CONSUMER = "rumi_tool_local_executor_pack"

_READ_TOOLS = {"scheduler_list", "scheduler_get"}
_STATE_ACTIONS = {
    "scheduler_create": "create",
    "scheduler_update": "update",
    "scheduler_delete": "delete",
    "scheduler_pause": "pause",
    "scheduler_resume": "resume",
    "scheduler_cancel": "cancel",
}
_RUNTIME_ACTIONS = {
    "scheduler_trigger": "trigger",
    "scheduler_stop": "stop",
}


def create_definition_contribution(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Expose deterministic scheduler definitions to the global registry."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name not in {"list", "catalog"}:
            raise ValueError(f"unknown scheduler tool catalog operation: {name}")
        del payload
        return {"definitions": _definitions(), "aliases": {}}

    return operation


def create_local_operation(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Project selected scheduler tools into global owner contracts."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "invoke":
            raise ValueError(f"unknown scheduler tool operation: {name}")
        if str(payload.get("_contract_consumer_pack_id") or "") != EXPECTED_CONSUMER:
            raise PermissionError("scheduler tool consumer is not authorized")
        tool_id = str(payload.get("tool_id") or "")
        caller_id = str(payload.get("caller_id") or "")
        profile_id = str(payload.get("profile_id") or "")
        arguments = payload.get("arguments")
        if (
            not tool_id
            or not caller_id
            or not profile_id
            or not isinstance(arguments, Mapping)
        ):
            raise ValueError("scheduler tool invocation scope is incomplete")
        arguments = dict(arguments)
        if tool_id in _READ_TOOLS:
            return _read(client, tool_id, profile_id, arguments)
        _require_consumed_authorization(
            payload.get("authorization"),
            tool_id=tool_id,
            caller_id=caller_id,
            profile_id=profile_id,
            arguments=arguments,
        )
        if tool_id in _STATE_ACTIONS:
            return _state_action(
                client,
                _STATE_ACTIONS[tool_id],
                profile_id,
                caller_id,
                tool_id,
                arguments,
            )
        if tool_id in _RUNTIME_ACTIONS:
            return _runtime_action(
                client,
                _RUNTIME_ACTIONS[tool_id],
                profile_id,
                caller_id,
                tool_id,
                arguments,
            )
        raise ValueError("scheduler tool is unknown")

    return operation


def _read(
    client: Any,
    tool_id: str,
    profile_id: str,
    arguments: Mapping[str, Any],
) -> Any:
    if tool_id == "scheduler_list":
        return client.invoke(SCHEDULE_RESOURCE, "list", {"profile_id": profile_id})
    schedule_id = str(arguments.get("schedule_id") or "")
    if not schedule_id:
        raise ValueError("schedule_id is required")
    return client.invoke(
        SCHEDULE_RESOURCE,
        "get",
        {"profile_id": profile_id, "schedule_id": schedule_id},
    )


def _state_action(
    client: Any,
    name: str,
    profile_id: str,
    caller_id: str,
    tool_id: str,
    input_arguments: Mapping[str, Any],
) -> Any:
    snapshot = client.invoke(SCHEDULE_RESOURCE, "list", {"profile_id": profile_id})
    arguments: dict[str, Any] = {
        "schedule_id": str(input_arguments.get("schedule_id") or ""),
        "expected_revision": int(snapshot.get("revision") or 0),
    }
    if name == "create":
        arguments.update(
            {
                "name": str(input_arguments.get("name") or "Scheduled action"),
                "action_id": str(input_arguments.get("action_id") or ""),
                "payload": _object(input_arguments.get("payload")),
                "next_run_at_ms": max(
                    0,
                    int(input_arguments.get("next_run_at_ms") or 0),
                ),
                "interval_ms": max(0, int(input_arguments.get("interval_ms") or 0)),
                "max_attempts": max(
                    1,
                    min(20, int(input_arguments.get("max_attempts") or 3)),
                ),
            }
        )
    elif name == "update":
        arguments["updates"] = _object(input_arguments.get("updates"))
    receipt = _authorize(
        client,
        target_pack_id=STORE_PACK_ID,
        operation=f"schedule.{name}",
        authority="schedule.manage",
        caller_id=caller_id,
        caller_function_id=f"tool.{tool_id}",
        profile_id=profile_id,
        arguments=arguments,
    )
    return client.invoke(
        SCHEDULE_ACTION,
        name,
        {
            **arguments,
            "profile_id": profile_id,
            "authority_receipt": receipt,
            "caller_id": caller_id,
            "caller_pack_id": SERVICE_PACK_ID,
            "caller_function_id": f"tool.{tool_id}",
            "session_id": "",
        },
    )


def _runtime_action(
    client: Any,
    name: str,
    profile_id: str,
    caller_id: str,
    tool_id: str,
    input_arguments: Mapping[str, Any],
) -> Any:
    arguments = (
        {"schedule_id": str(input_arguments.get("schedule_id") or "")}
        if name == "trigger"
        else {"stop": True}
    )
    receipt = _authorize(
        client,
        target_pack_id=RUNTIME_PACK_ID,
        operation=f"scheduler.{name}",
        authority="scheduler.control",
        caller_id=caller_id,
        caller_function_id=f"tool.{tool_id}",
        profile_id=profile_id,
        arguments=arguments,
    )
    return client.invoke(
        SCHEDULER_ACTION,
        name,
        {
            **arguments,
            "profile_id": profile_id,
            "authority_receipt": receipt,
            "caller_id": caller_id,
            "caller_pack_id": SERVICE_PACK_ID,
            "caller_function_id": f"tool.{tool_id}",
            "session_id": "",
        },
    )


def _authorize(
    client: Any,
    *,
    target_pack_id: str,
    operation: str,
    authority: str,
    caller_id: str,
    caller_function_id: str,
    profile_id: str,
    arguments: Mapping[str, Any],
) -> str:
    result = client.invoke(
        AUTHORITY,
        "authorize",
        {
            "service_pack_id": target_pack_id,
            "operation": operation,
            "authority": authority,
            "caller_id": caller_id,
            "caller_pack_id": SERVICE_PACK_ID,
            "caller_function_id": caller_function_id,
            "profile_id": profile_id,
            "workspace_id": "",
            "session_id": "",
            "arguments": dict(arguments),
            "approval_required": False,
        },
    )
    if not result.get("authorized") or not result.get("receipt"):
        raise PermissionError(str(result.get("reason") or "scheduler action denied"))
    return str(result["receipt"])


def _require_consumed_authorization(
    value: Any,
    *,
    tool_id: str,
    caller_id: str,
    profile_id: str,
    arguments: Mapping[str, Any],
) -> None:
    if not isinstance(value, Mapping) or not isinstance(value.get("scope"), Mapping):
        raise PermissionError("scheduler tool approval is required")
    scope = value["scope"]
    args_hash = hashlib.sha256(
        json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    valid = bool(
        value.get("authorized") is True
        and value.get("consumed") is True
        and scope.get("tool_id") == tool_id
        and scope.get("caller_id") == caller_id
        and scope.get("profile_id") == profile_id
        and scope.get("authority") == "service.mutate"
        and scope.get("args_hash") == args_hash
        and scope.get("replay_policy") == "one_shot"
    )
    if not valid:
        raise PermissionError("scheduler tool approval is invalid or already used")


def _definitions() -> list[dict[str, Any]]:
    read_schema = {"type": "object", "additionalProperties": False, "properties": {}}
    schedule_id_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schedule_id"],
        "properties": {"schedule_id": {"type": "string", "minLength": 1}},
    }
    definitions = [
        _definition(
            "scheduler_list",
            "List schedules",
            read_schema,
            "service.invoke",
            "low",
        ),
        _definition(
            "scheduler_get",
            "Get a schedule",
            schedule_id_schema,
            "service.invoke",
            "low",
        ),
        _definition(
            "scheduler_create",
            "Create a schedule",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["schedule_id", "action_id", "next_run_at_ms"],
                "properties": {
                    "schedule_id": {"type": "string", "minLength": 1},
                    "name": {"type": "string"},
                    "action_id": {"type": "string", "minLength": 1},
                    "payload": {"type": "object"},
                    "next_run_at_ms": {"type": "integer", "minimum": 0},
                    "interval_ms": {"type": "integer", "minimum": 0},
                    "max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
                },
            },
            "service.mutate",
            "high",
        ),
        _definition(
            "scheduler_update",
            "Update a schedule",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["schedule_id", "updates"],
                "properties": {
                    "schedule_id": {"type": "string", "minLength": 1},
                    "updates": {"type": "object"},
                },
            },
            "service.mutate",
            "high",
        ),
    ]
    for tool_id, label in (
        ("scheduler_delete", "Delete a schedule"),
        ("scheduler_pause", "Pause a schedule"),
        ("scheduler_resume", "Resume a schedule"),
        ("scheduler_cancel", "Cancel a schedule"),
        ("scheduler_trigger", "Run a schedule now"),
    ):
        definitions.append(
            _definition(tool_id, label, schedule_id_schema, "service.mutate", "high")
        )
    definitions.append(
        _definition(
            "scheduler_stop",
            "Stop scheduler dispatch",
            read_schema,
            "service.mutate",
            "critical",
        )
    )
    return definitions


def _definition(
    tool_id: str,
    description: str,
    schema: Mapping[str, Any],
    authority: str,
    risk: str,
) -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "display_name": description,
        "description": description,
        "input_schema": dict(schema),
        "result_schema": {"type": "object"},
        "execution": {
            "kind": "local",
            "contract_id": LOCAL_OPERATION,
            "provider_instance_id": LOCAL_PROVIDER,
        },
        "authority": authority,
        "risk": risk,
        "policy_tags": ["scheduler", "local-first"],
        "aliases": [],
        "widget": {},
        "source_adapter_id": SERVICE_PACK_ID,
    }


def _object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("object value is required")
    return dict(value)

