"""Typed, domain-neutral global contract models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Generic, Mapping, TypeVar

from .semver import parse_version

_CONTRACT_ID = re.compile(
    r"^rumi\.(service|action|event|resource|policy|ui|storage|transport)\."
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*\.v[1-9][0-9]*$"
)
_CONTENT_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")


class Cardinality(str, Enum):
    """Supported provider resolution semantics."""

    ONE = "one"
    MANY = "many"
    KEYED = "keyed"
    CHAIN = "chain"
    FANOUT = "fanout"
    OPTIONAL = "optional"


class ContractStatus(str, Enum):
    """Non-lossy result statuses shared by all global contracts."""

    OK = "ok"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"
    DENIED = "denied"
    INCOMPATIBLE = "incompatible"
    MISSING_PROVIDER = "missing_provider"
    STALE_RESOLUTION = "stale_resolution"
    INVALID_MANIFEST = "invalid_manifest"


class SecurityClassification(str, Enum):
    """Contract data and operation security classification."""

    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class FailureSemantics(str, Enum):
    """Behavior when a selected provider fails."""

    FAIL_CLOSED = "fail_closed"
    ISOLATE = "isolate"
    CONTINUE_CHAIN = "continue_chain"
    BEST_EFFORT = "best_effort"


@dataclass(frozen=True)
class LifecycleMetadata:
    """Machine-readable lifecycle and ownership metadata."""

    introduced: str
    deprecated: bool = False
    deprecated_at: str | None = None
    sunset_at: str | None = None
    replacement_contract: str | None = None
    data_owner: str | None = None
    migration_id: str | None = None
    rollback_id: str | None = None

    def __post_init__(self) -> None:
        """Validate lifecycle metadata without treating it as authority."""
        parse_version(self.introduced)
        if self.deprecated and (not self.deprecated_at or not self.sunset_at):
            raise ValueError(
                "deprecated lifecycle requires deprecated_at and sunset_at"
            )


@dataclass(frozen=True)
class ContractDescriptor:
    """A versioned global contract independent of its provider pack."""

    contract_id: str
    version: str
    cardinality: Cardinality
    security: SecurityClassification
    failure: FailureSemantics
    lifecycle: LifecycleMetadata
    input_schema: Mapping[str, Any] | None = None
    output_schema: Mapping[str, Any] | None = None
    event_schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        """Reject invalid identifiers and versions at the runtime boundary."""
        if _CONTRACT_ID.fullmatch(self.contract_id) is None:
            raise ValueError(f"invalid global contract ID: {self.contract_id!r}")
        parse_version(self.version)


@dataclass(frozen=True)
class ContractRequirement:
    """A consumer's explicit contract requirement."""

    contract_id: str
    version_range: str
    cardinality: Cardinality
    optional: bool = False
    instance_key: str | None = None


@dataclass(frozen=True)
class ProviderDescriptor:
    """Data-only provider metadata; it never contains a source path."""

    contract: ContractDescriptor
    provider_instance_id: str
    source_pack_id: str
    source_pack_version: str
    content_hash: str
    build_identity: str
    trust_class: str
    isolation: str
    required_capabilities: tuple[str, ...] = ()
    instance_key: str | None = None
    priority: int = 0
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject malformed or self-authorizing provider metadata."""
        parse_version(self.source_pack_version)
        if _CONTENT_HASH.fullmatch(self.content_hash) is None:
            raise ValueError(f"invalid provider content hash: {self.content_hash!r}")
        if self.trust_class not in {"untrusted", "local", "verified", "system"}:
            raise ValueError(f"invalid trust class: {self.trust_class!r}")
        if self.contract.cardinality is not Cardinality.CHAIN and (
            self.before or self.after
        ):
            raise ValueError("before/after are valid only for chain providers")


T = TypeVar("T")


@dataclass(frozen=True)
class ContractResult(Generic[T]):
    """Non-lossy contract result envelope."""

    status: ContractStatus
    value: T | None = None
    diagnostics: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Return whether this result contains a successful value."""
        return self.status is ContractStatus.OK

