"""Data-only v4 Pack Architecture protocol and validation helpers.

The package deliberately contains no Pack loader, dispatcher, or authority
store.  It validates serialized inputs and produces migration/inventory
evidence; execution remains outside this package and must enforce the
fail-closed decisions recorded here.
"""

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
from .ids import (
    validate_artifact_digest,
    validate_canonical_id,
    validate_contract_id,
    validate_opaque_reference,
    validate_semver,
)
from .migration import (
    load_and_migrate_legacy_profile,
    migrate_legacy_profile,
    migrate_legacy_profile_or_raise,
)
from .validation import validate_document

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
