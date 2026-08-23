"""Legacy Company contracts backed only by canonical transactional Team state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id

from .team_store import TeamStateConflict, TransactionalTeamStore

AUTHORITY = "rumi.service.host.authorize.v1"
SERVICE_PACK_ID = "rumi_company_state_store_pack"
CompanyStateConflict = TeamStateConflict


class CompanyStateStore(TransactionalTeamStore):
    """Compatibility adapter over the canonical transactional Team store."""

    def __init__(self, profile_id: str, *, root: Path | None = None) -> None:
        validated = validate_profile_id(profile_id)
        owner_root = (
            Path(root or USER_DATA_DIR) / "packs" / SERVICE_PACK_ID / "profiles" / validated
        )
        super().__init__(validated, owner_root)


def create_company_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create legacy Company reads over canonical Team projections."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = CompanyStateStore(_profile(payload))
        if name == "list":
            return store.snapshot(
                limit=max(1, min(1_000, int(payload.get("limit") or 1_000))),
                cursor=str(payload.get("cursor") or ""),
            )
        if name == "get":
            return store.get(str(payload.get("company_id") or ""))
        if name == "timeline":
            return store.list_timeline(
                str(payload.get("company_id") or ""),
                kind=str(payload.get("kind") or "message"),
                limit=max(1, min(1_000, int(payload.get("limit") or 100))),
                after_sequence=max(0, int(payload.get("after_sequence") or 0)),
            )
        raise ValueError(f"unknown Company resource operation: {name}")

    return operation


def create_company_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated legacy writes into canonical Team state."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        arguments = _arguments(name, payload)
        _redeem(client, payload, name, arguments)
        return CompanyStateStore(_profile(payload)).apply(name, arguments)

    return operation


def _arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "company.create",
        "company.update",
        "company.delete",
        "agent.upsert",
        "agent.delete",
        "role.upsert",
        "role.delete",
        "member.upsert",
        "member.delete",
        "channel.upsert",
        "channel.delete",
        "route.upsert",
        "route.delete",
        "task.upsert",
        "task.delete",
        "task.transition",
        "inbound.append",
        "message.append",
        "migration.operations.import",
    }
    if name not in allowed:
        raise ValueError(f"unknown Company action: {name}")
    arguments: dict[str, Any] = {
        "company_id": str(payload.get("company_id") or ""),
        "expected_revision": max(0, int(payload.get("expected_revision") or 0)),
    }
    if "expected_entity_revision" in payload:
        arguments["expected_entity_revision"] = max(0, int(payload["expected_entity_revision"]))
    if name == "company.create":
        arguments.update(
            {
                "name": str(payload.get("name") or "Company"),
                "settings": dict(_mapping(payload.get("settings"))),
                "description": str(payload.get("description") or ""),
                "metadata": dict(_mapping(payload.get("metadata"))),
                "conversation_group_id": str(payload.get("conversation_group_id") or ""),
            }
        )
    elif name == "migration.operations.import":
        arguments["legacy_state"] = _legacy_operations_state(payload.get("legacy_state"))
    elif name == "company.update":
        updates = dict(_mapping(payload.get("updates")))
        allowed_updates = {
            "name",
            "status",
            "settings",
            "description",
            "metadata",
            "conversation_group_id",
        }
        if set(updates) - allowed_updates:
            raise ValueError("Company update contains unsupported fields")
        for key in {"settings", "metadata"} & set(updates):
            updates[key] = dict(_mapping(updates[key]))
        arguments["updates"] = updates
        arguments["replace_settings"] = bool(payload.get("replace_settings"))
    elif name == "agent.upsert":
        arguments["role"] = dict(_mapping(payload.get("role")))
        arguments["member"] = dict(_mapping(payload.get("member")))
    elif name == "agent.delete":
        arguments["record_id"] = str(payload.get("agent_id") or "")
    elif name.endswith(".upsert"):
        record = dict(_mapping(payload.get("record")))
        arguments["record_id"] = str(record.get("id") or payload.get("record_id") or "")
        arguments["record"] = record
    elif name == "task.delete":
        arguments["record_id"] = str(payload.get("task_id") or "")
    elif name.endswith(".delete") and name != "company.delete":
        arguments["record_id"] = str(payload.get("record_id") or "")
    elif name == "task.transition":
        arguments.update(
            {
                "record_id": str(payload.get("task_id") or ""),
                "status": str(payload.get("status") or ""),
                "details": dict(_mapping(payload.get("details"))),
            }
        )
    elif name in {"inbound.append", "message.append"}:
        arguments["record"] = dict(_mapping(payload.get("record")))
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
            "operation": f"company.state.{name}",
            "authority": "company.state.manage",
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
        raise PermissionError(str(result.get("reason") or "Company state denied"))


def _legacy_operations_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("legacy Operations Company state is required")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > 128 * 1024:
        raise ValueError("legacy Operations Company state is too large")
    normalized = {
        "org_id": _legacy_text(value.get("org_id"), 255),
        "conversation_id": _legacy_text(value.get("conversation_id"), 255),
        "conversation_group_id": _legacy_text(value.get("conversation_group_id"), 255),
        "schedule_ids": _legacy_schedule_ids(value.get("schedule_ids")),
    }
    if not any(normalized.values()):
        raise ValueError("legacy Operations Company state is empty")
    return normalized


def _legacy_schedule_ids(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        _legacy_text(key, 100): _legacy_text(item, 255)
        for key, item in value.items()
        if _legacy_text(key, 100) and _legacy_text(item, 255)
    }


def _legacy_text(value: Any, limit: int) -> str:
    return str(value or "").strip().replace("\x00", "")[:limit]


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")
