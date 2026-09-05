"""HTTP boundary modules for the Tobkiri runtime.

Production code imports the small v4 boundary modules directly.  Historical
handler modules remain importable for offline tooling, but importing this
package never loads them into the production reachability graph.
"""

from __future__ import annotations

import importlib


_LAZY_EXPORTS = {
    "APIResponse": ("api_response", "APIResponse"),
    "AuthGateMixin": ("auth_gate", "AuthGateMixin"),
    "RequestBodyMixin": ("request_body", "RequestBodyMixin"),
    "ResponseWriterMixin": ("http_response", "ResponseWriterMixin"),
    "SetupHandlersMixin": ("setup_handlers", "SetupHandlersMixin"),
    "WebMountMixin": ("web_mounts", "WebMountMixin"),
}

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str) -> object:
    """Load a boundary export only when a caller explicitly requests it."""

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(importlib.import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value
