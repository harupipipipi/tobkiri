"""Revision-guarded provider-neutral tool definition registry."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import LockTimeout, NamedLock
from tobkiri_protocol.secure_persistence import SecureDirectory

STORE_VERSION = "rumi.tool-definition-registry.v1"
_STORE_LIMIT = 16 * 1024 * 1024
DEFINITION_CONTRIBUTION = "rumi.resource.tool.definition.contribution.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class ToolDefinitionRegistry:
    """Own tool definitions and finite aliases, but never execute tools."""

    def __init__(
        self,
        profile_id: str,
        *,
        user_data_root: Path | None = None,
        guard: Callable[[], None] | None = None,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self._guard = guard
        self._directory: SecureDirectory | None = None
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
                dict(state["definitions"][key]) for key in sorted(state["definitions"])
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
        with self._mutation_lock():
            state = self._read()
            _assert_revision(state, expected_revision)
            tool_id = normalized["tool_id"]
            if tool_id in state["aliases"] and tool_id not in state["definitions"]:
                raise ValueError("tool definition collides with an alias")
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
        with self._mutation_lock():
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
        with self._mutation_lock():
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
        raw_aliases = {str(alias): str(target) for alias, target in aliases.items()}
        source = {
            "definitions": raw_definitions,
            "aliases": dict(sorted(raw_aliases.items())),
        }
        source_hash = hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if source_hash != str(expected_source_hash or ""):
            raise RuntimeError("tool registry migration source changed")
        normalized = [_definition(item) for item in raw_definitions]
        normalized.sort(key=lambda item: item["tool_id"])
        if len({item["tool_id"] for item in normalized}) != len(normalized):
            raise ValueError("tool migration contains duplicate definitions")
        normalized_aliases = {
            _identifier(alias): _identifier(target)
            for alias, target in raw_aliases.items()
        }
        with self._mutation_lock():
            store = self._store(create=True)
            assert store is not None
            if store.exists(self.path.name):
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
            self._check_current()
            relative_backup = (
                backup.relative_to(self.root) / "legacy-tool-registry.json"
            )
            # Preserve earlier backups even if a migration identity collides.
            descriptor = store.open_lock(relative_backup, exclusive=True)
            os.close(descriptor)
            self._write_json(relative_backup, source)
            state = self._empty()
            state["definitions"] = {item["tool_id"]: item for item in normalized}
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

    def rollback_migration(
        self, migration_id: str, expected_revision: int = 1
    ) -> dict[str, Any]:
        """Roll back an exact revision; legacy calls require pristine migration."""
        with self._mutation_lock():
            state = self._read()
            _assert_revision(state, expected_revision)
            marker = state.get("migration")
            if (
                not isinstance(marker, Mapping)
                or marker.get("migration_id") != migration_id
            ):
                raise ValueError("tool registry migration marker mismatch")
            self._check_current()
            self._write_json(f"rollback-{migration_id}.json", state)
            self._check_current()
            store = self._store(create=True)
            assert store is not None
            store.unlink(self.path.name, missing_ok=True)
        return {"migration_id": migration_id, "rolled_back": True}

    def _check_current(self) -> None:
        if self._guard is not None:
            self._guard()

    @contextmanager
    def _mutation_lock(self) -> Iterator[None]:
        # Use the same exclusive file/JSON lock protocol as existing writers.
        self._check_current()
        store = self._store(create=True)
        assert store is not None
        store.exists(self.path.name)  # Validate the captured root before locking.
        locks = SecureDirectory(store.root / "locks")
        lock = NamedLock(
            locks.root,
            "tool-definitions",
            directory=locks,
            timeout_ms=0 if self._guard is not None else 60000,
        )
        if self._guard is None:
            with lock:
                yield
            return
        while True:
            self._check_current()
            try:
                lock.acquire()
                break
            except LockTimeout:
                time.sleep(0.01)
        try:
            self._check_current()
            yield
        finally:
            lock.release()

    def _read(self) -> dict[str, Any]:
        self._check_current()
        store = self._store(create=False)
        if store is None:
            return self._empty()
        try:
            encoded = store.read_bytes_bounded(self.path.name, max_bytes=_STORE_LIMIT)
        except FileNotFoundError:
            return self._empty()
        value = json.loads(encoded)
        if (
            not isinstance(value, dict)
            or value.get("version") != STORE_VERSION
            or value.get("profile_id") != self.profile_id
            or type(value.get("revision")) is not int
            or value["revision"] < 0
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

    def _store(self, *, create: bool) -> SecureDirectory | None:
        if self._directory is None:
            if not create:
                try:
                    self.root.lstat()
                except FileNotFoundError:
                    return None
            self._directory = SecureDirectory(self.root, create=create)
        return self._directory

    def _write_json(self, relative: str | Path, value: Mapping[str, Any]) -> None:
        self._check_current()
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(encoded) > _STORE_LIMIT:
            raise ValueError("tool definition registry exceeds the storage limit")
        store = self._store(create=True)
        assert store is not None
        store.write_bytes_atomic(relative, encoded, before_publish=self._check_current)

    def _write(self, state: Mapping[str, Any]) -> None:
        self._write_json(self.path.name, state)


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


def _composed_catalog(
    client: Any, registry: ToolDefinitionRegistry, *,
    contribution_contract: str = DEFINITION_CONTRIBUTION,
    pack_definitions: tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any]:
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
    for raw in pack_definitions:
        definition = _definition(raw)
        tool_id = definition["tool_id"]
        if tool_id in definitions or tool_id in aliases:
            raise RuntimeError(f"packaged tool definition collides: {tool_id}")
        definitions[tool_id] = definition
        for alias in definition["aliases"]:
            if alias in definitions or alias in aliases:
                raise RuntimeError(f"packaged tool alias collides: {alias}")
            aliases[alias] = tool_id
    sources: list[dict[str, str]] = []
    providers = sorted(
        client.providers(contribution_contract),
        key=lambda item: str(item.get("provider_instance_id") or ""),
    )
    for provider in providers:
        provider_id = str(provider.get("provider_instance_id") or "").strip()
        if not provider_id:
            raise RuntimeError("tool definition contribution provider is invalid")
        operation = (
            "list" if contribution_contract == DEFINITION_CONTRIBUTION
            else provider.get("operation_id")
        )
        if not isinstance(operation, str) or not operation:
            raise RuntimeError("tool contribution operation is unavailable")
        value = client.invoke(
            contribution_contract,
            operation,
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
    aliases = value.get("aliases")
    aliases = aliases if isinstance(aliases, list) else []
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
    if "connection_id" in execution:
        normalized["execution"]["connection_id"] = _identifier(execution["connection_id"])
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
