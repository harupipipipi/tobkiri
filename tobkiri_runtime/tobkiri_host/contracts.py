"""Exact operation routing and capability-free structural adapter planning."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Protocol, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .errors import AdapterError, ResolutionError
from .models import (
    ArtifactVariant,
    ContractOperation,
    FunctionArtifact,
    OpaqueAuthorityRef,
    PackArtifact,
    require_digest,
)


@dataclass(frozen=True)
class OperationRoute:
    """ResolvedPlan entry pinning one operation to exact executable metadata."""

    contract_id: str
    operation_id: str
    artifact_digest: str
    function_id: str
    variant_id: str
    execution_domain_profile: str
    materialization_mode: str
    target_principal_ref: OpaqueAuthorityRef
    adapter_ids: tuple[str, ...] = ()
    catalog_digest: str | None = None
    platform: str | None = None
    architecture: str | None = None
    runtime_abi: str | None = None
    backend: str | None = None
    execution_kind: str | None = None
    domain_kind: str | None = None

    def __post_init__(self) -> None:
        require_digest(self.artifact_digest, "route artifact")
        if self.catalog_digest is not None:
            require_digest(self.catalog_digest, "route executable catalog")
        if self.materialization_mode not in {
            "eager",
            "continuous",
            "on_demand",
            "event_wake",
        }:
            raise ResolutionError("invalid materialization mode")


@dataclass(frozen=True)
class ResolvedOperationBinding:
    """Inventory-verified operation binding used by live dispatch."""

    artifact: PackArtifact
    function: FunctionArtifact
    variant: ArtifactVariant
    operation: ContractOperation
    route: OperationRoute
    principal_ref: OpaqueAuthorityRef


class OperationCatalog:
    """Resolve only routes already pinned by an immutable ResolvedPlan."""

    def __init__(
        self,
        artifacts: Sequence[PackArtifact],
        routes: Sequence[OperationRoute],
    ) -> None:
        self._artifacts = {artifact.digest: artifact for artifact in artifacts}
        self._bindings: dict[tuple[str, str], ResolvedOperationBinding] = {}
        if len(self._artifacts) != len(artifacts):
            raise ResolutionError("duplicate artifact digest")
        for route in routes:
            key = (route.contract_id, route.operation_id)
            if key in self._bindings:
                raise ResolutionError(f"ambiguous operation route: {key}")
            self._bindings[key] = self._bind(route)

    def _bind(self, route: OperationRoute) -> ResolvedOperationBinding:
        artifact = self._artifacts.get(route.artifact_digest)
        if artifact is None:
            raise ResolutionError("route references an artifact outside the plan")
        function = artifact.function(route.function_id)
        variants = [
            variant
            for variant in artifact.variants
            if variant.variant_id == route.variant_id
        ]
        if len(variants) != 1 or function.variant_id != route.variant_id:
            raise ResolutionError("route variant does not match Function inventory")
        variant = variants[0]
        if artifact.catalog_digest is not None:
            expected = {
                "catalog_digest": artifact.catalog_digest,
                "platform": variant.os,
                "architecture": variant.architecture,
                "runtime_abi": variant.runtime_abi,
                "backend": variant.backend,
                "execution_kind": variant.execution_kind.value,
                "domain_kind": variant.domain_kind,
            }
            actual = {
                "catalog_digest": route.catalog_digest,
                "platform": route.platform,
                "architecture": route.architecture,
                "runtime_abi": route.runtime_abi,
                "backend": route.backend,
                "execution_kind": route.execution_kind,
                "domain_kind": route.domain_kind,
            }
            if None in actual.values() or actual != expected:
                raise ResolutionError("route executable variant pin does not match artifact")
        operations = [
            operation
            for operation in function.operations
            if operation.contract_id == route.contract_id
            and operation.operation_id == route.operation_id
        ]
        if len(operations) != 1:
            raise ResolutionError("route operation is not in Function inventory")
        operation = operations[0]
        return ResolvedOperationBinding(
            artifact=artifact,
            function=function,
            variant=variant,
            operation=operation,
            route=route,
            principal_ref=route.target_principal_ref,
        )

    def resolve(
        self,
        contract_id: str,
        operation_id: str,
        version_range: str | None,
    ) -> ResolvedOperationBinding:
        """Return one exact binding without performing live provider discovery.

        An omitted caller constraint resolves against the exact Contract
        version already captured in the immutable plan. An explicit range is
        an additional constraint and cannot select a different binding.
        """
        binding = self._bindings.get((contract_id, operation_id))
        if binding is None:
            raise ResolutionError("operation is not pinned by the active plan")
        if version_range is None:
            version_range = self.pinned_version_range(contract_id, operation_id)
        try:
            specifier = SpecifierSet(version_range)
            version = Version(binding.operation.contract_version)
        except (InvalidSpecifier, InvalidVersion) as exc:
            raise ResolutionError("invalid Contract version constraint") from exc
        if version not in specifier:
            raise ResolutionError("pinned Contract version is incompatible")
        return binding

    def pinned_version_range(self, contract_id: str, operation_id: str) -> str:
        """Return an exact constraint for the version pinned by the active plan.

        This is the Host-owned path for callers that did not supply a Contract
        compatibility requirement.  It does not widen compatibility: the
        returned constraint names the one executable version already bound by
        the immutable ResolvedPlan.
        """
        binding = self._bindings.get((contract_id, operation_id))
        if binding is None:
            raise ResolutionError("operation is not pinned by the active plan")
        try:
            version = Version(binding.operation.contract_version)
        except InvalidVersion as exc:
            raise ResolutionError("invalid pinned Contract version") from exc
        return f"=={version}"

    def resolve_pinned(
        self,
        contract_id: str,
        operation_id: str,
    ) -> ResolvedOperationBinding:
        """Resolve against the exact Contract version in the active plan."""
        return self.resolve(
            contract_id,
            operation_id,
            self.pinned_version_range(contract_id, operation_id),
        )

    @staticmethod
    def validate_input(
        binding: ResolvedOperationBinding,
        payload: Mapping[str, Any],
    ) -> None:
        """Validate input before adapters or provider dispatch."""
        _validate_schema(binding.operation.input_schema, payload, "input")

    @staticmethod
    def validate_output(
        binding: ResolvedOperationBinding,
        payload: Mapping[str, Any],
    ) -> None:
        """Validate untrusted provider output before returning it."""
        _validate_schema(binding.operation.output_schema, payload, "output")


def _validate_schema(
    schema: Mapping[str, Any],
    value: Mapping[str, Any],
    label: str,
) -> None:
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        raise ResolutionError(f"operation {label} schema validation failed") from exc


@dataclass(frozen=True)
class StructuralAdapter:
    """Pinned metadata for a capability-free structural Wasm adapter."""

    adapter_id: str
    artifact_digest: str
    source_schema_digest: str
    target_schema_digest: str
    source_schema: Mapping[str, Any]
    target_schema: Mapping[str, Any]
    execution_kind: str = "wasm"
    pure: bool = True
    deterministic: bool = True
    bounded: bool = True
    network: bool = False
    secrets: bool = False
    stateful: bool = False
    external_effect: bool = False
    lossy: bool = False

    def __post_init__(self) -> None:
        require_digest(self.artifact_digest, "adapter artifact")
        require_digest(self.source_schema_digest, "adapter source schema")
        require_digest(self.target_schema_digest, "adapter target schema")
        if _mapping_digest(self.source_schema) != self.source_schema_digest:
            raise AdapterError("adapter source schema digest mismatch")
        if _mapping_digest(self.target_schema) != self.target_schema_digest:
            raise AdapterError("adapter target schema digest mismatch")


class AdapterExecutor(Protocol):
    """Capability-free execution port, normally backed by Wasmtime."""

    def execute(
        self,
        adapter: StructuralAdapter,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Execute a pinned adapter in a bounded, no-authority domain."""


class AdapterPlanner:
    """Validate structural adapter chains without executing Pack code."""

    def __init__(self, adapters: Sequence[StructuralAdapter]) -> None:
        self._adapters = {adapter.adapter_id: adapter for adapter in adapters}
        if len(self._adapters) != len(adapters):
            raise AdapterError("duplicate structural adapter ID")

    def plan(
        self,
        adapter_ids: Sequence[str],
        *,
        allow_lossy: bool = False,
    ) -> tuple[StructuralAdapter, ...]:
        """Return a valid maximum-two-hop structural adapter chain."""
        if len(adapter_ids) > 2:
            raise AdapterError("structural adapter chains are limited to two hops")
        if len(set(adapter_ids)) != len(adapter_ids):
            raise AdapterError("structural adapter cycle")
        result: list[StructuralAdapter] = []
        previous_target: str | None = None
        for adapter_id in adapter_ids:
            adapter = self._adapters.get(adapter_id)
            if adapter is None:
                raise AdapterError(f"adapter is not pinned: {adapter_id}")
            if not (
                adapter.execution_kind == "wasm"
                and adapter.pure
                and adapter.deterministic
                and adapter.bounded
                and not adapter.network
                and not adapter.secrets
                and not adapter.stateful
                and not adapter.external_effect
            ):
                raise AdapterError("adapter requires authority or is not structural")
            if adapter.lossy and not allow_lossy:
                raise AdapterError("lossy adapter requires explicit Profile opt-in")
            if previous_target is not None:
                if adapter.source_schema_digest != previous_target:
                    raise AdapterError("adapter schema chain is discontinuous")
            previous_target = adapter.target_schema_digest
            result.append(adapter)
        return tuple(result)

    def execute(
        self,
        plan: Sequence[StructuralAdapter],
        payload: Mapping[str, Any],
        executor: AdapterExecutor,
    ) -> Mapping[str, Any]:
        """Execute a previously validated chain through the restricted port."""
        current = dict(payload)
        for adapter in plan:
            _validate_schema(adapter.source_schema, current, "adapter input")
            current = dict(executor.execute(adapter, current))
            _validate_schema(adapter.target_schema, current, "adapter output")
        return current


def schema_digest(schema: Mapping[str, Any]) -> str:
    """Return the canonical digest used by structural adapter metadata."""
    return _mapping_digest(schema)


def _mapping_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
