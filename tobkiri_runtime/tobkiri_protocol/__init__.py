"""Data-only v4 Pack Architecture protocol and validation helpers.

The package deliberately contains no Pack loader, dispatcher, or authority
store.  It validates serialized inputs and produces migration/inventory
evidence; execution remains outside this package and must enforce the
fail-closed decisions recorded here.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .canonical import (
    MAX_CANONICAL_JSON_BYTES,
    canonical_bytes,
    canonical_digest,
    canonical_json,
    strict_loads,
)
from .errors import (
    CanonicalizationError,
    MigrationBlockedError,
    MigrationError,
    ProtocolError,
    SchemaValidationError,
)
from .ids import (
    validate_artifact_digest,
    validate_canonical_id,
    validate_contract_id,
    validate_opaque_reference,
    validate_semver,
)
if TYPE_CHECKING:
    from .composition import (
        CompositionError,
        RuntimeProfileBinding,
        VerifiedCatalog,
        catalog_payload,
        compose_runtime_profile,
        definition_revision,
        load_verified_catalog,
        verify_profile_lock,
    )
    from .migration import (
        load_and_migrate_legacy_profile,
        migrate_legacy_profile,
        migrate_legacy_profile_or_raise,
    )
    from .validation import validate_document


# Pure data helpers must remain importable without schema/crypto dependencies.
# Requesting these public APIs still imports their full enforcing implementation.
_LAZY_MODULES = {
    "CompositionError": ".composition",
    "RuntimeProfileBinding": ".composition",
    "VerifiedCatalog": ".composition",
    "catalog_payload": ".composition",
    "compose_runtime_profile": ".composition",
    "definition_revision": ".composition",
    "load_verified_catalog": ".composition",
    "verify_profile_lock": ".composition",
    "load_and_migrate_legacy_profile": ".migration",
    "migrate_legacy_profile": ".migration",
    "migrate_legacy_profile_or_raise": ".migration",
    "validate_document": ".validation",
}


def __getattr__(name: str) -> Any:
    """Load public protocol APIs only when explicitly requested."""
    module = _LAZY_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "CanonicalizationError",
    "CompositionError",
    "MAX_CANONICAL_JSON_BYTES",
    "MigrationBlockedError",
    "MigrationError",
    "ProtocolError",
    "SchemaValidationError",
    "RuntimeProfileBinding",
    "VerifiedCatalog",
    "canonical_bytes",
    "canonical_digest",
    "canonical_json",
    "catalog_payload",
    "compose_runtime_profile",
    "definition_revision",
    "load_and_migrate_legacy_profile",
    "migrate_legacy_profile",
    "migrate_legacy_profile_or_raise",
    "load_verified_catalog",
    "strict_loads",
    "validate_artifact_digest",
    "validate_canonical_id",
    "validate_contract_id",
    "validate_document",
    "validate_opaque_reference",
    "validate_semver",
    "verify_profile_lock",
]
