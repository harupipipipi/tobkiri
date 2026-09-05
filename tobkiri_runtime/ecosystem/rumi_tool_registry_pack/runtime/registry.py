"""Revision-guarded provider-neutral tool definition registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

STORE_VERSION = "rumi.tool-definition-registry.v1"
DEFINITION_CONTRIBUTION = "rumi.resource.tool.definition.contribution.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class ToolDefinitionRegistry:
    """Own tool definitions and finite aliases, but never execute tools."""

    def __init__(
        self,
        profile_id: str,
        *,
        user_data_root: Path | None = None,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(user_data_root or USER_DATA_DIR)
            / "packs"
            / "rumi_tool_registry_pack"
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "tool-definitions.json"
        self.lock_root = self.root / "locks"
        self.backup_root = self.root / "migration_backups"

    def snapshot(self) -> dict[str, Any]:
        """Return definitions and aliases in deterministic order."""
        state = self._read()
        return {
            "version": STORE_VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "definitions": [
                dict(state["definitions"][key])
                for key in sorted(state["definitions"])
            ],
            "aliases": dict(sorted(state["aliases"].items())),
            "migration": dict(state["migration"])
            if isinstance(state.get("migration"), dict)
            else None,
        }

    def resolve(self, tool_id: str) -> dict[str, Any] | None:
        """Resolve an exact definition or explicit finite alias."""
        requested = _identifier(tool_id)
        state = self._read()
        resolved = state["aliases"].get(requested, requested)
        definition = state["definitions"].get(resolved)
        if not isinstance(definition, dict):
            return None
        return {
            "requested_tool_id": requested,
            "resolved_tool_id": resolved,
            "aliased": requested != resolved,
            "definition": dict(definition),
            "registry_revision": state["revision"],
        }

    def save(self, record: Mapping[str, Any], expected_revision: int) -> dict[str, Any]:
        """Save one normalized definition at an exact revision."""
        normalized = _definition(record)
        with NamedLock(self.lock_root, "tool-definitions"):
            state = self._read()
            _assert_revision(state, expected_revision)
            state["definitions"][normalized["tool_id"]] = normalized
            state["revision"] += 1
            self._write(state)
        return {
            "action": "saved",
            "definition": normalized,
            "registry_revision": state["revision"],
        }

    def delete(self, tool_id: str, expected_revision: int) -> dict[str, Any]:
        """Delete one definition and aliases pointing to it."""
        tool_id = _identifier(tool_id)
        with NamedLock(self.lock_root, "tool-definitions"):
            state = self._read()
            _assert_revision(state, expected_revision)
            if tool_id not in state["definitions"]:
                raise KeyError("tool definition is unknown")
            del state["definitions"][tool_id]
            state["aliases"] = {
                alias: target
                for alias, target in state["aliases"].items()
                if target != tool_id
            }
            state["revision"] += 1
            self._write(state)
        return {
            "action": "deleted",
            "tool_id": tool_id,
            "registry_revision": state["revision"],
        }

    def alias(
        self, alias: str, target_tool_id: str, expected_revision: int
    ) -> dict[str, Any]:
        """Bind an explicit compatibility alias to an existing definition."""
        alias = _identifier(alias)
        target_tool_id = _identifier(target_tool_id)
        with NamedLock(self.lock_root, "tool-definitions"):
            state = self._read()
            _assert_revision(state, expected_revision)
            if target_tool_id not in state["definitions"]:
                raise KeyError("tool alias target is unknown")
            if alias in state["definitions"] and alias != target_tool_id:
                raise ValueError("tool alias collides with a definition")
            state["aliases"][alias] = target_tool_id
            state["revision"] += 1
            self._write(state)
        return {
            "action": "alias_saved",
            "alias": alias,
            "target_tool_id": target_tool_id,
            "registry_revision": state["revision"],
        }

    def migrate(
        self,
        definitions: list[Mapping[str, Any]],
        aliases: Mapping[str, Any],
        expected_source_hash: str,
    ) -> dict[str, Any]:
        """Atomically import one deterministic legacy registry snapshot."""
        raw_definitions = [dict(item) for item in definitions]
        raw_definitions.sort(
            key=lambda item: str(item.get("tool_id") or item.get("name") or "")
        )
        raw_aliases = {
            str(alias): str(target) for alias, target in aliases.items()
        }
        source = {
            "definitions": raw_definitions,
            "aliases": dict(sorted(raw_aliases.items())),
        }
        source_hash = hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if source_hash != str(expected_source_hash or ""):
            raise RuntimeError("tool registry migration source changed")
        normalized = [_definition(item) for item in raw_definitions]
        normalized.sort(key=lambda item: item["tool_id"])
        normalized_aliases = {
            _identifier(alias): _identifier(target)
            for alias, target in raw_aliases.items()
        }
        with NamedLock(self.lock_root, "tool-definitions"):
            if self.path.is_file():
                raise RuntimeError("tool registry target is already initialized")
            tool_ids = {item["tool_id"] for item in normalized}
            if any(target not in tool_ids for target in normalized_aliases.values()):
                raise ValueError("tool alias target is missing")
            if any(
                alias in tool_ids and alias != target
                for alias, target in normalized_aliases.items()
            ):
                raise ValueError("tool alias collides with a definition")
            migration_id = f"migration-{uuid.uuid4().hex}"
            backup = self.backup_root / migration_id
            backup.mkdir(parents=True, exist_ok=False)
            os.chmod(backup, 0o700)
            _atomic_json(backup / "legacy-tool-registry.json", source)
            state = self._empty()
            state["definitions"] = {
                item["tool_id"]: item for item in normalized
            }
            state["aliases"] = normalized_aliases
            state["revision"] = 1
            state["migration"] = {
                "migration_id": migration_id,
                "source_hash": source_hash,
                "backup": str(backup),
            }
            self._write(state)
        return {
            "migration_id": migration_id,
            "source_hash": source_hash,
            "definitions": len(normalized),
            "aliases": len(normalized_aliases),
            "registry_revision": 1,
        }

    def rollback_migration(self, migration_id: str) -> dict[str, Any]:
        """Remove migrated owner state only for the exact migration marker."""
        with NamedLock(self.lock_root, "tool-definitions"):
            state = self._read()
            marker = state.get("migration")
            if (
                not isinstance(marker, Mapping)
                or marker.get("migration_id") != migration_id
            ):
                raise ValueError("tool registry migration marker mismatch")
            _atomic_json(self.root / f"rollback-{migration_id}.json", state)
            self.path.unlink(missing_ok=True)
        return {"migration_id": migration_id, "rolled_back": True}

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("version") != STORE_VERSION
            or value.get("profile_id") != self.profile_id
            or not isinstance(value.get("definitions"), dict)
            or not isinstance(value.get("aliases"), dict)
        ):
            raise ValueError("tool definition registry is invalid")
        return value

    def _empty(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "profile_id": self.profile_id,
            "revision": 0,
            "definitions": {},
            "aliases": {},
            "migration": None,
        }

    def _write(self, state: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".tools-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def create_resource_operation(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the read-only definition resource operation."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        registry = ToolDefinitionRegistry(_profile_id(payload))
        catalog = _composed_catalog(client, registry)
        if name in {"list", "catalog"}:
            return catalog
        if name in {"get", "resolve"}:
            return _resolve_composed(catalog, str(payload.get("tool_id") or ""))
        raise ValueError(f"unknown tool definition operation: {name}")

    return operation


def _composed_catalog(client: Any, registry: ToolDefinitionRegistry) -> dict[str, Any]:
    """Compose stored definitions with explicit profile contributions."""

    snapshot = registry.snapshot()
    definitions = {
        str(item["tool_id"]): dict(item)
        for item in snapshot.get("definitions") or []
        if isinstance(item, Mapping)
    }
    aliases = {
        _identifier(alias): _identifier(target)
        for alias, target in (snapshot.get("aliases") or {}).items()
    }
    sources: list[dict[str, str]] = []
    providers = sorted(
        client.providers(DEFINITION_CONTRIBUTION),
        key=lambda item: str(item.get("provider_instance_id") or ""),
    )
    for provider in providers:
        provider_id = str(provider.get("provider_instance_id") or "").strip()
        if not provider_id:
            raise RuntimeError("tool definition contribution provider is invalid")
        value = client.invoke(
            DEFINITION_CONTRIBUTION,
            "list",
            {"profile_id": registry.profile_id},
            provider_instance_id=provider_id,
        )
        if not isinstance(value, Mapping):
            raise RuntimeError("tool definition contribution is invalid")
        contributed = value.get("definitions")
        contributed_aliases = value.get("aliases")
        if not isinstance(contributed, list) or not isinstance(
            contributed_aliases, Mapping
        ):
            raise RuntimeError("tool definition contribution catalog is invalid")
        for raw in contributed:
            if not isinstance(raw, Mapping):
                raise RuntimeError("contributed tool definition is invalid")
            definition = _definition(raw)
            tool_id = definition["tool_id"]
            if tool_id in definitions or tool_id in aliases:
                raise RuntimeError(f"tool definition contribution collides: {tool_id}")
            definitions[tool_id] = definition
        for raw_alias, raw_target in contributed_aliases.items():
            alias = _identifier(raw_alias)
            target = _identifier(raw_target)
            if alias in definitions or alias in aliases:
                raise RuntimeError(f"tool alias contribution collides: {alias}")
            aliases[alias] = target
        sources.append(
            {
                "provider_instance_id": provider_id,
                "content_hash": str(provider.get("content_hash") or ""),
            }
        )
    missing = sorted(
        {target for target in aliases.values() if target not in definitions}
    )
    if missing:
        raise RuntimeError("tool alias contribution target is missing")
    return {
        **snapshot,
        "definitions": [definitions[key] for key in sorted(definitions)],
        "aliases": dict(sorted(aliases.items())),
        "contributions": sources,
    }


def _resolve_composed(
    catalog: Mapping[str, Any],
    tool_id: str,
) -> dict[str, Any] | None:
    """Resolve one exact definition or explicit alias from a composed catalog."""

    requested = _identifier(tool_id)
    aliases = catalog.get("aliases")
    aliases = aliases if isinstance(aliases, Mapping) else {}
    resolved = str(aliases.get(requested) or requested)
    definitions = {
        str(item.get("tool_id") or ""): item
        for item in catalog.get("definitions") or []
        if isinstance(item, Mapping)
    }
    definition = definitions.get(resolved)
    if not isinstance(definition, Mapping):
        return None
    return {
        "requested_tool_id": requested,
        "resolved_tool_id": resolved,
        "aliased": requested != resolved,
        "definition": dict(definition),
        "registry_revision": int(catalog.get("revision") or 0),
        "contributions": list(catalog.get("contributions") or []),
    }


def create_manage_operation(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create revision-guarded definition management operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        registry = ToolDefinitionRegistry(_profile_id(payload))
        expected = int(payload.get("expected_revision") or 0)
        if name == "save":
            record = payload.get("definition")
            if not isinstance(record, Mapping):
                raise ValueError("tool definition is required")
            return registry.save(record, expected)
        if name == "delete":
            return registry.delete(str(payload.get("tool_id") or ""), expected)
        if name in {"alias", "set_alias"}:
            return registry.alias(
                str(payload.get("alias") or ""),
                str(payload.get("target_tool_id") or ""),
                expected,
            )
        raise ValueError(f"unknown tool definition management operation: {name}")

    return operation


def create_migrate_operation(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create explicit source-hash migration and marker-bound rollback."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        registry = ToolDefinitionRegistry(_profile_id(payload))
        if name == "migrate":
            definitions = payload.get("definitions")
            aliases = payload.get("aliases")
            if not isinstance(definitions, list) or not isinstance(aliases, Mapping):
                raise ValueError("tool migration source is invalid")
            return registry.migrate(
                [item for item in definitions if isinstance(item, Mapping)],
                aliases,
                str(payload.get("expected_source_hash") or ""),
            )
        if name == "rollback":
            return registry.rollback_migration(
                str(payload.get("migration_id") or "")
            )
        raise ValueError(f"unknown tool registry migration operation: {name}")

    return operation


def _definition(value: Mapping[str, Any]) -> dict[str, Any]:
    tool_id = _identifier(value.get("tool_id") or value.get("name"))
    schema = value.get("input_schema") or value.get("parameters") or {}
    if not isinstance(schema, Mapping):
        raise ValueError("tool input schema must be an object")
    execution = value.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    kind = _identifier(execution.get("kind") or value.get("execution_kind") or "local")
    contract_id = str(
        execution.get("contract_id") or value.get("execution_contract_id") or ""
    ).strip()
    if not contract_id:
        raise ValueError("tool execution contract_id is required")
    authority = str(value.get("authority") or "").strip()
    if not authority:
        raise ValueError("tool authority operation is required")
    aliases = value.get("aliases") if isinstance(value.get("aliases"), list) else []
    normalized = {
        "tool_id": tool_id,
        "display_name": str(value.get("display_name") or tool_id)[:200],
        "description": str(value.get("description") or "")[:4000],
        "input_schema": _json_object(schema),
        "result_schema": _json_object(value.get("result_schema") or {}),
        "execution": {
            "kind": kind,
            "contract_id": contract_id,
            "provider_instance_id": str(
                execution.get("provider_instance_id") or ""
            ).strip(),
            "namespace": str(execution.get("namespace") or "").strip(),
            "operation": str(execution.get("operation") or "").strip(),
        },
        "authority": _identifier(authority),
        "risk": str(value.get("risk") or "unknown"),
        "policy_tags": sorted({str(item) for item in value.get("policy_tags") or []}),
        "aliases": sorted({_identifier(item) for item in aliases}),
        "widget": _json_object(value.get("widget") or {}),
        "source_adapter_id": str(value.get("source_adapter_id") or ""),
    }
    normalized["definition_hash"] = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return normalized


def _json_object(value: Any) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValueError("value must be a JSON object")
    return decoded


def _identifier(value: Any) -> str:
    identifier = str(value or "").strip()
    if not _SAFE_ID.fullmatch(identifier):
        raise ValueError("identifier is invalid")
    return identifier


def _profile_id(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
    if int(state.get("revision") or 0) != expected:
        raise RuntimeError("tool definition registry revision is stale")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}-", suffix=".tmp"
    )
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

