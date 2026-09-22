"""Assemble the sole live Pack v4 composition from one active snapshot."""

from __future__ import annotations

import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from ecosystem.defaultspack.domain.runtime_v4 import (
    ActivationStore,
    ActiveDefaultProfile,
    BundledCatalog,
)
from tobkiri_host.admission import AdmissionEstimate, QueueScope, ResourceReservation
from tobkiri_host.artifact_materialization import capture_materialized_artifact
from tobkiri_host.backends import BackendRegistry, BackendStatus, ExecutionBackend
from tobkiri_host.broker import AdmissionTicket, RequestAdmissionPort
from tobkiri_host.composition import AuthorityCeilings
from tobkiri_host.contracts import AdapterPlanner, ResolvedOperationBinding, StructuralAdapter
from tobkiri_host.effects import InMemoryReconciliationStore
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    ExecutionKind,
    FunctionArtifact,
    OpaqueAuthorityRef,
    PackArtifact,
    PackageKind,
    RequestContext,
)
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.runtime import ProductionRuntimeV4, V4DispatchSession
from tobkiri_protocol.canonical import canonical_digest, canonical_json
from tobkiri_protocol.errors import ProtocolError
from tobkiri_protocol.platform_artifact import verify_platform_artifact

from ..authority.v4 import (
    ApprovalRecord,
    AuthorityDenied,
    AuthorityMode,
    AuthorityScope,
    AuthorityStore,
    DomainBoundary,
    ExecutionDomain,
    FunctionPrincipal,
    GrantLifetime,
    GrantRecord,
    HostExtensionTrustRecord,
    ProviderAuthorityRecord,
    authority_digest,
)
from ..pack_catalog_backend_v4 import (
    PackControlBackendV4,
)
from ..pack_control_v4 import (
    CONTROL_PRESENTATION_CONTRACT,
    PACK_CONTROL_CONTRACT,
    capture_pack_control_session,
    capture_valid_pack_approval,
)
from ..external_pack_catalog_v4 import resolve_admitted_pack_roots
from ..credential_transport import (
    AuthorizedEnvelopeCredentialTransport,
    CredentialMaterialStoreFactory,
)
from ..global_contract_dispatch import GlobalContractClient
from ..host_provider_backend_v4 import (
    ExactHostProviderBackendV4,
    HostProviderCaptureContextV4,
    HostProviderInvocationContextV4,
)
from ..host_provider_hooks_v4 import load_host_provider_factory


_PACK_CATALOG_KEY = (PACK_CONTROL_CONTRACT, "catalog.read")
_CONTROL_CONTRACTS = {PACK_CONTROL_CONTRACT, CONTROL_PRESENTATION_CONTRACT}
_PYTHON_PACK_BACKEND_ID = "tobkiri.python-pack-v4"
_BASELINE_CONVERSATION_KEY = ("conversation.turn.v1", "complete")
_BASELINE_CONVERSATION_PACK_ID = "defaultspack"
_BASELINE_CONVERSATION_FUNCTION_ID = "defaultspack.conversation"
_BASELINE_CONVERSATION_CALLER_ID = "shell.tauri.default"
_BRIDGED_AI_GENERATE_KEY = (
    "tobkiri.service.ai.generate.v1",
    "rumi_ai_gateway_pack.ai-gateway.generate",
)
_BRIDGED_AI_GENERATE_PACK_ID = "rumi_ai_gateway_pack"
_BRIDGED_AI_GENERATE_FUNCTION_ID = "rumi_ai_gateway_pack.ai-gateway.generate"
_PACKVM_BRIDGE_PROTOCOL = "io.tobkiri.packvm.bridge.v1"
_PACKVM_BRIDGE_MAX_REQUEST_BYTES = 64 * 1024
_PACKVM_BRIDGE_MAX_RESULT_BYTES = 512 * 1024


class _UnavailablePythonPackBackend:
    """Exact fail-closed registration when no authenticated PackVM exists."""

    def __init__(self) -> None:
        self.status = BackendStatus(
            backend_id=_PYTHON_PACK_BACKEND_ID,
            execution_kind=ExecutionKind.PACK_VM,
            platform="any",
            backend_digest=canonical_digest(
                {
                    "backend": _PYTHON_PACK_BACKEND_ID,
                    "state": "authenticated-supervisor-unavailable",
                }
            ),
            production_enabled=False,
            conformance_only=True,
            unavailable_reason=(
                "authenticated PackVM supervisor is not registered for tobkiri.python-pack-v4"
            ),
        )

    def materialize(self, binding: Any, reservation_id: str) -> Any:
        del binding, reservation_id
        raise BackendUnavailableError(self.status.unavailable_reason or "backend unavailable")

    def invoke(self, request: object) -> object:
        del request
        raise BackendUnavailableError(self.status.unavailable_reason or "backend unavailable")

    def cancel(self, request_id: str) -> None:
        del request_id

    def terminate(self, domain_id: str) -> None:
        del domain_id


def _authenticated_packvm_backend(provisioner: Any | None = None) -> ExecutionBackend | None:
    """Build the PackVM backend only from verified direct-VZ facts.

    The production composition root intentionally accepts neither a generic
    VM driver nor Lima's development provisioning surface.  A lifecycle may
    supply its already-authenticated direct-VZ registration; otherwise the
    direct provisioner itself may do so.  In both cases the result must be the
    immutable fact type produced by the direct VZ provisioner, and it must
    yield an allocation-scoped authenticated transport factory before a driver
    is constructed.
    """

    if provisioner is None:
        return None

    try:
        from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
            MacOSVZProvisionedFacts,
        )
        from tobkiri_host.macos_vz_supervisor import MacOSVZSupervisorDriver
        from tobkiri_host.platform_backends import MacOSVZBackend

        registration = getattr(provisioner, "production_backend_registration", None)
        if callable(registration):
            facts = registration()
        else:
            prepare_direct_vz = getattr(provisioner, "prepare_direct_vz", None)
            if not callable(prepare_direct_vz):
                return None
            facts = prepare_direct_vz()
        if not isinstance(facts, MacOSVZProvisionedFacts):
            return None

        transport_factory = facts.transport_or_factory()
        if transport_factory is None:
            return None

        driver = MacOSVZSupervisorDriver(
            transport_factory=transport_factory,
            helper_path=facts.helper_path,
            helper_identity=facts.helper_identity,
            launch_assets=facts.launch_assets,
            agent_identity=facts.agent_identity,
            domain_allocator=facts.domain_allocator,
        )
        return MacOSVZBackend(driver)
    except Exception:
        # This is a capability promotion boundary.  The Host must remain
        # unavailable when any direct-VZ evidence, identity, or constructor
        # check cannot be established; no alternate VM substrate is eligible.
        return None


class _NoAdapterExecution:
    def execute(self, adapter: StructuralAdapter, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError(f"unexpected structural adapter: {adapter.adapter_id}")


class _FiniteAdmission(RequestAdmissionPort):
    """Small bounded admission port for the bootstrap Broker."""

    def estimate(
        self,
        context: RequestContext,
        binding: ResolvedOperationBinding,
        payload: Mapping[str, Any],
    ) -> AdmissionEstimate:
        del context, binding, payload
        return AdmissionEstimate(
            measured_p95_bytes=1024 * 1024,
            declared_minimum_bytes=1024 * 1024,
            runtime_floor_bytes=1024 * 1024,
            profile_reservation_bytes=1024 * 1024,
            backend_overhead_bytes=1024 * 1024,
        )

    def acquire(
        self,
        scope: QueueScope,
        estimate: AdmissionEstimate,
        wait_timeout_seconds: float,
    ) -> AdmissionTicket:
        if wait_timeout_seconds <= 0:
            raise TimeoutError("admission deadline expired")
        return AdmissionTicket(
            ResourceReservation(
                reservation_id="reservation." + secrets.token_hex(16),
                profile_id=scope.profile_id,
                amount=estimate.charge(),
            )
        )

    def release(self, ticket: AdmissionTicket) -> None:
        del ticket


def _pack_root_identities(pack_roots: Mapping[str, Path]) -> dict[str, tuple[int, int]]:
    """Reject symlinks and capture exact Pack-root filesystem identities."""

    identities: dict[str, tuple[int, int]] = {}
    for pack_id, root in sorted(pack_roots.items()):
        if root.is_symlink() or not root.is_dir():
            raise AuthorityDenied(f"selected Pack root is unavailable: {pack_id}")
        for current, directories, names in os.walk(root, followlinks=False):
            current_path = Path(current)
            if current_path.is_symlink() or any(
                (current_path / name).is_symlink() for name in (*directories, *names)
            ):
                raise AuthorityDenied(f"selected Pack contains a symlink: {pack_id}")
        stat_result = root.stat()
        identities[pack_id] = (int(stat_result.st_dev), int(stat_result.st_ino))
    return identities


def _shell_artifact(
    catalog: BundledCatalog,
    shell_id: str,
    selected_shell: Mapping[str, Any],
) -> PackArtifact:
    manifest = catalog.packs[shell_id]
    definition = catalog.shells[str(selected_shell["provider_id"])]
    selected_variants = [
        item
        for item in definition["launch"]["variants"]
        if item["platform"] == selected_shell["platform"]
        and item["architecture"] == selected_shell["architecture"]
        and item["entrypoint_digest"] == selected_shell["executable_artifact_digest"]
    ]
    if definition.get("availability") != "verified" or len(selected_variants) != 1:
        raise AuthorityDenied("captured Shell artifact variant is unavailable or ambiguous")
    selected_variant = selected_variants[0]
    if catalog.artifact_root is None:
        raise AuthorityDenied("captured Shell artifact root is unavailable")
    try:
        verify_platform_artifact(
            catalog.artifact_root,
            selected_variant,
            require_macos_code_signature=True,
        )
    except ProtocolError as exc:
        raise AuthorityDenied(f"captured Shell artifact verification failed: {exc}") from exc
    functions: list[FunctionArtifact] = []
    variants: list[ArtifactVariant] = []
    for index, function in enumerate(manifest["functions"]):
        contract = next(
            item
            for item in manifest["contracts"]
            if item["revision_digest"] == function["contract_revision_digest"]
        )
        variant_id = f"{shell_id}.captured.{index}"
        functions.append(
            FunctionArtifact(
                function_id=function["id"],
                implementation_digest=function["implementation_digest"],
                variant_id=variant_id,
                operations=tuple(
                    ContractOperation(
                        contract_id=contract["contract_id"],
                        contract_version="1.0.0",
                        revision_digest=contract["revision_digest"],
                        operation_id=operation_id,
                        input_schema={"type": "object"},
                        output_schema={"type": "object"},
                    )
                    for operation_id in function["operations"]
                ),
            )
        )
        variants.append(
            ArtifactVariant(
                variant_id=variant_id,
                digest=selected_variant["entrypoint_digest"],
                execution_kind=ExecutionKind.HOST_EXTENSION,
                os=str(selected_variant["platform"]),
                architecture=str(selected_variant["architecture"]),
                runtime_abi="tobkiri-shell-v4",
                backend="tobkiri.shell-host-v4",
            )
        )
    return PackArtifact(
        pack_id=shell_id,
        version=manifest["pack"]["version"],
        digest=manifest["pack"]["artifact_digest"],
        publisher_lineage="tobkiri.repository",
        package_kind=PackageKind.RUNTIME_TCB,
        functions=tuple(functions),
        variants=tuple(variants),
    )


def _operation_scope(
    contract_id: str,
    operation_id: str,
    target: FunctionPrincipal,
) -> AuthorityScope:
    return AuthorityScope(
        capability="operation.invoke",
        semantics_digest=target.contract_revision_digest,
        dimensions={
            "contract": (contract_id,),
            "operation": (operation_id,),
        },
    )


def _committed_operation_scope(
    edge: Mapping[str, Any], target: FunctionPrincipal
) -> AuthorityScope:
    """Materialize only the normalized scope committed by the ResolvedPlan."""

    template = edge.get("requested_scope_template")
    if not isinstance(template, Mapping):
        raise AuthorityDenied("Profile requested scope is unavailable")
    scope = AuthorityScope.from_dict(template)
    exact = _operation_scope(str(edge["contract_id"]), str(edge["operation_id"]), target)
    if not scope.is_subset_of(exact):
        raise AuthorityDenied("Profile requested scope expands its exact operation edge")
    return scope


def _execution_domain(
    *,
    domain_id: str,
    principal: FunctionPrincipal,
    active: ActiveDefaultProfile,
    boundary: DomainBoundary,
    channel_seed: str,
) -> ExecutionDomain:
    activation = active.activation
    return ExecutionDomain(
        domain_id=domain_id,
        profile_id=str(active.resolved.profile["profile_id"]),
        activation_id=str(activation["activation_id"]),
        boot_epoch=1,
        process_identity=f"process.{domain_id}",
        authenticated_channel_digest=authority_digest(
            {
                "channel": channel_seed,
                "activation_id": activation["activation_id"],
                "principal_id": principal.principal_id,
            }
        ),
        sandbox_profile_digest=authority_digest(
            {
                "boundary": boundary.value,
                "provider": principal.principal_id,
                "network": "denied",
                "process": "denied",
            }
        ),
        resource_namespace=f"resource.{domain_id}",
        principals=(principal,),
        boundary=boundary,
        security_epoch=int(activation["security_epoch"]),
        fencing_token=int(activation["fencing_token"]),
    )


def _register_exact_domain(
    store: AuthorityStore,
    control: Any,
    domain: ExecutionDomain,
    *,
    session_id: str,
    principal: FunctionPrincipal,
) -> None:
    existing = store.get_domain(domain.domain_id)
    if existing is None:
        control.register_execution_domain(
            domain,
            session_id=session_id,
            channel_digest=domain.authenticated_channel_digest,
            principal_ref=OpaqueAuthorityRef(principal.principal_id),
        )
        return
    if existing != domain:
        raise AuthorityDenied("captured execution domain identity changed")
    try:
        session_domain, principal_id = store.resolve_authenticated_session(session_id)
    except AuthorityDenied:
        store.bind_authenticated_session(
            session_id=session_id,
            domain=domain,
            channel_digest=domain.authenticated_channel_digest,
            principal_id=principal.principal_id,
        )
        return
    if session_domain != domain or principal_id != principal.principal_id:
        raise AuthorityDenied("authenticated session identity changed")


def _binding_principal(binding: ResolvedOperationBinding) -> FunctionPrincipal:
    """Reconstruct the exact authority principal from verified binding data."""

    return FunctionPrincipal(
        parent_artifact_digest=binding.artifact.digest,
        function_implementation_digest=binding.function.implementation_digest,
        function_id=binding.function.function_id,
        contract_revision_digest=binding.operation.revision_digest,
        operation_id=binding.operation.operation_id,
    )


def _validate_host_provider_bindings(
    function_id: str,
    provider_bindings: tuple[ResolvedOperationBinding, ...],
) -> str:
    """Validate a complete Host Extension inventory before importing its hook."""

    if not provider_bindings:
        raise AuthorityDenied("Host Provider hook has no verified bindings")
    artifact = provider_bindings[0].artifact
    if artifact.package_kind is not PackageKind.HOST_EXTENSION:
        raise AuthorityDenied("Host Provider hook requires a Host Extension package")
    backend_ids = {variant.backend for variant in artifact.variants}
    if len(backend_ids) != 1 or any(
        variant.execution_kind is not ExecutionKind.HOST_EXTENSION
        for variant in artifact.variants
    ):
        raise AuthorityDenied("Host Provider hook artifact boundary is invalid")
    for binding in provider_bindings:
        if (
            binding.artifact != artifact
            or binding.artifact.package_kind is not PackageKind.HOST_EXTENSION
            or binding.function.function_id != function_id
            or binding.function not in artifact.functions
            or binding.variant not in artifact.variants
            or binding.function.variant_id != binding.variant.variant_id
            or binding.operation not in binding.function.operations
            or binding.variant.execution_kind is not ExecutionKind.HOST_EXTENSION
            or binding.variant.backend not in backend_ids
            or binding.principal_ref.value != _binding_principal(binding).principal_id
        ):
            raise AuthorityDenied("Host Provider hook verified identity is invalid")
    return next(iter(backend_ids))


def _load_verified_host_provider_factory(
    pack_root: Path,
    function_id: str,
    provider_bindings: tuple[ResolvedOperationBinding, ...],
) -> tuple[Any, str]:
    """Import a hook only after its complete Host Extension identity is valid."""

    backend_id = _validate_host_provider_bindings(function_id, provider_bindings)
    return load_host_provider_factory(pack_root, provider_bindings[0]), backend_id


def _host_extension_trust_record(
    *,
    active: ActiveDefaultProfile,
    binding: ResolvedOperationBinding,
    valid_from: float,
) -> HostExtensionTrustRecord:
    """Create exact, activation-bound trust for one verified Host Extension Provider."""

    principal = _binding_principal(binding)
    _validate_host_provider_bindings(binding.function.function_id, (binding,))
    activation = active.activation
    identity_suffix = str(activation["activation_id"]).replace(":", ".")
    principal_suffix = principal.principal_id.removeprefix("sha256:")[:24]
    return HostExtensionTrustRecord(
        trust_id=f"host-extension.{principal_suffix}.{identity_suffix}",
        parent_artifact_digest=binding.artifact.digest,
        publisher_lineage=binding.artifact.publisher_lineage,
        provider_principal_ids=(principal.principal_id,),
        trust_provenance_digest=canonical_digest(
            {
                "source": "verified-host-extension-artifact",
                "plan_digest": activation["plan_digest"],
                "artifact_digest": binding.artifact.digest,
                "publisher_lineage": binding.artifact.publisher_lineage,
                "provider_principal_id": principal.principal_id,
            }
        ),
        security_epoch=int(activation["security_epoch"]),
        valid_from=valid_from,
    )


def _commit_pack_control_authority(
    store: AuthorityStore,
    control: Any,
    *,
    active: ActiveDefaultProfile,
    caller: FunctionPrincipal,
    target: FunctionPrincipal,
    target_domain: ExecutionDomain,
    scope: AuthorityScope,
    authority_label: str = "pack-control",
    pack_approval_revision: str | None = None,
    host_extension_binding: ResolvedOperationBinding | None = None,
) -> None:
    activation = active.activation
    profile = active.resolved.profile
    decided_at = datetime.fromisoformat(
        str(activation["created_at"]).replace("Z", "+00:00")
    ).timestamp()
    identity_suffix = str(activation["activation_id"]).replace(":", ".")
    operation_suffix = target.operation_id.replace(".", "-")
    authority_label = authority_label.replace("/", "-").replace(".", "-")
    approval_identity = (
        pack_approval_revision.removeprefix("sha256:")[:24]
        if pack_approval_revision is not None
        else identity_suffix
    )
    record_identity = (
        f"{identity_suffix}.{approval_identity}"
        if pack_approval_revision is not None
        else identity_suffix
    )
    # A Pack approval revision names the stable user decision, not one runtime
    # activation.  The immutable Authority snapshot below is activation-bound,
    # so every record in its bundle must use the activation generation as part
    # of its durable identity.  This preserves prior rows without replaying or
    # colliding with them when an unchanged Pack is activated again.
    approval = ApprovalRecord(
        approval_id=(f"approval.defaults.{authority_label}.{operation_suffix}.{record_identity}"),
        snapshot_digest=canonical_digest(
            {
                "ceremony": "defaults.activate",
                "activation_id": activation["activation_id"],
                "plan_digest": activation["plan_digest"],
                "profile_authority_snapshot_digest": activation[
                    "profile_authority_snapshot_digest"
                ],
                "security_epoch": activation["security_epoch"],
                "scope": scope.to_dict(),
                "pack_approval_revision": pack_approval_revision,
            }
        ),
        actor_id=(
            "user.pack-approval"
            if pack_approval_revision is not None
            else "user.defaults-confirmation"
        ),
        decision="approved",
        decided_at=decided_at,
        caller=caller,
        target=target,
        profile_id=str(profile["profile_id"]),
        effect_bundle_digest=scope.digest,
        security_epoch=int(activation["security_epoch"]),
    )
    host_extension_trust = (
        _host_extension_trust_record(
            active=active,
            binding=host_extension_binding,
            valid_from=decided_at,
        )
        if host_extension_binding is not None
        else None
    )
    provider = ProviderAuthorityRecord(
        record_id=(f"provider.defaults.{authority_label}.{operation_suffix}.{record_identity}"),
        provider=target,
        execution_domain_id=target_domain.domain_id,
        execution_domain_identity_digest=target_domain.identity_digest,
        scope=scope,
        authority_mode=AuthorityMode.LEASE_ONLY,
        security_epoch=int(activation["security_epoch"]),
        trust_provenance_digest=canonical_digest(
            {
                "source": "locked-defaults-profile",
                "plan_digest": activation["plan_digest"],
                "target": target.to_dict(),
            }
        ),
        publisher_lineage=(
            host_extension_trust.publisher_lineage
            if host_extension_trust is not None
            else "tobkiri.repository"
        ),
        host_extension_id=(
            host_extension_trust.trust_id
            if host_extension_trust is not None
            else "runtime-tcb"
        ),
        valid_from=decided_at,
        host_broker_binding="tobkiri.request-broker.v4",
    )
    grant = GrantRecord(
        grant_id=(f"grant.defaults.{authority_label}.{operation_suffix}.{record_identity}"),
        caller=caller,
        target=target,
        profile_id=str(profile["profile_id"]),
        activation_id=str(activation["activation_id"]),
        profile_authority_digest=str(activation["profile_authority_snapshot_digest"]),
        caller_publisher_lineage="tobkiri.repository",
        target_publisher_lineage=provider.publisher_lineage,
        scope=scope,
        lifetime=GrantLifetime.PERSISTENT_PROFILE,
        security_epoch=int(activation["security_epoch"]),
        approval_id=approval.approval_id,
        issued_at=decided_at,
    )
    existing = (
        (
            store.get_host_extension_trust(host_extension_trust.trust_id)
            if host_extension_trust is not None
            else None
        ),
        store.get_approval(approval.approval_id),
        store.get_provider_authority(provider.record_id),
        store.get_grant(grant.grant_id),
    )
    expected = (host_extension_trust, approval, provider, grant)
    if existing == (None, None, None, None):
        control.commit_approval_bundle(
            approval,
            host_extension_trust=host_extension_trust,
            provider_authorities=(provider,),
            grants=(grant,),
        )
    elif existing != expected:
        raise AuthorityDenied("Pack catalog authority snapshot changed")


def _bind_baseline_conversation_authority(
    *,
    active: ActiveDefaultProfile,
    catalog: BundledCatalog,
    profile: Mapping[str, Any],
    binding: Mapping[str, Any],
    resolved_binding: ResolvedOperationBinding,
    caller: FunctionPrincipal,
    scope: AuthorityScope,
    mandatory_pack_ids: set[str],
    static_edge_keys: set[tuple[str, str]],
    activation_suffix: str,
    authority_store: AuthorityStore,
    authority_control: Any,
    registered_backends: tuple[ExecutionBackend, ...],
    target_backend_digests: dict[str, str],
) -> ExecutionBackend | None:
    """Commit the exact Defaults Conversation authority only for a live PackVM.

    The Defaults confirmation is the approval source for its required baseline
    Pack.  Optional Pack approvals are deliberately not consulted here: they
    cannot authorize, widen, or substitute this fixed Profile edge.
    """

    key = _BASELINE_CONVERSATION_KEY
    target = FunctionPrincipal.from_dict(binding["function_principal"])
    expected_profile_edges = tuple(
        edge
        for edge in catalog.profiles["defaults"]["requested_edges"]
        if (str(edge["contract_id"]), str(edge["operation_id"])) == key
    )
    active_profile_edges = tuple(
        edge
        for edge in profile["requested_edges"]
        if (str(edge["contract_id"]), str(edge["operation_id"])) == key
    )
    if (
        key not in static_edge_keys
        or _BASELINE_CONVERSATION_PACK_ID not in mandatory_pack_ids
        or str(binding["pack_id"]) != _BASELINE_CONVERSATION_PACK_ID
        or len(expected_profile_edges) != 1
        or len(active_profile_edges) != 1
        or str(expected_profile_edges[0]["caller_function_id"])
        != _BASELINE_CONVERSATION_CALLER_ID
        or str(expected_profile_edges[0]["target_provider_id"])
        != _BASELINE_CONVERSATION_FUNCTION_ID
        or str(active_profile_edges[0]["caller_function_id"])
        != _BASELINE_CONVERSATION_CALLER_ID
        or str(active_profile_edges[0]["target_provider_id"])
        != _BASELINE_CONVERSATION_FUNCTION_ID
        or target != _binding_principal(resolved_binding)
        or target.function_id != _BASELINE_CONVERSATION_FUNCTION_ID
        or target.operation_id != key[1]
        or resolved_binding.artifact.pack_id != _BASELINE_CONVERSATION_PACK_ID
        or resolved_binding.function.function_id
        != _BASELINE_CONVERSATION_FUNCTION_ID
        or resolved_binding.operation.contract_id != key[0]
        or resolved_binding.operation.operation_id != key[1]
        or resolved_binding.variant.execution_kind is not ExecutionKind.PACK_VM
        or resolved_binding.variant.backend != _PYTHON_PACK_BACKEND_ID
    ):
        raise AuthorityDenied("Defaults baseline Conversation identity changed")

    try:
        backend = BackendRegistry(registered_backends).select(resolved_binding)
    except Exception:
        # A missing, non-production, or otherwise ineligible backend must not
        # create a domain, approval, ProviderAuthority, or Grant.  The catalog
        # remains fail-closed and reports the exact backend diagnostic.
        return None
    target_domain_binder = getattr(backend, "bind_target_domain_resolver", None)
    bridge_binder = getattr(backend, "bind_capability_bridge", None)
    if not callable(target_domain_binder) or not callable(bridge_binder):
        # A backend which cannot consume the Authority-owned target identity is
        # not eligible to receive the baseline Grant.  Conversation also
        # requires the verified Host continuation bridge; a ready descriptor
        # alone is never enough to expose this capability.
        return None

    target_suffix = target.principal_id.removeprefix("sha256:")[:24]
    target_domain = _execution_domain(
        domain_id=f"domain.provider.{target_suffix}.{activation_suffix}",
        principal=target,
        active=active,
        boundary=DomainBoundary.DEDICATED_PROCESS,
        channel_seed=f"baseline-packvm-provider:{key[0]}:{key[1]}",
    )
    _register_exact_domain(
        authority_store,
        authority_control,
        target_domain,
        session_id=f"session.provider.baseline-packvm.{target_suffix}.{activation_suffix}",
        principal=target,
    )
    _commit_pack_control_authority(
        authority_store,
        authority_control,
        active=active,
        caller=caller,
        target=target,
        target_domain=target_domain,
        scope=scope,
        authority_label="baseline-packvm",
    )
    # Context evidence must be locked to the selected, production-ready
    # backend rather than a caller-supplied stale digest.
    target_backend_digests[target.principal_id] = backend.status.backend_digest
    return backend


def _validated_conversation_bridge_binding(
    *,
    catalog: BundledCatalog,
    profile: Mapping[str, Any],
    binding_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    resolved_binding_by_key: Mapping[tuple[str, str], ResolvedOperationBinding],
    static_edge_keys: set[tuple[str, str]],
) -> ResolvedOperationBinding:
    """Return the only Host capability reachable from PackVM Conversation.

    The guest can request neither a different Contract target nor a broader
    provider identity.  This check deliberately uses both the shipped Defaults
    profile and the active immutable Profile before binding the Host bridge.
    """

    key = _BRIDGED_AI_GENERATE_KEY
    binding = binding_by_key.get(key)
    resolved_binding = resolved_binding_by_key.get(key)
    expected_edges = tuple(
        edge
        for edge in catalog.profiles["defaults"]["requested_edges"]
        if (str(edge["contract_id"]), str(edge["operation_id"])) == key
    )
    active_edges = tuple(
        edge
        for edge in profile["requested_edges"]
        if (str(edge["contract_id"]), str(edge["operation_id"])) == key
    )
    if binding is None or resolved_binding is None:
        raise AuthorityDenied("Defaults Conversation bridge target is unavailable")
    target = FunctionPrincipal.from_dict(binding["function_principal"])
    if (
        key not in static_edge_keys
        or str(binding["pack_id"]) != _BRIDGED_AI_GENERATE_PACK_ID
        or len(expected_edges) != 1
        or len(active_edges) != 1
        or str(expected_edges[0]["caller_function_id"])
        != _BASELINE_CONVERSATION_FUNCTION_ID
        or str(expected_edges[0]["target_provider_id"])
        != _BRIDGED_AI_GENERATE_FUNCTION_ID
        or str(active_edges[0]["caller_function_id"])
        != _BASELINE_CONVERSATION_FUNCTION_ID
        or str(active_edges[0]["target_provider_id"])
        != _BRIDGED_AI_GENERATE_FUNCTION_ID
        or target != _binding_principal(resolved_binding)
        or target.function_id != _BRIDGED_AI_GENERATE_FUNCTION_ID
        or target.operation_id != key[1]
        or resolved_binding.artifact.pack_id != _BRIDGED_AI_GENERATE_PACK_ID
        or resolved_binding.function.function_id
        != _BRIDGED_AI_GENERATE_FUNCTION_ID
        or resolved_binding.operation.contract_id != key[0]
        or resolved_binding.operation.operation_id != key[1]
        # Calling PackVM again from this bridge would let a guest-controlled
        # continuation create a recursive capability boundary.
        or resolved_binding.variant.execution_kind is ExecutionKind.PACK_VM
        or resolved_binding.variant.backend == _PYTHON_PACK_BACKEND_ID
    ):
        raise AuthorityDenied("Defaults Conversation bridge identity changed")
    return resolved_binding


def _provider_unavailable_bridge_result() -> dict[str, Any]:
    """Return the fixed error projection allowed across the guest boundary."""

    return {
        "status": "error",
        "error": {
            "code": "PROVIDER_UNAVAILABLE",
            "message": "The verified AI capability is unavailable.",
        },
    }


def capture_production_dispatch(
    active: ActiveDefaultProfile,
    *,
    bundle_root: Path,
    ecosystem_root: Path,
    authority_store: AuthorityStore,
    backends: BackendRegistry | None = None,
    target_backend_digests: Mapping[str, str] | None = None,
    packvm_provisioner: Any | None = None,
    packvm_readiness_reader: Callable[[], Mapping[str, Any]] | None = None,
    frontend_contract_bindings: tuple[Any, ...] = (),
    credential_store_factory: CredentialMaterialStoreFactory | None = None,
) -> V4DispatchSession:
    """Capture ProductionRuntimeV4 and its RequestBroker from verified records."""

    authority_path = authority_store.path.resolve()
    if authority_path.name != "v4.sqlite3" or authority_path.parent.name != "authority":
        raise AuthorityDenied("Authority store path is not canonical")
    authority_user_data = authority_path.parent.parent
    authority_workspace = authority_user_data / "workspaces" / "defaults"
    try:
        activation_store = ActivationStore(
            authority_workspace / "activation",
            authority_workspace,
            profile_id="defaults",
            authority=authority_store,
            catalog=BundledCatalog.load(bundle_root),
        )
        persisted_active = activation_store.load_active_snapshot()
    except Exception as exc:
        raise AuthorityDenied(
            "Authority store is not bound to the captured Defaults activation"
        ) from exc
    if (
        dict(persisted_active.activation) != dict(active.activation)
        or dict(persisted_active.resolved.profile) != dict(active.resolved.profile)
        or dict(persisted_active.resolved.lock) != dict(active.resolved.lock)
        or dict(persisted_active.resolved.plan) != dict(active.resolved.plan)
    ):
        raise AuthorityDenied("Authority store is not bound to the captured Defaults activation")
    active = persisted_active
    activation_suffix = str(active.activation["fencing_token"])

    catalog = BundledCatalog.load(bundle_root)
    profile = active.resolved.profile
    lock = active.resolved.lock
    plan = active.resolved.plan
    shell_id = str(profile["shell"]["pack_id"])
    shell = _shell_artifact(
        catalog,
        shell_id,
        profile["shell"],
    )
    principals: dict[str, FunctionPrincipal] = {}
    for function in shell.functions:
        for operation in function.operations:
            principal = FunctionPrincipal(
                shell.digest,
                function.implementation_digest,
                function.function_id,
                operation.revision_digest,
                operation.operation_id,
            )
            principals[principal.function_id] = principal
    for binding in plan["bindings"]:
        principal = FunctionPrincipal.from_dict(binding["function_principal"])
        principals[principal.function_id] = principal

    # Dispatch must follow the persisted immutable Profile, including exact
    # operation edges contributed by an enabled/approved optional Pack.
    edges = profile["requested_edges"]
    binding_by_key = {
        (item["contract_id"], item["operation_id"]): item for item in plan["bindings"]
    }
    ceilings: dict[tuple[str, str], AuthorityCeilings] = {}
    caller_by_operation: dict[tuple[str, str], FunctionPrincipal] = {}
    scope_by_operation: dict[tuple[str, str], AuthorityScope] = {}
    for edge in edges:
        key = (edge["contract_id"], edge["operation_id"])
        binding = binding_by_key[key]
        caller = principals[str(edge["caller_function_id"])]
        target = FunctionPrincipal.from_dict(binding["function_principal"])
        scope = _committed_operation_scope(edge, target)
        if binding["requested_scope_digest"] != canonical_digest(scope.to_dict()):
            raise AuthorityDenied("ResolvedPlan requested scope binding changed")
        ceilings[(caller.principal_id, target.principal_id)] = AuthorityCeilings(
            caller_effect=scope,
            runtime_safety=scope,
            profile_admin=scope,
        )
        caller_by_operation[key] = caller
        scope_by_operation[key] = scope

    binding_pack_ids = {str(item["pack_id"]) for item in plan["bindings"]}
    pack_roots = resolve_admitted_pack_roots(
        tuple(sorted(binding_pack_ids)),
        ecosystem_root,
    )
    captured_pack_root_identities = _pack_root_identities(pack_roots)

    def artifact_resolver(binding: ResolvedOperationBinding) -> Any:
        pack_id = binding.artifact.pack_id
        root = pack_roots.get(pack_id)
        expected_identity = captured_pack_root_identities.get(pack_id)
        if root is None or expected_identity is None:
            raise AuthorityDenied("resolved Pack artifact root is unavailable")
        before = root.lstat()
        if root.is_symlink() or (int(before.st_dev), int(before.st_ino)) != expected_identity:
            raise AuthorityDenied("resolved Pack artifact root identity changed")
        artifact = capture_materialized_artifact(root, binding)
        after = root.lstat()
        if root.is_symlink() or (int(after.st_dev), int(after.st_ino)) != expected_identity:
            raise AuthorityDenied("resolved Pack artifact root changed during materialization")
        return artifact

    effective = {
        str(item["identity"]): str(item["artifact_digest"]) for item in lock["effective_set"]
    }
    runtime = ProductionRuntimeV4.capture(
        profile=profile,
        lock=lock,
        plan=plan,
        activation=active.activation,
        pack_roots=pack_roots,
        supporting_artifacts=(shell,),
        verified_effective_artifacts=effective,
        authority_ceilings=ceilings,
    )
    catalog_bindings = tuple(
        runtime.composition.catalog.resolve_pinned(
            str(binding["contract_id"]),
            str(binding["operation_id"]),
        )
        for binding in plan["bindings"]
    )
    resolved_binding_by_key = {
        (binding.operation.contract_id, binding.operation.operation_id): binding
        for binding in catalog_bindings
    }
    registered_backends = tuple((backends or BackendRegistry(())).registered)
    if backends is None:
        authenticated_backend = _authenticated_packvm_backend(packvm_provisioner)
        if authenticated_backend is not None:
            registered_backends += (authenticated_backend,)
    target_backend_digests = dict(target_backend_digests or {})
    authority_control = runtime.composition.authority_adapter(authority_store)
    control_targets: dict[tuple[str, str], tuple[str, str, str]] = {}
    control_backend: PackControlBackendV4 | None = None
    control_bindings = {
        key: binding for key, binding in binding_by_key.items() if key[0] in _CONTROL_CONTRACTS
    }
    if control_bindings:
        def load_active_profile() -> ActiveDefaultProfile:
            from .profile_capture import capture_default_profile

            return capture_default_profile()

        control_session = capture_pack_control_session(
            active=active,
            packvm_readiness_reader=packvm_readiness_reader,
            active_profile_loader=load_active_profile,
            bundle_root=bundle_root,
        )
        for key, control_binding in sorted(control_bindings.items()):
            operation_id = key[1]
            target = FunctionPrincipal.from_dict(control_binding["function_principal"])
            target_suffix = target.principal_id.removeprefix("sha256:")[:24]
            target_domain = _execution_domain(
                domain_id=f"domain.provider.{target_suffix}.{activation_suffix}",
                principal=target,
                active=active,
                boundary=DomainBoundary.DEDICATED_PROCESS,
                channel_seed=f"pack-control-provider:{operation_id}",
            )
            _register_exact_domain(
                authority_store,
                authority_control,
                target_domain,
                session_id=(f"session.provider.pack-control.{operation_id}.{activation_suffix}"),
                principal=target,
            )
            scope = scope_by_operation[key]
            caller = caller_by_operation[key]
            _commit_pack_control_authority(
                authority_store,
                authority_control,
                active=active,
                caller=caller,
                target=target,
                target_domain=target_domain,
                scope=scope,
            )
            control_targets[key] = (
                target.principal_id,
                target.function_implementation_digest,
                target_domain.domain_id,
            )
        backend_digest = canonical_digest(
            {
                "backend": "tobkiri.host-pack-control.v4",
                "targets": {
                    f"{key[0]}::{key[1]}": list(target) for key, target in control_targets.items()
                },
                "profile_id": profile["profile_id"],
                "plan_digest": plan["plan_digest"],
                "security_epoch": active.activation["security_epoch"],
            }
        )
        control_backend = PackControlBackendV4(
            session=control_session,
            targets=control_targets,
            backend_digest=backend_digest,
        )
        target_backend_digests = {
            **dict(target_backend_digests or {}),
            **{target[0]: backend_digest for target in control_targets.values()},
        }
    static_edge_keys = {
        (str(edge["contract_id"]), str(edge["operation_id"]))
        for edge in catalog.profiles["defaults"]["requested_edges"]
    }
    dynamic_bindings = {
        key: binding
        for key, binding in binding_by_key.items()
        if key not in static_edge_keys and key[0] not in _CONTROL_CONTRACTS
    }
    mandatory_pack_ids = {
        str(item["pack_id"])
        for item in catalog.profiles["defaults"].get("packs", ())
        if item.get("role") != "application"
    }
    optional_pack_ids = {
        str(item["pack_id"])
        for item in profile.get("packs", ())
        if item.get("role") != "application"
        and str(item["pack_id"]) not in mandatory_pack_ids
    }
    pack_by_function = {
        str(binding["function_principal"]["function_id"]): str(binding["pack_id"])
        for binding in plan["bindings"]
    }
    edge_by_key = {
        (str(edge["contract_id"]), str(edge["operation_id"])): edge
        for edge in profile["requested_edges"]
    }
    captured_dynamic_approvals: dict[str, str] = {}
    approved_host_binding_keys: set[tuple[str, str]] = set()
    dynamic_domain_ids: dict[tuple[str, str, str], str] = {}
    for key, dynamic_binding in sorted(dynamic_bindings.items()):
        contract_id, operation_id = key
        target_pack_id = str(dynamic_binding["pack_id"])
        caller_pack_id = pack_by_function.get(
            str(edge_by_key[key]["caller_function_id"]),
            "",
        )
        approval_pack_ids = {
            pack_id for pack_id in (target_pack_id, caller_pack_id) if pack_id in optional_pack_ids
        }
        if len(approval_pack_ids) != 1:
            # Dynamic authority must trace to exactly one approved optional
            # Pack, either as the operation owner or as the signed direct
            # dependency caller.  Never mint authority from dependency
            # presence alone.
            continue
        approval_pack_id = next(iter(approval_pack_ids))
        try:
            pack_approval = capture_valid_pack_approval(approval_pack_id)
            pack_approval_revision = str(pack_approval["approval_revision"])
            captured_dynamic_approvals[approval_pack_id] = pack_approval_revision
        except Exception:
            # The immutable plan may still contain a previously selected Pack,
            # but missing/corrupt/stale approval must never recreate authority.
            continue
        resolved_dynamic_binding = resolved_binding_by_key[key]
        is_host_extension = (
            resolved_dynamic_binding.artifact.package_kind
            is PackageKind.HOST_EXTENSION
        )
        if is_host_extension:
            _validate_host_provider_bindings(
                resolved_dynamic_binding.function.function_id,
                (resolved_dynamic_binding,),
            )
        target = FunctionPrincipal.from_dict(dynamic_binding["function_principal"])
        target_suffix = target.principal_id.removeprefix("sha256:")[:24]
        target_domain = _execution_domain(
            domain_id=f"domain.provider.{target_suffix}.{activation_suffix}",
            principal=target,
            active=active,
            boundary=DomainBoundary.DEDICATED_PROCESS,
            channel_seed=f"dynamic-provider:{contract_id}:{operation_id}",
        )
        _register_exact_domain(
            authority_store,
            authority_control,
            target_domain,
            session_id=f"session.provider.dynamic.{target_suffix}.{activation_suffix}",
            principal=target,
        )
        caller = caller_by_operation[key]
        _commit_pack_control_authority(
            authority_store,
            authority_control,
            active=active,
            caller=caller,
            target=target,
            target_domain=target_domain,
            scope=scope_by_operation[key],
            authority_label="dynamic-pack",
            pack_approval_revision=pack_approval_revision,
            host_extension_binding=(
                resolved_dynamic_binding if is_host_extension else None
            ),
        )
        if is_host_extension:
            approved_host_binding_keys.add(key)
        dynamic_domain_ids[(contract_id, operation_id, target.principal_id)] = (
            target_domain.domain_id
        )

    built_in_host_pack_ids = {
        "rumi_ai_gateway_pack",
        "rumi_ai_pipeline_pack",
        "rumi_ai_routing_pack",
        "rumi_ai_stream_pack",
        "rumi_ai_tool_bridge_pack",
        "rumi_ai_usage_pack",
        "rumi_model_registry_pack",
        "rumi_provider_adapters_pack",
        "rumi_provider_registry_pack",
    }
    for key, host_binding in sorted(binding_by_key.items()):
        if (
            key in approved_host_binding_keys
            or str(host_binding["pack_id"]) not in built_in_host_pack_ids
        ):
            continue
        resolved_host_binding = resolved_binding_by_key[key]
        _validate_host_provider_bindings(
            resolved_host_binding.function.function_id,
            (resolved_host_binding,),
        )
        target = FunctionPrincipal.from_dict(host_binding["function_principal"])
        target_suffix = target.principal_id.removeprefix("sha256:")[:24]
        target_domain = _execution_domain(
            domain_id=f"domain.provider.{target_suffix}.{activation_suffix}",
            principal=target,
            active=active,
            boundary=DomainBoundary.DEDICATED_PROCESS,
            channel_seed=f"built-in-host-provider:{key[0]}:{key[1]}",
        )
        _register_exact_domain(
            authority_store,
            authority_control,
            target_domain,
            session_id=f"session.provider.built-in.{target_suffix}.{activation_suffix}",
            principal=target,
        )
        _commit_pack_control_authority(
            authority_store,
            authority_control,
            active=active,
            caller=caller_by_operation[key],
            target=target,
            target_domain=target_domain,
            scope=scope_by_operation[key],
            authority_label=f"built-in-{host_binding['pack_id']}",
            host_extension_binding=resolved_host_binding,
        )
        approved_host_binding_keys.add(key)
        dynamic_domain_ids[(key[0], key[1], target.principal_id)] = target_domain.domain_id

    baseline_binding = binding_by_key.get(_BASELINE_CONVERSATION_KEY)
    baseline_resolved_binding = resolved_binding_by_key.get(
        _BASELINE_CONVERSATION_KEY
    )
    if baseline_binding is None or baseline_resolved_binding is None:
        raise AuthorityDenied("Defaults baseline Conversation binding is unavailable")
    _validated_conversation_bridge_binding(
        catalog=catalog,
        profile=profile,
        binding_by_key=binding_by_key,
        resolved_binding_by_key=resolved_binding_by_key,
        static_edge_keys=static_edge_keys,
    )
    baseline_target = _binding_principal(baseline_resolved_binding)
    if caller_by_operation.get(_BRIDGED_AI_GENERATE_KEY) != baseline_target:
        raise AuthorityDenied("Defaults Conversation bridge caller identity changed")
    baseline_backend = _bind_baseline_conversation_authority(
        active=active,
        catalog=catalog,
        profile=profile,
        binding=baseline_binding,
        resolved_binding=baseline_resolved_binding,
        caller=caller_by_operation[_BASELINE_CONVERSATION_KEY],
        scope=scope_by_operation[_BASELINE_CONVERSATION_KEY],
        mandatory_pack_ids=mandatory_pack_ids,
        static_edge_keys=static_edge_keys,
        activation_suffix=activation_suffix,
        authority_store=authority_store,
        authority_control=authority_control,
        registered_backends=registered_backends,
        target_backend_digests=target_backend_digests,
    )

    def authority_target_domain(binding: ResolvedOperationBinding) -> str:
        target_suffix = binding.principal_ref.value.removeprefix("sha256:")[:24]
        domain_id = f"domain.provider.{target_suffix}.{activation_suffix}"
        domain = authority_store.get_domain(domain_id)
        if domain is None or not any(
            principal.principal_id == binding.principal_ref.value
            for principal in domain.principals
        ):
            raise AuthorityDenied(
                "production PackVM target domain is not registered by Authority"
            )
        return domain_id

    # The bridge is Host-owned and receives only an authenticated outer
    # RequestEnvelope from the VZ supervisor.  The callback is intentionally
    # closed over the immutable capture; it never accepts caller, profile,
    # session, contract, provider, or plan identity from the guest.
    dispatch_holder: list[V4DispatchSession] = []

    def capability_bridge(
        outer_request: object,
        bridge_request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        outer_context = getattr(outer_request, "context", None)
        outer_target = getattr(
            getattr(outer_request, "target_principal", None),
            "value",
            None,
        )
        outer_domain = getattr(
            getattr(outer_request, "target_domain", None),
            "value",
            None,
        )
        expected_target_domain = authority_target_domain(baseline_resolved_binding)
        if (
            getattr(outer_request, "contract_id", None)
            != _BASELINE_CONVERSATION_KEY[0]
            or getattr(outer_request, "operation_id", None)
            != _BASELINE_CONVERSATION_KEY[1]
            or outer_target != baseline_target.principal_id
            or outer_domain != expected_target_domain
            or getattr(outer_context, "caller_principal", None)
            != OpaqueAuthorityRef(
                caller_by_operation[_BASELINE_CONVERSATION_KEY].principal_id
            )
            or getattr(outer_context, "profile_id", None) != profile["profile_id"]
            or getattr(outer_context, "activation_id", None)
            != active.activation["activation_id"]
            or getattr(outer_context, "activation_digest", None)
            != canonical_digest(active.activation)
            or getattr(outer_context, "plan_digest", None) != plan["plan_digest"]
            or getattr(outer_context, "security_epoch", None)
            != active.activation["security_epoch"]
            or getattr(outer_context, "fencing_token", None)
            != active.activation["fencing_token"]
            or getattr(outer_context, "profile_authority_digest", None)
            != active.activation["profile_authority_snapshot_digest"]
            or getattr(outer_context, "target_domain_id", None)
            != expected_target_domain
            or getattr(outer_context, "target_backend_digest", None)
            != target_backend_digests[baseline_target.principal_id]
        ):
            raise AuthorityDenied("PackVM capability bridge outer identity is invalid")

        expected_fields = {
            "kind",
            "protocol",
            "version",
            "target",
            "request",
            "request_digest",
            "continuation",
        }
        target = bridge_request.get("target")
        request = bridge_request.get("request")
        continuation = bridge_request.get("continuation")
        expected_bridge_target = {
            "contract_id": _BRIDGED_AI_GENERATE_KEY[0],
            "operation_id": _BRIDGED_AI_GENERATE_KEY[1],
        }
        if (
            set(bridge_request) != expected_fields
            or bridge_request.get("kind") != "tobkiri.packvm.bridge.request.v1"
            or bridge_request.get("protocol") != _PACKVM_BRIDGE_PROTOCOL
            or bridge_request.get("version") != 1
            or not isinstance(target, Mapping)
            or dict(target) != expected_bridge_target
            or not isinstance(request, Mapping)
            or set(request) != {"messages", "requirements"}
            or not isinstance(request.get("messages"), list)
            or not request["messages"]
            or any(not isinstance(item, Mapping) for item in request["messages"])
            or request.get("requirements")
            != {"request_surface": "defaultspack.conversation"}
            or not isinstance(bridge_request.get("request_digest"), str)
            or not isinstance(continuation, Mapping)
        ):
            raise AuthorityDenied("PackVM capability bridge request is invalid")

        requested_payload = {
            "messages": list(request["messages"]),
            "requirements": {"request_surface": "defaultspack.conversation"},
        }
        try:
            if (
                len(canonical_json(requested_payload))
                > _PACKVM_BRIDGE_MAX_REQUEST_BYTES
                or bridge_request["request_digest"]
                != canonical_digest(requested_payload)
            ):
                raise AuthorityDenied("PackVM capability bridge request is invalid")
        except AuthorityDenied:
            raise
        except Exception as error:
            raise AuthorityDenied("PackVM capability bridge request is invalid") from error

        nonce = continuation.get("nonce")
        expected_continuation = {
            "kind": "tobkiri.packvm.continuation.v1",
            "protocol": _PACKVM_BRIDGE_PROTOCOL,
            "version": 1,
            "operation_id": _BASELINE_CONVERSATION_KEY[1],
            "nonce": nonce,
            "target": expected_bridge_target,
            "request_digest": bridge_request["request_digest"],
        }
        if (
            dict(continuation) != expected_continuation
            or not isinstance(nonce, str)
            or len(nonce) != 48
            or any(character not in "0123456789abcdef" for character in nonce)
        ):
            raise AuthorityDenied("PackVM capability bridge continuation is invalid")

        if not dispatch_holder:
            raise AuthorityDenied("PackVM capability bridge is not initialized")
        dispatch = dispatch_holder[0]
        dispatch.assert_current()
        request_id = getattr(outer_context, "request_id", None)
        if not isinstance(request_id, str) or not request_id or len(request_id) > 160:
            raise AuthorityDenied("PackVM capability bridge request identity is invalid")
        # This session identity is generated in the Host.  The guest nonce
        # binds its continuation but never becomes an Authority session id.
        bridge_session_id = (
            f"session.packvm-bridge.{request_id}.{secrets.token_hex(16)}"
        )
        try:
            provider_result = dispatch.invoke(
                _BRIDGED_AI_GENERATE_KEY[0],
                _BRIDGED_AI_GENERATE_KEY[1],
                {
                    "messages": requested_payload["messages"],
                    "requirements": requested_payload["requirements"],
                    "_session_id": bridge_session_id,
                },
            )
            if not isinstance(provider_result, Mapping):
                raise TypeError("verified AI capability returned a non-object")
            result = {"status": "ok", "value": dict(provider_result)}
            if len(canonical_json(result)) > _PACKVM_BRIDGE_MAX_RESULT_BYTES:
                raise ValueError("verified AI capability result is too large")
        except Exception:
            # Do not project provider/backend details through the PackVM ABI.
            # The guest receives a typed, bounded result it can safely render.
            result = _provider_unavailable_bridge_result()

        response = {
            "kind": "tobkiri.packvm.bridge.result.v1",
            "protocol": _PACKVM_BRIDGE_PROTOCOL,
            "version": 1,
            "operation_id": _BASELINE_CONVERSATION_KEY[1],
            "nonce": nonce,
            "target": expected_bridge_target,
            "request_digest": bridge_request["request_digest"],
            "result": result,
        }
        response["result_digest"] = canonical_digest(response["result"])
        if len(canonical_json(response)) > _PACKVM_BRIDGE_MAX_RESULT_BYTES:
            response["result"] = _provider_unavailable_bridge_result()
            response["result_digest"] = canonical_digest(response["result"])
        return response

    for registered_backend in registered_backends:
        if registered_backend.status.backend_id != _PYTHON_PACK_BACKEND_ID:
            continue
        binder = getattr(registered_backend, "bind_artifact_resolver", None)
        if binder is None:
            raise AuthorityDenied("production PackVM backend cannot bind authenticated artifacts")
        binder(artifact_resolver)
        domain_binder = getattr(
            registered_backend,
            "bind_target_domain_resolver",
            None,
        )
        if domain_binder is not None:
            domain_binder(authority_target_domain)
    if baseline_backend is not None:
        bridge_binder = getattr(baseline_backend, "bind_capability_bridge", None)
        if not callable(bridge_binder):
            raise AuthorityDenied("production PackVM backend cannot bind capability bridge")
        bridge_binder(capability_bridge)
    if not any(
        item.status.backend_id == _PYTHON_PACK_BACKEND_ID
        for item in registered_backends
    ):
        # The descriptor remains unavailable unless the composition root
        # supplies a real authenticated supervisor.  Registering the exact
        # disabled identity preserves a stable user-facing diagnostic without
        # substituting in-process Python execution.
        registered_backends += (_UnavailablePythonPackBackend(),)
    if control_backend is not None:
        registered_backends += (control_backend,)
    binding_by_function: dict[str, list[ResolvedOperationBinding]] = {}
    for resolved_binding in catalog_bindings:
        key = (
            resolved_binding.operation.contract_id,
            resolved_binding.operation.operation_id,
        )
        if key in approved_host_binding_keys:
            binding_by_function.setdefault(
                resolved_binding.function.function_id,
                [],
            ).append(resolved_binding)
    host_contributions_by_backend: dict[str, list[Any]] = {}
    close_callbacks: list[Callable[[], None]] = []
    credential_store_binding = (
        credential_store_factory(user_data_root=authority_user_data)
        if credential_store_factory is not None
        else None
    )
    principal_by_id = {
        principal.principal_id: principal
        for binding in plan["bindings"]
        for principal in (FunctionPrincipal.from_dict(binding["function_principal"]),)
    }
    pack_by_principal = {
        FunctionPrincipal.from_dict(binding["function_principal"]).principal_id: str(
            binding["pack_id"]
        )
        for binding in plan["bindings"]
    }

    class _InvocationSession:
        """Bind nested dispatch to the authenticated provider invocation."""

        def __init__(self, envelope: Any) -> None:
            self._envelope = envelope
            self.profile_id = str(profile["profile_id"])
            self.plan_digest = str(plan["plan_digest"])

        def provider_metadata(
            self, contract_id: str
        ) -> tuple[Mapping[str, Any], ...]:
            if not dispatch_holder:
                raise AuthorityDenied("Host Provider dispatch is not initialized")
            return dispatch_holder[0].provider_metadata(contract_id)

        def invoke(
            self,
            contract_id: str,
            operation_id: str,
            payload: Mapping[str, Any],
            *,
            version_range: str | None = None,
        ) -> Mapping[str, Any]:
            if not dispatch_holder:
                raise AuthorityDenied("Host Provider dispatch is not initialized")
            nested_session_id = (
                f"session.host-provider.{self._envelope.context.request_id}."
                f"{self._envelope.target_principal.value.removeprefix('sha256:')[:24]}"
            )
            return dispatch_holder[0].invoke(
                contract_id,
                operation_id,
                {**dict(payload), "_session_id": nested_session_id},
                version_range=version_range,
            )

    class _HostInvocation(HostProviderInvocationContextV4):
        """Expose only declared nested dispatch and one credential transport."""

        def __init__(self, envelope: Any) -> None:
            self._envelope = envelope
            self._client: GlobalContractClient | None = None
            self._client_binding: tuple[frozenset[str], str] | None = None

        @property
        def envelope(self) -> Any:
            return self._envelope

        def contract_client(
            self,
            *,
            allowed_contract_ids: frozenset[str],
            consumer_pack_id: str,
        ) -> GlobalContractClient:
            expected_pack_id = pack_by_principal.get(
                self._envelope.target_principal.value
            )
            binding = (allowed_contract_ids, consumer_pack_id)
            if expected_pack_id != consumer_pack_id:
                raise AuthorityDenied("Host Provider consumer identity is invalid")
            if self._client is not None:
                if binding != self._client_binding:
                    raise AuthorityDenied("Host Provider client binding changed")
                return self._client
            provider_principal = principal_by_id.get(
                self._envelope.target_principal.value
            )
            if provider_principal is None:
                raise AuthorityDenied("Host Provider principal is unavailable")
            transport = (
                AuthorizedEnvelopeCredentialTransport(
                    envelope=self._envelope,
                    provider_principal=provider_principal,
                    store=credential_store_binding.store,
                    authority_store=authority_store,
                    current_security_epoch=lambda: authority_store.security_epoch,
                    credential_key_version=credential_store_binding.key_version,
                    consumer_pack_id=consumer_pack_id,
                )
                if credential_store_binding is not None
                else None
            )
            self._client = GlobalContractClient(
                session=_InvocationSession(self._envelope),
                allowed_contract_ids=allowed_contract_ids,
                consumer_pack_id=consumer_pack_id,
                host_credential_transport=transport,
            )
            self._client_binding = binding
            return self._client

    def invocation_context(envelope: Any) -> HostProviderInvocationContextV4:
        return _HostInvocation(envelope)

    for function_id, provider_bindings in sorted(binding_by_function.items()):
        captured_bindings = tuple(provider_bindings)
        factory, backend_id = _load_verified_host_provider_factory(
            pack_roots[provider_bindings[0].artifact.pack_id],
            function_id,
            captured_bindings,
        )
        if factory is None:
            continue
        if factory.function_id != function_id:
            raise AuthorityDenied("Host Provider hook Function identity changed")
        captured_provider = factory.capture(
            HostProviderCaptureContextV4(
                profile_id=str(profile["profile_id"]),
                plan_digest=str(plan["plan_digest"]),
                security_epoch=int(active.activation["security_epoch"]),
                activation=active.activation,
                state_root=authority_user_data / "host_provider_state",
                provider_bindings=captured_bindings,
                catalog_bindings=catalog_bindings,
                domain_ids=dynamic_domain_ids,
                user_data_root=authority_user_data,
            )
        )
        expected_keys = {
            (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            for binding in provider_bindings
        }
        if {item.key for item in captured_provider.contributions} != expected_keys:
            captured_provider.close()
            raise AuthorityDenied("Host Provider hook contribution set is incomplete")
        host_contributions_by_backend.setdefault(backend_id, []).extend(
            captured_provider.contributions
        )
        close_callbacks.append(captured_provider.close)
    for backend_id, contributions in sorted(host_contributions_by_backend.items()):
        registered_backends += (
            ExactHostProviderBackendV4(
                tuple(contributions),
                backend_id=backend_id,
                profile_id=str(profile["profile_id"]),
                plan_digest=str(plan["plan_digest"]),
                security_epoch=int(active.activation["security_epoch"]),
                invocation_context=invocation_context,
            ),
        )
    backend_registry = BackendRegistry(registered_backends)
    for binding in plan["bindings"]:
        target = FunctionPrincipal.from_dict(binding["function_principal"])
        if target.principal_id in target_backend_digests:
            continue
        try:
            resolved_binding = runtime.composition.catalog.resolve_pinned(
                binding["contract_id"],
                binding["operation_id"],
            )
            selected_backend = backend_registry.select(resolved_binding)
        except Exception:
            continue
        target_backend_digests[target.principal_id] = selected_backend.status.backend_digest
    broker = runtime.broker(
        authority_store=authority_store,
        adapters=AdapterPlanner(()),
        adapter_executor=_NoAdapterExecution(),
        backends=backend_registry,
        materialization=MaterializationCoordinator(),
        admission=_FiniteAdmission(),
        reconciliation=InMemoryReconciliationStore(),
        authority_adapter=authority_control,
    )
    activation_digest = canonical_digest(active.activation)
    target_by_operation = {
        (item["contract_id"], item["operation_id"]): FunctionPrincipal.from_dict(
            item["function_principal"]
        )
        for item in plan["bindings"]
    }

    caller_sessions: set[str] = set()
    caller_sessions_lock = threading.RLock()

    def context_for(contract_id: str, operation_id: str, session_id: str) -> RequestContext:
        key = (contract_id, operation_id)
        caller = caller_by_operation[key]
        target = target_by_operation[key]
        target_suffix = target.principal_id.removeprefix("sha256:")[:24]
        # One authenticated panel session may invoke operations whose
        # resolved Shell caller principals differ.  Authority session
        # bindings are principal-specific, so include that exact caller in
        # the Host-derived session identity instead of reusing a session
        # already bound to another caller.
        caller_identity_suffix = caller.principal_id.removeprefix("sha256:")[:24]
        authority_session_id = f"{session_id}.{caller_identity_suffix}.{activation_suffix}"
        caller_suffix = canonical_digest(
            {"session_id": authority_session_id, "caller": caller.principal_id}
        ).removeprefix("sha256:")[:24]
        context = RequestContext(
            request_id="request." + secrets.token_hex(16),
            trace_id="trace." + secrets.token_hex(16),
            caller_principal=OpaqueAuthorityRef(caller.principal_id),
            profile_id=str(profile["profile_id"]),
            activation_id=str(active.activation["activation_id"]),
            activation_digest=activation_digest,
            plan_digest=str(plan["plan_digest"]),
            security_epoch=int(active.activation["security_epoch"]),
            caller_session_id=authority_session_id,
            caller_domain_id=f"domain.panel.{caller_suffix}.{activation_suffix}",
            caller_boot_epoch=1,
            target_domain_id=f"domain.provider.{target_suffix}.{activation_suffix}",
            target_boot_epoch=1,
            target_backend_digest=target_backend_digests.get(
                target.principal_id,
                canonical_digest(
                    {
                        "backend": "captured-but-not-materialized",
                        "target": target.principal_id,
                    }
                ),
            ),
            profile_authority_digest=str(active.activation["profile_authority_snapshot_digest"]),
            fencing_token=int(active.activation["fencing_token"]),
            handle_namespace=f"activation.{target_suffix}",
        )
        with caller_sessions_lock:
            if authority_session_id not in caller_sessions:
                caller_domain = _execution_domain(
                    domain_id=context.caller_domain_id,
                    principal=caller,
                    active=active,
                    boundary=DomainBoundary.UNPRIVILEGED_WORKER,
                    channel_seed=f"panel:{session_id}",
                )
                _register_exact_domain(
                    authority_store,
                    authority_control,
                    caller_domain,
                    session_id=authority_session_id,
                    principal=caller,
                )
                caller_sessions.add(authority_session_id)
        return context

    providers: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for binding in plan["bindings"]:
        resolved_binding = broker._catalog.resolve_pinned(
            binding["contract_id"],
            binding["operation_id"],
        )
        backend_error: str | None = None
        projected_backend: ExecutionBackend | None
        try:
            projected_backend = broker._backends.select(resolved_binding)
        except Exception as error:
            projected_backend = None
            backend_error = str(error) or "production backend is unavailable"
        function_principal = binding["function_principal"]
        providers.setdefault(binding["contract_id"], ())
        providers[binding["contract_id"]] += (
            {
                "provider_id": function_principal["function_id"],
                "function_id": function_principal["function_id"],
                "principal_id": resolved_binding.principal_ref.value,
                "implementation_digest": resolved_binding.function.implementation_digest,
                "contract_id": binding["contract_id"],
                "operation_id": binding["operation_id"],
                "artifact_digest": binding["artifact_digest"],
                **(
                    {
                        "backend_id": projected_backend.status.backend_id,
                        "backend_digest": projected_backend.status.backend_digest,
                    }
                    if projected_backend is not None
                    else {}
                ),
                **(
                    {"backend_unavailable_reason": backend_error}
                    if backend_error is not None
                    else {}
                ),
                "profile_id": profile["profile_id"],
                "plan_digest": plan["plan_digest"],
            },
        )

    captured_activation = dict(active.activation)
    captured_profile = dict(active.resolved.profile)
    captured_lock = dict(active.resolved.lock)
    captured_plan = dict(active.resolved.plan)

    def assert_current_capture() -> None:
        from ..pack_control_v4 import PackControlDenied
        from .profile_capture import capture_default_profile

        # Reuse only the explicit operation-local capture opened by the HTTP
        # boundary or runtime-surface operation. Outside that scope this is
        # still a fresh canonical capture on every assertion.
        current = capture_default_profile()
        if (
            dict(current.activation) != captured_activation
            or dict(current.resolved.profile) != captured_profile
            or dict(current.resolved.lock) != captured_lock
            or dict(current.resolved.plan) != captured_plan
            or authority_store.security_epoch != int(captured_activation["security_epoch"])
        ):
            raise AuthorityDenied(
                "captured Defaults activation is stale",
                code="stale_revision",
            )
        if _pack_root_identities(pack_roots) != captured_pack_root_identities:
            raise AuthorityDenied(
                "captured Pack filesystem identity changed",
                code="digest_mismatch",
            )
        for pack_id, approval_revision in captured_dynamic_approvals.items():
            try:
                current_approval = capture_valid_pack_approval(pack_id)
            except PackControlDenied:
                raise
            except Exception as error:
                raise AuthorityDenied("captured optional Pack approval is unavailable") from error
            if current_approval.get("approval_revision") != approval_revision:
                raise AuthorityDenied(
                    "captured optional Pack approval changed",
                    code="digest_mismatch",
                )

    dispatch = runtime.dispatch_session(
        broker=broker,
        context_for=context_for,
        effect_scope_for=lambda contract_id, operation_id, _payload: scope_by_operation[
            (contract_id, operation_id)
        ].to_dict(),
        providers=providers,
        authority_control=authority_control,
        current_capture_check=assert_current_capture,
        owned_authority_store=authority_store,
        close_callbacks=(
            *close_callbacks,
            *((control_session.close,) if control_session is not None else ()),
        ),
        stop_callbacks=(
            (control_session.cancel_pending_reads,) if control_session is not None else ()
        ),
    )
    dispatch_holder.append(dispatch)
    if control_session is not None and frontend_contract_bindings:
        capability_bindings = tuple(
            binding
            for binding in frontend_contract_bindings
            if getattr(binding, "method", "") == "POST"
            and getattr(binding, "path", "") == "/api/ui/capability/invoke"
        )
        if len(capability_bindings) != 1:
            dispatch.close()
            raise AuthorityDenied("capability invocation binding is absent or ambiguous")
        capability_binding = capability_bindings[0]

        def capability_binding_reader() -> Mapping[str, Any]:
            from ..capability_bindings_v4 import capture_capability_binding_snapshot
            from ..pack_control_v4 import capture_pack_catalog_reader

            snapshot = capture_capability_binding_snapshot(
                capability_binding,
                session=dispatch,
                catalog=capture_pack_catalog_reader().read(),
            )
            return snapshot.to_mapping(
                profile_id=dispatch.profile_id,
                plan_digest=dispatch.plan_digest,
            )

        control_session.bind_capability_reader(capability_binding_reader)
    return dispatch


__all__ = ["capture_production_dispatch"]
