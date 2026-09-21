"""Explicit host-to-runtime contract values.

The process environment may carry non-sensitive routing information, but it is
never a credential source.  The launcher may bind a signed contract for a
request (or point to one through the non-secret contract path); consumers only
read values through this module and receive an empty result when the contract
is absent, foreign, or malformed.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping, cast


HOST_CONTRACT_SCHEMA = "tobkiri.host-contract.v1"
EXECUTION_IDENTITY_FIELDS = (
    "profile_id",
    "profile_revision",
    "activation_id",
    "plan_digest",
)
# The Launcher owns this synthetic identity only while the first Host process
# exposes the Profile-ceremony surface.  It is deliberately not a resolved
# Profile identity and must never enter a normal execution-value path.
_LAUNCHER_BOOTSTRAP_IDENTITY = {
    "profile_id": "defaults",
    "profile_revision": (
        "sha256:cce92a9b1d3092cdac63ba80b39e5d3a17d0905f3a716241250e8ac724095580"
    ),
    "activation_id": "activation:bootstrap-template",
    "plan_digest": (
        "sha256:2a08fdc2de1e0d5e51d2f248b0984d4510db442e6905bcebc2984a44d23131a5"
    ),
}
_ACTIVATION_ID_RE = re.compile(r"^activation:[a-z0-9][a-z0-9._-]{7,127}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTRACT_FIELDS = frozenset(
    {
        "schema_version",
        *EXECUTION_IDENTITY_FIELDS,
        "provider_id",
        "values",
        "credentials",
    }
)


class HostContractError(ValueError):
    """Raised when a Host contract is unavailable or fails closed validation."""


@dataclass(frozen=True, slots=True)
class ExecutionProfileIdentity:
    """The complete identity captured for one Host/Application/Shell run."""

    profile_id: str
    profile_revision: str
    activation_id: str
    plan_digest: str

    def __post_init__(self) -> None:
        _validate_identity(self.as_mapping())

    def as_mapping(self) -> dict[str, str]:
        """Return the identity as the contract's four canonical fields."""

        return {
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
            "activation_id": self.activation_id,
            "plan_digest": self.plan_digest,
        }

    @classmethod
    def from_source(
        cls, source: Mapping[str, Any] | object
    ) -> "ExecutionProfileIdentity":
        """Capture identity fields from a mapping or an immutable session object."""

        values = {
            field: (
                source.get(field)
                if isinstance(source, Mapping)
                else getattr(source, field, None)
            )
            for field in EXECUTION_IDENTITY_FIELDS
        }
        if any(not isinstance(value, str) for value in values.values()):
            raise HostContractError("captured execution identity is incomplete")
        try:
            return cls(**cast(dict[str, str], values))
        except TypeError as error:
            raise HostContractError("captured execution identity is invalid") from error

    def matches(self, other: "ExecutionProfileIdentity") -> bool:
        """Return whether every identity field is exactly equal."""

        return self == other


_CONTRACT: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "tobkiri_host_contract", default=None
)


@contextmanager
def bind_host_contract(contract: Mapping[str, Any]) -> Iterator[None]:
    """Bind an explicit host contract for the current request/task."""

    snapshot = validate_host_contract(contract)
    token = _CONTRACT.set(snapshot)
    try:
        yield
    finally:
        _CONTRACT.reset(token)


def _load_contract_file(
    *,
    allow_launcher_bootstrap: bool = False,
) -> Mapping[str, Any] | None:
    """Load a launcher-owned contract file, if the host supplied its path."""

    raw_path = os.getenv("TOBKIRI_HOST_CONTRACT_PATH", "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    try:
        configured_root = os.getenv("RUMI_USER_DATA", "").strip()
        user_data_root = (
            Path(configured_root)
            if configured_root
            else Path(__file__).resolve().parents[1] / "user_data"
        )
        expected = user_data_root / "host_contract.json"
        if path.absolute() != expected.absolute() or path.is_symlink():
            return None
        root_metadata = user_data_root.stat()
        if not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_mode & 0o077:
            return None
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            return None
        getuid = getattr(os, "geteuid", None)
        if callable(getuid) and (
            root_metadata.st_uid != getuid() or metadata.st_uid != getuid()
        ):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    try:
        return validate_host_contract(
            payload,
            allow_launcher_bootstrap=allow_launcher_bootstrap,
        )
    except HostContractError:
        return None


def validate_host_contract(
    contract: Mapping[str, Any],
    *,
    expected_identity: Mapping[str, Any] | object | None = None,
    allow_launcher_bootstrap: bool = False,
) -> Mapping[str, Any]:
    """Validate and freeze one Host contract, optionally against a capture.

    The returned mapping is an immutable process-local snapshot.  In
    particular, callers must not retain the shared contract pathname as an
    authority source after this function returns.
    """

    if not isinstance(contract, Mapping):
        raise HostContractError("host contract must be an object")
    if set(contract) - _CONTRACT_FIELDS:
        raise HostContractError("host contract contains unknown fields")
    if contract.get("schema_version") != HOST_CONTRACT_SCHEMA:
        raise HostContractError("host contract schema is unsupported")
    if any(field not in contract for field in EXECUTION_IDENTITY_FIELDS):
        raise HostContractError("host contract execution identity is incomplete")

    identity_values = {
        field: contract.get(field) for field in EXECUTION_IDENTITY_FIELDS
    }
    if any(not isinstance(value, str) for value in identity_values.values()):
        raise HostContractError("host contract execution identity has invalid types")
    _validate_identity(identity_values)

    has_values = "values" in contract
    has_credentials = "credentials" in contract
    if has_values == has_credentials:
        raise HostContractError("host contract must contain exactly one values mapping")
    raw_values = contract.get("values" if has_values else "credentials")
    if not isinstance(raw_values, Mapping):
        raise HostContractError("host contract values must be an object")
    values: dict[str, str] = {}
    for key, value in raw_values.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise HostContractError("host contract values must be string keyed")
        values[key] = value

    identity = ExecutionProfileIdentity(**cast(dict[str, str], identity_values))
    if _is_launcher_bootstrap_identity(identity) and not allow_launcher_bootstrap:
        raise HostContractError(
            "launcher bootstrap contract is not execution authority"
        )
    if expected_identity is not None:
        expected = ExecutionProfileIdentity.from_source(expected_identity)
        if not identity.matches(expected):
            raise HostContractError(
                "host contract does not match the captured execution identity"
            )

    frozen = {
        "schema_version": HOST_CONTRACT_SCHEMA,
        **identity.as_mapping(),
        "values": MappingProxyType(values),
    }
    provider_id = contract.get("provider_id")
    if provider_id is not None:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise HostContractError("host contract provider_id is invalid")
        frozen["provider_id"] = provider_id.strip()
    return MappingProxyType(frozen)


def capture_host_contract(
    expected_identity: Mapping[str, Any] | object | None = None,
    *,
    contract: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Capture one immutable contract and require its optional identity match."""

    candidate = contract
    if candidate is None:
        candidate = _CONTRACT.get()
    if candidate is None:
        candidate = _load_contract_file()
    if candidate is None:
        raise HostContractError("host contract is unavailable")
    return validate_host_contract(candidate, expected_identity=expected_identity)


def capture_host_contract_from_file(
    expected_identity: Mapping[str, Any] | object | None = None,
) -> Mapping[str, Any]:
    """Capture the current shared contract, bypassing an older request snapshot."""

    candidate = _load_contract_file()
    if candidate is None:
        raise HostContractError("host contract is unavailable")
    return validate_host_contract(candidate, expected_identity=expected_identity)


def capture_launcher_bootstrap_secret() -> str:
    """Capture only the Launcher-owned panel credential snapshot.

    The initial Profile-ceremony process may read the credential from the
    Launcher bootstrap contract, but it must not gain that contract's
    synthetic identity as route authority.  Normal contract capture rejects
    that marker; this narrow API returns only the one credential needed for
    the local panel handoff.
    """

    candidate = _load_contract_file(allow_launcher_bootstrap=True)
    if candidate is None:
        raise HostContractError("launcher panel bootstrap credential is unavailable")
    values = candidate.get("values")
    secret = values.get("panel_bootstrap_secret") if isinstance(values, Mapping) else None
    if (
        not isinstance(secret, str)
        or not secret
        or secret != secret.strip()
        or len(secret) > 4096
    ):
        raise HostContractError("launcher panel bootstrap credential is invalid")
    return secret


def _is_launcher_bootstrap_identity(identity: ExecutionProfileIdentity) -> bool:
    """Return whether ``identity`` is the Launcher-only bootstrap marker."""

    return all(
        hmac.compare_digest(
            getattr(identity, field),
            expected,
        )
        for field, expected in _LAUNCHER_BOOTSTRAP_IDENTITY.items()
    )


def _validate_identity(identity: Mapping[str, Any]) -> None:
    """Validate the same four identity fields emitted by the Launcher."""

    profile_id = identity.get("profile_id")
    if (
        not isinstance(profile_id, str)
        or profile_id != profile_id.strip()
        or not profile_id
        or len(profile_id) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in profile_id)
        or "/" in profile_id
        or "\\" in profile_id
        or ".." in profile_id
    ):
        raise HostContractError("host contract profile_id is invalid")
    for field in ("profile_revision", "plan_digest"):
        value = identity.get(field)
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise HostContractError(f"host contract {field} is invalid")
    if identity["profile_revision"] == identity["plan_digest"]:
        raise HostContractError(
            "host contract profile_revision cannot be the plan_digest"
        )
    activation_id = identity.get("activation_id")
    if (
        not isinstance(activation_id, str)
        or _ACTIVATION_ID_RE.fullmatch(activation_id) is None
    ):
        raise HostContractError("host contract activation_id is invalid")


def host_contract_value(
    name: str,
    *,
    profile_id: str | None = None,
    provider_id: str | None = None,
    contract: Mapping[str, Any] | None = None,
    expected_identity: Mapping[str, Any] | object | None = None,
) -> str:
    """Return one value after schema, identity, Profile, and Provider checks."""

    try:
        snapshot = capture_host_contract(
            expected_identity=expected_identity,
            contract=contract,
        )
    except HostContractError:
        return ""
    expected_profile = str(profile_id or "").strip()
    if not expected_profile:
        try:
            from .profile_credentials import active_profile_id

            expected_profile = str(active_profile_id() or "").strip()
        except ImportError:
            expected_profile = ""
    contract_profile = str(snapshot.get("profile_id") or "").strip()
    if not contract_profile or (
        expected_profile and contract_profile != expected_profile
    ):
        return ""
    expected_provider = str(provider_id or "").strip()
    contract_provider = str(snapshot.get("provider_id") or "").strip()
    if expected_provider and contract_provider != expected_provider:
        return ""
    values = snapshot["values"]
    value = values.get(str(name or "").strip())
    return value.strip() if isinstance(value, str) else ""


def host_contract_mapping() -> dict[str, Any]:
    """Return redacted metadata only; material is intentionally omitted."""

    try:
        contract = capture_host_contract()
    except HostContractError:
        return {}
    return {
        "profile_id": str(contract.get("profile_id") or ""),
        "profile_revision": str(contract.get("profile_revision") or ""),
        "activation_id": str(contract.get("activation_id") or ""),
        "plan_digest": str(contract.get("plan_digest") or ""),
        "provider_id": str(contract.get("provider_id") or ""),
        "bound": isinstance(contract.get("values"), Mapping),
    }
