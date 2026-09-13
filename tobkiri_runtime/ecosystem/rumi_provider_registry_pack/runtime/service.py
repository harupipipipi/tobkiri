"""Provider registry global contract service."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from core_runtime.profile_paths import active_profile_id

from .registry import ProviderRegistry


class ProviderRegistryService:
    """Dispatch provider registry operations for one profile."""

    def __init__(self, *, user_data_root: Path | None = None) -> None:
        self.user_data_root = user_data_root

    def invoke(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke a provider-neutral registry operation."""
        data = dict(payload)
        profile_id = str(data.get("profile_id") or active_profile_id() or "")
        if not profile_id:
            raise ValueError("profile_id is required")
        registry = ProviderRegistry(profile_id, user_data_root=self.user_data_root)
        if operation in {
            "list",
            "get",
            "rumi_provider_registry_pack.provider-registry-resource.generate",
            "rumi_provider_registry_pack.provider-registry-resource.stream",
        }:
            return registry.snapshot()
        if operation == "health":
            return registry.health()
        if operation == "save":
            record = data.get("record")
            return registry.save(
                record if isinstance(record, Mapping) else data,
                expected_revision=int(data.get("expected_revision") or 0),
            )
        if operation == "delete":
            return registry.delete(
                str(data.get("provider_instance_id") or ""),
                expected_revision=int(data.get("expected_revision") or 0),
            )
        if operation == "migration.apply":
            providers = data.get("providers")
            if not isinstance(providers, list):
                raise ValueError("provider migration payload is invalid")
            return registry.migrate(
                providers,
                expected_source_hash=str(data.get("expected_source_hash") or ""),
            )
        if operation == "migration.rollback":
            return registry.rollback_migration(str(data.get("migration_id") or ""))
        raise ValueError(f"unknown provider registry operation: {operation}")
