"""Data-only identities and requests for the Pack v4 execution path."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Mapping

from .errors import InvalidArtifactError

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def require_digest(value: str, field_name: str) -> None:
    """Reject any identity which is not an exact SHA-256 digest."""
    if _DIGEST.fullmatch(value) is None:
        raise InvalidArtifactError(f"{field_name} must be an exact sha256 digest")


def require_identifier(value: str, field_name: str) -> None:
    """Reject ambiguous or path-like identifiers."""
    if _IDENTIFIER.fullmatch(value) is None:
        raise InvalidArtifactError(f"invalid {field_name}: {value!r}")


class PackageKind(str, Enum):
    """Administrative package kind; it does not grant authority."""

    NORMAL = "normal"
    HOST_EXTENSION = "host_extension"
    RUNTIME_TCB = "runtime_tcb"


class EffectClass(str, Enum):
    """Broad UI hint; concrete authorization remains multi-axis."""

    PURE = "pure"
    READ = "read"
    WRITE = "write"
    EXTERNAL_EFFECT = "external_effect"
    PRIVILEGED = "privileged"


class MaterializationMode(str, Enum):
    """When a workload is physically instantiated."""

    EAGER = "eager"
    CONTINUOUS = "continuous"
    ON_DEMAND = "on_demand"
    EVENT_WAKE = "event_wake"


class ExecutionKind(str, Enum):
    """Supported execution containment categories."""

    WASM = "wasm"
    PACK_VM = "pack_vm"
    HOST_EXTENSION = "host_extension"
    REMOTE = "remote"


@dataclass(frozen=True)
class ArtifactVariant:
    """One prebuilt executable variant pinned by the resolved plan."""

    variant_id: str
    digest: str
    execution_kind: ExecutionKind
    os: str
    architecture: str
    runtime_abi: str
    backend: str
    prebuilt: bool = True
    domain_kind: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.variant_id, "variant_id")
        require_digest(self.digest, "variant digest")
        if not self.prebuilt:
            raise InvalidArtifactError("production variants must be prebuilt")


@dataclass(frozen=True)
class ContractOperation:
    """One public Contract operation implemented by one Function."""

    contract_id: str
    contract_version: str
    revision_digest: str
    operation_id: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    error_schema: Mapping[str, Any] | None = None
    progress_schema: Mapping[str, Any] | None = None
    effect_class: EffectClass = EffectClass.PURE
    timeout_default_ms: int = 30_000
    timeout_hard_max_ms: int = 60_000
    idempotency: str = "none"
    reconcile_operation: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.contract_id, "contract_id")
        require_identifier(self.operation_id, "operation_id")
        require_digest(self.revision_digest, "contract revision")
        if self.timeout_default_ms <= 0:
            raise InvalidArtifactError("default timeout must be positive")
        if self.timeout_hard_max_ms < self.timeout_default_ms:
            raise InvalidArtifactError("hard timeout cannot be below default")
        if self.idempotency not in {"none", "keyed", "replayable"}:
            raise InvalidArtifactError("unsupported idempotency mode")
        if self.reconcile_operation is not None:
            require_identifier(self.reconcile_operation, "reconcile_operation")


@dataclass(frozen=True)
class FunctionArtifact:
    """Verified Function subartifact and its complete public inventory."""

    function_id: str
    implementation_digest: str
    variant_id: str
    operations: tuple[ContractOperation, ...]
    exported: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.function_id, "function_id")
        require_digest(self.implementation_digest, "function implementation")
        require_identifier(self.variant_id, "variant_id")
        if self.exported and not self.operations:
            raise InvalidArtifactError("a public Function must expose an operation")
        if not self.exported and self.operations:
            raise InvalidArtifactError("a private Function cannot expose operations")
        keys = {(operation.contract_id, operation.operation_id) for operation in self.operations}
        if len(keys) != len(self.operations):
            raise InvalidArtifactError("duplicate operation in Function inventory")


@dataclass(frozen=True)
class PackArtifact:
    """Administrative Pack identity with exact Function/variant inventory."""

    pack_id: str
    version: str
    digest: str
    publisher_lineage: str
    package_kind: PackageKind
    functions: tuple[FunctionArtifact, ...]
    variants: tuple[ArtifactVariant, ...]
    catalog_digest: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.pack_id, "pack_id")
        require_digest(self.digest, "pack digest")
        require_identifier(self.publisher_lineage, "publisher_lineage")
        if len({item.function_id for item in self.functions}) != len(self.functions):
            raise InvalidArtifactError("duplicate Function ID in artifact")
        variant_ids = {item.variant_id for item in self.variants}
        if len(variant_ids) != len(self.variants):
            raise InvalidArtifactError("duplicate variant ID in artifact")
        missing = {
            function.variant_id
            for function in self.functions
            if function.variant_id not in variant_ids
        }
        if missing:
            raise InvalidArtifactError(
                f"Function inventory references unknown variants: {sorted(missing)}"
            )
        if self.catalog_digest is not None:
            require_digest(self.catalog_digest, "executable catalog")

    def function(self, function_id: str) -> FunctionArtifact:
        """Return an exact inventoried Function or fail closed."""
        matches = [item for item in self.functions if item.function_id == function_id]
        if len(matches) != 1:
            raise InvalidArtifactError(f"unknown Function: {function_id}")
        return matches[0]


@dataclass(frozen=True)
class OpaqueAuthorityRef:
    """Opaque reference owned and interpreted only by the authority core."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or len(self.value) > 512:
            raise ValueError("authority reference must be non-empty and bounded")


@dataclass(frozen=True)
class InvocationFrame:
    """Caller-controlled invocation fields; identity is intentionally absent."""

    contract_id: str
    version_range: str | None
    operation_id: str
    payload: Mapping[str, Any]
    timeout_ms: int | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.contract_id, "contract_id")
        require_identifier(self.operation_id, "operation_id")
        if self.timeout_ms is not None and self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")


@dataclass(frozen=True)
class RequestContext:
    """Host-authenticated request context captured at request start."""

    request_id: str
    trace_id: str
    caller_principal: OpaqueAuthorityRef
    profile_id: str
    activation_id: str
    activation_digest: str
    plan_digest: str
    security_epoch: int
    caller_session_id: str
    caller_domain_id: str
    caller_boot_epoch: int
    target_domain_id: str
    target_boot_epoch: int
    target_backend_digest: str
    profile_authority_digest: str
    fencing_token: int
    handle_namespace: str
    # Kept distinct from ``profile_id`` and the activation/plan digests.  Old
    # conformance callers may omit it; production capture always supplies the
    # signed ResolvedPlan profile revision.
    profile_revision: str = ""
    delegation_chain: tuple[OpaqueAuthorityRef, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        require_digest(self.activation_digest, "activation")
        require_digest(self.plan_digest, "plan")
        require_digest(self.profile_authority_digest, "profile authority")
        require_digest(self.target_backend_digest, "target backend")
        if self.profile_revision:
            require_digest(self.profile_revision, "profile revision")
        if (
            self.security_epoch <= 0
            or self.caller_boot_epoch <= 0
            or self.target_boot_epoch <= 0
            or self.fencing_token <= 0
        ):
            raise ValueError("epochs and fencing token must be positive")
        if len(self.delegation_chain) > 4:
            raise ValueError("delegation depth exceeds four")
        if len(set(self.delegation_chain)) != len(self.delegation_chain):
            raise ValueError("delegation cycle")


@dataclass(frozen=True)
class RuntimeEvidence:
    """Host-verified runtime evidence, not guest self-attestation."""

    domain_ref: OpaqueAuthorityRef
    executable_digest: str
    backend_digest: str
    authenticated_channel: bool
    nonce_fresh: bool
    platform: str | None = None
    isolation_profile: str | None = None
    attestation_digest: str | None = None
    domain_lease_id: str | None = None
    resource_reservation_id: str | None = None

    def __post_init__(self) -> None:
        require_digest(self.executable_digest, "evidence executable")
        require_digest(self.backend_digest, "evidence backend")
        if self.attestation_digest is not None:
            require_digest(self.attestation_digest, "evidence attestation")
