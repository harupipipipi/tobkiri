"""One-way, read-only projection from the legacy interface registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .canonical import content_identity
from .models import (
    Cardinality,
    ContractDescriptor,
    FailureSemantics,
    LifecycleMetadata,
    ProviderDescriptor,
    SecurityClassification,
)


class LegacyRegistry(Protocol):
    """Minimal legacy registry surface consumed by the projection."""

    def list(
        self,
        prefix: str | None = None,
        include_meta: bool = False,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class LegacyProjectionRule:
    """Explicit migration rule from a legacy prefix to a global contract."""

    legacy_prefix: str
    contract_id: str
    version: str = "1.0.0"
    cardinality: Cardinality = Cardinality.MANY
    removal_wave: int = 10
    sunset_at: str = "2027-12-31"


class LegacyRegistryProjection:
    """Project legacy registrations without mutating either registry."""

    def __init__(
        self,
        legacy_registry: LegacyRegistry,
        rules: tuple[LegacyProjectionRule, ...],
    ) -> None:
        self._legacy_registry = legacy_registry
        self._rules = rules

    def snapshot(self) -> tuple[ProviderDescriptor, ...]:
        """Return a deterministic data-only snapshot of configured rules."""
        projected: list[ProviderDescriptor] = []
        for rule in sorted(self._rules, key=lambda item: item.legacy_prefix):
            entries = self._legacy_registry.list(
                prefix=rule.legacy_prefix,
                include_meta=True,
            )
            for key, raw_entry in sorted(entries.items()):
                entry = raw_entry if isinstance(raw_entry, dict) else {}
                metadata = entry.get("last_meta") or {}
                owner = str(metadata.get("_source_pack_id", "legacy.unknown"))
                descriptor = ContractDescriptor(
                    contract_id=rule.contract_id,
                    version=rule.version,
                    cardinality=rule.cardinality,
                    security=SecurityClassification.INTERNAL,
                    failure=FailureSemantics.FAIL_CLOSED,
                    lifecycle=LifecycleMetadata(
                        introduced="3.0.0",
                        deprecated=True,
                        deprecated_at="2026-07-13",
                        sunset_at=rule.sunset_at,
                        data_owner="core_runtime.interface_registry",
                        migration_id=f"legacy:{key}",
                        rollback_id=f"legacy:{key}",
                    ),
                )
                projected.append(
                    ProviderDescriptor(
                        contract=descriptor,
                        provider_instance_id=f"legacy:{key}",
                        source_pack_id=owner,
                        source_pack_version=str(metadata.get("_source_pack_version", "0.0.0")),
                        content_hash=content_identity(
                            {"key": key, "owner": owner, "contract": rule.contract_id}
                        ),
                        build_identity="legacy-projection",
                        trust_class="untrusted",
                        isolation="in_process",
                    )
                )
        return tuple(
            sorted(projected, key=lambda item: item.provider_instance_id)
        )

