"""Fail-closed Tobkiri Pack v4 host execution primitives."""

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
