"""One-way, read-only projection from the legacy interface registry."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .canonical import content_identity
from .models import (
    Cardinality,
    ContractDescriptor,
    FailureSemantics,
    LifecycleMetadata,
    ProviderDescriptor,
    SecurityClassification,
)
from .semver import parse_version

_PACK_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")


class LegacyRegistry(Protocol):
    """Minimal legacy registry surface consumed by the projection."""

    def list(
        self,
        prefix: str | None = None,
        include_meta: bool = False,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class LegacyProjectionRule:
    """Explicit migration rule from a legacy prefix or exact key."""

    legacy_prefix: str
    contract_id: str
    version: str = "1.0.0"
    cardinality: Cardinality = Cardinality.MANY
    removal_wave: int = 10
    sunset_at: str = "2027-12-31"
    exact_key: bool = False

    def __post_init__(self) -> None:
        """Validate the rule through the same typed contract boundary."""
        if not self.legacy_prefix:
            raise ValueError("legacy_prefix must not be empty")
        if not 0 <= self.removal_wave <= 10:
            raise ValueError("removal_wave must be between 0 and 10")
        if not isinstance(self.exact_key, bool):
            raise TypeError("exact_key must be a boolean")
        ContractDescriptor(
            contract_id=self.contract_id,
            version=self.version,
            cardinality=self.cardinality,
            security=SecurityClassification.INTERNAL,
            failure=FailureSemantics.FAIL_CLOSED,
            lifecycle=LifecycleMetadata(
                introduced="3.0.0",
                deprecated=True,
                deprecated_at="2026-07-13",
                sunset_at=self.sunset_at,
            ),
        )


class LegacyRegistryProjection:
    """Project legacy registrations without mutating either registry."""

    def __init__(
        self,
        legacy_registry: LegacyRegistry,
        rules: tuple[LegacyProjectionRule, ...],
    ) -> None:
        if len({rule.legacy_prefix for rule in rules}) != len(rules):
            raise ValueError("legacy projection prefixes must be unique")
        self._legacy_registry = legacy_registry
        self._rules = tuple(rules)

    def snapshot(self) -> tuple[ProviderDescriptor, ...]:
        """Return a deterministic data-only snapshot of configured rules."""
        projected: list[ProviderDescriptor] = []
        for rule in sorted(self._rules, key=lambda item: item.legacy_prefix):
            entries = self._legacy_registry.list(
                prefix=rule.legacy_prefix,
                include_meta=True,
            )
            if not isinstance(entries, Mapping):
                raise TypeError("legacy registry list result must be a mapping")
            for raw_key, raw_entry in sorted(
                entries.items(), key=lambda item: str(item[0])
            ):
                key = str(raw_key)
                if rule.exact_key and key != rule.legacy_prefix:
                    continue
                entry = raw_entry if isinstance(raw_entry, Mapping) else {}
                raw_metadata = entry.get("last_meta")
                metadata = (
                    raw_metadata if isinstance(raw_metadata, Mapping) else {}
                )
                owner = _safe_pack_id(metadata.get("_source_pack_id"))
                source_version = _safe_version(
                    metadata.get("_source_pack_version")
                )
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
                opaque_id = content_identity({"legacy_key": key})[7:]
                projected.append(
                    ProviderDescriptor(
                        contract=descriptor,
                        provider_instance_id=f"legacy:{opaque_id}",
                        source_pack_id=owner,
                        source_pack_version=source_version,
                        content_hash=content_identity(
                            {
                                "key": key,
                                "owner": owner,
                                "contract": rule.contract_id,
                            }
                        ),
                        build_identity="legacy-projection",
                        trust_class="untrusted",
                        isolation="in_process",
                    )
                )
        return tuple(
            sorted(projected, key=lambda item: item.provider_instance_id)
        )


def _safe_pack_id(value: Any) -> str:
    """Return an evidence-only pack identity for untrusted legacy metadata."""
    candidate = str(value or "legacy.unknown")
    return candidate if _PACK_ID.fullmatch(candidate) else "legacy.unknown"


def _safe_version(value: Any) -> str:
    """Return a strict SemVer value for untrusted legacy metadata."""
    candidate = str(value or "0.0.0")
    try:
        parse_version(candidate)
    except ValueError:
        return "0.0.0"
    return candidate
