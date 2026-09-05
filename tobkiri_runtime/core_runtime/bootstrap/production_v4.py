"""Assemble the live Pack v4 composition from one active snapshot."""

from __future__ import annotations

import json
import os
import secrets
import stat
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from tobkiri_host.admission import (
    AdmissionEstimate,
    DurableResourceLedger,
    FairAdmissionQueue,
    QueueScope,
    ResourceAmount,
)
from tobkiri_host.artifact_materialization import capture_materialized_artifact
from tobkiri_host.backends import BackendRegistry, BackendStatus, ExecutionBackend
from tobkiri_host.broker import AdmissionTicket, RequestAdmissionPort
from tobkiri_host.composition import AuthorityCeilings
from tobkiri_host.contracts import (
    AdapterPlanner,
    ResolvedOperationBinding,
    StructuralAdapter,
)
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
from tobkiri_host.interactive_effects import (
    LateBoundInteractiveEffectPort,
    PendingEffectController,
)
from tobkiri_host.runtime import ProductionRuntimeV4, V4DispatchSession
from tobkiri_host.workspace_mutation import (
    HostWorkspaceMutationPort,
    WorkspaceMutationBinding,
    WorkspaceMutationCoordinator,
)
from tobkiri_protocol.canonical import canonical_digest, canonical_json
from tobkiri_protocol.errors import ProtocolError
from tobkiri_protocol.platform_artifact import verify_platform_artifact
from tobkiri_protocol.secure_persistence import (
    SecureDirectory,
    SecurePersistenceError,
)

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
    RuntimeSurfaceFactory,
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
from ..interactive_effect_coordinator import (
    CapturedInteractiveEffectRoute,
    HostInteractiveEffectService,
    INTERACTIVE_EFFECT_COORDINATOR_CONTRACT_ID,
    INTERACTIVE_EFFECT_COORDINATOR_OPERATION_ID,
    INTERACTIVE_EFFECT_SPECS,
)


def _allow_unsigned_development_shell(catalog: Any) -> bool:
    """Allow only the generated checkout Shell to omit a macOS signature."""

    if os.environ.get("RUMI_ENVIRONMENT") != "development":
        return False
    runtime_root = Path(__file__).resolve().parents[2]
    configured_app = os.environ.get("RUMI_APP_DIR")
    artifact_root = catalog.artifact_root
    if artifact_root is None:
        return False
    expected_artifacts = (
        runtime_root.parent
        / "tobkiri_launcher"
        / "src-tauri"
        / "target"
        / "dev-defaults"
        / "platform-artifacts"
    )
    bundled_artifacts = (
        runtime_root / "bundled" / "dev-defaults" / "platform-artifacts"
    )
    try:
        checkout_artifacts_match = (
            expected_artifacts.is_dir()
            and not expected_artifacts.is_symlink()
            and artifact_root.resolve(strict=True)
            == expected_artifacts.resolve(strict=True)
        )
        bundled_artifacts_match = (
            bundled_artifacts.is_dir()
            and not bundled_artifacts.is_symlink()
            and artifact_root.resolve(strict=True)
            == bundled_artifacts.resolve(strict=True)
        )
        configured_app_matches = (
            configured_app is not None
            and Path(configured_app).resolve(strict=True)
            == runtime_root.resolve(strict=True)
        )
        return configured_app_matches and (
            checkout_artifacts_match or bundled_artifacts_match
        )
    except OSError:
        return False


_CONTROL_CONTRACTS = {PACK_CONTROL_CONTRACT, CONTROL_PRESENTATION_CONTRACT}
_PACKVM_BRIDGE_PROTOCOL = "io.tobkiri.packvm.bridge.v1"
_PACKVM_BRIDGE_MAX_REQUEST_BYTES = 64 * 1024
_PACKVM_BRIDGE_MAX_RESULT_BYTES = 512 * 1024
# Keep resource-axis defaults aligned with the bounded admission queue.  A
# request reservation remains held while a verified provider performs a
# nested Host dispatch, so ResourceAmount's constructor defaults of one slot
# would reject valid nested work before the queue bounds are reached.
_DEFAULT_RUNTIME_RESOURCE_SLOTS = 256
_DEFAULT_PROFILE_RESOURCE_SLOTS = 64
_REQUESTED_EDGE_AUTHORITY_MODES = frozenset({"profile_grant", "interactive_only"})


class ActivationSnapshotLoader(Protocol):
    """Application-provided verification of the persisted activation snapshot."""

    def __call__(
        self,
        *,
        active: object,
        workspace: Path,
        profile_id: str,
        authority_store: AuthorityStore,
        catalog: object,
    ) -> object:
        """Load the active snapshot using the app's verified Profile store."""


class CapabilityBindingSnapshotFactory(Protocol):
    """Application-owned capability snapshot projection for control reads."""

    def __call__(
        self,
        binding: object,
        *,
        session: object,
        catalog: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Return a serializable capability snapshot bound to this dispatch."""


class CapabilityBindingSelector(Protocol):
    """Application-owned selection of its capability HTTP binding."""

    def __call__(self, bindings: tuple[object, ...]) -> object | None:
        """Return one immutable application capability binding or ``None``."""


@dataclass(frozen=True)
class _CapturedPlanEdge:
    """One Profile edge joined to its signed plan and verified target."""

    key: tuple[str, str, str, str, str, str]
    binding_key: tuple[str, str, str]
    edge: Mapping[str, Any]
    binding: Mapping[str, Any]
    resolved_binding: ResolvedOperationBinding
    caller: FunctionPrincipal
    target: FunctionPrincipal
    ceilings: AuthorityCeilings
    authority_mode: str


class _UnavailablePackVmBackend:
    """Exact fail-closed registration for one missing PackVM backend."""

    def __init__(self, backend_id: str) -> None:
        self.status = BackendStatus(
            backend_id=backend_id,
            execution_kind=ExecutionKind.PACK_VM,
            platform="any",
            backend_digest=canonical_digest(
                {
                    "backend": backend_id,
                    "state": "authenticated-supervisor-unavailable",
                }
            ),
            production_enabled=False,
            conformance_only=True,
            unavailable_reason=(
                "authenticated PackVM supervisor is not registered for the selected backend"
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


def _authenticated_packvm_backend(
    backend_factory: Callable[[], ExecutionBackend | None] | None = None,
) -> ExecutionBackend | None:
    """Admit only a composition-verified production PackVM backend."""

    if backend_factory is None:
        return None

    try:
        backend = backend_factory()
        status = getattr(backend, "status", None)
        if (
            backend is None
            or status is None
            or getattr(status, "execution_kind", None) is not ExecutionKind.PACK_VM
            or getattr(status, "production_enabled", None) is not True
            or getattr(status, "conformance_only", None) is not False
            or not str(getattr(status, "backend_digest", "")).startswith("sha256:")
        ):
            return None
        return backend
    except Exception:
        # This is a capability promotion boundary.  The Host must remain
        # unavailable when any direct-VZ evidence, identity, or constructor
        # check cannot be established; no alternate VM substrate is eligible.
        return None


class _NoAdapterExecution:
    def execute(self, adapter: StructuralAdapter, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError(f"unexpected structural adapter: {adapter.adapter_id}")


class _PlanAdmission(RequestAdmissionPort):
    """Plan-bound Host admission with measured, durable reservations."""

    def __init__(
        self,
        *,
        profile_id: str,
        activation_id: str,
        plan: Mapping[str, Any],
        state_path: Path,
    ) -> None:
        self._profile_id = profile_id
        self._plan_digest = str(plan["plan_digest"])
        policy = _admission_mapping(plan.get("admission_policy"))
        host_memory = _host_memory_bytes()
        runtime_limit = _positive_int(
            policy.get("runtime_limit_bytes"),
            default=max(host_memory, 64 * 1024 * 1024),
        )
        guard = _nonnegative_int(
            policy.get("host_free_guard_bytes"),
            default=max(16 * 1024 * 1024, runtime_limit // 10),
        )
        if guard >= runtime_limit:
            raise AuthorityDenied("Host admission free-resource guard exceeds runtime limit")
        profile_limit = _positive_int(
            policy.get("profile_limit_bytes"),
            default=runtime_limit - guard,
        )
        self._ledger = DurableResourceLedger(
            runtime_limit=ResourceAmount(
                memory_bytes=runtime_limit,
                process_slots=_DEFAULT_RUNTIME_RESOURCE_SLOTS,
                start_slots=_DEFAULT_RUNTIME_RESOURCE_SLOTS,
            ),
            host_free_guard=ResourceAmount(
                memory_bytes=guard,
                process_slots=0,
                start_slots=0,
            ),
            profile_limits={
                profile_id: ResourceAmount(
                    memory_bytes=profile_limit,
                    process_slots=_DEFAULT_PROFILE_RESOURCE_SLOTS,
                    start_slots=_DEFAULT_PROFILE_RESOURCE_SLOTS,
                )
            },
            state_path=state_path,
            identity={
                "profile_id": profile_id,
                "profile_revision": str(plan["profile_revision"]),
                "activation_id": activation_id,
                "plan_digest": self._plan_digest,
            },
        )
        self._queue = FairAdmissionQueue(self._ledger)
        self._policy = policy
        self._binding_policies = {
            (
                str(item["contract_id"]),
                str(item["operation_id"]),
                canonical_digest(item["function_principal"]),
            ): _admission_mapping(item.get("admission"))
            for item in plan.get("bindings", ())
        }

    def estimate(
        self,
        context: RequestContext,
        binding: ResolvedOperationBinding,
        payload: Mapping[str, Any],
    ) -> AdmissionEstimate:
        if context.profile_id != self._profile_id or context.plan_digest != self._plan_digest:
            raise AuthorityDenied("admission context is outside the captured plan")
        policy = dict(self._policy)
        policy.update(
            self._binding_policies.get(
                (
                    binding.operation.contract_id,
                    binding.operation.operation_id,
                    binding.principal_ref.value,
                ),
                {},
            )
        )
        measured = _measure_payload_bytes(payload)
        upper_bound = _positive_int(
            policy.get("declared_upper_bound_bytes"),
            default=max(measured, 4096),
        )
        if measured > upper_bound:
            raise AuthorityDenied("request exceeds the selected Provider resource bound")
        return AdmissionEstimate(
            measured_p95_bytes=measured,
            declared_minimum_bytes=_nonnegative_int(
                policy.get("declared_minimum_bytes"),
                default=measured,
            ),
            runtime_floor_bytes=_nonnegative_int(
                policy.get("runtime_floor_bytes"),
                default=min(max(measured, 4096), upper_bound),
            ),
            profile_reservation_bytes=_nonnegative_int(
                policy.get("profile_reservation_bytes"),
                default=measured,
            ),
            backend_overhead_bytes=_nonnegative_int(
                policy.get("backend_overhead_bytes"),
                default=0,
            ),
            declared_upper_bound_bytes=upper_bound,
            concurrency=_positive_int(policy.get("concurrency"), default=1),
            disk_bytes=_nonnegative_int(policy.get("disk_bytes"), default=0),
        )

    def acquire(
        self,
        scope: QueueScope,
        estimate: AdmissionEstimate,
        wait_timeout_seconds: float,
    ) -> AdmissionTicket:
        if wait_timeout_seconds <= 0:
            raise TimeoutError("admission deadline expired")
        reservation = self._queue.admit(
            scope,
            estimate.charge(),
            wait_timeout_seconds=wait_timeout_seconds,
        )
        return AdmissionTicket(reservation)

    def release(self, ticket: AdmissionTicket) -> None:
        self._ledger.release(ticket.reservation.reservation_id)


def _admission_mapping(value: object) -> dict[str, Any]:
    """Return bounded declarative admission metadata or an empty mapping."""

    return dict(value) if isinstance(value, Mapping) else {}


def _measure_payload_bytes(payload: Mapping[str, Any]) -> int:
    """Measure a validated operation payload using the Broker's JSON profile.

    Provider contracts may carry finite floating-point deadline values, while
    ``canonical_json`` intentionally accepts only strict I-JSON values.  The
    Broker request digest uses this JSON profile as well, so admission sizing
    must measure the same serialized request without widening the accepted
    value set to non-finite numbers or unsupported objects.
    """

    try:
        return len(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8", errors="strict")
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise AuthorityDenied("request payload cannot be canonically measured") from error


def _positive_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AuthorityDenied("admission resource values must be positive integers")
    return value


def _nonnegative_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthorityDenied("admission resource values must be non-negative integers")
    return value


def _host_memory_bytes() -> int:
    """Measure the Host's physical memory without consulting a Pack."""

    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return 0
    return pages * page_size if pages > 0 and page_size > 0 else 0


def _pack_root_identities(pack_roots: Mapping[str, Path]) -> dict[str, tuple[int, int]]:
    """Reject symlinked roots and capture exact Pack-root identities.

    Indexed artifacts are opened separately through the descriptor-relative,
    no-follow materialization path.  Walking every unrelated file here would
    make ordinary Pack assets (for example npm ``.bin`` links) part of the
    execution authority without improving the selected artifact binding.
    """

    identities: dict[str, tuple[int, int]] = {}
    for pack_id, root in sorted(pack_roots.items()):
        try:
            stat_result = root.lstat()
        except OSError as exc:
            raise AuthorityDenied(f"selected Pack root is unavailable: {pack_id}") from exc
        if stat.S_ISLNK(stat_result.st_mode) or not stat.S_ISDIR(stat_result.st_mode):
            raise AuthorityDenied(f"selected Pack root is unavailable: {pack_id}")
        identities[pack_id] = (int(stat_result.st_dev), int(stat_result.st_ino))
    return identities


def _host_profile_catalog(
    bundle_root: Path,
    *,
    authority_user_data: Path,
) -> object:
    """Capture the Host catalog without dropping registry-owned Profiles.

    ``BundledCatalog.load`` verifies the Host-global artifact inventory, but it
    intentionally contains only packaged Profile documents.  Runtime capture
    also needs the immutable successor definition selected by the Host
    registry, especially after a Named Profile activation.  Keep both inputs
    in one catalog snapshot so activation revalidation and dispatch cannot
    disagree about Profile identity.
    """

    from .profile_capture import host_profile_catalog

    return host_profile_catalog(
        base_dir=authority_user_data,
        bundle_root=bundle_root,
        user_data_root=authority_user_data,
    )


def _shell_artifact(
    catalog: Any,
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
            require_macos_code_signature=not _allow_unsigned_development_shell(catalog),
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


def _requested_edge_authority_mode(
    edge: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> str:
    """Require Profile and ResolvedPlan policy fields to agree exactly."""

    edge_mode = edge.get("authority_mode", "profile_grant")
    binding_mode = binding.get("authority_mode", "profile_grant")
    if (
        edge_mode not in _REQUESTED_EDGE_AUTHORITY_MODES
        or binding_mode not in _REQUESTED_EDGE_AUTHORITY_MODES
        or edge_mode != binding_mode
    ):
        raise AuthorityDenied("ResolvedPlan edge authority mode changed")
    return str(edge_mode)


def _authority_ceilings_for_edge(
    edge: Mapping[str, Any],
    target: FunctionPrincipal,
) -> AuthorityCeilings:
    """Resolve the three independent authority axes for one Profile edge.

    New plan producers may expose axis templates under ``authority_axes`` (or
    the equivalent top-level names).  Older v4 Profiles contain one committed
    operation scope; those records are converted into three independent
    immutable objects for compatibility, while every new axis remains
    constrained by the exact operation.  No axis is widened or inferred from
    a provider name.
    """

    operation_scope = _committed_operation_scope(edge, target)
    axis_templates = edge.get("authority_axes")
    if not isinstance(axis_templates, Mapping):
        axis_templates = {}

    def axis_scope(name: str) -> AuthorityScope:
        template = edge.get(f"{name}_scope_template")
        if template is None:
            template = axis_templates.get(name)
        if template is None:
            return AuthorityScope.from_dict(operation_scope.to_dict())
        if not isinstance(template, Mapping):
            raise AuthorityDenied(f"Profile authority axis {name} is invalid")
        try:
            selected = AuthorityScope.from_dict(template)
        except Exception as exc:
            raise AuthorityDenied(f"Profile authority axis {name} is invalid") from exc
        if not selected.is_subset_of(operation_scope):
            raise AuthorityDenied(f"Profile authority axis {name} expands its edge")
        return selected

    return AuthorityCeilings(
        caller_effect=axis_scope("caller_effect"),
        runtime_safety=axis_scope("runtime_safety"),
        profile_admin=axis_scope("profile_admin"),
    )


def _execution_domain(
    *,
    domain_id: str,
    principal: FunctionPrincipal,
    active: Any,
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
        variant.execution_kind is not ExecutionKind.HOST_EXTENSION for variant in artifact.variants
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
    active: Any,
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


def _commit_plan_authority(
    store: AuthorityStore,
    control: Any,
    *,
    active: Any,
    caller: FunctionPrincipal,
    target: FunctionPrincipal,
    contract_id: str,
    caller_publisher_lineage: str,
    target_publisher_lineage: str,
    target_domain: ExecutionDomain,
    scope: AuthorityScope,
    authority_label: str = "profile-edge",
    authority_mode: str = "profile_grant",
    pack_approval_revision: str | None = None,
    host_extension_binding: ResolvedOperationBinding | None = None,
) -> None:
    if authority_mode not in _REQUESTED_EDGE_AUTHORITY_MODES:
        raise AuthorityDenied("Profile edge authority mode is invalid")
    activation = active.activation
    profile = active.resolved.profile
    profile_identity = str(profile["profile_id"])
    decided_at = datetime.fromisoformat(
        str(activation["created_at"]).replace("Z", "+00:00")
    ).timestamp()
    identity_suffix = str(activation["activation_id"]).replace(":", ".")
    contract_suffix = authority_digest({"contract_id": contract_id}).removeprefix("sha256:")[:24]
    operation_suffix = target.operation_id.replace(".", "-")
    caller_suffix = caller.principal_id.removeprefix("sha256:")[:24]
    target_suffix = target.principal_id.removeprefix("sha256:")[:24]
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
        record_id=(
            f"provider.{profile_identity}.{authority_label}."
            f"{contract_suffix}.{operation_suffix}.{caller_suffix}."
            f"{target_suffix}.{record_identity}"
        ),
        provider=target,
        execution_domain_id=target_domain.domain_id,
        execution_domain_identity_digest=target_domain.identity_digest,
        scope=scope,
        authority_mode=AuthorityMode.LEASE_ONLY,
        security_epoch=int(activation["security_epoch"]),
        trust_provenance_digest=canonical_digest(
            {
                "source": f"locked-{profile_identity}-profile",
                "plan_digest": activation["plan_digest"],
                "target": target.to_dict(),
            }
        ),
        publisher_lineage=(
            host_extension_trust.publisher_lineage
            if host_extension_trust is not None
            else target_publisher_lineage
        ),
        host_extension_id=(
            host_extension_trust.trust_id if host_extension_trust is not None else "runtime-tcb"
        ),
        valid_from=decided_at,
        host_broker_binding="tobkiri.request-broker.v4",
    )
    if authority_mode == "interactive_only":
        interactive_existing = (
            (
                store.get_host_extension_trust(host_extension_trust.trust_id)
                if host_extension_trust is not None
                else None
            ),
            store.get_provider_authority(provider.record_id),
        )
        interactive_expected = (host_extension_trust, provider)
        if interactive_existing == (None, None):
            control.commit_provider_authority_bundle(
                host_extension_trust=host_extension_trust,
                provider_authorities=(provider,),
            )
        elif (
            host_extension_trust is not None
            and interactive_existing == (host_extension_trust, None)
        ):
            control.commit_provider_authority_bundle(
                provider_authorities=(provider,),
            )
        elif interactive_existing != interactive_expected:
            raise AuthorityDenied("Pack catalog authority snapshot changed")
        return
    # A Pack approval revision names the stable user decision, not one runtime
    # activation.  The immutable Authority snapshot below is activation-bound,
    # so every record in its bundle must use the activation generation as part
    # of its durable identity.  This preserves prior rows without replaying or
    # colliding with them when an unchanged Pack is activated again.
    approval = ApprovalRecord(
        approval_id=(
            f"approval.{profile_identity}.{authority_label}."
            f"{contract_suffix}.{operation_suffix}.{caller_suffix}."
            f"{target_suffix}.{record_identity}"
        ),
        snapshot_digest=canonical_digest(
            {
                "ceremony": f"{profile_identity}.activate",
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
            else f"user.{profile_identity}-confirmation"
        ),
        decision="approved",
        decided_at=decided_at,
        caller=caller,
        target=target,
        profile_id=str(profile["profile_id"]),
        effect_bundle_digest=scope.digest,
        security_epoch=int(activation["security_epoch"]),
    )
    grant = GrantRecord(
        grant_id=(
            f"grant.{profile_identity}.{authority_label}."
            f"{contract_suffix}.{operation_suffix}.{caller_suffix}."
            f"{target_suffix}.{record_identity}"
        ),
        caller=caller,
        target=target,
        profile_id=str(profile["profile_id"]),
        activation_id=str(activation["activation_id"]),
        profile_authority_digest=str(activation["profile_authority_snapshot_digest"]),
        caller_publisher_lineage=caller_publisher_lineage,
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
    elif (
        host_extension_trust is not None
        and existing == (host_extension_trust, None, None, None)
    ):
        control.commit_approval_bundle(
            approval,
            provider_authorities=(provider,),
            grants=(grant,),
        )
    elif existing != expected:
        raise AuthorityDenied("Pack catalog authority snapshot changed")


def _packvm_approval_provenance(
    *,
    caller_artifact_digest: str,
    target_pack_id: str,
    optional_pack_ids: set[str],
    pack_ids_by_artifact_digest: Mapping[str, set[str]],
) -> tuple[bool, str | None]:
    """Resolve one unambiguous optional-Pack approval source for a PackVM edge."""

    caller_pack_ids = pack_ids_by_artifact_digest.get(caller_artifact_digest, set())
    if len(caller_pack_ids) != 1:
        return False, None
    caller_pack_id = next(iter(caller_pack_ids))
    approval_pack_ids = {
        pack_id for pack_id in (target_pack_id, caller_pack_id) if pack_id in optional_pack_ids
    }
    if len(approval_pack_ids) > 1:
        return False, None
    return True, next(iter(approval_pack_ids), None)


def _static_profile_pack_ids(
    catalog: Any,
    profile_id: str,
) -> frozenset[str]:
    """Return the canonical Profile closure before optional Pack additions."""

    definition = catalog.profiles.get(profile_id)
    if definition is None:
        raise AuthorityDenied("selected Profile is absent from the verified catalog")
    selected = [str(item["pack_id"]) for item in definition["packs"]]
    pending = list(selected)
    while pending:
        pack_id = pending.pop(0)
        manifest = catalog.packs.get(pack_id)
        if manifest is None:
            raise AuthorityDenied("selected Profile dependency is absent from the verified catalog")
        for dependency_id in manifest["requirements"]["pack_dependencies"]:
            dependency = str(dependency_id)
            if dependency not in selected:
                selected.append(dependency)
                pending.append(dependency)
    return frozenset(selected)


def _bridge_targets_by_outer_edge(
    edges: tuple[_CapturedPlanEdge, ...],
) -> dict[tuple[str, str, str, str, str, str], tuple[_CapturedPlanEdge, ...]]:
    """Derive Host continuation targets from signed Profile edges only.

    A PackVM may receive a bridge only to non-PackVM edges whose signed caller
    is that PackVM Function.  The guest selects one of those exact edges by
    Contract and operation; provider names, product IDs, and operation-only
    maps cannot create a continuation capability.
    """

    result: dict[tuple[str, str, str, str, str, str], tuple[_CapturedPlanEdge, ...]] = {}
    for outer in edges:
        if outer.resolved_binding.variant.execution_kind is not ExecutionKind.PACK_VM:
            continue
        candidates = tuple(
            candidate
            for candidate in edges
            if candidate.caller.principal_id == outer.target.principal_id
            and candidate.resolved_binding.variant.execution_kind is not ExecutionKind.PACK_VM
            and candidate.binding["contract_id"] not in _CONTROL_CONTRACTS
        )
        if candidates:
            result[outer.key] = candidates
    return result


def _interactive_effect_coordinator_factory(
    factories: tuple[tuple[str, tuple[ResolvedOperationBinding, ...], Any, str], ...],
) -> tuple[str, tuple[ResolvedOperationBinding, ...], Any, str] | None:
    """Return the one exact coordinator Factory or reject a widened capture."""

    selected = tuple(
        item
        for item in factories
        if getattr(item[2], "requires_interactive_effect_port", False) is True
    )
    if len(selected) > 1:
        raise AuthorityDenied("interactive effect coordinator is ambiguous")
    if not selected:
        return None
    coordinator = selected[0]
    bindings = coordinator[1]
    if (
        len(bindings) != 1
        or coordinator[0] != bindings[0].function.function_id
        or bindings[0].operation.contract_id != INTERACTIVE_EFFECT_COORDINATOR_CONTRACT_ID
        or bindings[0].operation.operation_id != INTERACTIVE_EFFECT_COORDINATOR_OPERATION_ID
    ):
        raise AuthorityDenied("interactive effect coordinator binding is invalid")
    return coordinator


def _captured_interactive_effect_routes(
    edges: tuple[_CapturedPlanEdge, ...],
    *,
    coordinator_principal: OpaqueAuthorityRef,
    dynamic_domain_ids: Mapping[tuple[str, str, str], str],
) -> tuple[CapturedInteractiveEffectRoute, ...]:
    """Build finite prepare/execute pairs from signed Profile edges only."""

    routes: list[CapturedInteractiveEffectRoute] = []
    for spec in INTERACTIVE_EFFECT_SPECS.values():
        prepare_edges = tuple(
            edge
            for edge in edges
            if edge.caller.principal_id == coordinator_principal.value
            and edge.resolved_binding.operation.contract_id == spec.prepare_contract_id
            and edge.resolved_binding.operation.operation_id == spec.prepare_operation_id
        )
        execute_edges = tuple(
            edge
            for edge in edges
            if edge.caller.principal_id == coordinator_principal.value
            and edge.resolved_binding.operation.contract_id == spec.execute_contract_id
            and edge.resolved_binding.operation.operation_id == spec.execute_operation_id
        )
        if not prepare_edges and not execute_edges:
            continue
        if len(prepare_edges) != 1 or len(execute_edges) != 1:
            raise AuthorityDenied("interactive effect route is ambiguous")
        prepare_edge = prepare_edges[0]
        execute_edge = execute_edges[0]
        if (
            prepare_edge.authority_mode != "profile_grant"
            or execute_edge.authority_mode != "interactive_only"
            or execute_edge.resolved_binding.artifact.package_kind is not PackageKind.HOST_EXTENSION
            or execute_edge.resolved_binding.variant.execution_kind
            is not ExecutionKind.HOST_EXTENSION
        ):
            raise AuthorityDenied("interactive effect route authority is invalid")
        execute_key = (
            execute_edge.resolved_binding.operation.contract_id,
            execute_edge.resolved_binding.operation.operation_id,
            execute_edge.target.principal_id,
        )
        if execute_key not in dynamic_domain_ids:
            raise AuthorityDenied("interactive effect target domain is unavailable")
        routes.append(
            CapturedInteractiveEffectRoute(
                spec=spec,
                coordinator_principal=coordinator_principal,
                execute_target_principal=OpaqueAuthorityRef(execute_edge.target.principal_id),
                execute_ceiling=execute_edge.ceilings.caller_effect,
            )
        )
    if not routes:
        raise AuthorityDenied("interactive effect routes are unavailable")
    return tuple(routes)


def _nested_host_provider_session_id(envelope: Any) -> str:
    """Derive a stable Host session for a Provider's nested contract calls.

    The outer request ID intentionally does not participate: a panel performs
    prepare, status, and resume as separate HTTP requests, but the durable
    presentation owner must remain the same authenticated panel session.  The
    caller session and Provider target are Host-generated envelope fields.
    """

    context = getattr(envelope, "context", None)
    target = getattr(envelope, "target_principal", None)
    caller_session_id = getattr(context, "caller_session_id", None)
    target_principal_id = getattr(target, "value", None)
    profile_id = getattr(context, "profile_id", None)
    activation_id = getattr(context, "activation_id", None)
    plan_digest = getattr(context, "plan_digest", None)
    return _nested_host_provider_session_id_for(
        caller_session_id=caller_session_id,
        target_principal_id=target_principal_id,
        profile_id=profile_id,
        activation_id=activation_id,
        plan_digest=plan_digest,
    )


def _nested_host_provider_session_id_for(
    *,
    caller_session_id: object,
    target_principal_id: object,
    profile_id: object,
    activation_id: object,
    plan_digest: object,
) -> str:
    """Derive one stable nested session solely from Host-authenticated fields."""

    if (
        not isinstance(caller_session_id, str)
        or not caller_session_id
        or not isinstance(target_principal_id, str)
        or not target_principal_id
        or not isinstance(profile_id, str)
        or not isinstance(activation_id, str)
        or not isinstance(plan_digest, str)
    ):
        raise AuthorityDenied("nested Host Provider session is unavailable")
    return "session.host-provider." + canonical_digest(
        {
            "caller_session_id": caller_session_id,
            "provider_principal_id": target_principal_id,
            "profile_id": profile_id,
            "activation_id": activation_id,
            "plan_digest": plan_digest,
        }
    ).removeprefix("sha256:")


def _recover_interactive_effect_controller(controller: PendingEffectController) -> None:
    """Converge crash-interrupted pending effects before exposing the port."""

    try:
        controller.recover()
    except Exception as error:
        raise AuthorityDenied("interactive effect recovery is unavailable") from error


def _provider_unavailable_bridge_result() -> dict[str, Any]:
    """Return the bounded error projection allowed across the guest boundary."""

    return {
        "status": "error",
        "error": {
            "code": "PROVIDER_UNAVAILABLE",
            "message": "The verified AI capability is unavailable.",
        },
    }


def capture_production_dispatch(
    active: Any,
    *,
    bundle_root: Path,
    ecosystem_root: Path,
    authority_store: AuthorityStore,
    backends: BackendRegistry | None = None,
    target_backend_digests: Mapping[str, str] | None = None,
    packvm_provisioner: Any | None = None,
    packvm_readiness_reader: Callable[[], Mapping[str, Any]] | None = None,
    http_contract_bindings: tuple[Any, ...] = (),
    activation_snapshot_loader: ActivationSnapshotLoader | None = None,
    runtime_surface_factory: RuntimeSurfaceFactory | None = None,
    capability_binding_snapshot_factory: CapabilityBindingSnapshotFactory | None = None,
    capability_binding_selector: CapabilityBindingSelector | None = None,
    credential_store_factory: CredentialMaterialStoreFactory | None = None,
) -> V4DispatchSession:
    """Capture ProductionRuntimeV4 and its RequestBroker from verified records."""

    authority_path = authority_store.path.resolve()
    if authority_path.name != "v4.sqlite3" or authority_path.parent.name != "authority":
        raise AuthorityDenied("Authority store path is not canonical")
    authority_user_data = authority_path.parent.parent
    profile_id = str(active.resolved.profile["profile_id"])
    authority_workspace = authority_user_data / "workspaces" / profile_id
    try:
        catalog = _host_profile_catalog(
            bundle_root,
            authority_user_data=authority_user_data,
        )
        if activation_snapshot_loader is None:
            raise AuthorityDenied("Profile activation loader is unavailable")
        persisted_active: Any = activation_snapshot_loader(
            active=active,
            workspace=authority_workspace,
            profile_id=profile_id,
            authority_store=authority_store,
            catalog=catalog,
        )
    except Exception as exc:
        raise AuthorityDenied(
            "Authority store is not bound to the captured Profile activation"
        ) from exc
    if (
        dict(persisted_active.activation) != dict(active.activation)
        or dict(persisted_active.resolved.profile) != dict(active.resolved.profile)
        or dict(persisted_active.resolved.lock) != dict(active.resolved.lock)
        or dict(persisted_active.resolved.plan) != dict(active.resolved.plan)
    ):
        raise AuthorityDenied("Authority store is not bound to the captured Profile activation")
    active = persisted_active
    activation_suffix = str(active.activation["fencing_token"])

    profile = active.resolved.profile
    lock = active.resolved.lock
    plan = active.resolved.plan
    shell_id = str(profile["shell"]["pack_id"])
    shell = _shell_artifact(
        catalog,
        shell_id,
        profile["shell"],
    )
    principals_by_function: dict[str, tuple[FunctionPrincipal, ...]] = {}
    for function in shell.functions:
        function_principals: list[FunctionPrincipal] = []
        for operation in function.operations:
            principal = FunctionPrincipal(
                shell.digest,
                function.implementation_digest,
                function.function_id,
                operation.revision_digest,
                operation.operation_id,
            )
            function_principals.append(principal)
        principals_by_function[function.function_id] = tuple(function_principals)
    for binding in plan["bindings"]:
        principal = FunctionPrincipal.from_dict(binding["function_principal"])
        existing = list(principals_by_function.get(principal.function_id, ()))
        if principal not in existing:
            existing.append(principal)
        principals_by_function[principal.function_id] = tuple(existing)

    # Dispatch must follow the persisted immutable Profile, including exact
    # operation edges contributed by an enabled/approved optional Pack.
    edges = profile["requested_edges"]
    binding_by_edge: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for item in plan["bindings"]:
        binding_key = (
            str(item["caller_function_id"]),
            str(item["contract_id"]),
            str(item["operation_id"]),
        )
        if binding_key in binding_by_edge:
            raise AuthorityDenied("ResolvedPlan contains a duplicate operation edge")
        binding_by_edge[binding_key] = item

    profile_id = str(profile["profile_id"])
    activation_id = str(active.activation["activation_id"])
    ceilings: dict[tuple[str, ...], AuthorityCeilings] = {}
    edge_specs: list[
        tuple[
            Mapping[str, Any],
            Mapping[str, Any],
            FunctionPrincipal,
            FunctionPrincipal,
            AuthorityCeilings,
            tuple[str, str, str, str, str, str],
            str,
        ]
    ] = []
    seen_binding_edges: set[tuple[str, str, str]] = set()
    for edge in edges:
        binding_key = (
            str(edge["caller_function_id"]),
            str(edge["contract_id"]),
            str(edge["operation_id"]),
        )
        binding = binding_by_edge.get(binding_key)
        if binding is None:
            raise AuthorityDenied("Profile edge is absent from the signed ResolvedPlan")
        seen_binding_edges.add(binding_key)
        callers = principals_by_function.get(binding_key[0], ())
        if len(callers) != 1:
            raise AuthorityDenied("Profile edge caller does not identify one principal")
        caller = callers[0]
        target = FunctionPrincipal.from_dict(binding["function_principal"])
        if str(edge["target_provider_id"]) != target.function_id:
            raise AuthorityDenied("Profile edge target differs from its ResolvedPlan binding")
        scope = _committed_operation_scope(edge, target)
        if binding["requested_scope_digest"] != canonical_digest(scope.to_dict()):
            raise AuthorityDenied("ResolvedPlan requested scope binding changed")
        authority_mode = _requested_edge_authority_mode(edge, binding)
        axis_ceilings = _authority_ceilings_for_edge(edge, target)
        authority_key = (
            profile_id,
            activation_id,
            caller.principal_id,
            target.principal_id,
            str(edge["contract_id"]),
            str(edge["operation_id"]),
        )
        if authority_key in ceilings and ceilings[authority_key] != axis_ceilings:
            raise AuthorityDenied("Profile edge authority is duplicated")
        ceilings[authority_key] = axis_ceilings
        edge_specs.append(
            (
                edge,
                binding,
                caller,
                target,
                axis_ceilings,
                authority_key,
                authority_mode,
            )
        )
    if set(binding_by_edge) != seen_binding_edges:
        raise AuthorityDenied("ResolvedPlan contains an edge outside the active Profile")

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
    resolved_binding_by_edge: dict[tuple[str, str, str], ResolvedOperationBinding] = {}
    for binding in plan["bindings"]:
        binding_key = (
            str(binding["caller_function_id"]),
            str(binding["contract_id"]),
            str(binding["operation_id"]),
        )
        resolved_binding = runtime.composition.catalog.resolve_pinned(
            str(binding["contract_id"]),
            str(binding["operation_id"]),
        )
        if resolved_binding.principal_ref.value != canonical_digest(binding["function_principal"]):
            raise AuthorityDenied("ResolvedPlan target principal is not route-bound")
        resolved_binding_by_edge[binding_key] = resolved_binding
    catalog_bindings = tuple(resolved_binding_by_edge.values())
    captured_edges = tuple(
        _CapturedPlanEdge(
            key=authority_key,
            binding_key=(
                str(binding["caller_function_id"]),
                str(binding["contract_id"]),
                str(binding["operation_id"]),
            ),
            edge=edge,
            binding=binding,
            resolved_binding=resolved_binding_by_edge[
                (
                    str(binding["caller_function_id"]),
                    str(binding["contract_id"]),
                    str(binding["operation_id"]),
                )
            ],
            caller=caller,
            target=target,
            ceilings=axis_ceilings,
            authority_mode=authority_mode,
        )
        for (
            edge,
            binding,
            caller,
            target,
            axis_ceilings,
            authority_key,
            authority_mode,
        ) in edge_specs
    )
    registered_backends = tuple((backends or BackendRegistry(())).registered)
    if backends is None:
        authenticated_backend = _authenticated_packvm_backend(packvm_provisioner)
        if authenticated_backend is not None:
            registered_backends += (authenticated_backend,)
    target_backend_digests = dict(target_backend_digests or {})
    authority_control = runtime.composition.authority_adapter(authority_store)
    control_targets: dict[tuple[str, str], tuple[str, str, str]] = {}
    control_backend: PackControlBackendV4 | None = None
    control_session: Any | None = None
    control_edges = tuple(
        edge
        for edge in captured_edges
        if edge.resolved_binding.operation.contract_id in _CONTROL_CONTRACTS
    )
    if control_edges:

        def load_active_profile() -> Any:
            from .profile_capture import capture_active_profile

            return capture_active_profile()

        control_session = capture_pack_control_session(
            active=active,
            packvm_readiness_reader=packvm_readiness_reader,
            active_profile_loader=load_active_profile,
            bundle_root=bundle_root,
            runtime_surface_factory=runtime_surface_factory,
        )
        for captured_edge in sorted(control_edges, key=lambda item: item.key):
            if (
                captured_edge.resolved_binding.artifact.package_kind
                is not PackageKind.HOST_EXTENSION
                or captured_edge.resolved_binding.variant.execution_kind
                is not ExecutionKind.HOST_EXTENSION
            ):
                raise AuthorityDenied(
                    "Host control binding must use the explicit Host Extension namespace"
                )
            key = (
                captured_edge.resolved_binding.operation.contract_id,
                captured_edge.resolved_binding.operation.operation_id,
            )
            operation_id = key[1]
            target = captured_edge.target
            target_suffix = target.principal_id.removeprefix("sha256:")[:24]
            target_domain = _execution_domain(
                domain_id=f"domain.provider.{target_suffix}.{activation_suffix}",
                principal=target,
                active=active,
                boundary=DomainBoundary.DEDICATED_PROCESS,
                channel_seed=f"host-control-provider:{key[0]}:{operation_id}",
            )
            _register_exact_domain(
                authority_store,
                authority_control,
                target_domain,
                session_id=(f"session.provider.host-control.{target_suffix}.{activation_suffix}"),
                principal=target,
            )
            _commit_plan_authority(
                authority_store,
                authority_control,
                active=active,
                caller=captured_edge.caller,
                target=target,
                contract_id=key[0],
                caller_publisher_lineage=shell.publisher_lineage,
                target_publisher_lineage=captured_edge.resolved_binding.artifact.publisher_lineage,
                target_domain=target_domain,
                scope=captured_edge.ceilings.caller_effect,
                authority_label="host-control",
                authority_mode=captured_edge.authority_mode,
            )
            selected_target = (
                target.principal_id,
                target.function_implementation_digest,
                target_domain.domain_id,
            )
            previous_target = control_targets.get(key)
            if previous_target is not None and previous_target != selected_target:
                raise AuthorityDenied("Host control operation target is ambiguous")
            control_targets[key] = selected_target
        backend_digest = canonical_digest(
            {
                "backend": "tobkiri.host-control.v4",
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
    captured_dynamic_approvals: dict[str, str] = {}
    approved_host_binding_keys: set[tuple[str, str, str]] = set()
    dynamic_domain_ids: dict[tuple[str, str, str], str] = {}
    static_profile_pack_ids = _static_profile_pack_ids(catalog, profile_id)
    optional_pack_ids = {
        str(item["pack_id"])
        for item in profile.get("packs", ())
        if str(item["pack_id"]) not in static_profile_pack_ids
    }
    pack_ids_by_artifact_digest: dict[str, set[str]] = {}
    for item in lock["effective_set"]:
        pack_ids_by_artifact_digest.setdefault(
            str(item["artifact_digest"]),
            set(),
        ).add(str(item["identity"]))
    selected_backend_registry = BackendRegistry(registered_backends)
    for captured_edge in sorted(captured_edges, key=lambda item: item.key):
        if captured_edge.resolved_binding.operation.contract_id in _CONTROL_CONTRACTS:
            continue
        resolved_binding = captured_edge.resolved_binding
        target = captured_edge.target
        is_host_extension = resolved_binding.artifact.package_kind is PackageKind.HOST_EXTENSION
        if is_host_extension:
            _validate_host_provider_bindings(
                resolved_binding.function.function_id,
                (resolved_binding,),
            )
            target_domain = _execution_domain(
                domain_id=(
                    f"domain.provider.{target.principal_id.removeprefix('sha256:')[:24]}."
                    f"{activation_suffix}"
                ),
                principal=target,
                active=active,
                boundary=DomainBoundary.DEDICATED_PROCESS,
                channel_seed=(
                    f"host-extension-provider:{resolved_binding.operation.contract_id}:"
                    f"{resolved_binding.operation.operation_id}"
                ),
            )
            _register_exact_domain(
                authority_store,
                authority_control,
                target_domain,
                session_id=(
                    f"session.provider.host-extension."
                    f"{target.principal_id.removeprefix('sha256:')[:24]}."
                    f"{activation_suffix}"
                ),
                principal=target,
            )
            _commit_plan_authority(
                authority_store,
                authority_control,
                active=active,
                caller=captured_edge.caller,
                target=target,
                contract_id=resolved_binding.operation.contract_id,
                caller_publisher_lineage=shell.publisher_lineage,
                target_publisher_lineage=resolved_binding.artifact.publisher_lineage,
                target_domain=target_domain,
                scope=captured_edge.ceilings.caller_effect,
                authority_label="profile-host-extension",
                authority_mode=captured_edge.authority_mode,
                host_extension_binding=resolved_binding,
            )
            approved_host_binding_keys.add(captured_edge.binding_key)
            dynamic_domain_ids[
                (
                    resolved_binding.operation.contract_id,
                    resolved_binding.operation.operation_id,
                    target.principal_id,
                )
            ] = target_domain.domain_id
            continue

        if resolved_binding.variant.execution_kind is not ExecutionKind.PACK_VM:
            continue
        try:
            backend = selected_backend_registry.select(resolved_binding)
        except Exception:
            # A selected PackVM remains visible in the catalog but receives no
            # domain, Grant, or Provider authority until its exact backend is
            # authenticated and production-ready.
            continue
        if not callable(getattr(backend, "bind_target_domain_resolver", None)):
            continue
        provenance_valid, approval_pack_id = _packvm_approval_provenance(
            caller_artifact_digest=captured_edge.caller.parent_artifact_digest,
            target_pack_id=resolved_binding.artifact.pack_id,
            optional_pack_ids=optional_pack_ids,
            pack_ids_by_artifact_digest=pack_ids_by_artifact_digest,
        )
        if not provenance_valid:
            continue
        pack_approval_revision: str | None = None
        if approval_pack_id is not None:
            try:
                pack_approval = capture_valid_pack_approval(approval_pack_id)
                pack_approval_revision = str(pack_approval["approval_revision"])
            except Exception:
                # A selected Pack can remain in an immutable historical Plan,
                # but a missing, stale, corrupt, or revoked approval must never
                # recreate runtime authority for it.
                continue
            captured_dynamic_approvals[approval_pack_id] = pack_approval_revision
        target_domain = _execution_domain(
            domain_id=(
                f"domain.provider.{target.principal_id.removeprefix('sha256:')[:24]}."
                f"{activation_suffix}"
            ),
            principal=target,
            active=active,
            boundary=DomainBoundary.DEDICATED_PROCESS,
            channel_seed=(
                f"packvm-provider:{resolved_binding.operation.contract_id}:"
                f"{resolved_binding.operation.operation_id}"
            ),
        )
        _register_exact_domain(
            authority_store,
            authority_control,
            target_domain,
            session_id=(
                f"session.provider.packvm."
                f"{target.principal_id.removeprefix('sha256:')[:24]}."
                f"{activation_suffix}"
            ),
            principal=target,
        )
        _commit_plan_authority(
            authority_store,
            authority_control,
            active=active,
            caller=captured_edge.caller,
            target=target,
            contract_id=resolved_binding.operation.contract_id,
            caller_publisher_lineage=shell.publisher_lineage,
            target_publisher_lineage=resolved_binding.artifact.publisher_lineage,
            target_domain=target_domain,
            scope=captured_edge.ceilings.caller_effect,
            authority_label="profile-pack-vm",
            authority_mode=captured_edge.authority_mode,
            pack_approval_revision=pack_approval_revision,
        )
        target_backend_digests[target.principal_id] = backend.status.backend_digest

    # An optional Pack approval is part of the captured runtime boundary even
    # when its selected PackVM backend is unavailable.  The absence of a
    # backend must suppress grants and providers, but it must not turn a
    # mutable approval into an uncaptured dependency of this session.
    for pack_id in sorted(optional_pack_ids):
        if pack_id in captured_dynamic_approvals:
            continue
        try:
            pack_approval = capture_valid_pack_approval(pack_id)
        except Exception:
            continue
        captured_dynamic_approvals[pack_id] = str(pack_approval["approval_revision"])

    def authority_target_domain(binding: ResolvedOperationBinding) -> str:
        target_suffix = binding.principal_ref.value.removeprefix("sha256:")[:24]
        domain_id = f"domain.provider.{target_suffix}.{activation_suffix}"
        domain = authority_store.get_domain(domain_id)
        if domain is None or not any(
            principal.principal_id == binding.principal_ref.value for principal in domain.principals
        ):
            raise AuthorityDenied("production PackVM target domain is not registered by Authority")
        return domain_id

    # The bridge is Host-owned and receives only an authenticated outer
    # RequestEnvelope from the VZ supervisor.  The callback is intentionally
    # closed over the immutable capture; it never accepts caller, profile,
    # session, contract, provider, or plan identity from the guest.
    dispatch_holder: list[V4DispatchSession] = []
    bridge_targets = _bridge_targets_by_outer_edge(captured_edges)

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
        outer_caller = getattr(
            getattr(outer_context, "caller_principal", None),
            "value",
            None,
        )
        if not isinstance(outer_target, str):
            raise AuthorityDenied("PackVM capability bridge target identity is invalid")
        outer_edge = next(
            (
                edge
                for edge in captured_edges
                if edge.resolved_binding.operation.contract_id
                == getattr(outer_request, "contract_id", None)
                and edge.resolved_binding.operation.operation_id
                == getattr(outer_request, "operation_id", None)
                and edge.target.principal_id == outer_target
                and edge.caller.principal_id == outer_caller
            ),
            None,
        )
        if outer_edge is None or outer_edge.key not in bridge_targets:
            raise AuthorityDenied("PackVM capability bridge outer edge is not selected")
        expected_target_domain = authority_target_domain(outer_edge.resolved_binding)
        expected_target_backend_digest = target_backend_digests.get(outer_target)
        if (
            outer_target != outer_edge.target.principal_id
            or outer_domain != expected_target_domain
            or outer_caller != outer_edge.caller.principal_id
            or getattr(outer_context, "profile_id", None) != profile["profile_id"]
            or getattr(outer_context, "profile_revision", "") not in {"", plan["profile_revision"]}
            or getattr(outer_context, "activation_id", None) != active.activation["activation_id"]
            or getattr(outer_context, "activation_digest", None)
            != canonical_digest(active.activation)
            or getattr(outer_context, "plan_digest", None) != plan["plan_digest"]
            or getattr(outer_context, "security_epoch", None) != active.activation["security_epoch"]
            or getattr(outer_context, "fencing_token", None) != active.activation["fencing_token"]
            or getattr(outer_context, "profile_authority_digest", None)
            != active.activation["profile_authority_snapshot_digest"]
            or getattr(outer_context, "target_domain_id", None) != expected_target_domain
            or getattr(outer_context, "target_backend_digest", None)
            != expected_target_backend_digest
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
        bridge_candidates = bridge_targets[outer_edge.key]
        if not isinstance(target, Mapping):
            raise AuthorityDenied("PackVM capability bridge request is invalid")
        bridge_edges = tuple(
            edge
            for edge in bridge_candidates
            if edge.resolved_binding.operation.contract_id == target.get("contract_id")
            and edge.resolved_binding.operation.operation_id == target.get("operation_id")
        )
        if len(bridge_edges) != 1:
            raise AuthorityDenied("PackVM capability bridge target is ambiguous")
        bridge_edge = bridge_edges[0]
        expected_bridge_target = {
            "contract_id": bridge_edge.resolved_binding.operation.contract_id,
            "operation_id": bridge_edge.resolved_binding.operation.operation_id,
        }
        if (
            set(bridge_request) != expected_fields
            or bridge_request.get("kind") != "tobkiri.packvm.bridge.request.v1"
            or bridge_request.get("protocol") != _PACKVM_BRIDGE_PROTOCOL
            or bridge_request.get("version") != 1
            or not isinstance(target, Mapping)
            or dict(target) != expected_bridge_target
            or not isinstance(request, Mapping)
            or not isinstance(bridge_request.get("request_digest"), str)
            or not isinstance(continuation, Mapping)
        ):
            raise AuthorityDenied("PackVM capability bridge request is invalid")

        try:
            if len(canonical_json(request)) > _PACKVM_BRIDGE_MAX_REQUEST_BYTES or bridge_request[
                "request_digest"
            ] != canonical_digest(request):
                raise AuthorityDenied("PackVM capability bridge request is invalid")
            runtime.composition.catalog.validate_input(
                bridge_edge.resolved_binding,
                request,
            )
        except AuthorityDenied:
            raise
        except Exception as error:
            raise AuthorityDenied("PackVM capability bridge request is invalid") from error

        nonce = continuation.get("nonce")
        expected_continuation = {
            "kind": "tobkiri.packvm.continuation.v1",
            "protocol": _PACKVM_BRIDGE_PROTOCOL,
            "version": 1,
            "operation_id": outer_edge.resolved_binding.operation.operation_id,
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
        authority_target_domain(bridge_edge.resolved_binding)
        if target_backend_digests.get(bridge_edge.target.principal_id) is None:
            raise AuthorityDenied("PackVM capability bridge target is not ready")
        request_id = getattr(outer_context, "request_id", None)
        if not isinstance(request_id, str) or not request_id or len(request_id) > 160:
            raise AuthorityDenied("PackVM capability bridge request identity is invalid")
        # This session identity is generated in the Host.  The guest nonce
        # binds its continuation but never becomes an Authority session id.
        bridge_session_id = f"session.packvm-bridge.{request_id}.{secrets.token_hex(16)}"
        with caller_session_bindings_lock:
            caller_session_bindings[bridge_session_id] = outer_edge.target.principal_id
        try:
            provider_result = dispatch.invoke(
                bridge_edge.resolved_binding.operation.contract_id,
                bridge_edge.resolved_binding.operation.operation_id,
                {**dict(request), "_session_id": bridge_session_id},
            )
            if not isinstance(provider_result, Mapping):
                raise TypeError("verified Provider capability returned a non-object")
            result = {"status": "ok", "value": dict(provider_result)}
            if len(canonical_json(result)) > _PACKVM_BRIDGE_MAX_RESULT_BYTES:
                raise ValueError("verified Provider capability result is too large")
        except Exception:
            # Do not project provider/backend details through the PackVM ABI.
            # The guest receives a typed, bounded result it can safely render.
            result = _provider_unavailable_bridge_result()
        finally:
            with caller_session_bindings_lock:
                caller_session_bindings.pop(bridge_session_id, None)

        response = {
            "kind": "tobkiri.packvm.bridge.result.v1",
            "protocol": _PACKVM_BRIDGE_PROTOCOL,
            "version": 1,
            "operation_id": outer_edge.resolved_binding.operation.operation_id,
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

    packvm_backend_ids = {
        edge.resolved_binding.variant.backend
        for edge in captured_edges
        if edge.resolved_binding.variant.execution_kind is ExecutionKind.PACK_VM
    }
    for registered_backend in registered_backends:
        if registered_backend.status.backend_id not in packvm_backend_ids:
            continue
        binder = getattr(registered_backend, "bind_artifact_resolver", None)
        if not callable(binder):
            raise AuthorityDenied("production PackVM backend cannot bind authenticated artifacts")
        binder(artifact_resolver)
        domain_binder = getattr(
            registered_backend,
            "bind_target_domain_resolver",
            None,
        )
        if callable(domain_binder):
            domain_binder(authority_target_domain)
        bridge_binder = getattr(registered_backend, "bind_capability_bridge", None)
        if bridge_targets and callable(bridge_binder):
            bridge_binder(capability_bridge)
    registered_backend_ids = {item.status.backend_id for item in registered_backends}
    for backend_id in sorted(packvm_backend_ids - registered_backend_ids):
        registered_backends += (_UnavailablePackVmBackend(backend_id),)
    if control_backend is not None:
        registered_backends += (control_backend,)
    binding_by_function: dict[str, list[ResolvedOperationBinding]] = {}
    for binding_key, resolved_binding in resolved_binding_by_edge.items():
        if binding_key in approved_host_binding_keys:
            provider_bindings = binding_by_function.setdefault(
                resolved_binding.function.function_id,
                [],
            )
            if resolved_binding not in provider_bindings:
                provider_bindings.append(resolved_binding)
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
    caller_session_bindings: dict[str, str] = {}
    presentation_owner_bindings: dict[str, tuple[str, str]] = {}
    nested_session_refcounts: dict[str, int] = {}
    caller_session_bindings_lock = threading.RLock()

    def authority_session_id(session_id: str, caller_principal_id: str) -> str:
        """Derive the same Host authority-session identity for every call site."""

        caller_identity_suffix = caller_principal_id.removeprefix("sha256:")[:24]
        return f"{session_id}.{caller_identity_suffix}.{activation_suffix}"

    def presentation_owner_for(envelope: Any) -> tuple[str, str]:
        """Resolve the Host-preserved root owner for one nested invocation."""

        context = envelope.context
        with caller_session_bindings_lock:
            inherited = presentation_owner_bindings.get(context.caller_session_id)
        if inherited is not None:
            return inherited
        return context.caller_principal.value, context.caller_session_id

    def bind_nested_session(
        session_id: str,
        caller_principal_id: str,
        presentation_owner: tuple[str, str],
    ) -> str:
        """Atomically bind one stable nested session and retain concurrent users."""

        resolved_session_id = authority_session_id(session_id, caller_principal_id)
        with caller_session_bindings_lock:
            existing_caller = caller_session_bindings.get(session_id)
            existing_owner = presentation_owner_bindings.get(resolved_session_id)
            if existing_caller not in {None, caller_principal_id}:
                raise AuthorityDenied("nested Host Provider caller binding changed")
            if existing_owner not in {None, presentation_owner}:
                raise AuthorityDenied("nested Host Provider owner binding changed")
            caller_session_bindings[session_id] = caller_principal_id
            presentation_owner_bindings[resolved_session_id] = presentation_owner
            nested_session_refcounts[session_id] = (
                nested_session_refcounts.get(session_id, 0) + 1
            )
        return resolved_session_id

    def release_nested_session(session_id: str, resolved_session_id: str) -> None:
        """Release one stable nested-session user without racing a peer call."""

        with caller_session_bindings_lock:
            remaining = nested_session_refcounts.get(session_id, 0) - 1
            if remaining > 0:
                nested_session_refcounts[session_id] = remaining
                return
            nested_session_refcounts.pop(session_id, None)
            caller_session_bindings.pop(session_id, None)
            presentation_owner_bindings.pop(resolved_session_id, None)

    class _InvocationSession:
        """Bind nested dispatch to the authenticated provider invocation."""

        def __init__(
            self,
            envelope: Any,
            *,
            presentation_owner: tuple[str, str],
        ) -> None:
            self._envelope = envelope
            self._presentation_owner = presentation_owner
            self.profile_id = str(profile["profile_id"])
            self.plan_digest = str(plan["plan_digest"])
            self.profile_revision = str(plan["profile_revision"])
            self.activation_id = str(active.activation["activation_id"])

        def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
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
            nested_session_id = _nested_host_provider_session_id(self._envelope)
            nested_authority_session_id = bind_nested_session(
                nested_session_id,
                self._envelope.target_principal.value,
                self._presentation_owner,
            )
            try:
                return dispatch_holder[0].invoke(
                    contract_id,
                    operation_id,
                    {**dict(payload), "_session_id": nested_session_id},
                    version_range=version_range,
                )
            finally:
                release_nested_session(nested_session_id, nested_authority_session_id)

    class _HostInvocation(HostProviderInvocationContextV4):
        """Expose only declared nested dispatch and one credential transport."""

        def __init__(self, envelope: Any) -> None:
            self._envelope = envelope
            (
                self._presentation_owner_principal_id,
                self._presentation_owner_session_id,
            ) = presentation_owner_for(envelope)
            self._client: GlobalContractClient | None = None
            self._client_binding: tuple[frozenset[str], str] | None = None

        @property
        def envelope(self) -> Any:
            return self._envelope

        @property
        def presentation_owner_principal_id(self) -> str:
            return self._presentation_owner_principal_id

        @property
        def presentation_owner_session_id(self) -> str:
            return self._presentation_owner_session_id

        def contract_client(
            self,
            *,
            allowed_contract_ids: frozenset[str],
            consumer_pack_id: str,
        ) -> GlobalContractClient:
            expected_pack_id = pack_by_principal.get(self._envelope.target_principal.value)
            binding = (allowed_contract_ids, consumer_pack_id)
            if expected_pack_id != consumer_pack_id:
                raise AuthorityDenied("Host Provider consumer identity is invalid")
            if self._client is not None:
                if binding != self._client_binding:
                    raise AuthorityDenied("Host Provider client binding changed")
                return self._client
            provider_principal = principal_by_id.get(self._envelope.target_principal.value)
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
                session=_InvocationSession(
                    self._envelope,
                    presentation_owner=(
                        self._presentation_owner_principal_id,
                        self._presentation_owner_session_id,
                    ),
                ),
                allowed_contract_ids=allowed_contract_ids,
                consumer_pack_id=consumer_pack_id,
                host_credential_transport=transport,
            )
            self._client_binding = binding
            return self._client

    def invocation_context(envelope: Any) -> HostProviderInvocationContextV4:
        return _HostInvocation(envelope)

    try:
        host_provider_state_root = SecureDirectory(authority_user_data / "host_provider_state").root
    except (OSError, SecurePersistenceError) as exc:
        raise AuthorityDenied("Host Provider state root is unavailable") from exc

    def host_provider_capture_context(
        provider_bindings: tuple[ResolvedOperationBinding, ...],
        *,
        workspace_mutation_port: HostWorkspaceMutationPort | None,
        interactive_effect_port: LateBoundInteractiveEffectPort | None = None,
    ) -> HostProviderCaptureContextV4:
        """Build one narrow, activation-bound capture context for a Provider."""

        return HostProviderCaptureContextV4(
            profile_id=str(profile["profile_id"]),
            plan_digest=str(plan["plan_digest"]),
            security_epoch=int(active.activation["security_epoch"]),
            activation=active.activation,
            state_root=host_provider_state_root,
            provider_bindings=provider_bindings,
            catalog_bindings=catalog_bindings,
            domain_ids=dynamic_domain_ids,
            user_data_root=authority_user_data,
            interactive_approval_port=authority_control,
            interactive_effect_port=interactive_effect_port,
            workspace_mutation_port=workspace_mutation_port,
        )

    loaded_host_factories: list[tuple[str, tuple[ResolvedOperationBinding, ...], Any, str]] = []
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
        loaded_host_factories.append((function_id, captured_bindings, factory, backend_id))

    interactive_effect_coordinator = _interactive_effect_coordinator_factory(
        tuple(loaded_host_factories)
    )
    interactive_effect_port: LateBoundInteractiveEffectPort | None = None
    if interactive_effect_coordinator is not None:
        interactive_effect_port = LateBoundInteractiveEffectPort()

    workspace_binding_resolver: Callable[[str, str], WorkspaceMutationBinding] | None = None
    for _function_id, captured_bindings, factory, _backend_id in loaded_host_factories:
        resolver_capture = getattr(factory, "capture_workspace_binding_resolver", None)
        if not callable(resolver_capture):
            continue
        if workspace_binding_resolver is not None:
            raise AuthorityDenied("workspace mutation resolver is ambiguous")
        candidate_resolver = resolver_capture(
            host_provider_capture_context(
                captured_bindings,
                workspace_mutation_port=None,
            )
        )
        if not callable(candidate_resolver):
            raise AuthorityDenied("workspace mutation resolver is invalid")
        workspace_binding_resolver = candidate_resolver

    workspace_mutation_port = (
        HostWorkspaceMutationPort(
            WorkspaceMutationCoordinator(host_provider_state_root / "workspace_mutation"),
            binding_resolver=workspace_binding_resolver,
        )
        if workspace_binding_resolver is not None
        else None
    )

    for _function_id, captured_bindings, factory, backend_id in loaded_host_factories:
        captured_provider = factory.capture(
            host_provider_capture_context(
                captured_bindings,
                workspace_mutation_port=workspace_mutation_port,
                interactive_effect_port=(
                    interactive_effect_port
                    if interactive_effect_coordinator is not None
                    and factory is interactive_effect_coordinator[2]
                    else None
                ),
            )
        )
        expected_keys = {
            (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            for binding in captured_bindings
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
        binding_key = (
            str(binding["caller_function_id"]),
            str(binding["contract_id"]),
            str(binding["operation_id"]),
        )
        target = FunctionPrincipal.from_dict(binding["function_principal"])
        resolved_binding = resolved_binding_by_edge[binding_key]
        try:
            selected_backend = backend_registry.select(resolved_binding)
        except Exception:
            selected_backend = None
        selected_digest = (
            selected_backend.status.backend_digest
            if selected_backend is not None
            else next(
                (
                    candidate.status.backend_digest
                    for candidate in registered_backends
                    if candidate.status.backend_id == resolved_binding.variant.backend
                ),
                None,
            )
        )
        if selected_digest is not None:
            previous_digest = target_backend_digests.get(target.principal_id)
            if previous_digest is not None and previous_digest != selected_digest:
                raise AuthorityDenied("selected Provider backend identity changed")
            target_backend_digests[target.principal_id] = selected_digest
    broker = runtime.broker(
        authority_store=authority_store,
        adapters=AdapterPlanner(()),
        adapter_executor=_NoAdapterExecution(),
        backends=backend_registry,
        materialization=MaterializationCoordinator(),
        admission=_PlanAdmission(
            profile_id=profile_id,
            activation_id=activation_id,
            plan=plan,
            state_path=authority_workspace / "admission" / "reservations.json",
        ),
        reconciliation=InMemoryReconciliationStore(),
        authority_adapter=authority_control,
    )
    activation_digest = canonical_digest(active.activation)
    edge_candidates_by_operation: dict[tuple[str, str], list[_CapturedPlanEdge]] = {}
    for captured_edge in captured_edges:
        operation_key = (
            captured_edge.resolved_binding.operation.contract_id,
            captured_edge.resolved_binding.operation.operation_id,
        )
        edge_candidates_by_operation.setdefault(operation_key, []).append(captured_edge)

    caller_sessions: set[str] = set()
    caller_sessions_lock = threading.RLock()

    def select_edge(
        contract_id: str,
        operation_id: str,
        session_id: str,
    ) -> _CapturedPlanEdge:
        """Select one exact signed edge; never fall back by operation name."""

        candidates = tuple(edge_candidates_by_operation.get((contract_id, operation_id), ()))
        if not candidates:
            raise AuthorityDenied("operation edge is outside the captured Profile")
        with caller_session_bindings_lock:
            bound_caller = caller_session_bindings.get(session_id)
        if bound_caller is not None:
            matches = tuple(edge for edge in candidates if edge.caller.principal_id == bound_caller)
            if len(matches) != 1:
                raise AuthorityDenied("authenticated nested caller edge is invalid")
            return matches[0]
        if len(candidates) != 1:
            raise AuthorityDenied(
                "operation has multiple caller edges without an authenticated binding"
            )
        return candidates[0]

    def context_for(contract_id: str, operation_id: str, session_id: str) -> RequestContext:
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512:
            raise AuthorityDenied("authenticated session binding is invalid")
        captured_edge = select_edge(contract_id, operation_id, session_id)
        caller = captured_edge.caller
        target = captured_edge.target
        target_suffix = target.principal_id.removeprefix("sha256:")[:24]
        # One authenticated panel session may invoke operations whose
        # resolved Shell caller principals differ.  Authority session
        # bindings are principal-specific, so include that exact caller in
        # the Host-derived session identity instead of reusing a session
        # already bound to another caller.
        resolved_authority_session_id = authority_session_id(
            session_id,
            caller.principal_id,
        )
        caller_suffix = canonical_digest(
            {
                "session_id": resolved_authority_session_id,
                "caller": caller.principal_id,
            }
        ).removeprefix("sha256:")[:24]
        context = RequestContext(
            request_id="request." + secrets.token_hex(16),
            trace_id="trace." + secrets.token_hex(16),
            caller_principal=OpaqueAuthorityRef(caller.principal_id),
            profile_id=str(profile["profile_id"]),
            activation_id=str(active.activation["activation_id"]),
            activation_digest=activation_digest,
            plan_digest=str(plan["plan_digest"]),
            profile_revision=str(plan["profile_revision"]),
            security_epoch=int(active.activation["security_epoch"]),
            caller_session_id=resolved_authority_session_id,
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
            if resolved_authority_session_id not in caller_sessions:
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
                    session_id=resolved_authority_session_id,
                    principal=caller,
                )
                caller_sessions.add(resolved_authority_session_id)
        return context

    providers: dict[str, tuple[Mapping[str, Any], ...]] = {}
    provider_keys: set[tuple[str, str, str]] = set()
    for binding in plan["bindings"]:
        binding_key = (
            str(binding["caller_function_id"]),
            str(binding["contract_id"]),
            str(binding["operation_id"]),
        )
        resolved_binding = resolved_binding_by_edge[binding_key]
        provider_key = (
            resolved_binding.operation.contract_id,
            resolved_binding.operation.operation_id,
            resolved_binding.principal_ref.value,
        )
        if provider_key in provider_keys:
            continue
        provider_keys.add(provider_key)
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
                "profile_revision": plan["profile_revision"],
                "activation_id": active.activation["activation_id"],
                "plan_digest": plan["plan_digest"],
            },
        )

    captured_activation = dict(active.activation)
    captured_profile = dict(active.resolved.profile)
    captured_lock = dict(active.resolved.lock)
    captured_plan = dict(active.resolved.plan)

    def assert_current_capture() -> None:
        from ..pack_control_v4 import PackControlDenied
        from .profile_capture import capture_active_profile

        # Reuse only the explicit operation-local capture opened by the HTTP
        # boundary or runtime-surface operation. Outside that scope this is
        # still a fresh canonical capture on every assertion.
        current = capture_active_profile()
        if (
            dict(current.activation) != captured_activation
            or dict(current.resolved.profile) != captured_profile
            or dict(current.resolved.lock) != captured_lock
            or dict(current.resolved.plan) != captured_plan
            or authority_store.security_epoch != int(captured_activation["security_epoch"])
        ):
            raise AuthorityDenied(
                "captured Profile activation is stale",
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

    if interactive_effect_port is not None and interactive_effect_coordinator is not None:
        coordinator_binding = interactive_effect_coordinator[1][0]
        coordinator_principal = OpaqueAuthorityRef(coordinator_binding.principal_ref.value)
        routes = _captured_interactive_effect_routes(
            captured_edges,
            coordinator_principal=coordinator_principal,
            dynamic_domain_ids=dynamic_domain_ids,
        )

        def context_for_interactive_effect(
            route: CapturedInteractiveEffectRoute,
            presentation_context: RequestContext,
        ) -> RequestContext:
            """Recreate the signed prepare caller for the execute-only edge."""

            session_id = _nested_host_provider_session_id_for(
                caller_session_id=presentation_context.caller_session_id,
                target_principal_id=route.coordinator_principal.value,
                profile_id=presentation_context.profile_id,
                activation_id=presentation_context.activation_id,
                plan_digest=presentation_context.plan_digest,
            )
            with caller_session_bindings_lock:
                caller_session_bindings[session_id] = route.coordinator_principal.value
            try:
                context = context_for(
                    route.spec.execute_contract_id,
                    route.spec.execute_operation_id,
                    session_id,
                )
            finally:
                with caller_session_bindings_lock:
                    caller_session_bindings.pop(session_id, None)
            expected_domain = dynamic_domain_ids.get(
                (
                    route.spec.execute_contract_id,
                    route.spec.execute_operation_id,
                    route.execute_target_principal.value,
                )
            )
            if (
                expected_domain is None
                or context.caller_principal != route.coordinator_principal
                or context.target_domain_id != expected_domain
            ):
                raise AuthorityDenied("interactive effect context binding changed")
            return context

        interactive_effect_controller = PendingEffectController(
            persistence=authority_control,
            approvals=authority_control,
            coordinator_principal=coordinator_principal,
            coordinator_publisher_lineage=coordinator_binding.artifact.publisher_lineage,
        )
        _recover_interactive_effect_controller(interactive_effect_controller)
        interactive_effect_port.bind(
            HostInteractiveEffectService(
                broker=broker,
                controller=interactive_effect_controller,
                routes=tuple(routes),
                context_for_execute=context_for_interactive_effect,
                assert_current_capture=assert_current_capture,
                profile_id=str(profile["profile_id"]),
                activation_id=str(active.activation["activation_id"]),
                plan_digest=str(plan["plan_digest"]),
                security_epoch=int(active.activation["security_epoch"]),
            )
        )

    def effect_scope_for(
        contract_id: str,
        operation_id: str,
        _payload: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        """Return the caller-specific effect ceiling for one captured edge."""

        candidates = tuple(edge_candidates_by_operation.get((contract_id, operation_id), ()))
        if context is not None:
            caller_id = context.caller_principal.value
            candidates = tuple(edge for edge in candidates if edge.caller.principal_id == caller_id)
        if len(candidates) != 1:
            raise AuthorityDenied("operation effect edge is ambiguous or outside the Profile")
        return candidates[0].ceilings.caller_effect.to_dict()

    dispatch = runtime.dispatch_session(
        broker=broker,
        context_for=context_for,
        effect_scope_for=effect_scope_for,
        providers=providers,
        authority_control=authority_control,
        current_capture_check=assert_current_capture,
        owned_authority_store=authority_store,
        close_callbacks=(
            *close_callbacks,
            *((workspace_mutation_port.close,) if workspace_mutation_port else ()),
            *((control_session.close,) if control_session is not None else ()),
        ),
        stop_callbacks=(
            (control_session.cancel_pending_reads,) if control_session is not None else ()
        ),
    )
    dispatch_holder.append(dispatch)
    if control_session is not None and http_contract_bindings:
        if capability_binding_selector is None:
            dispatch.close()
            raise AuthorityDenied("application capability binding selector is unavailable")
        capability_binding = capability_binding_selector(http_contract_bindings)
        if capability_binding is None or capability_binding not in http_contract_bindings:
            dispatch.close()
            raise AuthorityDenied("capability invocation binding is absent or ambiguous")

        def capability_binding_reader() -> Mapping[str, Any]:
            from ..pack_control_v4 import capture_pack_catalog_reader

            if capability_binding_snapshot_factory is None:
                raise AuthorityDenied("capability projection factory is unavailable")
            return capability_binding_snapshot_factory(
                capability_binding,
                session=dispatch,
                catalog=capture_pack_catalog_reader().read(),
            )

        control_session.bind_capability_reader(capability_binding_reader)
    return dispatch


__all__ = ["capture_production_dispatch"]
