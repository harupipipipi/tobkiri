"""Fail-closed Tobkiri Pack v4 host execution primitives.

Data/metadata imports do not initialize unrelated native execution backends.
Requesting a native public API still imports its full enforcing implementation.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .models import (
    ArtifactVariant,
    ContractOperation,
    EffectClass,
    FunctionArtifact,
    InvocationFrame,
    OpaqueAuthorityRef,
    PackArtifact,
    RequestContext,
)

if TYPE_CHECKING:
    from .authority_v4 import AuthorityV4Adapter
    from .backends import production_backend_registry
    from .macos_vz_supervisor import (
        MacOSVZAgentIdentity,
        MacOSVZDomainAllocation,
        MacOSVZDomainAllocator,
        MacOSVZHelperIdentity,
        MacOSVZLaunchAssets,
        MacOSVZRuntime,
        MacOSVZSupervisorDriver,
        MacOSVZSupervisorTransport,
        MacOSVZTransportFactory,
    )
    from .extension_sdk import (
        CapabilityProviderRegistration,
        HostExtensionRegistration,
        HostExtensionSDK,
    )
    from .artifact_compiler import CompiledPack, compile_pack_root, routes_for_plan
    from .composition import AuthorityCeilings, HostV4Composition
    from .runtime import (
        ProductionRuntimeV4,
        V4DispatchSession,
        install_dispatch_session,
    )

_LAZY_MODULES = {
    "AuthorityV4Adapter": ".authority_v4",
    "production_backend_registry": ".backends",
    "MacOSVZAgentIdentity": ".macos_vz_supervisor",
    "MacOSVZDomainAllocation": ".macos_vz_supervisor",
    "MacOSVZDomainAllocator": ".macos_vz_supervisor",
    "MacOSVZHelperIdentity": ".macos_vz_supervisor",
    "MacOSVZLaunchAssets": ".macos_vz_supervisor",
    "MacOSVZRuntime": ".macos_vz_supervisor",
    "MacOSVZSupervisorDriver": ".macos_vz_supervisor",
    "MacOSVZSupervisorTransport": ".macos_vz_supervisor",
    "MacOSVZTransportFactory": ".macos_vz_supervisor",
    "CapabilityProviderRegistration": ".extension_sdk",
    "HostExtensionRegistration": ".extension_sdk",
    "HostExtensionSDK": ".extension_sdk",
    "CompiledPack": ".artifact_compiler",
    "compile_pack_root": ".artifact_compiler",
    "routes_for_plan": ".artifact_compiler",
    "AuthorityCeilings": ".composition",
    "HostV4Composition": ".composition",
    "ProductionRuntimeV4": ".runtime",
    "V4DispatchSession": ".runtime",
    "install_dispatch_session": ".runtime",
}

# The original eager from-imports also exposed these module attributes.
_LAZY_SUBMODULES = frozenset(
    {
        "authority_v4",
        "backends",
        "macos_vz_supervisor",
        "models",
        "extension_sdk",
        "artifact_compiler",
        "composition",
        "runtime",
    }
)
models = import_module(".models", __name__)

# Reload must not keep stale cached exports from a defining module.
for _export_name in _LAZY_MODULES:
    globals().pop(_export_name, None)
del _export_name
for _submodule_name in _LAZY_SUBMODULES - {"models"}:
    globals().pop(_submodule_name, None)
del _submodule_name


def __getattr__(name: str) -> Any:
    """Return the same public definition after its module initialized fully."""
    if name in _LAZY_SUBMODULES:
        value = import_module("." + name, __name__)
    else:
        module = _LAZY_MODULES.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Keep lazy public APIs discoverable before their first use."""
    return sorted(set(globals()) | set(__all__) | _LAZY_SUBMODULES)


__all__ = [
    "ArtifactVariant",
    "AuthorityV4Adapter",
    "AuthorityCeilings",
    "CompiledPack",
    "ContractOperation",
    "EffectClass",
    "FunctionArtifact",
    "CapabilityProviderRegistration",
    "HostExtensionRegistration",
    "HostExtensionSDK",
    "MacOSVZAgentIdentity",
    "MacOSVZDomainAllocation",
    "MacOSVZDomainAllocator",
    "MacOSVZHelperIdentity",
    "MacOSVZLaunchAssets",
    "MacOSVZRuntime",
    "MacOSVZSupervisorDriver",
    "MacOSVZSupervisorTransport",
    "MacOSVZTransportFactory",
    "InvocationFrame",
    "HostV4Composition",
    "OpaqueAuthorityRef",
    "PackArtifact",
    "ProductionRuntimeV4",
    "RequestContext",
    "V4DispatchSession",
    "install_dispatch_session",
    "production_backend_registry",
    "compile_pack_root",
    "routes_for_plan",
]
