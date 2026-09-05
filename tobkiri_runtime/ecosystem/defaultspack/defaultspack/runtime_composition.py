"""Defaultspack-owned composition for the generic core runtime boundaries."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.credential_transport import CredentialMaterialStoreFactory
from core_runtime.pack_api_server import RuntimeCaptureInputs
from tobkiri_host.backends import ExecutionBackend
from tobkiri_host.credential_store import host_credential_store_factory

if TYPE_CHECKING:
    from core_runtime.bootstrap.runtime import Kernel


def defaultspack_activation_snapshot_loader(
    *,
    active: object,
    workspace: Path,
    profile_id: str,
    authority_store: object,
    catalog: object,
) -> object:
    """Load the exact persisted Profile activation using Defaultspack records."""

    from ecosystem.defaultspack.domain.runtime_v4 import (
        ActivationStore,
        BundledCatalog,
    )

    if not isinstance(authority_store, AuthorityStore):
        raise TypeError("Host authority store is unavailable for Profile activation")
    if not isinstance(catalog, BundledCatalog):
        raise TypeError("Defaultspack Profile catalog is unavailable")

    return ActivationStore(
        workspace / "activation",
        workspace,
        profile_id=profile_id,
        authority=authority_store,
        catalog=catalog,
    ).load_active_snapshot()


def defaultspack_runtime_capture_inputs(
    active: object | None = None,
    *,
    packvm_provisioner: object | None = None,
    bundle_root: Path | None = None,
    credential_store_factory: CredentialMaterialStoreFactory = (
        host_credential_store_factory
    ),
) -> RuntimeCaptureInputs:
    """Select one signed Defaultspack application map for a runtime refresh."""

    from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
        resolve_frontend_contract_map_path,
        load_frontend_contract_bindings,
    )
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        defaultspack_profile_bundle_root,
    )
    from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        create_runtime_surface_services,
    )
    from ecosystem.defaultspack.defaultspack.http_contract_composition import (
        defaultspack_capability_binding,
        defaultspack_capability_snapshot_mapping,
    )

    runtime_root = Path(__file__).resolve().parents[3]
    bundle_root = bundle_root or defaultspack_profile_bundle_root()
    catalog = BundledCatalog.load(bundle_root)
    application_id = _application_id(active, catalog.packs)
    application = catalog.packs.get(application_id)
    if application is None:
        raise RuntimeError("active application pack is unavailable")
    pack_root = runtime_root / "ecosystem" / "defaultspack"
    map_path = resolve_frontend_contract_map_path(application, pack_root)
    context = _contract_context(active)
    bindings = load_frontend_contract_bindings(
        map_path,
        application,
        artifact_root=pack_root,
        **context,
    )
    return RuntimeCaptureInputs(
        bundle_root=bundle_root,
        ecosystem_root=runtime_root / "ecosystem",
        contract_bindings=bindings,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        capability_binding_snapshot_factory=defaultspack_capability_snapshot_mapping,
        capability_binding_selector=defaultspack_capability_binding,
        packvm_backend_factory=(
            defaultspack_packvm_backend_factory(packvm_provisioner)
            if packvm_provisioner is not None
            else None
        ),
        credential_store_factory=credential_store_factory,
    )


def defaultspack_packvm_backend_factory(
    provisioner: object,
) -> Callable[[], ExecutionBackend | None]:
    """Build a production VZ backend only from Defaultspack's exact facts."""

    from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
        MacOSVZProvisionedFacts,
    )
    from tobkiri_host.macos_vz_supervisor import MacOSVZSupervisorDriver
    from tobkiri_host.platform_backends import MacOSVZBackend

    def build() -> ExecutionBackend | None:
        registration = getattr(provisioner, "production_backend_registration", None)
        facts = registration() if callable(registration) else None
        if not isinstance(facts, MacOSVZProvisionedFacts):
            return None
        transport_factory = facts.transport_or_factory()
        if transport_factory is None:
            return None
        return MacOSVZBackend(
            MacOSVZSupervisorDriver(
                transport_factory=transport_factory,
                helper_path=facts.helper_path,
                helper_identity=facts.helper_identity,
                launch_assets=facts.launch_assets,
                agent_identity=facts.agent_identity,
                domain_allocator=facts.domain_allocator,
            )
        )

    return build


def create_defaultspack_kernel(
    *,
    bundle_root: Path | None = None,
    credential_store_factory: CredentialMaterialStoreFactory = (
        host_credential_store_factory
    ),
) -> Kernel:
    """Compose the generic Host bootstrap with Defaultspack-owned services."""

    from core_runtime.bootstrap.runtime import Kernel
    from core_runtime.di_container import get_container
    from core_runtime.packvm_lifecycle_v4 import PackVMLifecycleV4
    from ecosystem.defaultspack.backend.sandbox.isolation import (
        ManagedSandboxSupervisor,
    )
    from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
        default_packvm_provisioner,
    )
    from ecosystem.defaultspack.defaultspack.http_contract_composition import (
        defaultspack_capability_snapshot,
    )
    from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
        DefaultspackHTTPPresentation,
    )
    from ecosystem.defaultspack.defaultspack.runtime_surface_targets import (
        host_profile_control_bindings,
    )
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )
    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        create_runtime_surface_services,
    )

    install_defaultspack_profile_runtime()
    provisioner = default_packvm_provisioner()
    lifecycle = PackVMLifecycleV4(provisioner)
    get_container().register("managed_sandbox_supervisor", ManagedSandboxSupervisor)
    return Kernel(
        packvm_lifecycle=lifecycle,
        runtime_capture_factory=partial(
            defaultspack_runtime_capture_inputs,
            packvm_provisioner=lifecycle,
            bundle_root=bundle_root,
            credential_store_factory=credential_store_factory,
        ),
        capability_snapshot_factory=defaultspack_capability_snapshot,
        application_presentation=DefaultspackHTTPPresentation(),
        host_profile_bindings_factory=host_profile_control_bindings,
        runtime_surface_factory=create_runtime_surface_services,
    )


def _application_id(active: object | None, packs: Mapping[str, object]) -> str:
    if active is not None:
        resolved = getattr(active, "resolved", None)
        plan = getattr(resolved, "plan", None)
        if isinstance(plan, Mapping):
            application = plan.get("application")
            if isinstance(application, Mapping) and isinstance(application.get("pack_id"), str):
                return application["pack_id"]
        profile = getattr(resolved, "profile", None)
        if isinstance(profile, Mapping):
            for item in profile.get("packs", []):
                if isinstance(item, Mapping) and item.get("role") == "application":
                    candidate = item.get("pack_id")
                    if isinstance(candidate, str):
                        return candidate
    application_ids = sorted(
        pack_id
        for pack_id, manifest in packs.items()
        if isinstance(manifest, Mapping)
        and isinstance(manifest.get("pack"), Mapping)
        and manifest["pack"].get("kind") == "application"
    )
    if len(application_ids) != 1:
        raise RuntimeError("active application pack is unavailable or ambiguous")
    return application_ids[0]


def _contract_context(active: object | None) -> dict[str, str]:
    if active is None:
        return {}
    resolved = getattr(active, "resolved", None)
    profile = getattr(resolved, "profile", None)
    plan = getattr(resolved, "plan", None)
    activation = getattr(active, "activation", None)
    if not isinstance(profile, Mapping) or not isinstance(plan, Mapping) or not isinstance(activation, Mapping):
        raise RuntimeError("active Profile contract identity is unavailable")
    return {
        "profile_id": str(profile["profile_id"]),
        "profile_revision": str(plan["profile_revision"]),
        "activation_id": str(activation["activation_id"]),
        "plan_digest": str(plan["plan_digest"]),
    }


__all__ = [
    "defaultspack_activation_snapshot_loader",
    "create_defaultspack_kernel",
    "defaultspack_packvm_backend_factory",
    "defaultspack_runtime_capture_inputs",
]
