"""Compatibility exports for the public connection-model policy contract."""

from tobkiri_protocol.provider_compiler.model_access import (
    VERSION,
    compile_connection_parameters,
    effective_model_access,
    model_is_allowed,
    normalize_model_access,
)

__all__ = [
    "VERSION",
    "compile_connection_parameters",
    "effective_model_access",
    "model_is_allowed",
    "normalize_model_access",
]
