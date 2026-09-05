"""Stable errors raised by the data-only protocol layer."""

from __future__ import annotations


class ProtocolError(ValueError):
    """Base class for invalid or unsafe protocol data."""

    code = "protocol_error"


class CanonicalizationError(ProtocolError):
    """Raised when JSON cannot be parsed under the I-JSON profile."""

    code = "canonicalization_error"


class SchemaValidationError(ProtocolError):
    """Raised when a document fails schema or semantic validation."""

    code = "schema_validation_failed"

    def __init__(self, message: str, *, diagnostics: tuple[str, ...] = ()) -> None:
        detail = f"{message}: {'; '.join(diagnostics)}" if diagnostics else message
        super().__init__(detail)
        self.diagnostics = diagnostics


class MigrationError(ProtocolError):
    """Base class for legacy profile migration failures."""

    code = "migration_error"


class MigrationBlockedError(MigrationError):
    """Raised when legacy input is ambiguous or cannot be made safe."""

    code = "migration_blocked"
