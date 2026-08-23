"""Profile-scoped Team organizations, tasks, channels, and routing state."""

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
SERVICE_PACK_ID = "rumi_team_state_store_pack"
LEGACY_SERVICE_PACK_ID = "rumi_company_state_store_pack"
VERSION = "rumi.team-state.v1"
LEGACY_VERSION = "rumi.company-state.v1"
MIGRATION_ID = "company-to-team.v1"
COMPATIBILITY_SUNSET_AT = "2027-12-31"
MAX_LEGACY_STATE_BYTES = 64 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TASK_TRANSITIONS = {
    "queued": {"assigned", "cancelled", "blocked"},
    "assigned": {"running", "cancelled", "blocked"},
    "running": {"waiting", "completed", "failed", "cancelled", "blocked"},
    "waiting": {"running", "cancelled", "blocked"},
    "blocked": {"queued", "assigned", "cancelled"},
    "failed": {"queued", "cancelled"},
}


class TeamStateConflict(RuntimeError):
    """Raised for stale state or invalid Team lifecycle transitions."""


class TeamStateStore:
    """Own canonical Team data without coordination or transport logic."""

    def __init__(self, profile_id: str, *, root: Path | None = None) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.data_root = Path(root or USER_DATA_DIR)
        self.root = (
            self.data_root
            / "packs"
            / SERVICE_PACK_ID
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "teams.json"
        self.lock_root = self.root / "locks"
        self.legacy_path = (
            self.data_root
            / "packs"
            / LEGACY_SERVICE_PACK_ID
            / "profiles"
            / self.profile_id
            / "companies.json"
        )
        self._migrate_legacy_state()

    def snapshot(self) -> dict[str, Any]:
        """Return all Team records in deterministic order."""

        state = self._read()
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "teams": [
                state["teams"][key] for key in sorted(state["teams"])
            ],
        }

    def get(self, team_id: str) -> dict[str, Any] | None:
        """Return one exact Team record."""

        value = self._read()["teams"].get(_identifier(team_id))
        return _copy(value) if isinstance(value, Mapping) else None

    def apply(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one exact revision-bound Team mutation."""

        with NamedLock(self.lock_root, "teams"):
            state = self._read()
            _assert_revision(state, int(arguments["expected_revision"]))
            result = self._transition(state, name, arguments)
            if name == "migration.operations.import" and result.get("deduplicated"):
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
        team_id = _identifier(arguments["team_id"])
        now_ms = _now_ms()
        if name == "team.create":
            if team_id in state["teams"]:
                raise ValueError("Team already exists")
            team = {
                "id": team_id,
                "name": str(arguments["name"])[:200],
                "description": str(arguments["description"])[:4_000],
                "status": "active",
                "settings": _copy(arguments["settings"]),
                "metadata": _copy(arguments["metadata"]),
                "conversation_group_id": str(
                    arguments["conversation_group_id"]
                )[:255],
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
            state["teams"][team_id] = team
            return {"team": _copy(team)}
        if name == "migration.operations.import":
            return self._import_operations_team(state, arguments, now_ms)
        team = state["teams"].get(team_id)
        if team is None:
            raise KeyError("Team is unknown")
        if name == "team.delete":
            if any(
                task["status"] in {"assigned", "running", "waiting"}
                for task in team["tasks"].values()
            ):
                raise TeamStateConflict("Team has active tasks")
            del state["teams"][team_id]
            return {"deleted_team_id": team_id}
        if name == "team.update":
            updates = arguments["updates"]
            if "name" in updates:
                team["name"] = str(updates["name"])[:200]
            if "status" in updates:
                team["status"] = str(updates["status"])
            if "settings" in updates:
                next_settings = _copy(updates["settings"])
                if arguments.get("replace_settings"):
                    team["settings"] = next_settings
                else:
                    team["settings"] = {
                        **team["settings"],
                        **next_settings,
                    }
            if "description" in updates:
                team["description"] = str(updates["description"])[:4_000]
            if "metadata" in updates:
                team["metadata"] = {
                    **_copy(_mapping(team.get("metadata"))),
                    **_copy(updates["metadata"]),
                }
            if "conversation_group_id" in updates:
                team["conversation_group_id"] = str(
                    updates["conversation_group_id"]
                )[:255]
            team["updated_at_ms"] = now_ms
            return {"team": _copy(team)}
        if name == "agent.upsert":
            role = dict(arguments["role"])
            role_id = _identifier(role.get("id"))
            self._named_record(
                team,
                "roles",
                "role.upsert",
                {"record_id": role_id, "record": role},
                now_ms,
            )
            member = dict(arguments["member"])
            member_id = _identifier(member.get("id"))
            result = self._member(
                team,
                "member.upsert",
                {"record_id": member_id, "record": member},
                now_ms,
            )
            return {
                "agent": _copy(result["member"]),
                "role": _copy(team["roles"][role_id]),
            }
        if name == "agent.delete":
            member_id = _identifier(arguments["record_id"])
            self._member(
                team,
                "member.delete",
                {"record_id": member_id},
                now_ms,
            )
            return {"deleted_agent_id": member_id}
        if name.startswith("role."):
            return self._named_record(team, "roles", name, arguments, now_ms)
        if name.startswith("member."):
            return self._member(team, name, arguments, now_ms)
        if name.startswith("channel."):
            return self._named_record(team, "channels", name, arguments, now_ms)
        if name.startswith("route."):
            return self._named_record(team, "routes", name, arguments, now_ms)
        if name == "task.upsert":
            task = _task(arguments["record"], team)
            current = team["tasks"].get(task["id"])
            task["created_at_ms"] = current["created_at_ms"] if current else now_ms
            task["updated_at_ms"] = now_ms
            team["tasks"][task["id"]] = task
            team["updated_at_ms"] = now_ms
            return {"task": _copy(task)}
        if name == "task.delete":
            task_id = _identifier(arguments["record_id"])
            if team["tasks"].pop(task_id, None) is None:
                raise KeyError("Team task is unknown")
            team["updated_at_ms"] = now_ms
            return {"deleted_task_id": task_id}
        if name == "task.transition":
            task_id = _identifier(arguments["record_id"])
            task = team["tasks"].get(task_id)
            if task is None:
                raise KeyError("Team task is unknown")
            target = str(arguments["status"])
            if target not in _TASK_TRANSITIONS.get(task["status"], set()):
                raise TeamStateConflict("Team task transition is invalid")
            task["status"] = target
            details = dict(arguments["details"])
            if "assignee_member_id" in details:
                assignee = str(details["assignee_member_id"])
                if assignee and assignee not in team["members"]:
                    raise KeyError("Team task assignee is unknown")
                task["assignee_member_id"] = assignee
            if target == "completed":
                task["result_reference"] = details.get("result_reference")
            if target in {"failed", "blocked"}:
                task["error"] = str(details.get("error") or "")[:1000]
            task["updated_at_ms"] = now_ms
            team["updated_at_ms"] = now_ms
            return {"task": _copy(task)}
        if name in {"inbound.append", "message.append"}:
            key = "inbound" if name.startswith("inbound") else "messages"
            record = _timeline_record(arguments["record"])
            result_key = key[:-1] if key.endswith("s") else key
            if any(item["id"] == record["id"] for item in team[key]):
                return {result_key: _copy(record), "deduplicated": True}
            record["created_at_ms"] = now_ms
            team[key].append(record)
            if len(team[key]) > 10_000:
                del team[key][:-10_000]
            team["updated_at_ms"] = now_ms
            return {result_key: _copy(record), "deduplicated": False}
        raise ValueError(f"unknown Team action: {name}")

    def _import_operations_team(
        self,
        state: dict[str, Any],
        arguments: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        """Import one redacted legacy Operations Team snapshot exactly once."""

        legacy = _legacy_operations_state(arguments["legacy_state"])
        source_hash = _canonical_hash(legacy)
        migration_id = "operations-team-v1"
        previous = state["migrations"].get(migration_id)
        team_id = _identifier(arguments["team_id"])
        if isinstance(previous, Mapping):
            if previous.get("source_hash") != source_hash:
                raise TeamStateConflict("Operations Team migration differs")
            existing = state["teams"].get(team_id)
            if not isinstance(existing, Mapping):
                raise TeamStateConflict("Operations Team migration is incomplete")
            return {
                "team": _copy(existing),
                "migration_id": migration_id,
                "deduplicated": True,
            }
        if team_id in state["teams"]:
            raise TeamStateConflict("Team exists before Operations migration")
        conversation_id = _legacy_text(legacy.get("conversation_id"), 255)
        group_id = _legacy_text(legacy.get("conversation_group_id"), 255)
        team = {
            "id": team_id,
            "name": "Rumi Operations Team",
            "description": "Migrated legacy Operations Team.",
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
            "metadata": {"migration_source": "operations-team-v1"},
            "conversation_group_id": group_id or "team:" + team_id,
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
                "ops-team": {
                    "id": "ops-team",
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
        state["teams"][team_id] = team
        state["migrations"][migration_id] = {
            "id": migration_id,
            "team_id": team_id,
            "source_hash": source_hash,
            "imported_at_ms": now_ms,
        }
        return {
            "team": _copy(team),
            "migration_id": migration_id,
            "deduplicated": False,
        }

    def _named_record(
        self,
        team: dict[str, Any],
        key: str,
        name: str,
        arguments: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        record_id = _identifier(arguments["record_id"])
        if name.endswith(".delete"):
            if team[key].pop(record_id, None) is None:
                raise KeyError(f"Team {key[:-1]} is unknown")
            team["updated_at_ms"] = now_ms
            return {f"deleted_{key[:-1]}_id": record_id}
        record = dict(arguments["record"])
        record["id"] = record_id
        record["name"] = str(record.get("name") or record_id)[:200]
        record["updated_at_ms"] = now_ms
        current = team[key].get(record_id)
        record["created_at_ms"] = current["created_at_ms"] if current else now_ms
        team[key][record_id] = _copy(record)
        team["updated_at_ms"] = now_ms
        return {key[:-1]: _copy(record)}

    def _member(
        self,
        team: dict[str, Any],
        name: str,
        arguments: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        member_id = _identifier(arguments["record_id"])
        if name == "member.delete":
            if any(
                task.get("assignee_member_id") == member_id
                and task["status"] in {"assigned", "running", "waiting"}
                for task in team["tasks"].values()
            ):
                raise TeamStateConflict("Team member has active tasks")
            if team["members"].pop(member_id, None) is None:
                raise KeyError("Team member is unknown")
            team["updated_at_ms"] = now_ms
            return {"deleted_member_id": member_id}
        record = dict(arguments["record"])
        role_id = _identifier(record.get("role_id"))
        if role_id not in team["roles"]:
            raise KeyError("Team member role is unknown")
        member = {
            "id": member_id,
            "display_name": str(record.get("display_name") or member_id)[:200],
            "role_id": role_id,
            "agent_profile_id": _identifier(
                record.get("agent_profile_id") or "default"
            ),
            "mentions": sorted(
                {str(item).casefold()[:100] for item in record.get("mentions") or []}
            )[:100],
            "enabled": bool(record.get("enabled", True)),
            "metadata": _copy(_mapping(record.get("metadata"))),
            "updated_at_ms": now_ms,
        }
        current = team["members"].get(member_id)
        member["created_at_ms"] = current["created_at_ms"] if current else now_ms
        team["members"][member_id] = member
        team["updated_at_ms"] = now_ms
        return {"member": _copy(member)}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": VERSION,
                "profile_id": self.profile_id,
                "revision": 0,
                "teams": {},
                "migrations": {},
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, Mapping)
            or value.get("version") != VERSION
            or value.get("profile_id") != self.profile_id
            or not isinstance(value.get("teams"), Mapping)
            or not isinstance(value.get("migrations", {}), Mapping)
        ):
            raise ValueError("Team state is invalid")
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": max(0, int(value.get("revision") or 0)),
            "teams": _copy(value["teams"]),
            "migrations": _copy(value.get("migrations", {})),
        }

    def _migrate_legacy_state(self) -> None:
        """Atomically activate a source-digest-bound legacy Company import.

        The legacy file remains untouched as the rollback source.  The new
        Team state becomes active only when its complete JSON document is
        atomically published, so interruption leaves either the old source or
        the complete canonical state available.
        """

        if self.path.exists() or not self.legacy_path.is_file():
            return
        with NamedLock(self.lock_root, "company-to-team-migration"):
            if self.path.exists() or not self.legacy_path.is_file():
                return
            source = self.legacy_path.read_bytes()
            if len(source) > MAX_LEGACY_STATE_BYTES:
                raise ValueError("legacy Company state exceeds the migration limit")
            source_digest = hashlib.sha256(source).hexdigest()
            try:
                legacy = json.loads(source.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("legacy Company state is corrupt") from exc
            if (
                not isinstance(legacy, Mapping)
                or legacy.get("version") not in {LEGACY_VERSION, VERSION}
                or legacy.get("profile_id") != self.profile_id
                or not isinstance(legacy.get("companies"), Mapping)
            ):
                raise ValueError("legacy Company state is invalid")
            teams: dict[str, Any] = {}
            conflicts: list[dict[str, str]] = []
            for legacy_id, raw_team in sorted(legacy["companies"].items()):
                team_id = _identifier(legacy_id)
                if not isinstance(raw_team, Mapping):
                    conflicts.append(
                        {"legacy_id": str(legacy_id), "reason": "record_not_object"}
                    )
                    continue
                if team_id in teams:
                    conflicts.append(
                        {"legacy_id": str(legacy_id), "reason": "duplicate_team_id"}
                    )
                    continue
                team = _copy(raw_team)
                team["id"] = team_id
                teams[team_id] = team
            if conflicts:
                raise ValueError("legacy Company state has unresolved conflicts")
            migrated_at_ms = _now_ms()
            migration = {
                "migration_id": MIGRATION_ID,
                "legacy_pack_id": LEGACY_SERVICE_PACK_ID,
                "legacy_version": str(legacy.get("version")),
                "source_digest": f"sha256:{source_digest}",
                "canonical_pack_id": SERVICE_PACK_ID,
                "canonical_team_ids": sorted(teams),
                "actor": "system:company-to-team-migration",
                "migrated_at_ms": migrated_at_ms,
                "unresolved_conflicts": [],
                "rollback_source": str(self.legacy_path),
                "activation": "committed",
            }
            canonical = {
                "version": VERSION,
                "profile_id": self.profile_id,
                "revision": max(0, int(legacy.get("revision") or 0)),
                "teams": teams,
                "migrations": {MIGRATION_ID: migration},
            }
            _atomic_json(self.path, canonical)

    def _write(self, value: Mapping[str, Any]) -> None:
        _atomic_json(self.path, value)


def create_team_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create Team state read operations."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = TeamStateStore(_profile(payload))
        if name == "list":
            return store.snapshot()
        if name == "get":
            return store.get(str(payload.get("team_id") or ""))
        raise ValueError(f"unknown Team resource operation: {name}")

    return operation


def create_team_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated Team state mutations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        arguments = _arguments(name, payload)
        _redeem(client, payload, name, arguments)
        return TeamStateStore(_profile(payload)).apply(name, arguments)

    return operation


class CompanyStateStore(TeamStateStore):
    """Deprecated Company facade over the single canonical Team store."""

    def get(self, company_id: str) -> dict[str, Any] | None:
        _record_legacy_usage(self, "CompanyStateStore.get")
        return _legacy_result(super().get(company_id))

    def snapshot(self) -> dict[str, Any]:
        _record_legacy_usage(self, "CompanyStateStore.snapshot")
        return _legacy_result(super().snapshot())

    def apply(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _record_legacy_usage(self, f"CompanyStateStore.apply:{name}")
        canonical_name, canonical_arguments = _canonical_legacy_call(name, arguments)
        return _legacy_result(super().apply(canonical_name, canonical_arguments))


CompanyStateConflict = TeamStateConflict


def create_company_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the sunset Company read adapter over Team authority."""

    canonical = create_team_resource(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        converted = _canonical_legacy_payload(payload)
        store = TeamStateStore(_profile(converted))
        result = canonical(name, converted)
        _record_legacy_usage(store, f"resource:{name}")
        return _legacy_result(result)

    return operation


def create_company_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the sunset Company write adapter over Team authority."""

    canonical = create_team_action(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        canonical_name, converted = _canonical_legacy_call(name, payload)
        result = canonical(canonical_name, converted)
        _record_legacy_usage(
            TeamStateStore(_profile(converted)),
            f"action:{name}",
        )
        return _legacy_result(result)

    return operation


def _canonical_legacy_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    converted = dict(payload)
    if "team_id" not in converted and "company_id" in converted:
        converted["team_id"] = converted.pop("company_id")
    return converted


def _canonical_legacy_call(
    name: str,
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    canonical_name = f"team.{name.removeprefix('company.')}" if name.startswith(
        "company."
    ) else name
    return canonical_name, _canonical_legacy_payload(payload)


def _legacy_result(value: Any) -> Any:
    if isinstance(value, list):
        return [_legacy_result(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    aliases = {
        "team": "company",
        "teams": "companies",
        "team_id": "company_id",
        "deleted_team_id": "deleted_company_id",
    }
    projected = {
        aliases.get(str(key), str(key)): _legacy_result(item)
        for key, item in value.items()
    }
    if projected.get("version") == VERSION:
        projected["version"] = LEGACY_VERSION
    return projected


def _record_legacy_usage(store: TeamStateStore, alias: str) -> None:
    """Persist bounded, payload-free compatibility usage telemetry."""

    path = store.root / "compatibility_usage.v1.json"
    try:
        with NamedLock(store.lock_root, "compatibility-usage"):
            if path.is_file():
                current = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(current, Mapping):
                    current = {}
            else:
                current = {}
            entries = dict(_mapping(current.get("aliases")))
            normalized = re.sub(r"[^A-Za-z0-9._:-]+", "_", alias)[:160]
            existing = dict(_mapping(entries.get(normalized)))
            now_ms = _now_ms()
            if normalized not in entries and len(entries) >= 64:
                normalized = "other"
                existing = dict(_mapping(entries.get(normalized)))
            entries[normalized] = {
                "count": min(1_000_000, int(existing.get("count") or 0) + 1),
                "first_used_at_ms": int(existing.get("first_used_at_ms") or now_ms),
                "last_used_at_ms": now_ms,
            }
            _atomic_json(
                path,
                {
                    "version": "tobkiri.team-compatibility-usage.v1",
                    "profile_id": store.profile_id,
                    "sunset_at": COMPATIBILITY_SUNSET_AT,
                    "aliases": entries,
                },
            )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Compatibility telemetry must never turn an already-authorized Team
        # mutation into an apparent failure and trigger an unsafe retry.
        return


def _arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "team.create",
        "team.update",
        "team.delete",
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
        raise ValueError(f"unknown Team action: {name}")
    arguments: dict[str, Any] = {
        "team_id": str(payload.get("team_id") or ""),
        "expected_revision": max(0, int(payload.get("expected_revision") or 0)),
    }
    if name == "team.create":
        arguments["name"] = str(payload.get("name") or "Team")
        arguments["settings"] = dict(_mapping(payload.get("settings")))
        arguments["description"] = str(payload.get("description") or "")
        arguments["metadata"] = dict(_mapping(payload.get("metadata")))
        arguments["conversation_group_id"] = str(
            payload.get("conversation_group_id") or ""
        )
    elif name == "migration.operations.import":
        arguments["legacy_state"] = _legacy_operations_state(
            payload.get("legacy_state")
        )
    elif name == "team.update":
        updates = dict(_mapping(payload.get("updates")))
        if set(updates) - {
            "name",
            "status",
            "settings",
            "description",
            "metadata",
            "conversation_group_id",
        }:
            raise ValueError("Team update contains unsupported fields")
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
    elif name.endswith(".delete") and name != "team.delete":
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
            "operation": f"team.state.{name}",
            "authority": "team.state.manage",
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
        raise PermissionError(str(result.get("reason") or "Team state denied"))


def _task(value: Mapping[str, Any], team: Mapping[str, Any]) -> dict[str, Any]:
    task_id = _identifier(value.get("id") or uuid.uuid4())
    assignee = str(value.get("assignee_member_id") or "")
    if assignee and assignee not in team["members"]:
        raise KeyError("Team task assignee is unknown")
    status = str(value.get("status") or "queued")
    if status not in {*_TASK_TRANSITIONS, "completed", "cancelled"}:
        raise ValueError("Team task status is invalid")
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


def _legacy_operations_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("legacy Operations Team state is required")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > 128 * 1024:
        raise ValueError("legacy Operations Team state is too large")
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
        raise ValueError("legacy Operations Team state is empty")
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
        "metadata": {"migration_source": "operations-team-v1"},
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
        raise ValueError("Team identifier is invalid")
    return identifier


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
    if int(state.get("revision") or 0) != expected:
        raise TeamStateConflict("Team state revision is stale")


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".team-", suffix=".tmp")
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

