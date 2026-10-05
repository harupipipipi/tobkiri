"""Validate and publish declarative tool definitions without generating code."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractClient

MANAGE = "rumi.action.tool.definition.manage.v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_FORBIDDEN = {
    "code",
    "command",
    "entrypoint",
    "handler",
    "module",
    "python",
    "script",
    "source",
}


def create_validate_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a definition-only draft validator."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"validate", "plan"}:
            raise ValueError(f"unknown tool authoring operation: {name}")
        draft = payload.get("definition")
        if not isinstance(draft, Mapping):
            raise ValueError("tool definition draft is required")
        definition = _validate(draft)
        return {
            "valid": True,
            "definition": definition,
            "definition_hash": _hash(definition),
            "executable": False,
            "requires_publish_approval": True,
        }

    return operation


def create_publish_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create revision-guarded publication through the registry owner."""

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != "publish":
            raise ValueError(f"unknown tool authoring publish operation: {name}")
        draft = payload.get("definition")
        if not isinstance(draft, Mapping):
            raise ValueError("tool definition draft is required")
        definition = _validate(draft)
        result = client.invoke(
            MANAGE,
            "save",
            {
                "profile_id": str(payload.get("profile_id") or "default"),
                "expected_revision": int(payload.get("expected_revision") or 0),
                "definition": definition,
            },
        )
        if not isinstance(result, Mapping):
            raise RuntimeError("tool registry returned an invalid result")
        return dict(result)

    return operation


def _validate(value: Mapping[str, Any]) -> dict[str, Any]:
    forbidden = sorted(_FORBIDDEN.intersection({str(key).lower() for key in value}))
    if forbidden:
        raise ValueError("tool definition contains executable source fields")
    tool_id = str(value.get("tool_id") or "").strip()
    authority = str(value.get("authority") or "").strip()
    execution = value.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    kind = str(execution.get("kind") or "").strip()
    contract_id = str(execution.get("contract_id") or "").strip()
    provider_id = str(execution.get("provider_instance_id") or "").strip()
    if not all(
        _IDENTIFIER.fullmatch(item)
        for item in (tool_id, authority, kind, contract_id, provider_id)
    ):
        raise ValueError("tool identity or execution descriptor is invalid")
    input_schema = value.get("input_schema")
    result_schema = value.get("result_schema")
    if not isinstance(input_schema, Mapping) or not isinstance(
        result_schema, Mapping
    ):
        raise ValueError("tool input and result schemas must be objects")
    definition = json.loads(json.dumps(value, ensure_ascii=False))
    definition["tool_id"] = tool_id
    definition["authority"] = authority
    definition["execution"] = {
        "kind": kind,
        "contract_id": contract_id,
        "provider_instance_id": provider_id,
        "namespace": str(execution.get("namespace") or ""),
        "operation": str(execution.get("operation") or ""),
    }
    return definition


def _hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

