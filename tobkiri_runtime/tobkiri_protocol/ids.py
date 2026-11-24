"""Canonical identifier and digest validation for Pack Architecture v4."""

from __future__ import annotations

import re
import unicodedata

from .errors import ProtocolError

CANONICAL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
CONTRACT_ID_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\.v[1-9][0-9]*$"
)
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
OPAQUE_REFERENCE_PATTERN = re.compile(r"^(?:authority-ref|lease|handle):[a-z0-9][a-z0-9._-]{7,127}$")
LEGACY_PREFIXES = ("rumi.", "rumiai.", "viewer.", "legacy.")


def validate_canonical_id(value: str, *, field: str = "id") -> str:
    """Validate and return a v4 lowercase identifier.

    Canonical IDs are ASCII and case-sensitive by construction.  Legacy
    namespaces are rejected here; migration code may preserve them only as
    traceability metadata after explicit classification.
    """
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{field} must be a non-empty string")
    if len(value) > 128:
        raise ProtocolError(f"{field} exceeds 128 characters")
    if value != unicodedata.normalize("NFC", value):
        raise ProtocolError(f"{field} is not Unicode-normalized")
    if value != value.lower() or value.startswith(LEGACY_PREFIXES):
        raise ProtocolError(f"{field} is not a canonical v4 identifier: {value!r}")
    if CANONICAL_ID_PATTERN.fullmatch(value) is None:
        raise ProtocolError(f"{field} has invalid canonical ID syntax: {value!r}")
    return value


def validate_contract_id(value: str, *, field: str = "contract_id") -> str:
    """Validate a versioned canonical Contract ID."""
    validate_canonical_id(value, field=field)
    if CONTRACT_ID_PATTERN.fullmatch(value) is None:
        raise ProtocolError(f"{field} must end with a non-zero major version")
    return value


def validate_semver(value: str, *, field: str = "version") -> str:
    """Validate a strict SemVer 2.0.0 value."""
    if not isinstance(value, str) or SEMVER_PATTERN.fullmatch(value) is None:
        raise ProtocolError(f"{field} is not a valid semantic version: {value!r}")
    return value


def validate_artifact_digest(value: str, *, field: str = "digest") -> str:
    """Validate a lowercase SHA-256 content digest."""
    if not isinstance(value, str) or DIGEST_PATTERN.fullmatch(value) is None:
        raise ProtocolError(f"{field} must be a lowercase sha256 digest")
    return value


def validate_opaque_reference(value: str, *, field: str = "reference") -> str:
    """Validate a non-portable authority/lease/handle reference."""
    if not isinstance(value, str) or OPAQUE_REFERENCE_PATTERN.fullmatch(value) is None:
        raise ProtocolError(f"{field} must be an opaque, namespaced reference")
    return value
