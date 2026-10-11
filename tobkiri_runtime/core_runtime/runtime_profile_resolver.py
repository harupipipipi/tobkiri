"""Compatibility shim for the retired runtime profile resolver module.

The implementation lives in ``core_runtime.runtime_profile_context`` so that
canonical production entries no longer reach into this retired module.
"""

from __future__ import annotations

from .runtime_profile_context import resolve_runtime_profile_context

__all__ = ["resolve_runtime_profile_context"]
