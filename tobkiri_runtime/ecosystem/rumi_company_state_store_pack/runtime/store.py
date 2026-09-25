"""Profile-scoped Company organizations, tasks, channels, and routing state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

AUTHORITY = "rumi.service.host.authorize.v1"
SERVICE_PACK_ID = "rumi_company_state_store_pack"
VERSION = "rumi.company-state.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TASK_TRANSITIONS = {
    "queued": {"assigned", "cancelled", "blocked"},
    "assigned": {"running", "cancelled", "blocked"},
    "running": {"waiting", "completed", "failed", "cancelled", "blocked"},
    "waiting": {"running", "cancelled", "blocked"},
    "blocked": {"queued", "assigned", "cancelled"},
    "failed": {"queued", "cancelled"},
}


class CompanyStateConflict(RuntimeError):
    """Raised for stale state or invalid Company lifecycle transitions."""


class CompanyStateStore:
    """Own canonical Company data without coordination or transport logic."""

    def __init__(self, profile_id: str, *, root: Path | None = None) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(root or USER_DATA_DIR) / "packs" / SERVICE_PACK_ID / "profiles" / self.profile_id
        )
        self.path = self.root / "companies.json"
        self.lock_root = self.root / "locks"

    def snapshot(self) -> dict[str, Any]:
        """Return all Company records in deterministic order."""

        state = self._read()
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "companies": [state["companies"][key] for key in sorted(state["companies"])],
        }

    def get(self, company_id: str) -> dict[str, Any] | None:
        """Return one exact Company record."""

        value = self._read()["companies"].get(_identifier(company_id))
        return _copy(value) if isinstance(value, Mapping) else None

    def apply(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one exact revision-bound Company mutation."""

        with NamedLock(self.lock_root, "companies"):
            state = self._read()
            _assert_revision(state, int(arguments["expected_revision"]))
            result = self._transition(state, name, arguments)
            if result.get("deduplicated"):
                return {**result, "revision": state["revision"]}
            state["revision"] += 1
            self._write(state)
            return {**result, "revision": state["revision"]}

    def _transition(
        self,
        state: dict[str, Any],
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        company_id = _identifier(arguments["company_id"])
        now_ms = _now_ms()
        if name == "company.create":
            if company_id in state["companies"]:
                raise ValueError("Company already exists")
            company = {
                "id": company_id,
                "name": str(arguments["name"])[:200],
                "description": str(arguments["description"])[:4_000],
                "status": "active",
                "settings": _copy(arguments["settings"]),
                "metadata": _copy(arguments["metadata"]),
                "conversation_group_id": str(arguments["conversation_group_id"])[:255],
                "roles": {},
                "members": {},
                "channels": {},
                "tasks": {},
                "routes": {},
                "inbound": [],
                "messages": [],
                "created_at_ms": now_ms,
                "updated_at_ms": now_ms,
            }
            state["companies"][company_id] = company
            return {"company": _copy(company)}
        if name == "migration.operations.import":
            return self._import_operations_company(state, arguments, now_ms)
        company = state["companies"].get(company_id)
        if company is None:
            raise KeyError("Company is unknown")
        if name == "company.delete":
            if any(
                task["status"] in {"assigned", "running", "waiting"}
                for task in company["tasks"].values()
            ):
                raise CompanyStateConflict("Company has active tasks")
            del state["companies"][company_id]
            return {"deleted_company_id": company_id}
        if name == "company.update":
            original = _copy(company)
            updates = arguments["updates"]
            if "name" in updates:
                company["name"] = str(updates["name"])[:200]
            if "status" in updates:
                company["status"] = str(updates["status"])
            if "settings" in updates:
                next_settings = _copy(updates["settings"])
                if arguments.get("replace_settings"):
                    company["settings"] = next_settings
                else:
                    company["settings"] = {
                        **company["settings"],
                        **next_settings,
                    }
            if "description" in updates:
                company["description"] = str(updates["description"])[:4_000]
            if "metadata" in updates:
                company["metadata"] = {
                    **_copy(_mapping(company.get("metadata"))),
                    **_copy(updates["metadata"]),
                }
            if "conversation_group_id" in updates:
                company["conversation_group_id"] = str(updates["conversation_group_id"])[:255]
            if company == original:
                return {"company": _copy(company), "deduplicated": True}
            company["updated_at_ms"] = now_ms
            return {"company": _copy(company)}
        if name == "agent.upsert":
            supplied_member = dict(arguments["member"])
            member_id = _identifier(supplied_member.get("id"))
            operation_id = _record_operation_id(supplied_member)
            current_member = company["members"].get(member_id)
            current_role = (
                company["roles"].get(str(current_member.get("role_id") or ""))
                if isinstance(current_member, Mapping)
                else None
            )
            if (
                operation_id
                and isinstance(current_member, Mapping)
                and _record_operation_id(current_member) == operation_id
                and isinstance(current_role, Mapping)
            ):
                return {
                    "agent": _copy(current_member),
                    "role": _copy(current_role),
                    "deduplicated": True,
                }
            role = dict(arguments["role"])
            role_id = _identifier(role.get("id"))
            self._named_record(
                company,
                "roles",
                "role.upsert",
                {"record_id": role_id, "record": role},
                now_ms,
            )
            member = dict(arguments["member"])
            member_id = _identifier(member.get("id"))
            result = self._member(
                company,
                "member.upsert",
                {"record_id": member_id, "record": member},
                now_ms,
            )
            return {
                "agent": _copy(result["member"]),
                "role": _copy(company["roles"][role_id]),
            }
        if name == "agent.delete":
            member_id = _identifier(arguments["record_id"])
            self._member(
                company,
                "member.delete",
                {"record_id": member_id},
                now_ms,
            )
            return {"deleted_agent_id": member_id}
        if name.startswith("role."):
            return self._named_record(company, "roles", name, arguments, now_ms)
        if name.startswith("member."):
            return self._member(company, name, arguments, now_ms)
        if name.startswith("channel."):
            return self._named_record(company, "channels", name, arguments, now_ms)
        if name.startswith("route."):
            return self._named_record(company, "routes", name, arguments, now_ms)
        if name == "task.upsert":
            task = _task(arguments["record"], company)
            current = company["tasks"].get(task["id"])
            if (
                current
                and _record_operation_id(task)
                and _record_operation_id(current) == _record_operation_id(task)
                and current.get("idempotency_key") == task["idempotency_key"]
            ):
                return {"task": _copy(current), "deduplicated": True}
            task["created_at_ms"] = current["created_at_ms"] if current else now_ms
            task["updated_at_ms"] = now_ms
            company["tasks"][task["id"]] = task
            company["updated_at_ms"] = now_ms
            return {"task": _copy(task)}
        if name == "task.delete":
            task_id = _identifier(arguments["record_id"])
            if company["tasks"].pop(task_id, None) is None:
                raise KeyError("Company task is unknown")
            company["updated_at_ms"] = now_ms
            return {"deleted_task_id": task_id}
        if name == "task.transition":
            task_id = _identifier(arguments["record_id"])
            task = company["tasks"].get(task_id)
            if task is None:
                raise KeyError("Company task is unknown")
            target = str(arguments["status"])
            if target not in _TASK_TRANSITIONS.get(task["status"], set()):
                raise CompanyStateConflict("Company task transition is invalid")
            task["status"] = target
            details = dict(arguments["details"])
            if "assignee_member_id" in details:
                assignee = str(details["assignee_member_id"])
                if assignee and assignee not in company["members"]:
                    raise KeyError("Company task assignee is unknown")
                task["assignee_member_id"] = assignee
            if target == "completed":
                task["result_reference"] = details.get("result_reference")
            if target in {"failed", "blocked"}:
                task["error"] = str(details.get("error") or "")[:1000]
            task["updated_at_ms"] = now_ms
            company["updated_at_ms"] = now_ms
            return {"task": _copy(task)}
        if name in {"inbound.append", "message.append"}:
            key = "inbound" if name.startswith("inbound") else "messages"
            record = _timeline_record(arguments["record"])
            result_key = key[:-1] if key.endswith("s") else key
            existing = next(
                (item for item in company[key] if item["id"] == record["id"]),
                None,
            )
            if existing is not None:
                return {result_key: _copy(existing), "deduplicated": True}
            record["created_at_ms"] = now_ms
            company[key].append(record)
            if len(company[key]) > 10_000:
                del company[key][:-10_000]
            company["updated_at_ms"] = now_ms
            return {result_key: _copy(record), "deduplicated": False}
        raise ValueError(f"unknown Company action: {name}")

    def _import_operations_company(
        self,
        state: dict[str, Any],
        arguments: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        """Import one redacted legacy Operations Company snapshot exactly once."""

        legacy = _legacy_operations_state(arguments["legacy_state"])
        source_hash = _canonical_hash(legacy)
        migration_id = "operations-company-v1"
        previous = state["migrations"].get(migration_id)
        company_id = _identifier(arguments["company_id"])
        if isinstance(previous, Mapping):
            if previous.get("source_hash") != source_hash:
                raise CompanyStateConflict("Operations Company migration differs")
            existing = state["companies"].get(company_id)
            if not isinstance(existing, Mapping):
                raise CompanyStateConflict("Operations Company migration is incomplete")
            return {
                "company": _copy(existing),
                "migration_id": migration_id,
                "deduplicated": True,
            }
        if company_id in state["companies"]:
            raise CompanyStateConflict("Company exists before Operations migration")
        conversation_id = _legacy_text(legacy.get("conversation_id"), 255)
        group_id = _legacy_text(legacy.get("conversation_group_id"), 255)
        company = {
            "id": company_id,
            "name": "Rumi Operations Company",
            "description": "Migrated legacy Operations Company.",
            "status": "active",
            "settings": {
                "legacy_operations": {
                    "source_hash": source_hash,
                    "org_id": _legacy_text(legacy.get("org_id"), 255),
                    "conversation_id": conversation_id,
                    "conversation_group_id": group_id,
                    "schedule_ids": _legacy_schedule_ids(legacy.get("schedule_ids")),
                }
            },
            "metadata": {"migration_source": "operations-company-v1"},
            "conversation_group_id": group_id or "company:" + company_id,
            "roles": {
                "legacy-client-manager": {
                    "id": "legacy-client-manager",
                    "name": "Client Manager",
                    "work_type": "agent",
                    "created_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                },
                "legacy-operations-monitor": {
                    "id": "legacy-operations-monitor",
                    "name": "Operations Monitor",
                    "work_type": "agent",
                    "created_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                },
            },
            "members": {
                "client_manager": _legacy_member(
                    "client_manager",
                    "Client Manager",
                    "legacy-client-manager",
                    now_ms,
                ),
                "operations_monitor": _legacy_member(
                    "operations_monitor",
                    "Operations Monitor",
                    "legacy-operations-monitor",
                    now_ms,
                ),
            },
            "channels": {
                "ops-company": {
                    "id": "ops-company",
                    "name": "Operations",
                    "created_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
            },
            "tasks": {},
            "routes": {},
            "inbound": [],
            "messages": [],
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
        }
        state["companies"][company_id] = company
        state["migrations"][migration_id] = {
            "id": migration_id,
            "company_id": company_id,
            "source_hash": source_hash,
            "imported_at_ms": now_ms,
        }
        return {
            "company": _copy(company),
            "migration_id": migration_id,
            "deduplicated": False,
        }

    def _named_record(
        self,
        company: dict[str, Any],
        key: str,
        name: str,
        arguments: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        record_id = _identifier(arguments["record_id"])
        if name.endswith(".delete"):
            if company[key].pop(record_id, None) is None:
                raise KeyError(f"Company {key[:-1]} is unknown")
            company["updated_at_ms"] = now_ms
            return {f"deleted_{key[:-1]}_id": record_id}
        record = dict(arguments["record"])
        record["id"] = record_id
        record["name"] = str(record.get("name") or record_id)[:200]
        record["updated_at_ms"] = now_ms
        current = company[key].get(record_id)
        operation_id = _record_operation_id(record)
        if (
            operation_id
            and isinstance(current, Mapping)
            and _record_operation_id(current) == operation_id
        ):
            return {key[:-1]: _copy(current), "deduplicated": True}
        record["created_at_ms"] = current["created_at_ms"] if current else now_ms
        company[key][record_id] = _copy(record)
        company["updated_at_ms"] = now_ms
        return {key[:-1]: _copy(record)}

    def _member(
        self,
        company: dict[str, Any],
        name: str,
        arguments: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        member_id = _identifier(arguments["record_id"])
        if name == "member.delete":
            if any(
                task.get("assignee_member_id") == member_id
                and task["status"] in {"assigned", "running", "waiting"}
                for task in company["tasks"].values()
            ):
                raise CompanyStateConflict("Company member has active tasks")
            if company["members"].pop(member_id, None) is None:
                raise KeyError("Company member is unknown")
            company["updated_at_ms"] = now_ms
            return {"deleted_member_id": member_id}
        record = dict(arguments["record"])
        role_id = _identifier(record.get("role_id"))
        if role_id not in company["roles"]:
            raise KeyError("Company member role is unknown")
        member = {
            "id": member_id,
            "display_name": str(record.get("display_name") or member_id)[:200],
            "role_id": role_id,
            "agent_profile_id": _identifier(record.get("agent_profile_id") or "default"),
            "mentions": sorted(
                {str(item).casefold()[:100] for item in record.get("mentions") or []}
            )[:100],
            "enabled": bool(record.get("enabled", True)),
            "metadata": _copy(_mapping(record.get("metadata"))),
            "updated_at_ms": now_ms,
        }
        current = company["members"].get(member_id)
        member["created_at_ms"] = current["created_at_ms"] if current else now_ms
        company["members"][member_id] = member
        company["updated_at_ms"] = now_ms
        return {"member": _copy(member)}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": VERSION,
                "profile_id": self.profile_id,
                "revision": 0,
                "companies": {},
                "migrations": {},
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, Mapping)
            or value.get("version") != VERSION
            or value.get("profile_id") != self.profile_id
            or not isinstance(value.get("companies"), Mapping)
            or not isinstance(value.get("migrations", {}), Mapping)
        ):
            raise ValueError("Company state is invalid")
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": max(0, int(value.get("revision") or 0)),
            "companies": _copy(value["companies"]),
            "migrations": _copy(value.get("migrations", {})),
        }

    def _write(self, value: Mapping[str, Any]) -> None:
        _atomic_json(self.path, value)


def create_company_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create Company state read operations."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = CompanyStateStore(_profile(payload))
        if name == "list":
            return store.snapshot()
        if name == "get":
            return store.get(str(payload.get("company_id") or ""))
        raise ValueError(f"unknown Company resource operation: {name}")

    return operation


def create_company_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated Company state mutations."""

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
    if name == "company.create":
        arguments["name"] = str(payload.get("name") or "Company")
        arguments["settings"] = dict(_mapping(payload.get("settings")))
        arguments["description"] = str(payload.get("description") or "")
        arguments["metadata"] = dict(_mapping(payload.get("metadata")))
        arguments["conversation_group_id"] = str(payload.get("conversation_group_id") or "")
    elif name == "migration.operations.import":
        arguments["legacy_state"] = _legacy_operations_state(payload.get("legacy_state"))
    elif name == "company.update":
        updates = dict(_mapping(payload.get("updates")))
        if set(updates) - {
            "name",
            "status",
            "settings",
            "description",
            "metadata",
            "conversation_group_id",
        }:
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
        arguments["record_id"] = str(payload.get("task_id") or "")
        arguments["status"] = str(payload.get("status") or "")
        arguments["details"] = dict(_mapping(payload.get("details")))
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


def _task(value: Mapping[str, Any], company: Mapping[str, Any]) -> dict[str, Any]:
    task_id = _identifier(value.get("id") or uuid.uuid4())
    assignee = str(value.get("assignee_member_id") or "")
    if assignee and assignee not in company["members"]:
        raise KeyError("Company task assignee is unknown")
    status = str(value.get("status") or "queued")
    if status not in {*_TASK_TRANSITIONS, "completed", "cancelled"}:
        raise ValueError("Company task status is invalid")
    return {
        "id": task_id,
        "title": str(value.get("title") or "Task")[:500],
        "description": str(value.get("description") or "")[:100_000],
        "status": status,
        "assignee_member_id": assignee,
        "channel_id": str(value.get("channel_id") or "")[:255],
        "priority": max(0, min(100, int(value.get("priority") or 50))),
        "idempotency_key": str(value.get("idempotency_key") or task_id)[:255],
        "result_reference": _copy(value.get("result_reference")),
        "error": str(value.get("error") or "")[:1000],
        "metadata": _copy(_mapping(value.get("metadata"))),
    }


def _timeline_record(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _identifier(value.get("id") or uuid.uuid4()),
        "type": str(value.get("type") or "message")[:120],
        "actor_id": str(value.get("actor_id") or "")[:255],
        "channel_id": str(value.get("channel_id") or "")[:255],
        "text": str(value.get("text") or "")[:100_000],
        "metadata": _copy(_mapping(value.get("metadata"))),
    }


def _record_operation_id(value: Mapping[str, Any]) -> str:
    metadata = value.get("metadata")
    if not isinstance(metadata, Mapping):
        return ""
    return str(metadata.get("operation_id") or "").strip()


def _legacy_operations_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("legacy Operations Company state is required")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > 128 * 1024:
        raise ValueError("legacy Operations Company state is too large")
    normalized = {
        "org_id": _legacy_text(value.get("org_id"), 255),
        "conversation_id": _legacy_text(value.get("conversation_id"), 255),
        "conversation_group_id": _legacy_text(
            value.get("conversation_group_id"),
            255,
        ),
        "schedule_ids": _legacy_schedule_ids(value.get("schedule_ids")),
    }
    if not any(
        (
            normalized["org_id"],
            normalized["conversation_id"],
            normalized["conversation_group_id"],
            normalized["schedule_ids"],
        )
    ):
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


def _legacy_member(
    member_id: str,
    display_name: str,
    role_id: str,
    now_ms: int,
) -> dict[str, Any]:
    return {
        "id": member_id,
        "display_name": display_name,
        "role_id": role_id,
        "agent_profile_id": "default",
        "mentions": [member_id],
        "enabled": True,
        "metadata": {"migration_source": "operations-company-v1"},
        "created_at_ms": now_ms,
        "updated_at_ms": now_ms,
    }


def _legacy_text(value: Any, limit: int) -> str:
    return str(value or "").strip().replace("\x00", "")[:limit]


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _identifier(value: Any) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError("Company identifier is invalid")
    return identifier


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
    if int(state.get("revision") or 0) != expected:
        raise CompanyStateConflict("Company state revision is stale")


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".company-", suffix=".tmp")
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
