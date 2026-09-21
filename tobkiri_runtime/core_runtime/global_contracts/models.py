"""Typed, domain-neutral global contract models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Generic, TypeVar

from .canonical import canonical_json
from .semver import parse_version, validate_version_range

_CONTRACT_ID = re.compile(
    r"^rumi\.(service|action|event|resource|policy|ui|storage|transport)\."
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*\.v[1-9][0-9]*$"
)
_PACK_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
_PROVIDER_INSTANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_CONTENT_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
_ISOLATION_MODES = {"in_process", "process", "sandbox", "remote"}
_TRUST_CLASSES = {"untrusted", "local", "verified", "system"}


def _validate_contract_id(contract_id: str) -> None:
    """Reject a value outside the stable global-contract namespace."""
    if not isinstance(contract_id, str) or _CONTRACT_ID.fullmatch(contract_id) is None:
        raise ValueError(f"invalid global contract ID: {contract_id!r}")


def _validate_date(value: str, field_name: str) -> None:
    """Reject a non-ISO calendar date."""
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}: {value!r}") from exc


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
        if self.deprecated_at is not None:
            _validate_date(self.deprecated_at, "deprecated_at")
        if self.sunset_at is not None:
            _validate_date(self.sunset_at, "sunset_at")
        if self.replacement_contract is not None:
            _validate_contract_id(self.replacement_contract)


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
        """Reject invalid identifiers, versions, and schema values."""
        _validate_contract_id(self.contract_id)
        parse_version(self.version)
        if not isinstance(self.cardinality, Cardinality):
            raise TypeError("cardinality must be a Cardinality")
        if not isinstance(self.security, SecurityClassification):
            raise TypeError("security must be a SecurityClassification")
        if not isinstance(self.failure, FailureSemantics):
            raise TypeError("failure must be a FailureSemantics")
        for field_name, schema in (
            ("input_schema", self.input_schema),
            ("output_schema", self.output_schema),
            ("event_schema", self.event_schema),
        ):
            if schema is None:
                continue
            if not isinstance(schema, Mapping):
                raise TypeError(f"{field_name} must be a mapping")
            canonical_json(schema)


@dataclass(frozen=True)
class ContractRequirement:
    """A consumer's explicit contract requirement."""

    contract_id: str
    version_range: str
    cardinality: Cardinality
    optional: bool = False
    instance_key: str | None = None

    def __post_init__(self) -> None:
        """Validate requirement identity and selection semantics."""
        _validate_contract_id(self.contract_id)
        validate_version_range(self.version_range)
        if not isinstance(self.cardinality, Cardinality):
            raise TypeError("cardinality must be a Cardinality")
        if self.cardinality is Cardinality.KEYED:
            if not self.instance_key:
                raise ValueError("keyed requirement requires instance_key")
        elif self.instance_key is not None:
            raise ValueError("instance_key is valid only for keyed requirements")


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
        if _PROVIDER_INSTANCE_ID.fullmatch(self.provider_instance_id) is None:
            raise ValueError(
                f"invalid provider instance ID: {self.provider_instance_id!r}"
            )
        if _PACK_ID.fullmatch(self.source_pack_id) is None:
            raise ValueError(f"invalid source pack ID: {self.source_pack_id!r}")
        parse_version(self.source_pack_version)
        if _CONTENT_HASH.fullmatch(self.content_hash) is None:
            raise ValueError(f"invalid provider content hash: {self.content_hash!r}")
        if not self.build_identity:
            raise ValueError("provider build_identity must not be empty")
        if self.trust_class not in _TRUST_CLASSES:
            raise ValueError(f"invalid trust class: {self.trust_class!r}")
        if self.isolation not in _ISOLATION_MODES:
            raise ValueError(f"invalid isolation mode: {self.isolation!r}")
        if type(self.priority) is not int:
            raise ValueError("provider priority must be an integer")

        capabilities = tuple(self.required_capabilities)
        before = tuple(self.before)
        after = tuple(self.after)
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(self, "before", before)
        object.__setattr__(self, "after", after)
        for capability in capabilities:
            if not isinstance(capability, str) or not capability:
                raise ValueError("required capabilities must be non-empty strings")
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("required capabilities must be unique")
        if self.contract.cardinality is Cardinality.KEYED:
            if not self.instance_key:
                raise ValueError("keyed provider requires instance_key")
        elif self.instance_key is not None:
            raise ValueError("instance_key is valid only for keyed providers")
        if self.contract.cardinality is not Cardinality.CHAIN and (before or after):
            raise ValueError("before/after are valid only for chain providers")
        if len(set(before)) != len(before) or len(set(after)) != len(after):
            raise ValueError("chain dependencies must be unique")
        if self.provider_instance_id in before or self.provider_instance_id in after:
            raise ValueError("chain provider cannot depend on itself")


T = TypeVar("T")


@dataclass(frozen=True)
class ContractResult(Generic[T]):
    """Non-lossy cross-language contract result envelope."""

    status: ContractStatus
    contract_id: str
    version: str
    provider_instance_id: str
    value: T | None = None
    diagnostics: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the stable envelope identity and normalize diagnostics."""
        if not isinstance(self.status, ContractStatus):
            raise TypeError("status must be a ContractStatus")
        _validate_contract_id(self.contract_id)
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("result version must be a non-empty string")
        if _PROVIDER_INSTANCE_ID.fullmatch(self.provider_instance_id) is None:
            raise ValueError(
                "result provider_instance_id must be a stable opaque identity"
            )
        diagnostics = tuple(self.diagnostics)
        if any(not isinstance(item, str) for item in diagnostics):
            raise ValueError("result diagnostics must be strings")
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def ok(self) -> bool:
        """Return whether this result contains a successful value."""
        return self.status is ContractStatus.OK

    def to_dict(self) -> dict[str, Any]:
        """Return the portable envelope described by the shared JSON Schema."""
        payload: dict[str, Any] = {
            "status": self.status.value,
            "contract_id": self.contract_id,
            "version": self.version,
            "provider_instance_id": self.provider_instance_id,
        }
        if self.diagnostics:
            payload["diagnostics"] = list(self.diagnostics)
        if self.value is not None:
            payload["value"] = self.value
        return payload
