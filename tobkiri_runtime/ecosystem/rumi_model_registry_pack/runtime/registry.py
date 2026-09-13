"""Atomic provider-neutral model profile and compatibility alias registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

STORE_VERSION = "rumi.model-registry.store.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class ModelRegistryConflict(RuntimeError):
    """Raised when a mutation is based on a stale store revision."""


class ModelRegistry:
    """Own model profiles and aliases without owning provider catalogs."""

    def __init__(
        self,
        profile_id: str,
        *,
        user_data_root: Path | None = None,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        root = Path(user_data_root or USER_DATA_DIR)
        self.root = (
            root
            / "packs"
            / "rumi_model_registry_pack"
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "model_registry.store.json"
        self.backup_root = self.root / "migration_backups"
        self.lock_root = self.root / "locks"

    def snapshot(self) -> dict[str, Any]:
        """Return profiles and aliases with no credential values."""
        state = self._read()
        return {
            "version": state["version"],
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "profiles": [
                dict(state["profiles"][key])
                for key in sorted(state["profiles"])
            ],
            "aliases": dict(sorted(state["aliases"].items())),
        }

    def get(self, model_profile_id: str) -> dict[str, Any] | None:
        """Return one saved model profile."""
        value = self._read()["profiles"].get(_identifier(model_profile_id))
        return dict(value) if isinstance(value, dict) else None

    def resolve(self, identifier: str) -> dict[str, Any] | None:
        """Resolve an exact profile ID or finite compatibility alias."""
        identifier = _identifier(identifier)
        state = self._read()
        target = state["aliases"].get(identifier, identifier)
        record = state["profiles"].get(target)
        if not isinstance(record, dict):
            return None
        return {
            "requested_id": identifier,
            "resolved_profile_id": target,
            "aliased": target != identifier,
            "profile": dict(record),
            "store_revision": state["revision"],
        }

    def save(
        self,
        record: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Atomically save a normalized profile at one expected revision."""
        normalized = _profile_record(record)
        with NamedLock(self.lock_root, "model-registry"):
            state = self._read()
            self._assert_revision(state, expected_revision)
            profile_id = normalized["model_profile_id"]
            current = state["profiles"].get(profile_id)
            now = _now()
            normalized["created_at"] = str(
                (current or {}).get("created_at") or now
            )
            normalized["updated_at"] = now
            normalized["record_revision"] = int(
                (current or {}).get("record_revision") or 0
            ) + 1
            normalized["record_hash"] = _hash(
                {
                    key: value
                    for key, value in normalized.items()
                    if key != "record_hash"
                }
            )
            state["profiles"][profile_id] = normalized
            state["revision"] += 1
            self._write(state)
            return {
                "action": "saved",
                "profile": dict(normalized),
                "store_revision": state["revision"],
            }

    def delete(
        self,
        model_profile_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Delete one profile and aliases pointing to it atomically."""
        model_profile_id = _identifier(model_profile_id)
        with NamedLock(self.lock_root, "model-registry"):
            state = self._read()
            self._assert_revision(state, expected_revision)
            if model_profile_id not in state["profiles"]:
                raise KeyError("model profile is unknown")
            del state["profiles"][model_profile_id]
            state["aliases"] = {
                alias: target
                for alias, target in state["aliases"].items()
                if target != model_profile_id
            }
            state["revision"] += 1
            self._write(state)
            return {
                "action": "deleted",
                "model_profile_id": model_profile_id,
                "store_revision": state["revision"],
            }

    def set_alias(
        self,
        alias: str,
        target_profile_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Bind one explicit alias to an existing profile."""
        alias = _identifier(alias)
        target_profile_id = _identifier(target_profile_id)
        with NamedLock(self.lock_root, "model-registry"):
            state = self._read()
            self._assert_revision(state, expected_revision)
            if target_profile_id not in state["profiles"]:
                raise KeyError("alias target profile is unknown")
            if alias in state["profiles"] and alias != target_profile_id:
                raise ValueError("alias collides with a model profile")
            state["aliases"][alias] = target_profile_id
            state["revision"] += 1
            self._write(state)
            return {
                "action": "alias_saved",
                "alias": alias,
                "target_profile_id": target_profile_id,
                "store_revision": state["revision"],
            }

    def migrate(
        self,
        profiles: list[Mapping[str, Any]],
        aliases: Mapping[str, Any],
        *,
        expected_source_hash: str,
    ) -> dict[str, Any]:
        """Import fixed-adapter records once, retaining an owner-only backup."""
        normalized_profiles = [
            _profile_record(item) for item in profiles if isinstance(item, Mapping)
        ]
        normalized_profiles.sort(key=lambda item: item["model_profile_id"])
        normalized_aliases = {
            _identifier(key): _identifier(value)
            for key, value in aliases.items()
        }
        source = {
            "profiles": normalized_profiles,
            "aliases": dict(sorted(normalized_aliases.items())),
        }
        if _hash(source) != expected_source_hash:
            raise ModelRegistryConflict("model registry migration source changed")
        with NamedLock(self.lock_root, "model-registry"):
            if self.path.is_file():
                raise RuntimeError("model registry target is already initialized")
            profile_ids = {
                item["model_profile_id"] for item in normalized_profiles
            }
            if any(target not in profile_ids for target in normalized_aliases.values()):
                raise ValueError("model alias target is missing")
            migration_id = f"migration-{uuid.uuid4().hex}"
            backup = self.backup_root / migration_id
            backup.mkdir(parents=True, exist_ok=False)
            os.chmod(backup, 0o700)
            _atomic_json(backup / "legacy-model-registry.json", source)
            state = self._empty()
            state["profiles"] = {
                item["model_profile_id"]: item
                for item in normalized_profiles
            }
            state["aliases"] = dict(normalized_aliases)
            state["revision"] = 1
            state["migration"] = {
                "migration_id": migration_id,
                "source_hash": expected_source_hash,
                "backup": str(backup),
                "migrated_at": _now(),
            }
            self._write(state)
            return {
                "migration_id": migration_id,
                "source_hash": expected_source_hash,
                "profiles": len(normalized_profiles),
                "aliases": len(normalized_aliases),
            }

    def rollback_migration(self, migration_id: str) -> dict[str, Any]:
        """Remove migrated owner state while retaining backup and rollback copy."""
        with NamedLock(self.lock_root, "model-registry"):
            state = self._read()
            migration = state.get("migration")
            if not isinstance(migration, dict) or migration.get(
                "migration_id"
            ) != migration_id:
                raise ValueError("model registry migration marker mismatch")
            snapshot = self.root / f"rollback-{migration_id}.json"
            _atomic_json(snapshot, state)
            self.path.unlink(missing_ok=True)
            return {"migration_id": migration_id, "rolled_back": True}

    @staticmethod
    def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
        if int(state.get("revision") or 0) != int(expected):
            raise ModelRegistryConflict("model registry revision is stale")

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != STORE_VERSION:
            raise ValueError("model registry store version is invalid")
        if payload.get("profile_id") != self.profile_id:
            raise ValueError("model registry profile binding is invalid")
        if not isinstance(payload.get("profiles"), dict) or not isinstance(
            payload.get("aliases"),
            dict,
        ):
            raise ValueError("model registry store is invalid")
        return payload

    def _write(self, state: Mapping[str, Any]) -> None:
        _atomic_json(self.path, state)

    def _empty(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "profile_id": self.profile_id,
            "revision": 0,
            "profiles": {},
            "aliases": {},
            "migration": None,
        }


def _profile_record(value: Mapping[str, Any]) -> dict[str, Any]:
    profile_id = _identifier(
        value.get("model_profile_id") or value.get("profile_id") or value.get("id")
    )
    model_id = _identifier(value.get("model_id") or value.get("model"))
    credential_handle = value.get("credential_handle")
    if credential_handle is not None and not str(credential_handle).startswith(
        ("credential:", "opaque:")
    ):
        raise ValueError("model profile credential must be an opaque handle")
    requirements = value.get("requirements")
    requirements = requirements if isinstance(requirements, Mapping) else {}
    return {
        "model_profile_id": profile_id,
        "display_name": str(value.get("display_name") or profile_id)[:200],
        "model_id": model_id,
        "requirements": _safe_requirements(requirements),
        "credential_handle": credential_handle,
        "parameters": _safe_scalars(value.get("parameters")),
        "enabled": bool(value.get("enabled", True)),
        "metadata": _safe_scalars(value.get("metadata")),
    }


def _safe_requirements(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "modalities",
        "capabilities",
        "tool_calling",
        "thinking",
        "minimum_context",
        "request_surface",
        "data_residency",
        "maximum_cost",
        "preferred_model_id",
        "preferred_provider_instance_id",
        "health_max_age",
    }
    return {
        str(key): item
        for key, item in value.items()
        if key in allowed and _json_safe(item)
    }


def _safe_scalars(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    blocked = {"secret", "token", "password", "api_key", "authorization"}
    return {
        str(key): item
        for key, item in value.items()
        if not any(marker in str(key).lower() for marker in blocked)
        and _json_safe(item)
    }


def _json_safe(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool, type(None))):
        return True
    if isinstance(value, list):
        return all(_json_safe(item) for item in value)
    if isinstance(value, Mapping):
        return all(_json_safe(item) for item in value.values())
    return False


def _identifier(value: Any) -> str:
    result = str(value or "").strip()
    if _SAFE_ID.fullmatch(result) is None:
        raise ValueError("model registry identifier is invalid")
    return result


def _hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

