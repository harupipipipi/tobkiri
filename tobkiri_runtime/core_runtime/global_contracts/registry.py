"""Deterministic typed global contract registration and resolution."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from threading import RLock
from typing import Any, Callable

from .canonical import content_identity
from .models import (
    Cardinality,
    ContractRequirement,
    ContractResult,
    ContractStatus,
    ProviderDescriptor,
)
from .semver import is_compatible

Operation = Callable[[str, dict[str, Any]], Any]


class ContractRegistry:
    """Authoritative in-memory registry for v3 contract projections.

    Provider metadata and executable operations are kept separately. Resolution
    returns opaque instance IDs, never source paths or imported modules.
    """

    def __init__(self) -> None:
        self._providers: dict[str, dict[str, ProviderDescriptor]] = defaultdict(dict)
        self._operations: dict[str, Operation] = {}
        self._lock = RLock()

    def register(
        self,
        provider: ProviderDescriptor,
        operation: Operation | None = None,
    ) -> ContractResult[ProviderDescriptor]:
        """Register provider metadata and an optional activated operation."""
        with self._lock:
            providers = self._providers[provider.contract.contract_id]
            if provider.provider_instance_id in providers:
                return ContractResult(
                    ContractStatus.INCOMPATIBLE,
                    diagnostics=(
                        f"duplicate provider instance: {provider.provider_instance_id}",
                    ),
                )
            providers[provider.provider_instance_id] = provider
            if operation is not None:
                self._operations[provider.provider_instance_id] = operation
        return ContractResult(ContractStatus.OK, value=provider)

    def resolve(
        self,
        requirement: ContractRequirement,
        expected_revision: str | None = None,
    ) -> ContractResult[tuple[ProviderDescriptor, ...]]:
        """Resolve providers with deterministic, explicit cardinality semantics."""
        with self._lock:
            revision = self._resolution_identity_locked()
            candidates = tuple(
                self._providers.get(requirement.contract_id, {}).values()
            )
        if expected_revision is not None and expected_revision != revision:
            return ContractResult(
                ContractStatus.STALE_RESOLUTION,
                diagnostics=(
                    f"expected registry revision {expected_revision}; found {revision}",
                ),
                metadata={"revision": revision},
            )
        compatible_items: list[ProviderDescriptor] = []
        invalid_versions: list[str] = []
        for provider in candidates:
            if provider.contract.cardinality is not requirement.cardinality:
                continue
            try:
                compatible_version = is_compatible(
                    provider.contract.version,
                    requirement.version_range,
                )
            except ValueError as exc:
                invalid_versions.append(str(exc))
                continue
            if compatible_version:
                compatible_items.append(provider)
        compatible = tuple(compatible_items)
        if not compatible:
            status = (
                ContractStatus.INCOMPATIBLE
                if candidates
                else ContractStatus.NOT_CONFIGURED
                if requirement.optional
                else ContractStatus.MISSING_PROVIDER
            )
            metadata: dict[str, Any] = {"revision": revision}
            conflict_report = _build_resolution_conflict(
                kind="incompatible_semantic",
                requirement=requirement,
                providers=candidates,
                revision=revision,
                diagnostics=(
                    f"no compatible provider for {requirement.contract_id} "
                    f"{requirement.version_range}",
                    *invalid_versions,
                ),
            )
            if conflict_report is not None:
                metadata["conflict_report"] = conflict_report
            return ContractResult(
                status,
                diagnostics=(
                    f"no compatible provider for {requirement.contract_id} "
                    f"{requirement.version_range}",
                    *invalid_versions,
                ),
                metadata=metadata,
            )
        ordered = tuple(
            sorted(
                compatible,
                key=lambda provider: (
                    -provider.priority,
                    provider.provider_instance_id,
                    provider.content_hash,
                ),
            )
        )
        if requirement.cardinality is Cardinality.ONE:
            top_priority = ordered[0].priority
            tied = tuple(item for item in ordered if item.priority == top_priority)
            if len(tied) != 1:
                ambiguity_diagnostic = (
                    "ambiguous one-provider resolution: "
                    + ", ".join(item.provider_instance_id for item in tied)
                )
                return ContractResult(
                    ContractStatus.INCOMPATIBLE,
                    diagnostics=(ambiguity_diagnostic,),
                    metadata={
                        "revision": revision,
                        "conflict_report": _build_resolution_conflict(
                            kind="ambiguous_one_provider",
                            requirement=requirement,
                            providers=tied,
                            revision=revision,
                            diagnostics=(ambiguity_diagnostic,),
                        ),
                    },
                )
            return ContractResult(
                ContractStatus.OK,
                value=(ordered[0],),
                metadata={"revision": revision},
            )
        if requirement.cardinality is Cardinality.KEYED:
            if not requirement.instance_key:
                return ContractResult(
                    ContractStatus.INCOMPATIBLE,
                    diagnostics=("keyed resolution requires instance_key",),
                )
            keyed = tuple(
                item for item in ordered if item.instance_key == requirement.instance_key
            )
            if len(keyed) != 1:
                return ContractResult(
                    ContractStatus.INCOMPATIBLE,
                    diagnostics=(
                        f"expected one provider for key {requirement.instance_key!r}; "
                        f"found {len(keyed)}",
                    ),
                )
            return ContractResult(
                ContractStatus.OK,
                value=keyed,
                metadata={"revision": revision},
            )
        if requirement.cardinality is Cardinality.OPTIONAL and len(ordered) > 1:
            return ContractResult(
                ContractStatus.INCOMPATIBLE,
                diagnostics=("optional contract resolved to multiple providers",),
            )
        if requirement.cardinality is Cardinality.CHAIN:
            chain, diagnostic = _order_chain(ordered)
            if diagnostic is not None:
                return ContractResult(
                    ContractStatus.INCOMPATIBLE,
                    diagnostics=(diagnostic,),
                    metadata={"revision": revision},
                )
            ordered = chain
        return ContractResult(
            ContractStatus.OK,
            value=ordered,
            metadata={"revision": revision},
        )

    def invoke(
        self,
        provider_instance_id: str,
        operation: str,
        payload: dict[str, Any],
    ) -> ContractResult[Any]:
        """Invoke an activated provider by opaque instance identity."""
        with self._lock:
            handler = self._operations.get(provider_instance_id)
        if handler is None:
            return ContractResult(
                ContractStatus.UNAVAILABLE,
                diagnostics=(f"provider is not active: {provider_instance_id}",),
            )
        try:
            value = handler(operation, dict(payload))
        except PermissionError as exc:
            return ContractResult(ContractStatus.DENIED, diagnostics=(str(exc),))
        except Exception as exc:
            return ContractResult(
                ContractStatus.UNAVAILABLE,
                diagnostics=(f"provider operation failed: {type(exc).__name__}",),
            )
        return ContractResult(ContractStatus.OK, value=value)

    def snapshot(self) -> tuple[ProviderDescriptor, ...]:
        """Return deterministic data-only provider metadata."""
        with self._lock:
            providers = [
                provider
                for contract_providers in self._providers.values()
                for provider in contract_providers.values()
            ]
        return tuple(
            sorted(
                providers,
                key=lambda provider: (
                    provider.contract.contract_id,
                    provider.provider_instance_id,
                ),
            )
        )

    def resolution_identity(self) -> str:
        """Return a stable identity for the current data-only registry snapshot."""
        with self._lock:
            return self._resolution_identity_locked()

    def _resolution_identity_locked(self) -> str:
        providers = [
            provider
            for contract_providers in self._providers.values()
            for provider in contract_providers.values()
        ]
        providers.sort(
            key=lambda provider: (
                provider.contract.contract_id,
                provider.provider_instance_id,
            )
        )
        return content_identity([asdict(provider) for provider in providers])


def _order_chain(
    providers: tuple[ProviderDescriptor, ...],
) -> tuple[tuple[ProviderDescriptor, ...], str | None]:
    """Topologically order a chain or return an actionable conflict."""
    by_id = {provider.provider_instance_id: provider for provider in providers}
    outgoing: dict[str, set[str]] = {provider_id: set() for provider_id in by_id}
    incoming = {provider_id: 0 for provider_id in by_id}
    for provider in providers:
        for target in provider.before:
            if target not in by_id:
                return (), f"unknown chain target in before: {target}"
            if target not in outgoing[provider.provider_instance_id]:
                outgoing[provider.provider_instance_id].add(target)
                incoming[target] += 1
        for source in provider.after:
            if source not in by_id:
                return (), f"unknown chain target in after: {source}"
            if provider.provider_instance_id not in outgoing[source]:
                outgoing[source].add(provider.provider_instance_id)
                incoming[provider.provider_instance_id] += 1
    available = [provider_id for provider_id, count in incoming.items() if count == 0]
    result: list[ProviderDescriptor] = []
    while available:
        available.sort(key=lambda item: (-by_id[item].priority, item))
        provider_id = available.pop(0)
        result.append(by_id[provider_id])
        for target in sorted(outgoing[provider_id]):
            incoming[target] -= 1
            if incoming[target] == 0:
                available.append(target)
    if len(result) != len(providers):
        cycle = sorted(provider_id for provider_id, count in incoming.items() if count)
        return (), "chain dependency cycle: " + ", ".join(cycle)
    return tuple(result), None


def _build_resolution_conflict(
    *,
    kind: str,
    requirement: ContractRequirement,
    providers: tuple[ProviderDescriptor, ...],
    revision: str,
    diagnostics: tuple[str, ...],
) -> dict[str, Any] | None:
    """Project a resolver failure without exposing provider implementation source."""

    if len(providers) < 2:
        return None
    # Local import keeps the contract model usable without initializing the repair
    # workspace and avoids giving conflict detection any generator capability.
    from ..pack_repair import build_pack_conflict_report

    return build_pack_conflict_report(
        kind=kind,
        profile_id="contract-registry",
        profile_fingerprint=revision,
        involved_packs=[
            {
                "pack_id": provider.source_pack_id,
                "version": provider.source_pack_version,
                "artifact_hash": provider.content_hash,
                "provider_instance_id": provider.provider_instance_id,
            }
            for provider in providers
        ],
        affected_contracts=(requirement.contract_id,),
        schemas=[
            schema
            for provider in providers
            for schema in (
                provider.contract.input_schema,
                provider.contract.output_schema,
                provider.contract.event_schema,
            )
            if schema is not None
        ],
        constraints=(requirement.version_range, requirement.cardinality.value),
        diagnostics=diagnostics,
    )
