"""Normative ADR-014/015 authority identities and immutable records.

These models deliberately do not accept legacy principal strings.  Authority is
bound to exact artifact, function implementation, contract, operation, execution
domain, activation, and security-epoch identities.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


_DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")


class AuthorityValidationError(ValueError):
    """Raised when an authority object is malformed or would fail open."""


class AuthorityDenied(RuntimeError):
    """Raised when the v4 authority kernel denies an operation."""

    def __init__(self, reason: str, *, code: str = "authority_denied") -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class AuthorityMode(str, Enum):
    """How a Provider is permitted to exercise Host authority."""

    LEASE_ONLY = "lease_only"
    OS_ENTITLEMENT = "os_entitlement"


class DomainBoundary(str, Enum):
    """Host-verifiable execution boundaries accepted by ADR-014."""

    DEDICATED_PROCESS = "dedicated_process"
    WASM_COMPONENT = "wasm_component"
    UNPRIVILEGED_WORKER = "unprivileged_worker"
    AUTHORITY_EQUIVALENCE = "authority_equivalence"


class DomainState(str, Enum):
    """Execution-domain lifecycle states relevant to authorization."""

    STARTING = "starting"
    ACTIVE = "active"
    DRAINING = "draining"
    FENCED = "fenced"
    REVOKED = "revoked"
    STOPPED = "stopped"


class GrantLifetime(str, Enum):
    """Supported persisted Grant lifetimes."""

    ONE_SHOT = "one_shot"
    SESSION = "session"
    WORKFLOW_REVISION = "workflow_revision"
    PERSISTENT_PROFILE = "persistent_profile"
    POLICY_EPHEMERAL = "policy_ephemeral"


class LeaseState(str, Enum):
    """Crash-recoverable InvocationLease state."""

    ISSUED = "issued"
    DISPATCHED = "dispatched"
    COMMITTED = "committed"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, order=True)
class SecurityEpoch:
    """Host-owned monotonic emergency-revocation epoch."""

    value: int
    advanced_at: float
    reason_digest: str

    def __post_init__(self) -> None:
        _require_positive_int("SecurityEpoch", self.value)
        _require_finite_time("advanced_at", self.advanced_at)
        _require_digest("reason_digest", self.reason_digest)


def canonical_json(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON used by authority digests.

    This is intentionally a narrow canonical form for internal records.  Protocol
    JCS canonicalization remains a separate concern.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def authority_digest(value: Any) -> str:
    """Return a tagged SHA-256 digest for an authority object."""

    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def _require_digest(name: str, value: str) -> None:
    if not _DIGEST_RE.fullmatch(str(value or "")):
        raise AuthorityValidationError(f"{name} must be an exact SHA-256 digest")


def _require_id(name: str, value: str) -> None:
    if not _ID_RE.fullmatch(str(value or "")):
        raise AuthorityValidationError(f"{name} is invalid")


def _require_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AuthorityValidationError(f"{name} must be a positive integer")


def _require_finite_time(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuthorityValidationError(f"{name} must be a finite timestamp")
    if not math.isfinite(float(value)) or value < 0:
        raise AuthorityValidationError(f"{name} must be a finite timestamp")


def _immutable_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True, order=True)
class FunctionPrincipal:
    """Exact Function/Operation authority principal from a verified artifact."""

    parent_artifact_digest: str
    function_implementation_digest: str
    function_id: str
    contract_revision_digest: str
    operation_id: str

    def __post_init__(self) -> None:
        _require_digest("parent_artifact_digest", self.parent_artifact_digest)
        _require_digest("function_implementation_digest", self.function_implementation_digest)
        _require_digest("contract_revision_digest", self.contract_revision_digest)
        _require_id("function_id", self.function_id)
        _require_id("operation_id", self.operation_id)

    @property
    def principal_id(self) -> str:
        """Return the stable digest identifier for this exact principal."""

        return authority_digest(self.to_dict())

    def to_dict(self) -> dict[str, str]:
        """Serialize the principal without derived fields."""

        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FunctionPrincipal":
        """Parse and validate an exact principal."""

        return cls(
            parent_artifact_digest=str(value.get("parent_artifact_digest") or ""),
            function_implementation_digest=str(value.get("function_implementation_digest") or ""),
            function_id=str(value.get("function_id") or ""),
            contract_revision_digest=str(value.get("contract_revision_digest") or ""),
            operation_id=str(value.get("operation_id") or ""),
        )


@dataclass(frozen=True)
class AuthorityScope:
    """Declarative, comparable scope for one Capability/Effect.

    Each dimension is a finite set of canonical values.  ``"*"`` is the only
    explicit wildcard.  An omitted dimension has no restriction and is therefore
    equivalent to a wildcard for lattice comparisons.  Quotas are upper bounds;
    an omitted quota has no upper bound.  Opaque semantics require an exact request
    digest and are therefore one-request scopes.
    """

    capability: str
    semantics_digest: str
    dimensions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    quotas: Mapping[str, int] = field(default_factory=dict)
    exact_request_digest: str | None = None
    opaque: bool = False

    def __post_init__(self) -> None:
        _require_id("capability", self.capability)
        _require_digest("semantics_digest", self.semantics_digest)
        normalized_dimensions: dict[str, tuple[str, ...]] = {}
        for name, raw_values in dict(self.dimensions).items():
            _require_id("scope dimension", str(name))
            if not isinstance(raw_values, (list, tuple, set, frozenset)) or any(
                not isinstance(item, str) for item in raw_values
            ):
                raise AuthorityValidationError("scope dimension values must be strings")
            values = tuple(sorted(set(raw_values)))
            if not values or any(not item for item in values):
                raise AuthorityValidationError("scope dimensions cannot be empty")
            if "*" in values and values != ("*",):
                raise AuthorityValidationError("scope wildcard cannot mix with values")
            normalized_dimensions[str(name)] = values
        normalized_quotas: dict[str, int] = {}
        for name, raw_value in dict(self.quotas).items():
            _require_id("scope quota", str(name))
            if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 0:
                raise AuthorityValidationError("scope quotas must be non-negative integers")
            normalized_quotas[str(name)] = raw_value
        if self.exact_request_digest is not None:
            _require_digest("exact_request_digest", self.exact_request_digest)
        if self.opaque and self.exact_request_digest is None:
            raise AuthorityValidationError("opaque scope requires an exact request digest")
        object.__setattr__(self, "dimensions", _immutable_mapping(normalized_dimensions))
        object.__setattr__(self, "quotas", _immutable_mapping(normalized_quotas))

    @property
    def digest(self) -> str:
        """Return the digest of the normalized scope."""

        return authority_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """Serialize normalized scope data."""

        return {
            "capability": self.capability,
            "semantics_digest": self.semantics_digest,
            "dimensions": {key: list(value) for key, value in self.dimensions.items()},
            "quotas": dict(self.quotas),
            "exact_request_digest": self.exact_request_digest,
            "opaque": self.opaque,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AuthorityScope":
        """Parse a declarative scope, rejecting unknown structural types."""

        allowed_keys = {
            "capability",
            "semantics_digest",
            "dimensions",
            "quotas",
            "exact_request_digest",
            "opaque",
        }
        if set(value) - allowed_keys:
            raise AuthorityValidationError("scope contains unknown fields")
        dimensions = value.get("dimensions", {})
        quotas = value.get("quotas", {})
        if not isinstance(dimensions, Mapping) or not isinstance(quotas, Mapping):
            raise AuthorityValidationError("scope dimensions and quotas must be objects")
        if any(
            not isinstance(items, (list, tuple, set, frozenset)) for items in dimensions.values()
        ):
            raise AuthorityValidationError("scope dimension values must be arrays")
        if any(not isinstance(item, str) for items in dimensions.values() for item in items):
            raise AuthorityValidationError("scope dimension values must be strings")
        if any(isinstance(item, bool) or not isinstance(item, int) for item in quotas.values()):
            raise AuthorityValidationError("scope quotas must be integers")
        return cls(
            capability=str(value.get("capability") or ""),
            semantics_digest=str(value.get("semantics_digest") or ""),
            dimensions={str(key): tuple(items) for key, items in dimensions.items()},
            quotas={str(key): item for key, item in quotas.items()},
            exact_request_digest=(
                str(value.get("exact_request_digest"))
                if value.get("exact_request_digest") is not None
                else None
            ),
            opaque=value.get("opaque") is True,
        )

    def is_subset_of(self, ceiling: "AuthorityScope") -> bool:
        """Return whether this scope is safely within ``ceiling``.

        Missing dimensions and quotas are unbounded (the lattice top), never an
        empty request.  Consequently an omitted or wildcard request dimension
        cannot fit within a finite ceiling, and an omitted request quota cannot fit
        within a bounded quota.  A finite request remains within an omitted ceiling
        because that ceiling imposes no restriction for the key.
        """

        if (
            self.capability != ceiling.capability
            or self.semantics_digest != ceiling.semantics_digest
        ):
            return False
        if ceiling.exact_request_digest is not None:
            if self.exact_request_digest != ceiling.exact_request_digest:
                return False
        if self.opaque != ceiling.opaque and (self.opaque or ceiling.opaque):
            return False
        dimension_names = set(self.dimensions) | set(ceiling.dimensions)
        for name in dimension_names:
            requested = self.dimensions.get(name)
            allowed = ceiling.dimensions.get(name)
            requested_is_unbounded = requested is None or requested == ("*",)
            allowed_is_unbounded = allowed is None or allowed == ("*",)
            if requested_is_unbounded:
                if not allowed_is_unbounded:
                    return False
            elif (
                requested is not None
                and allowed is not None
                and not allowed_is_unbounded
                and not set(requested).issubset(allowed)
            ):
                return False
        quota_names = set(self.quotas) | set(ceiling.quotas)
        for quota_name in quota_names:
            requested_quota = self.quotas.get(quota_name)
            allowed_quota = ceiling.quotas.get(quota_name)
            if requested_quota is None:
                if allowed_quota is not None:
                    return False
            elif allowed_quota is not None and requested_quota > allowed_quota:
                return False
        return True


def intersect_scopes(*scopes: AuthorityScope) -> AuthorityScope:
    """Compute the fail-closed effective intersection of compatible scopes."""

    if not scopes:
        raise AuthorityValidationError("at least one scope is required")
    first = scopes[0]
    if any(
        scope.capability != first.capability or scope.semantics_digest != first.semantics_digest
        for scope in scopes[1:]
    ):
        raise AuthorityValidationError("scope semantics do not match")

    dimension_names = set().union(*(scope.dimensions for scope in scopes))
    dimensions: dict[str, tuple[str, ...]] = {}
    for name in sorted(dimension_names):
        values: set[str] | None = None
        for scope in scopes:
            current = scope.dimensions.get(name)
            if current is None or current == ("*",):
                continue
            values = set(current) if values is None else values & set(current)
        dimensions[name] = ("*",) if values is None else tuple(sorted(values))
        if not dimensions[name]:
            raise AuthorityValidationError("scope intersection is empty")

    quota_names = set().union(*(scope.quotas for scope in scopes))
    quotas = {
        name: min(scope.quotas[name] for scope in scopes if name in scope.quotas)
        for name in quota_names
    }

    exact_values = {scope.exact_request_digest for scope in scopes if scope.exact_request_digest}
    if len(exact_values) > 1:
        raise AuthorityValidationError("exact request bindings conflict")
    opaque_values = {scope.opaque for scope in scopes}
    if len(opaque_values) > 1:
        raise AuthorityValidationError("opaque and declarative semantics cannot mix")
    return AuthorityScope(
        capability=first.capability,
        semantics_digest=first.semantics_digest,
        dimensions=dimensions,
        quotas=quotas,
        exact_request_digest=next(iter(exact_values), None),
        opaque=first.opaque,
    )


@dataclass(frozen=True)
class AuthorityEquivalence:
    """Security-relevant properties required for explicit co-location."""

    provider_ceiling_digest: str
    scope_class: str
    trust_class: str
    os_entitlements: tuple[str, ...] = ()
    handle_classes: tuple[str, ...] = ()
    credential_access: tuple[str, ...] = ()
    background_policy: str = "deny"
    lifecycle_class: str = "request"

    def __post_init__(self) -> None:
        _require_digest("provider_ceiling_digest", self.provider_ceiling_digest)
        for name, value in (
            ("scope_class", self.scope_class),
            ("trust_class", self.trust_class),
            ("background_policy", self.background_policy),
            ("lifecycle_class", self.lifecycle_class),
        ):
            _require_id(name, value)
        for attribute in ("os_entitlements", "handle_classes", "credential_access"):
            normalized = tuple(sorted({str(item) for item in getattr(self, attribute)}))
            object.__setattr__(self, attribute, normalized)

    @property
    def digest(self) -> str:
        """Return the complete authority-equivalence digest."""

        return authority_digest(asdict(self))


@dataclass(frozen=True)
class ExecutionDomain:
    """Host-assigned and Host-authenticated enforcement domain."""

    domain_id: str
    profile_id: str
    activation_id: str
    boot_epoch: int
    process_identity: str
    authenticated_channel_digest: str
    sandbox_profile_digest: str
    resource_namespace: str
    principals: tuple[FunctionPrincipal, ...]
    boundary: DomainBoundary
    security_epoch: int
    state: DomainState = DomainState.ACTIVE
    equivalence: AuthorityEquivalence | None = None
    mutual_colocation_principals: tuple[str, ...] = ()
    fencing_token: int = 1

    def __post_init__(self) -> None:
        _require_id("domain_id", self.domain_id)
        _require_id("profile_id", self.profile_id)
        _require_id("activation_id", self.activation_id)
        _require_id("process_identity", self.process_identity)
        _require_digest("authenticated_channel_digest", self.authenticated_channel_digest)
        _require_digest("sandbox_profile_digest", self.sandbox_profile_digest)
        _require_id("resource_namespace", self.resource_namespace)
        _require_positive_int("boot_epoch", self.boot_epoch)
        _require_positive_int("security_epoch", self.security_epoch)
        _require_positive_int("fencing_token", self.fencing_token)
        if not self.principals:
            raise AuthorityValidationError("execution domain requires an exact principal")
        principal_ids = tuple(sorted({item.principal_id for item in self.principals}))
        if len(principal_ids) != len(self.principals):
            raise AuthorityValidationError("execution domain principals must be unique")
        mutual = tuple(sorted({str(item) for item in self.mutual_colocation_principals}))
        object.__setattr__(self, "mutual_colocation_principals", mutual)
        if len(self.principals) > 1:
            if self.boundary is not DomainBoundary.AUTHORITY_EQUIVALENCE:
                raise AuthorityValidationError(
                    "multiple authority principals require an equivalence domain"
                )
            if self.equivalence is None or set(principal_ids) != set(mutual):
                raise AuthorityValidationError(
                    "co-location requires complete mutual principal approval"
                )
        if self.boundary is DomainBoundary.AUTHORITY_EQUIVALENCE and self.equivalence is None:
            raise AuthorityValidationError("equivalence domain requires security properties")

    @property
    def principal_ids(self) -> frozenset[str]:
        """Return principals fixed to this domain by Host routing."""

        return frozenset(item.principal_id for item in self.principals)

    @property
    def identity_digest(self) -> str:
        """Return a digest covering identity and enforcement configuration."""

        return authority_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """Serialize the domain record."""

        return {
            "domain_id": self.domain_id,
            "profile_id": self.profile_id,
            "activation_id": self.activation_id,
            "boot_epoch": self.boot_epoch,
            "process_identity": self.process_identity,
            "authenticated_channel_digest": self.authenticated_channel_digest,
            "sandbox_profile_digest": self.sandbox_profile_digest,
            "resource_namespace": self.resource_namespace,
            "principals": [item.to_dict() for item in self.principals],
            "boundary": self.boundary.value,
            "security_epoch": self.security_epoch,
            "state": self.state.value,
            "equivalence": asdict(self.equivalence) if self.equivalence else None,
            "mutual_colocation_principals": list(self.mutual_colocation_principals),
            "fencing_token": self.fencing_token,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionDomain":
        """Parse and validate an execution-domain record."""

        equivalence_raw = value.get("equivalence")
        return cls(
            domain_id=str(value.get("domain_id") or ""),
            profile_id=str(value.get("profile_id") or ""),
            activation_id=str(value.get("activation_id") or ""),
            boot_epoch=int(value.get("boot_epoch") or 0),
            process_identity=str(value.get("process_identity") or ""),
            authenticated_channel_digest=str(value.get("authenticated_channel_digest") or ""),
            sandbox_profile_digest=str(value.get("sandbox_profile_digest") or ""),
            resource_namespace=str(value.get("resource_namespace") or ""),
            principals=tuple(
                FunctionPrincipal.from_dict(item)
                for item in value.get("principals", [])
                if isinstance(item, Mapping)
            ),
            boundary=DomainBoundary(str(value.get("boundary") or "")),
            security_epoch=int(value.get("security_epoch") or 0),
            state=DomainState(str(value.get("state") or DomainState.ACTIVE.value)),
            equivalence=(
                AuthorityEquivalence(**dict(equivalence_raw))
                if isinstance(equivalence_raw, Mapping)
                else None
            ),
            mutual_colocation_principals=tuple(
                str(item) for item in value.get("mutual_colocation_principals", [])
            ),
            fencing_token=int(value.get("fencing_token") or 0),
        )


@dataclass(frozen=True)
class ProviderAuthorityRecord:
    """Maximum authority of one exact Provider Function/Operation."""

    record_id: str
    provider: FunctionPrincipal
    execution_domain_id: str
    execution_domain_identity_digest: str
    scope: AuthorityScope
    authority_mode: AuthorityMode
    security_epoch: int
    trust_provenance_digest: str
    publisher_lineage: str
    host_extension_id: str
    valid_from: float
    expires_at: float | None = None
    os_entitlements: tuple[str, ...] = ()
    host_broker_binding: str | None = None
    background_allowed: bool = False
    network_allowed: bool = False
    process_allowed: bool = False
    revoked: bool = False

    def __post_init__(self) -> None:
        _require_id("record_id", self.record_id)
        _require_id("execution_domain_id", self.execution_domain_id)
        _require_digest("execution_domain_identity_digest", self.execution_domain_identity_digest)
        _require_digest("trust_provenance_digest", self.trust_provenance_digest)
        _require_id("publisher_lineage", self.publisher_lineage)
        _require_id("host_extension_id", self.host_extension_id)
        _require_positive_int("security_epoch", self.security_epoch)
        _require_finite_time("valid_from", self.valid_from)
        if self.expires_at is not None:
            _require_finite_time("expires_at", self.expires_at)
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise AuthorityValidationError("provider authority expiry is invalid")
        if self.authority_mode is AuthorityMode.LEASE_ONLY:
            if self.os_entitlements or self.host_broker_binding is None:
                raise AuthorityValidationError(
                    "lease_only authority requires a Host Broker and no OS entitlement"
                )
        else:
            if not self.os_entitlements:
                raise AuthorityValidationError(
                    "os_entitlement authority requires explicit entitlements"
                )

    @property
    def digest(self) -> str:
        """Return the immutable provider-authority digest."""

        return authority_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """Serialize this immutable record."""

        return {
            "record_id": self.record_id,
            "provider": self.provider.to_dict(),
            "execution_domain_id": self.execution_domain_id,
            "execution_domain_identity_digest": self.execution_domain_identity_digest,
            "scope": self.scope.to_dict(),
            "authority_mode": self.authority_mode.value,
            "security_epoch": self.security_epoch,
            "trust_provenance_digest": self.trust_provenance_digest,
            "publisher_lineage": self.publisher_lineage,
            "host_extension_id": self.host_extension_id,
            "valid_from": self.valid_from,
            "expires_at": self.expires_at,
            "os_entitlements": list(self.os_entitlements),
            "host_broker_binding": self.host_broker_binding,
            "background_allowed": self.background_allowed,
            "network_allowed": self.network_allowed,
            "process_allowed": self.process_allowed,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderAuthorityRecord":
        """Parse a ProviderAuthorityRecord."""

        return cls(
            **{
                **dict(value),
                "provider": FunctionPrincipal.from_dict(value["provider"]),
                "scope": AuthorityScope.from_dict(value["scope"]),
                "authority_mode": AuthorityMode(str(value["authority_mode"])),
                "os_entitlements": tuple(value.get("os_entitlements", [])),
            }
        )


@dataclass(frozen=True)
class HostExtensionTrustRecord:
    """Administrative trust to register explicitly listed Provider principals."""

    trust_id: str
    parent_artifact_digest: str
    publisher_lineage: str
    provider_principal_ids: tuple[str, ...]
    trust_provenance_digest: str
    security_epoch: int
    valid_from: float
    expires_at: float | None = None
    package_kind: str = "host_extension"
    revoked: bool = False

    def __post_init__(self) -> None:
        _require_id("trust_id", self.trust_id)
        _require_digest("parent_artifact_digest", self.parent_artifact_digest)
        _require_id("publisher_lineage", self.publisher_lineage)
        _require_digest("trust_provenance_digest", self.trust_provenance_digest)
        _require_positive_int("security_epoch", self.security_epoch)
        _require_finite_time("valid_from", self.valid_from)
        if self.expires_at is not None:
            _require_finite_time("expires_at", self.expires_at)
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise AuthorityValidationError("Host Extension trust expiry is invalid")
        if self.package_kind != "host_extension":
            raise AuthorityValidationError("Host Extension trust has invalid package kind")
        normalized = tuple(sorted(set(self.provider_principal_ids)))
        if not normalized:
            raise AuthorityValidationError("Host Extension trust requires Providers")
        for principal_id in normalized:
            _require_digest("provider_principal_id", principal_id)
        object.__setattr__(self, "provider_principal_ids", normalized)

    @property
    def digest(self) -> str:
        """Return the immutable Host Extension trust digest."""

        return authority_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """Serialize trust without expanding it to a Pack-wide wildcard."""

        value = asdict(self)
        value["provider_principal_ids"] = list(self.provider_principal_ids)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HostExtensionTrustRecord":
        """Parse a HostExtensionTrustRecord."""

        return cls(
            **{
                **dict(value),
                "provider_principal_ids": tuple(value.get("provider_principal_ids", [])),
            }
        )


@dataclass(frozen=True)
class ApprovalRecord:
    """Immutable decision provenance; never runtime authority."""

    approval_id: str
    snapshot_digest: str
    actor_id: str
    decision: str
    decided_at: float
    caller: FunctionPrincipal
    target: FunctionPrincipal
    profile_id: str
    effect_bundle_digest: str
    security_epoch: int

    def __post_init__(self) -> None:
        _require_id("approval_id", self.approval_id)
        _require_digest("snapshot_digest", self.snapshot_digest)
        _require_id("actor_id", self.actor_id)
        if self.decision not in {"approved", "denied"}:
            raise AuthorityValidationError("approval decision is invalid")
        _require_id("profile_id", self.profile_id)
        _require_digest("effect_bundle_digest", self.effect_bundle_digest)
        _require_positive_int("security_epoch", self.security_epoch)
        _require_finite_time("decided_at", self.decided_at)

    @property
    def digest(self) -> str:
        """Return the immutable approval digest."""

        return authority_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """Serialize decision provenance."""

        value = asdict(self)
        value["caller"] = self.caller.to_dict()
        value["target"] = self.target.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApprovalRecord":
        """Parse an immutable ApprovalRecord."""

        return cls(
            **{
                **dict(value),
                "caller": FunctionPrincipal.from_dict(value["caller"]),
                "target": FunctionPrincipal.from_dict(value["target"]),
            }
        )


@dataclass(frozen=True)
class GrantRecord:
    """Caller-specific persisted use authority."""

    grant_id: str
    caller: FunctionPrincipal
    target: FunctionPrincipal
    profile_id: str
    activation_id: str
    profile_authority_digest: str
    caller_publisher_lineage: str
    target_publisher_lineage: str
    scope: AuthorityScope
    lifetime: GrantLifetime
    security_epoch: int
    approval_id: str | None
    issued_at: float
    expires_at: float | None = None
    max_uses: int | None = None
    session_id: str | None = None
    workflow_revision_digest: str | None = None
    delegation_allowed: bool = False
    max_delegation_depth: int = 0
    revoked: bool = False

    def __post_init__(self) -> None:
        _require_id("grant_id", self.grant_id)
        _require_id("profile_id", self.profile_id)
        _require_id("activation_id", self.activation_id)
        _require_digest("profile_authority_digest", self.profile_authority_digest)
        _require_id("caller_publisher_lineage", self.caller_publisher_lineage)
        _require_id("target_publisher_lineage", self.target_publisher_lineage)
        if self.approval_id is not None:
            _require_id("approval_id", self.approval_id)
        _require_positive_int("security_epoch", self.security_epoch)
        _require_finite_time("issued_at", self.issued_at)
        if self.expires_at is not None:
            _require_finite_time("expires_at", self.expires_at)
        if self.expires_at is not None and self.expires_at <= self.issued_at:
            raise AuthorityValidationError("grant expiry is invalid")
        if self.max_uses is not None:
            _require_positive_int("max_uses", self.max_uses)
        if self.lifetime is GrantLifetime.ONE_SHOT and self.max_uses != 1:
            raise AuthorityValidationError("one-shot Grant must have max_uses=1")
        if self.scope.opaque and self.lifetime is not GrantLifetime.ONE_SHOT:
            raise AuthorityValidationError("opaque semantics permit one-shot Grants only")
        if self.lifetime is GrantLifetime.SESSION and not self.session_id:
            raise AuthorityValidationError("session Grant requires session_id")
        if self.lifetime is GrantLifetime.WORKFLOW_REVISION and not self.workflow_revision_digest:
            raise AuthorityValidationError("workflow Grant requires a workflow revision digest")
        if self.workflow_revision_digest:
            _require_digest("workflow_revision_digest", self.workflow_revision_digest)
        if not self.delegation_allowed and self.max_delegation_depth != 0:
            raise AuthorityValidationError("delegation defaults to max_depth=0")
        if (
            isinstance(self.max_delegation_depth, bool)
            or not isinstance(self.max_delegation_depth, int)
            or self.max_delegation_depth < 0
            or self.max_delegation_depth > 4
        ):
            raise AuthorityValidationError("delegation depth must be between 0 and 4")

    @property
    def digest(self) -> str:
        """Return the immutable Grant definition digest."""

        return authority_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """Serialize the Grant definition."""

        return {
            "grant_id": self.grant_id,
            "caller": self.caller.to_dict(),
            "target": self.target.to_dict(),
            "profile_id": self.profile_id,
            "activation_id": self.activation_id,
            "profile_authority_digest": self.profile_authority_digest,
            "caller_publisher_lineage": self.caller_publisher_lineage,
            "target_publisher_lineage": self.target_publisher_lineage,
            "scope": self.scope.to_dict(),
            "lifetime": self.lifetime.value,
            "security_epoch": self.security_epoch,
            "approval_id": self.approval_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
            "session_id": self.session_id,
            "workflow_revision_digest": self.workflow_revision_digest,
            "delegation_allowed": self.delegation_allowed,
            "max_delegation_depth": self.max_delegation_depth,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GrantRecord":
        """Parse a GrantRecord."""

        return cls(
            **{
                **dict(value),
                "caller": FunctionPrincipal.from_dict(value["caller"]),
                "target": FunctionPrincipal.from_dict(value["target"]),
                "scope": AuthorityScope.from_dict(value["scope"]),
                "lifetime": GrantLifetime(str(value["lifetime"])),
            }
        )


@dataclass(frozen=True)
class InvocationLease:
    """Short-lived, single-use, non-transferable request authority."""

    lease_id: str
    request_id: str
    caller: FunctionPrincipal
    target: FunctionPrincipal
    caller_domain_id: str
    caller_boot_epoch: int
    target_domain_id: str
    target_boot_epoch: int
    request_digest: str
    effect_digest: str
    authorized_scope: AuthorityScope
    resource_namespace: str
    profile_id: str
    activation_id: str
    activation_digest: str
    plan_digest: str
    profile_authority_digest: str
    fencing_token: int
    caller_publisher_lineage: str
    target_publisher_lineage: str
    host_extension_id: str
    provider_authority_id: str
    provider_authority_digest: str
    grant_id: str
    audit_reservation_id: str
    security_epoch: int
    issued_at: float
    expires_at: float
    call_chain: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_id("lease_id", self.lease_id)
        _require_id("request_id", self.request_id)
        for name, value in (
            ("caller_domain_id", self.caller_domain_id),
            ("target_domain_id", self.target_domain_id),
            ("resource_namespace", self.resource_namespace),
            ("profile_id", self.profile_id),
            ("activation_id", self.activation_id),
            ("caller_publisher_lineage", self.caller_publisher_lineage),
            ("target_publisher_lineage", self.target_publisher_lineage),
            ("host_extension_id", self.host_extension_id),
            ("provider_authority_id", self.provider_authority_id),
            ("grant_id", self.grant_id),
            ("audit_reservation_id", self.audit_reservation_id),
        ):
            _require_id(name, value)
        for name, value in (
            ("request_digest", self.request_digest),
            ("effect_digest", self.effect_digest),
            ("activation_digest", self.activation_digest),
            ("plan_digest", self.plan_digest),
            ("profile_authority_digest", self.profile_authority_digest),
            ("provider_authority_digest", self.provider_authority_digest),
        ):
            _require_digest(name, value)
        _require_positive_int("caller_boot_epoch", self.caller_boot_epoch)
        _require_positive_int("target_boot_epoch", self.target_boot_epoch)
        _require_positive_int("security_epoch", self.security_epoch)
        _require_positive_int("fencing_token", self.fencing_token)
        _require_finite_time("issued_at", self.issued_at)
        _require_finite_time("expires_at", self.expires_at)
        if self.expires_at <= self.issued_at:
            raise AuthorityValidationError("lease expiry is invalid")
        if len(self.call_chain) > 4 or len(set(self.call_chain)) != len(self.call_chain):
            raise AuthorityValidationError("lease call chain is cyclic or too deep")

    @property
    def digest(self) -> str:
        """Return the immutable Lease digest."""

        return authority_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """Serialize the Lease without its transport MAC."""

        return {
            "lease_id": self.lease_id,
            "request_id": self.request_id,
            "caller": self.caller.to_dict(),
            "target": self.target.to_dict(),
            "caller_domain_id": self.caller_domain_id,
            "caller_boot_epoch": self.caller_boot_epoch,
            "target_domain_id": self.target_domain_id,
            "target_boot_epoch": self.target_boot_epoch,
            "request_digest": self.request_digest,
            "effect_digest": self.effect_digest,
            "authorized_scope": self.authorized_scope.to_dict(),
            "resource_namespace": self.resource_namespace,
            "profile_id": self.profile_id,
            "activation_id": self.activation_id,
            "activation_digest": self.activation_digest,
            "plan_digest": self.plan_digest,
            "profile_authority_digest": self.profile_authority_digest,
            "fencing_token": self.fencing_token,
            "caller_publisher_lineage": self.caller_publisher_lineage,
            "target_publisher_lineage": self.target_publisher_lineage,
            "host_extension_id": self.host_extension_id,
            "provider_authority_id": self.provider_authority_id,
            "provider_authority_digest": self.provider_authority_digest,
            "grant_id": self.grant_id,
            "audit_reservation_id": self.audit_reservation_id,
            "security_epoch": self.security_epoch,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "call_chain": list(self.call_chain),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InvocationLease":
        """Parse an InvocationLease."""

        return cls(
            **{
                **dict(value),
                "caller": FunctionPrincipal.from_dict(value["caller"]),
                "target": FunctionPrincipal.from_dict(value["target"]),
                "authorized_scope": AuthorityScope.from_dict(value["authorized_scope"]),
                "call_chain": tuple(value.get("call_chain", [])),
            }
        )


@dataclass(frozen=True)
class InvocationContext:
    """Host-generated immutable inputs to final authorization."""

    request_id: str
    request_digest: str
    effect_digest: str
    caller_session_id: str
    target: FunctionPrincipal
    target_domain_id: str
    target_boot_epoch: int
    profile_id: str
    activation_id: str
    activation_digest: str
    plan_digest: str
    profile_authority_digest: str
    fencing_token: int
    security_epoch: int
    call_chain: tuple[str, ...] = ()
    parent_lease_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("request_id", self.request_id),
            ("caller_session_id", self.caller_session_id),
            ("target_domain_id", self.target_domain_id),
            ("profile_id", self.profile_id),
            ("activation_id", self.activation_id),
        ):
            _require_id(name, value)
        for name, value in (
            ("request_digest", self.request_digest),
            ("effect_digest", self.effect_digest),
            ("activation_digest", self.activation_digest),
            ("plan_digest", self.plan_digest),
            ("profile_authority_digest", self.profile_authority_digest),
        ):
            _require_digest(name, value)
        _require_positive_int("target_boot_epoch", self.target_boot_epoch)
        _require_positive_int("fencing_token", self.fencing_token)
        _require_positive_int("security_epoch", self.security_epoch)
        if len(self.call_chain) > 4 or len(set(self.call_chain)) != len(self.call_chain):
            raise AuthorityValidationError("invocation call chain is cyclic or too deep")


@dataclass(frozen=True)
class UpdateTrustPolicy:
    """Host-owned policy for non-expanding successor authority."""

    policy_id: str
    publisher_lineage: str
    allow_non_expanding_successor: bool = False
    third_party_auto_update: bool = False

    def __post_init__(self) -> None:
        _require_id("policy_id", self.policy_id)
        _require_id("publisher_lineage", self.publisher_lineage)
        if self.third_party_auto_update and not self.allow_non_expanding_successor:
            raise AuthorityValidationError("third-party auto update cannot bypass successor policy")


@dataclass(frozen=True)
class SuccessorEvidence:
    """Verified update facts used to mint a new exact authority record."""

    old_publisher_lineage: str
    new_publisher_lineage: str
    old_trust_class: str
    new_trust_class: str
    semantics_non_expanding: bool
    implementation_non_expanding: bool
    domain_non_expanding: bool
    entitlement_non_expanding: bool
    background_non_expanding: bool
    network_non_expanding: bool
    process_identity_non_expanding: bool
    old_revoked: bool = False
    new_revoked: bool = False

    def permits_successor(self, policy: UpdateTrustPolicy) -> bool:
        """Return whether all ADR-014 successor requirements are proven."""

        return all(
            (
                policy.allow_non_expanding_successor,
                self.old_publisher_lineage == self.new_publisher_lineage,
                self.new_publisher_lineage == policy.publisher_lineage,
                self.old_trust_class == self.new_trust_class,
                self.semantics_non_expanding,
                self.implementation_non_expanding,
                self.domain_non_expanding,
                self.entitlement_non_expanding,
                self.background_non_expanding,
                self.network_non_expanding,
                self.process_identity_non_expanding,
                not self.old_revoked,
                not self.new_revoked,
            )
        )
