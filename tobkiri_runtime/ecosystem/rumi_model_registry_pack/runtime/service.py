"""Model registry resource, mutation, alias, and migration contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from core_runtime.profile_paths import active_profile_id

from .registry import ModelRegistry


class ModelRegistryService:
    """Dispatch model registry operations for one explicit profile."""

    def __init__(self, *, user_data_root: Path | None = None) -> None:
        self.user_data_root = user_data_root

    def invoke(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke without provider-specific branches or secret values."""
        data = dict(payload)
        profile_id = str(data.get("profile_id") or active_profile_id() or "")
        if not profile_id:
            raise ValueError("profile_id is required")
        registry = ModelRegistry(profile_id, user_data_root=self.user_data_root)
        if operation == "list":
            return registry.snapshot()
        if operation == "get":
            value = registry.get(str(data.get("model_profile_id") or ""))
            if value is None:
                raise KeyError("model profile is unknown")
            return {"profile": value}
        if operation in {
            "resolve",
            "rumi_model_registry_pack.model-profile-resource.generate",
            "rumi_model_registry_pack.model-profile-resource.stream",
        }:
            value = registry.resolve(str(data.get("identifier") or ""))
            if value is None:
                raise KeyError("model profile or alias is unknown")
            return value
        if operation == "save":
            record = data.get("record")
            if not isinstance(record, Mapping):
                record = data
            return registry.save(
                record,
                expected_revision=int(data.get("expected_revision") or 0),
            )
        if operation == "delete":
            return registry.delete(
                str(data.get("model_profile_id") or ""),
                expected_revision=int(data.get("expected_revision") or 0),
            )
        if operation == "alias.set":
            return registry.set_alias(
                str(data.get("alias") or ""),
                str(data.get("target_profile_id") or ""),
                expected_revision=int(data.get("expected_revision") or 0),
            )
        if operation == "migration.apply":
            profiles = data.get("profiles")
            aliases = data.get("aliases")
            if not isinstance(profiles, list) or not isinstance(aliases, Mapping):
                raise ValueError("model registry migration payload is invalid")
            return registry.migrate(
                profiles,
                aliases,
                expected_source_hash=str(data.get("expected_source_hash") or ""),
            )
        if operation == "migration.rollback":
            return registry.rollback_migration(
                str(data.get("migration_id") or "")
            )
        raise ValueError(f"unknown model registry operation: {operation}")
