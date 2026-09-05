"""Project legacy tool definitions into global schemas without owning services."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from ecosystem.defaultspack.domain.tool.executor import ToolExecutor
from ecosystem.defaultspack.domain.tool.registry import ToolRegistry
from ecosystem.defaultspack.domain.tool_policy.internal_context import (
    mark_tool_server_approval_context,
)

LOCAL_OPERATION = "rumi.service.tool.local.operation.v1"
LOCAL_PROVIDER = "tool-adapter.defaultspack-compat"
_EXPECTED_CONSUMER = "rumi_tool_local_executor_pack"
_AUTHORITIES = {
    "file.read",
    "file.write",
    "shell.inspect",
    "shell.execute",
    "git.read",
    "git.write",
    "git.publish",
    "browser.observe",
    "browser.control",
    "desktop.observe",
    "desktop.control",
    "clipboard.read",
    "clipboard.write",
}


def create_source_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a deterministic, secret-free legacy migration source."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"snapshot", "list"}:
            raise ValueError(f"unknown tool projection operation: {name}")
        del payload
        definitions: list[dict[str, Any]] = []
        aliases: dict[str, str] = {}
        for tool in sorted(
            ToolRegistry().list_tools(), key=lambda item: str(item.get("tool_id"))
        ):
            if not isinstance(tool, Mapping):
                continue
            definition = _definition(tool)
            definitions.append(definition)
            for alias in definition.pop("compatibility_aliases"):
                aliases.setdefault(alias, definition["tool_id"])
        source = {"definitions": definitions, "aliases": dict(sorted(aliases.items()))}
        source_hash = hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return {**source, "source_hash": source_hash}

    return operation


def create_local_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the finite local execution projection over legacy ToolExecutor."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != "invoke":
            raise ValueError(f"unknown default tool adapter operation: {name}")
        consumer = str(payload.get("_contract_consumer_pack_id") or "")
        if consumer != _EXPECTED_CONSUMER:
            raise PermissionError("default tool adapter consumer is not authorized")
        tool_id = str(payload.get("tool_id") or "").strip()
        caller_id = str(payload.get("caller_id") or "").strip()
        profile_id = str(payload.get("profile_id") or "").strip()
        arguments = payload.get("arguments")
        if not tool_id or not caller_id or not profile_id:
            raise ValueError("default tool invocation scope is incomplete")
        if not isinstance(arguments, Mapping):
            raise ValueError("default tool arguments must be an object")
        legacy_context = {
            "caller_id": caller_id,
            "profile_id": profile_id,
            "tool_call_id": str(payload.get("tool_call_id") or ""),
            "owner_pack": "rumi_default_tool_projection_pack",
        }
        if _approved_receipt(
            payload.get("authorization"),
            tool_id=tool_id,
            caller_id=caller_id,
            profile_id=profile_id,
            arguments=arguments,
        ):
            mark_tool_server_approval_context(legacy_context)
        result = ToolExecutor().execute(tool_id, dict(arguments), legacy_context)
        if not isinstance(result, Mapping):
            raise RuntimeError("legacy tool executor returned an invalid result")
        return dict(result)

    return operation


def _definition(tool: Mapping[str, Any]) -> dict[str, Any]:
    tool_id = str(tool.get("tool_id") or tool.get("name") or "").strip()
    schema = tool.get("schema")
    schema = schema if isinstance(schema, Mapping) else {}
    parameters = schema.get("parameters")
    input_schema = parameters if isinstance(parameters, Mapping) else schema
    metadata = tool.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    aliases = tool.get("aliases") or metadata.get("aliases") or []
    aliases = aliases if isinstance(aliases, list) else []
    return {
        "tool_id": tool_id,
        "display_name": str(tool.get("display_name") or tool_id),
        "description": str(tool.get("description") or tool.get("summary") or ""),
        "input_schema": dict(input_schema),
        "result_schema": {},
        "execution": {
            "kind": "local",
            "contract_id": LOCAL_OPERATION,
            "provider_instance_id": LOCAL_PROVIDER,
        },
        "authority": _authority(tool, metadata),
        "risk": str(tool.get("risk") or "unknown"),
        "policy_tags": [str(item) for item in tool.get("tags") or []],
        "aliases": [str(item) for item in aliases if str(item).strip()],
        "compatibility_aliases": [
            str(item) for item in aliases if str(item).strip()
        ],
        "widget": _widget(tool, metadata),
        "source_adapter_id": "defaultspack.tool.compatibility",
    }


def _authority(tool: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    grants = tool.get("capability_grants") or metadata.get("capability_grants") or []
    for grant in grants if isinstance(grants, list) else []:
        normalized = str(grant).strip()
        if normalized in _AUTHORITIES:
            return normalized
    if bool(tool.get("requires_approval") or metadata.get("requires_approval")):
        return "service.mutate"
    return "service.invoke"


def _widget(tool: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
    for value in (tool.get("widget"), tool.get("ui"), metadata.get("widget")):
        if isinstance(value, Mapping):
            return json.loads(json.dumps(value, ensure_ascii=False))
    return {}


def _approved_receipt(
    value: Any,
    *,
    tool_id: str,
    caller_id: str,
    profile_id: str,
    arguments: Mapping[str, Any],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    scope = value.get("scope")
    if not isinstance(scope, Mapping):
        return False
    args_hash = hashlib.sha256(
        json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return bool(
        value.get("authorized") is True
        and value.get("consumed") is True
        and scope.get("tool_id") == tool_id
        and scope.get("caller_id") == caller_id
        and scope.get("profile_id") == profile_id
        and scope.get("args_hash") == args_hash
        and scope.get("replay_policy") == "one_shot"
    )

