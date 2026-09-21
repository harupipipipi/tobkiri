"""Deterministic typed global contract registration and resolution."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict
from threading import RLock
from typing import Any

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
        self._providers: dict[str, dict[str, ProviderDescriptor]] = defaultdict(
            dict
        )
        self._providers_by_instance: dict[str, ProviderDescriptor] = {}
        self._operations: dict[str, Operation] = {}
        self._lock = RLock()

    def register(
        self,
        provider: ProviderDescriptor,
        operation: Operation | None = None,
    ) -> ContractResult[ProviderDescriptor]:
        """Register provider metadata and an optional activated operation."""
        if operation is not None and not callable(operation):
            return _provider_result(
                provider,
                ContractStatus.INCOMPATIBLE,
                diagnostics=("provider operation must be callable",),
            )
        with self._lock:
            if provider.provider_instance_id in self._providers_by_instance:
                return _provider_result(
                    provider,
                    ContractStatus.INCOMPATIBLE,
                    diagnostics=(
                        (
                            "duplicate provider instance: "
                            f"{provider.provider_instance_id}"
                        ),
                    ),
                )
            providers = self._providers[provider.contract.contract_id]
            providers[provider.provider_instance_id] = provider
            self._providers_by_instance[provider.provider_instance_id] = provider
            if operation is not None:
                self._operations[provider.provider_instance_id] = operation
        return _provider_result(provider, ContractStatus.OK, value=provider)

    def resolve(
        self,
        requirement: ContractRequirement,
        expected_revision: str | None = None,
    ) -> ContractResult[tuple[ProviderDescriptor, ...]]:
        """Resolve providers with deterministic cardinality semantics."""
        with self._lock:
            revision = self._resolution_identity_locked()
            candidates = tuple(
                self._providers.get(requirement.contract_id, {}).values()
            )
        if expected_revision is not None and expected_revision != revision:
            return _requirement_result(
                requirement,
                ContractStatus.STALE_RESOLUTION,
                diagnostics=(
                    (
                        f"expected registry revision {expected_revision}; "
                        f"found {revision}"
                    ),
                ),
                metadata={"revision": revision},
            )

        compatible = tuple(
            provider
            for provider in candidates
            if provider.contract.cardinality is requirement.cardinality
            and is_compatible(provider.contract.version, requirement.version_range)
        )
        if not compatible:
            if candidates:
                status = ContractStatus.INCOMPATIBLE
            elif (
                requirement.optional
                or requirement.cardinality is Cardinality.OPTIONAL
            ):
                status = ContractStatus.NOT_CONFIGURED
            else:
                status = ContractStatus.MISSING_PROVIDER
            return _requirement_result(
                requirement,
                status,
                diagnostics=(
                    (
                        f"no compatible provider for {requirement.contract_id} "
                        f"{requirement.version_range}"
                    ),
                ),
                metadata={"revision": revision},
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
                return _requirement_result(
                    requirement,
                    ContractStatus.INCOMPATIBLE,
                    diagnostics=(
                        "ambiguous one-provider resolution: "
                        + ", ".join(
                            item.provider_instance_id for item in tied
                        ),
                    ),
                    metadata={"revision": revision},
                )
            ordered = (ordered[0],)
        elif requirement.cardinality is Cardinality.KEYED:
            keyed = tuple(
                item
                for item in ordered
                if item.instance_key == requirement.instance_key
            )
            if len(keyed) != 1:
                return _requirement_result(
                    requirement,
                    ContractStatus.INCOMPATIBLE,
                    diagnostics=(
                        (
                            "expected one provider for key "
                            f"{requirement.instance_key!r}; found {len(keyed)}"
                        ),
                    ),
                    metadata={"revision": revision},
                )
            ordered = keyed
        elif requirement.cardinality is Cardinality.OPTIONAL:
            if len(ordered) > 1:
                return _requirement_result(
                    requirement,
                    ContractStatus.INCOMPATIBLE,
                    diagnostics=(
                        "optional contract resolved to multiple providers",
                    ),
                    metadata={"revision": revision},
                )
        elif requirement.cardinality is Cardinality.CHAIN:
            chain, diagnostic = _order_chain(ordered)
            if diagnostic is not None:
                return _requirement_result(
                    requirement,
                    ContractStatus.INCOMPATIBLE,
                    diagnostics=(diagnostic,),
                    metadata={"revision": revision},
                )
            ordered = chain

        provider_identity = (
            ordered[0].provider_instance_id
            if len(ordered) == 1
            else "multiple"
        )
        return ContractResult(
            status=ContractStatus.OK,
            contract_id=requirement.contract_id,
            version=requirement.version_range,
            provider_instance_id=provider_identity,
            value=ordered,
            metadata={"revision": revision},
        )

    def invoke(
        self,
        provider_instance_id: str,
        operation: str,
        payload: Mapping[str, Any],
        *,
        contract_id: str,
        contract_version: str,
        expected_revision: str | None = None,
    ) -> ContractResult[Any]:
        """Invoke an activated provider through a bound opaque identity."""
        with self._lock:
            revision = self._resolution_identity_locked()
            provider = self._providers_by_instance.get(provider_instance_id)
            handler = self._operations.get(provider_instance_id)
        if expected_revision is not None and expected_revision != revision:
            return ContractResult(
                status=ContractStatus.STALE_RESOLUTION,
                contract_id=contract_id,
                version=contract_version,
                provider_instance_id=provider_instance_id,
                diagnostics=(
                    (
                        f"expected registry revision {expected_revision}; "
                        f"found {revision}"
                    ),
                ),
                metadata={"revision": revision},
            )
        if provider is None:
            return ContractResult(
                status=ContractStatus.MISSING_PROVIDER,
                contract_id=contract_id,
                version=contract_version,
                provider_instance_id=provider_instance_id,
                diagnostics=("provider is not registered",),
                metadata={"revision": revision},
            )
        if (
            provider.contract.contract_id != contract_id
            or provider.contract.version != contract_version
        ):
            return ContractResult(
                status=ContractStatus.INCOMPATIBLE,
                contract_id=contract_id,
                version=contract_version,
                provider_instance_id=provider_instance_id,
                diagnostics=("opaque provider handle identity mismatch",),
                metadata={"revision": revision},
            )
        if not isinstance(operation, str) or not operation:
            return _provider_result(
                provider,
                ContractStatus.INCOMPATIBLE,
                diagnostics=("operation must be a non-empty string",),
                metadata={"revision": revision},
            )
        if not isinstance(payload, Mapping):
            return _provider_result(
                provider,
                ContractStatus.INCOMPATIBLE,
                diagnostics=("payload must be a mapping",),
                metadata={"revision": revision},
            )
        if handler is None:
            return _provider_result(
                provider,
                ContractStatus.UNAVAILABLE,
                diagnostics=("provider is not active",),
                metadata={"revision": revision},
            )
        try:
            value = handler(operation, dict(payload))
        except PermissionError as exc:
            return _provider_result(
                provider,
                ContractStatus.DENIED,
                diagnostics=(str(exc),),
                metadata={"revision": revision},
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return _provider_result(
                provider,
                ContractStatus.UNAVAILABLE,
                diagnostics=(
                    f"provider operation failed: {type(exc).__name__}",
                ),
                metadata={"revision": revision},
            )
        return _provider_result(
            provider,
            ContractStatus.OK,
            value=value,
            metadata={"revision": revision},
        )

    def snapshot(self) -> tuple[ProviderDescriptor, ...]:
        """Return deterministic data-only provider metadata."""
        with self._lock:
            providers = tuple(self._providers_by_instance.values())
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
        """Return a stable identity for the data-only registry snapshot."""
        with self._lock:
            return self._resolution_identity_locked()

    def _resolution_identity_locked(self) -> str:
        providers = sorted(
            self._providers_by_instance.values(),
            key=lambda provider: (
                provider.contract.contract_id,
                provider.provider_instance_id,
            ),
        )
        return content_identity([asdict(provider) for provider in providers])


def _provider_result(
    provider: ProviderDescriptor,
    status: ContractStatus,
    *,
    value: Any = None,
    diagnostics: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
) -> ContractResult[Any]:
    """Create a result bound to one provider descriptor."""
    return ContractResult(
        status=status,
        contract_id=provider.contract.contract_id,
        version=provider.contract.version,
        provider_instance_id=provider.provider_instance_id,
        value=value,
        diagnostics=diagnostics,
        metadata=metadata or {},
    )


def _requirement_result(
    requirement: ContractRequirement,
    status: ContractStatus,
    *,
    diagnostics: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
) -> ContractResult[tuple[ProviderDescriptor, ...]]:
    """Create an unresolved result bound to one requirement."""
    return ContractResult(
        status=status,
        contract_id=requirement.contract_id,
        version=requirement.version_range,
        provider_instance_id="unresolved",
        diagnostics=diagnostics,
        metadata=metadata or {},
    )


def _order_chain(
    providers: tuple[ProviderDescriptor, ...],
) -> tuple[tuple[ProviderDescriptor, ...], str | None]:
    """Topologically order a chain or return an actionable conflict."""
    by_id = {provider.provider_instance_id: provider for provider in providers}
    outgoing = {provider_id: set() for provider_id in by_id}
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
    available = [
        provider_id for provider_id, count in incoming.items() if count == 0
    ]
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
        cycle = sorted(
            provider_id for provider_id, count in incoming.items() if count
        )
        return (), "chain dependency cycle: " + ", ".join(cycle)
    return tuple(result), None
