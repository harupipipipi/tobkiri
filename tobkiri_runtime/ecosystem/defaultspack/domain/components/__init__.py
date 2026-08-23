from __future__ import annotations

import importlib
import sys
from types import ModuleType


_CANONICAL_PACKAGE = "ecosystem.defaultspack.domain.components"
_LEGACY_PACKAGE = "domain.components"
_OWNED_SUBMODULES = (
    "discovery",
    "entrypoints",
    "manifest",
    "registry",
    "validation",
)


def _canonicalize_legacy_package() -> ModuleType | None:
    """Bind the legacy package name to the canonical module objects.

    Importing this source tree through both names used to execute every module
    twice.  The compatibility path now imports the canonical package once and
    binds all owned submodules to those exact objects in ``sys.modules``.
    """

    if __name__ != _LEGACY_PACKAGE:
        return None
    canonical = importlib.import_module(_CANONICAL_PACKAGE)
    for suffix in _OWNED_SUBMODULES:
        module = importlib.import_module(f"{_CANONICAL_PACKAGE}.{suffix}")
        sys.modules[f"{_LEGACY_PACKAGE}.{suffix}"] = module
    sys.modules[_LEGACY_PACKAGE] = canonical
    return canonical


_canonical_package = _canonicalize_legacy_package()

if _canonical_package is None:
    from .discovery import (
        ComponentDiscoveryIssue,
        ComponentDiscoveryResult,
        discover_components,
    )
    from .entrypoints import (
        ComponentEntrypointResolutionError,
        ResolvedComponentEntrypoint,
        resolve_component_entrypoint,
    )
    from .manifest import DomainComponent
    from .registry import DomainComponentRegistry, get_domain_component_registry
    from .validation import ComponentManifestError, validate_component_manifest
else:
    __all__ = list(getattr(_canonical_package, "__all__", ()))

if _canonical_package is None:
    __all__ = [
        "ComponentDiscoveryIssue",
        "ComponentDiscoveryResult",
        "ComponentEntrypointResolutionError",
        "ComponentManifestError",
        "DomainComponent",
        "DomainComponentRegistry",
        "ResolvedComponentEntrypoint",
        "discover_components",
        "get_domain_component_registry",
        "resolve_component_entrypoint",
        "validate_component_manifest",
    ]
