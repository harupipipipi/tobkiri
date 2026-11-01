"""
Compatibility shim for the legacy core_runtime.ds_container module.

The DI container implementation moved to core_runtime.di_container, but some
callers still import the old module path directly.
"""

from __future__ import annotations

import warnings

from .di_container import DIContainer, get_container, reset_container

warnings.warn(
    "core_runtime.ds_container is deprecated. Use core_runtime.di_container instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["DIContainer", "get_container", "reset_container"]
