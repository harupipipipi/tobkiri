"""Assemble the live Pack v4 composition from one active snapshot."""

from __future__ import annotations

from core_runtime.native_generation_budget import (
    NativeGenerationBudgetTemplate, capture_native_generation_budget,
    owner_startup_generation_budget_template,
)

import contextvars
import json
import math
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, Protocol
from weakref import WeakValueDictionary

from core_runtime.capture_guard_traversal_v4 import (
    _capture_guard_traversal,
    _check_shared_capture,
)
from core_runtime.profile_operation_timing import (
    profile_operation_phase,
    timed_profile_reader,
)

from tobkiri_host.acceptance_receipts import AcceptanceReceiptPort
from tobkiri_host.admission import (
    AdmissionEstimate,
    DurableResourceLedger,
    FairAdmissionQueue,
    QueueScope,
    ResourceAmount,
    ResourceReservation,
    dead_owner_reservation_ids,
)
from tobkiri_host.artifact_materialization import capture_materialized_artifact
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.authority_approval_window import (
    AuthorityApprovalWindowController,
)
from tobkiri_host.backends import (
    BackendRegistry, BackendStatus, ExecutionBackend, NativeWasmBinding,
)
from tobkiri_host.broker import (
    AdmissionTicket,
    NestedCancellationProof,
    RequestAdmissionPort,
    RequestEnvelope,
)
from tobkiri_host.chat_approval_continuation import (
    ChatApprovalContinuationController,
)
from tobkiri_host.composition import AuthorityCeilings
from tobkiri_host.contracts import (
    AdapterPlanner,
    ResolvedOperationBinding,
    StructuralAdapter,
)
from tobkiri_host.effects import InMemoryReconciliationStore
from tobkiri_host.errors import BackendUnavailableError, ResourceExhaustedError
from tobkiri_host.interactive_effects import (
    LateBoundInteractiveEffectPort,
    PendingEffectController,
)
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.model_search import ModelSearchController
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    EffectClass,
    ExecutionKind,
    FunctionArtifact,
    OpaqueAuthorityRef,
    PackageKind,
    PackArtifact,
    RequestContext,
    require_digest,
)
from tobkiri_host.operation_cancellation import (
    OwnedCancellationBinding,
    OwnedCancellationHandles,
    nested_cancellation_proof_for,
)
from tobkiri_host.packvm_bridge_budget import PackVMBridgeBudgetRegistry
from tobkiri_host.ports import (
    AuthorityApprovalWindowOpenCommand,
    InteractiveApprovalGetQuery,
)
from tobkiri_host.runtime import ProductionRuntimeV4, V4DispatchSession
from tobkiri_host.workspace_mutation import (
    HostWorkspaceMutationPort,
    WorkspaceMutationBinding,
    WorkspaceMutationCoordinator,
)
from tobkiri_protocol.canonical import MAX_SAFE_INTEGER, canonical_digest, canonical_json
from tobkiri_protocol.data_codec import digest_payload
from tobkiri_protocol.errors import ProtocolError
from tobkiri_protocol.platform_artifact import verify_platform_artifact
from tobkiri_protocol.saved_conversation import (
    SAVED_CONVERSATION_CONTRACT,
    SAVED_CONVERSATION_OPERATION,
    validate_saved_conversation_input,
)
from tobkiri_protocol.secure_persistence import (
    SecureDirectory,
    SecurePersistenceError,
)

from core_runtime.local_model_authority import (
    LOCAL_REGISTRY_CONTRACT,
    LocalModelInvocationBinding,
    LocalModelRequest,
    capture_local_model_binding,
    create_local_model_transport,
)
from core_runtime.dispatch_diagnostics import log_nested_dispatch_failure
from core_runtime.captured_wake_v4 import (
    CapturedWakeDeclarationV4, CapturedWakeDriverV4, LateBoundWakePortV4,
    close_captured_wake_drivers_v4,
)
from core_runtime.production_wake_binding_v4 import bind_production_wake_v4

from ..authority.pack_approval_binding import pack_approval_snapshot_digest
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
from ..credential_transport import (
    AuthorizedEnvelopeCredentialTransport,
    CredentialMaterialStoreFactory,
)
from ..external_pack_catalog_v4 import resolve_admitted_pack_roots
from ..global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)
from ..host_provider_backend_v4 import (
    CapturedHostPackDataV4,
    ExactHostProviderBackendV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
    HostPublicationUnconfirmed,
    HostSavedTurnSettlementConflict,
    HostSavedTurnSettlementUnconfirmed,
    SavedConversationConfirmationPort,
    SavedMessageAppendPort,
    SavedTurnSettlementPort,
)
from ..host_provider_data_v4 import HostProviderDataCaptureV4
from ..invocation_scope_v4 import (
    CapturedInvocationScopeV4, ParentInvocationScopesV4,
    assert_dispatched_invocation, execution_session_id,
)
from ..host_provider_hooks_v4 import load_host_provider_factory
from ..interactive_effect_coordinator import (
    INTERACTIVE_EFFECT_COORDINATOR_CONTRACT_ID,
    INTERACTIVE_EFFECT_COORDINATOR_OPERATION_ID,
    INTERACTIVE_EFFECT_SPECS,
    CapturedInteractiveEffectRoute,
    HostInteractiveEffectService,
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
from ..panel_auth import (
    PanelAuthBinding,
    PanelAuthManager,
    get_panel_auth_manager,
)
from ..model_settings_native_v4 import (
    CONTRACT as NATIVE_MODEL_SETTINGS_CONTRACT,
    FUNCTION as NATIVE_MODEL_SETTINGS_FUNCTION,
    MINIMUM_MEMORY as NATIVE_MODEL_SETTINGS_MEMORY,
    OPERATION as NATIVE_MODEL_SETTINGS_OPERATION,
    PACK as NATIVE_MODEL_SETTINGS_PACK,
    SAVE_CONTRACT as _MODEL_SAVE_CONTRACT,
    SAVE_FUNCTION as _MODEL_SAVE_FUNCTION,
    SAVE_OPERATION as _MODEL_SAVE_OPERATION,
    current_model_save_http_scope,
    current_model_settings_http_scope,
    model_settings_presenter_input,
    publication_possible as model_save_publication_possible,
)
from ..provider_prepare_native_v4 import (
    CONTRACT as NATIVE_PROVIDER_PREPARE_CONTRACT,
    EXECUTE_FUNCTION as NATIVE_PROVIDER_EXECUTE_FUNCTION,
    EXECUTE_OPERATION as NATIVE_PROVIDER_EXECUTE_OPERATION,
    FUNCTION as NATIVE_PROVIDER_PREPARE_FUNCTION,
    OPERATION as NATIVE_PROVIDER_PREPARE_OPERATION,
    NativeProviderPrepareBorrow,
    NativeProviderPreparePlanner,
    invoke_native_provider_prepare,
    is_keyless_local,
)
from ..host_provider_backend_v4 import HostInvocationEvidenceReferenceV4
from ..invocation_evidence import (
    BoundInvocationEvidence, EvidenceBinding, InvocationEvidenceRegistry,
)
from ..workflow_v4.attempt_port import (
    LateBoundWorkflowAttemptPortV4, WorkflowAttemptServiceConfigV4,
    create_workflow_attempt_service_v4,
)
from ..workflow_v4.production_capture import capture_workflow_port_assembly_v4
from ..workflow_v4.evidence import EVIDENCE_KIND as WORKFLOW_EVIDENCE_KIND


_MODEL_PROVIDER_CONTRACT = "tobkiri.resource.ai.provider.registry.v1"
_MODEL_PROVIDER_OPERATION = "rumi_provider_registry_pack.provider-registry-resource"
_MODEL_PROVIDER_FUNCTION = "rumi_provider_registry_pack.provider-registry.resource"


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
_PACKVM_BRIDGE_MAX_HOPS = 16
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


@dataclass(frozen=True)
class _SavedLocalChatRoute:
    role: Literal["generate", "stream"]
    adapter: _CapturedPlanEdge
    registry: _CapturedPlanEdge
    parents: tuple[_CapturedPlanEdge, ...]


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


@dataclass(frozen=True)
class _PlanAdmissionEstimate(AdmissionEstimate):
    """Keep the signed binding's original charge available before any native work."""

    native_floor_bytes: int = 0

    def preferred_charge(self) -> ResourceAmount:
        if self.native_floor_bytes <= self.backend_overhead_bytes:
            return self.charge()
        return replace(self, backend_overhead_bytes=self.native_floor_bytes).charge()


class _PlanAdmission(RequestAdmissionPort):
    """Plan-bound Host admission with measured, durable reservations."""

    def __init__(
        self,
        *,
        profile_id: str,
        activation_id: str,
        plan: Mapping[str, Any],
        state_path: Path,
        native_bindings: tuple[
            tuple[
                ResolvedOperationBinding, int,
                Callable[[RequestContext, Mapping[str, Any]], bool],
            ], ...
        ] = (),
        confirmed_supervisor_release: Callable[
            [Mapping[str, str], tuple[ResourceReservation, ...]], tuple[str, ...]
        ]
        | None = None,
    ) -> None:
        self._profile_id = profile_id
        self._plan_digest = str(plan["plan_digest"])
        self._native_bindings: dict[str, list[tuple[
            ResolvedOperationBinding, int,
            Callable[[RequestContext, Mapping[str, Any]], bool],
        ]]] = {}
        for rule in native_bindings:
            binding_id = _admission_binding_id(rule[0])
            self._native_bindings.setdefault(binding_id, []).append(rule)
        self._native_reservations: dict[str, ResourceReservation] = {}
        self._native_lock = threading.RLock()
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
            confirmed_supervisor_release=confirmed_supervisor_release,
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
        backend_overhead = _nonnegative_int(
            policy.get("backend_overhead_bytes"), default=0,
        )
        native_floor_bytes = max((
            memory for native, memory, matches in self._native_bindings.get(
                _admission_binding_id(binding), ()
            ) if native == binding and matches(context, payload)
        ), default=0)
        return _PlanAdmissionEstimate(
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
            backend_overhead_bytes=backend_overhead,
            declared_upper_bound_bytes=upper_bound,
            concurrency=_positive_int(policy.get("concurrency"), default=1),
            disk_bytes=_nonnegative_int(policy.get("disk_bytes"), default=0),
            detached=binding.variant.execution_kind is ExecutionKind.PACK_VM,
            native_floor_bytes=native_floor_bytes,
        )

    def acquire(
        self,
        scope: QueueScope,
        estimate: AdmissionEstimate,
        wait_timeout_seconds: float,
    ) -> AdmissionTicket:
        if wait_timeout_seconds <= 0:
            raise TimeoutError("admission deadline expired")
        original = estimate.charge()
        preferred = (
            estimate.preferred_charge()
            if isinstance(estimate, _PlanAdmissionEstimate)
            else original
        )

        def admit(amount: ResourceAmount) -> ResourceReservation:
            return self._queue.admit(
                scope, amount,
                wait_timeout_seconds=wait_timeout_seconds,
                detached=estimate.detached,
            )

        try:
            reservation = admit(preferred)
        except ResourceExhaustedError:
            if preferred == original:
                raise
            reservation = admit(original)
        if scope.binding_id in self._native_bindings:
            with self._native_lock:
                self._native_reservations[reservation.reservation_id] = reservation
        return AdmissionTicket(reservation)

    def release(self, ticket: AdmissionTicket) -> None:
        if self._native_bindings:
            with self._native_lock:
                self._native_reservations.pop(ticket.reservation.reservation_id, None)
        self._ledger.release(ticket.reservation.reservation_id)

    def native_resources(self, reservation_id: str | None) -> ResourceAmount:
        """Borrow the current Host operation ticket's admitted capacity."""
        with self._native_lock:
            reservation = self._native_reservations.get(reservation_id or "")
        if reservation is None:
            raise AuthorityDenied("native operation admission is unavailable")
        return reservation.amount


def _admission_binding_id(binding: ResolvedOperationBinding) -> str:
    return (
        f"{binding.artifact.digest}:{binding.function.function_id}:"
        f"{binding.operation.operation_id}"
    )


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
) -> Any:
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


def _manifest_caller_principals(
    catalog: Any,
    admitted_pack_ids: set[str],
    *,
    expected_artifact_digests: Mapping[str, str] | None = None,
) -> dict[str, tuple[FunctionPrincipal, ...]]:
    """Index Function principals declared by admitted Pack manifests.

    A Profile edge is caller-Function-scoped while Authority is exercised by
    exact per-Operation principals.  Functions whose Pack is admitted but
    which are never bound as an edge target (a provider left with only
    outgoing edges after its callers were removed, or a Pack that only
    contributes callers) are not materialized for dispatch; their identity
    still comes from the verified manifest declarations, so a signed plan
    edge keeps an exact, finite set of caller principals.  Manifests outside
    the admitted set are never consulted: an unknown caller Function keeps
    failing closed instead of being masked by the bundle catalog.
    """

    principals: dict[str, list[FunctionPrincipal]] = {}
    for pack_id in sorted(admitted_pack_ids):
        manifest = catalog.packs.get(pack_id)
        if not isinstance(manifest, Mapping):
            continue
        artifact_digest = str(manifest["pack"]["artifact_digest"])
        if expected_artifact_digests is not None and expected_artifact_digests.get(pack_id) != artifact_digest:
            raise AuthorityDenied("caller manifest differs from the admitted ProfileLock artifact")
        functions = manifest.get("functions")
        if not isinstance(functions, list):
            continue
        for function in functions:
            function_id = str(function.get("id") or "")
            operations = function.get("operations")
            if not function_id or not isinstance(operations, list):
                raise AuthorityDenied("admitted Pack Function declaration is invalid")
            for operation_id in operations:
                principal = FunctionPrincipal(
                    artifact_digest,
                    str(function["implementation_digest"]),
                    function_id,
                    str(function["contract_revision_digest"]),
                    str(operation_id),
                )
                bucket = principals.setdefault(function_id, [])
                if any((item.parent_artifact_digest, item.function_implementation_digest, item.contract_revision_digest) !=
                       (principal.parent_artifact_digest, principal.function_implementation_digest, principal.contract_revision_digest) for item in bucket):
                    raise AuthorityDenied("caller Function has ambiguous artifact ownership")
                if principal not in bucket:
                    bucket.append(principal)
    return {key: tuple(value) for key, value in principals.items()}


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


@contextmanager
def _borrow_selected_saved_context(
    *, operation: object, consumer: ResolvedOperationBinding, conversation_owner: ResolvedOperationBinding,
    project_owner: ResolvedOperationBinding | None, factories: Any, profile_id: str,
    presentation_owner: Callable[[], tuple[str, str]],
    read_project: Callable[[Mapping[str, Any]], Mapping[str, Any]], guard: Callable[[], None],
) -> Iterator[Any]:
    """Lend fixed existing selected factories; never load or discover a module."""
    def current():
        guard()
        envelope = getattr(operation, "envelope", operation)
        if (getattr(getattr(envelope, "context", None), "profile_id", None) != profile_id
                or getattr(envelope, "target_principal", None) != consumer.principal_ref
                or (getattr(envelope, "contract_id", None), getattr(envelope, "operation_id", None))
                != (consumer.operation.contract_id, consumer.operation.operation_id)):
            raise AuthorityDenied("saved context invocation owner changed")

    current()
    if (consumer.function.function_id, consumer.operation.contract_id,
            consumer.operation.operation_id) not in {
        ("rumi_turn_runtime_pack.turn-runtime.saved", "tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved"),
        ("tobkiri_conversation_orchestration_pack.saved", "conversation.saved-turn.v1", "saved_complete"),
    }:
        raise AuthorityDenied("saved context consumer is unavailable")

    def selected(binding, function, contract, operation):
        if binding is None or (binding.function.function_id, binding.operation.contract_id,
                               binding.operation.operation_id) != (function, contract, operation):
            raise AuthorityDenied("saved context selected owner is unavailable")
        matches = [factory for function_id, bindings, factory, _backend in factories
                   if function_id == function and binding in bindings]
        if len(matches) != 1 or matches[0].function_id != function:
            raise AuthorityDenied("saved context captured owner is unavailable")
        return matches[0]

    owner = selected(conversation_owner, "rumi_conversation_store_pack.conversation-store.resource",
                     "tobkiri.resource.conversation.v1", "rumi_conversation_store_pack.conversation-resource")

    def validator():
        # Project absence is harmless for ordinary NoProject and clear. Group
        # resolution calls this fixed supplier and fails explicitly if unbound.
        current()
        factory = selected(project_owner, "tobkiri.project.state.read",
                           "tobkiri.resource.project.state.v1", "tobkiri_ui_settings_pack.projects-read")
        supply = getattr(factory, "saved_snapshot_validator", None)
        if not callable(supply):
            raise AuthorityDenied("saved Project captured validator is unavailable")
        return supply()

    borrow = getattr(owner, "borrow_saved_context", None)
    if not callable(borrow):
        # An older selected conversation owner can still serve ordinary saved
        # chat/coding. No App proof is minted: the existing Host-static full
        # validator below the caller still rejects unresolved owned context.
        yield None
        return
    port = borrow(operation=operation, profile_id=profile_id, presentation_owner=presentation_owner,
                  read_owner=read_project, guard=current, validator=validator)
    try:
        yield port
    finally:
        port.close()


def _validate_host_provider_contributions(
    provider_bindings: tuple[ResolvedOperationBinding, ...],
    contributions: tuple[HostProviderContributionV4, ...],
    domain_ids: Mapping[tuple[str, str, str], str],
) -> None:
    """Require every contribution to retain its selected artifact and domain."""

    expected = {
        (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        ): binding
        for binding in provider_bindings
    }
    if (
        len(contributions) != len(expected)
        or {item.key for item in contributions} != set(expected)
    ):
        raise AuthorityDenied("Host Provider hook contribution set is incomplete")
    for contribution in contributions:
        binding = expected[contribution.key]
        domain_id = domain_ids.get(contribution.key)
        if (
            contribution.contract_version != binding.operation.contract_version
            or contribution.artifact_digest != binding.artifact.digest
            or contribution.implementation_digest
            != binding.function.implementation_digest
            or domain_id is None
            or contribution.domain_id != domain_id
            or not callable(contribution.invoke)
        ):
            raise AuthorityDenied("Host Provider hook contribution identity is stale")


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
    execution_provider_label: str | None = None,
) -> tuple[Any, Any | None]:
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
    provider_suffix = (
        f"{contract_suffix}.{operation_suffix}.{caller_suffix}."
        f"{target_suffix}.{record_identity}"
    )
    logical_record_suffix = f"{profile_identity}.{authority_label}.{provider_suffix}"
    logical_provider_id = f"provider.{logical_record_suffix}"
    provider_label = execution_provider_label or authority_label
    provider_id = f"provider.{profile_identity}.{provider_label}.{provider_suffix}"
    previous_provider = None
    if execution_provider_label is not None:
        if authority_mode != "profile_grant" or host_extension_binding is not None:
            raise AuthorityDenied("native bounded execution requires the original Profile Grant")
        previous_provider = store.get_provider_authority(logical_provider_id)
        if (
            (previous_provider is not None and previous_provider.revoked)
            or store.is_revoked("provider_authority", logical_provider_id)
        ):
            raise AuthorityDenied("original Provider authority was revoked")
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
        record_id=provider_id,
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
    if previous_provider is not None and replace(
        provider,
        record_id=previous_provider.record_id,
        execution_domain_id=previous_provider.execution_domain_id,
        execution_domain_identity_digest=previous_provider.execution_domain_identity_digest,
    ) != previous_provider:
        raise AuthorityDenied("original Provider authority snapshot changed")
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
        return provider, None
    # A Pack approval revision names the stable user decision, not one runtime
    # activation.  The immutable Authority snapshot below is activation-bound,
    # so every record in its bundle must use the activation generation as part
    # of its durable identity.  This preserves prior rows without replaying or
    # colliding with them when an unchanged Pack is activated again.
    approval = ApprovalRecord(
        approval_id=f"approval.{logical_record_suffix}",
        snapshot_digest=pack_approval_snapshot_digest(
            profile_id=profile_identity,
            activation_id=activation["activation_id"],
            plan_digest=activation["plan_digest"],
            profile_authority_digest=activation["profile_authority_snapshot_digest"],
            security_epoch=activation["security_epoch"],
            scope=scope.to_dict(),
            approval_revision=pack_approval_revision,
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
        grant_id=f"grant.{logical_record_suffix}",
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
    if existing == (None, None, None, None) and previous_provider is None:
        control.commit_approval_bundle(
            approval,
            host_extension_trust=host_extension_trust,
            provider_authorities=(provider,),
            grants=(grant,),
        )
    elif (
        execution_provider_label is not None
        and existing == (None, approval, None, grant)
    ):
        if (
            store.is_revoked("grant", grant.grant_id)
            or store.is_revoked("provider_authority", provider.record_id)
        ):
            raise AuthorityDenied("original saved execution authority was revoked")
        control.commit_provider_authority_bundle(provider_authorities=(provider,))
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

    return provider, grant


def _packvm_approval_provenance(
    *,
    caller_artifact_digest: str,
    target_pack_id: str,
    optional_pack_ids: set[str],
    pack_ids_by_artifact_digest: Mapping[str, set[str]],
    approval_roots_by_pack: Mapping[str, set[str]] | None = None,
) -> tuple[bool, str | None]:
    """Resolve one unambiguous optional-Pack approval source for a PackVM edge."""

    caller_pack_ids = pack_ids_by_artifact_digest.get(caller_artifact_digest, set())
    if len(caller_pack_ids) != 1:
        return False, None
    caller_pack_id = next(iter(caller_pack_ids))
    approval_pack_ids = {
        pack_id for pack_id in (target_pack_id, caller_pack_id) if pack_id in optional_pack_ids
    }
    if not approval_pack_ids:
        return True, None
    covering_roots: set[str] | None = None
    for pack_id in approval_pack_ids:
        roots = (
            {pack_id} if approval_roots_by_pack is None
            else set(approval_roots_by_pack.get(pack_id, ()))
        )
        covering_roots = roots if covering_roots is None else covering_roots & roots
    if not covering_roots or len(covering_roots) != 1:
        return False, None
    return True, next(iter(covering_roots))


def _optional_pack_approval_roots(
    catalog: Any, optional_pack_ids: set[str]
) -> dict[str, set[str]]:
    """Bind each optional member to its selected signed dependency roots.

    Pack control approves roots, not the effective closure's dependency-only
    members. Traverse the already verified catalog without inventing a new
    caller edge or choosing among overlapping approval roots.
    """
    dependencies = {
        pack_id: set(manifest["requirements"]["pack_dependencies"])
        for pack_id, manifest in catalog.packs.items()
    }
    if not optional_pack_ids.issubset(dependencies):
        raise AuthorityDenied("optional Pack dependency catalog is incomplete")
    dependency_ids = {
        dependency
        for pack_id in optional_pack_ids
        for dependency in dependencies[pack_id]
        if dependency in optional_pack_ids
    }
    roots = optional_pack_ids - dependency_ids
    coverage: dict[str, set[str]] = {pack_id: set() for pack_id in optional_pack_ids}
    for root in sorted(roots):
        pending = [root]
        visited: set[str] = set()
        while pending:
            pack_id = pending.pop()
            if pack_id in visited:
                continue
            visited.add(pack_id)
            if pack_id not in dependencies:
                raise AuthorityDenied("signed Pack dependency is unavailable")
            if pack_id in coverage:
                coverage[pack_id].add(root)
            pending.extend(dependencies[pack_id] - visited)
    return coverage


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
    is that PackVM Function. The guest may name a Contract only when the Plan
    contains exactly one matching edge, or retain the legacy exact Contract
    and operation target. Provider names, product IDs, and operation-only maps
    cannot create a continuation capability.
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


def _resolve_packvm_bridge_target(
    candidates: tuple[_CapturedPlanEdge, ...],
    target: object,
) -> tuple[_CapturedPlanEdge, dict[str, str]]:
    """Resolve a guest target only through one caller-bound captured Plan edge."""

    if (
        not isinstance(target, Mapping)
        or set(target) not in (
            {"contract_id"},
            {"contract_id", "operation_id"},
        )
        or not isinstance(target.get("contract_id"), str)
        or not target.get("contract_id")
    ):
        raise AuthorityDenied("PackVM capability bridge request is invalid")
    requested_operation = target.get("operation_id")
    if requested_operation is not None and (
        not isinstance(requested_operation, str) or not requested_operation
    ):
        raise AuthorityDenied("PackVM capability bridge request is invalid")
    matches = tuple(
        edge
        for edge in candidates
        if edge.resolved_binding.operation.contract_id == target.get("contract_id")
        and (
            requested_operation is None
            or edge.resolved_binding.operation.operation_id == requested_operation
        )
    )
    if len(matches) != 1:
        raise AuthorityDenied("PackVM capability bridge target is ambiguous")
    return matches[0], {key: str(value) for key, value in target.items()}


def _validate_packvm_bridge_continuation(
    continuation: object,
    *,
    operation_id: str,
    target: Mapping[str, str],
    request_digest: str,
) -> dict[str, Any]:
    """Validate a legacy or bounded multi-hop PackVM continuation.

    The Host independently binds every continuation to the captured outer
    operation, the Plan-resolved target, and the canonical request digest.
    Chain ordering and one-shot consumption are additionally enforced by the
    PackVM backend; this boundary still rejects malformed predecessor and
    state bindings before any nested Provider invocation occurs.
    """

    if not isinstance(continuation, Mapping):
        raise AuthorityDenied("PackVM capability bridge continuation is invalid")
    common = {
        "kind": "tobkiri.packvm.continuation.v1",
        "protocol": _PACKVM_BRIDGE_PROTOCOL,
        "version": 1,
        "operation_id": operation_id,
        "nonce": continuation.get("nonce"),
        "target": dict(target),
        "request_digest": request_digest,
    }
    legacy_fields = set(common)
    extended_fields = legacy_fields | {
        "hop",
        "max_hops",
        "previous_result_digest",
        "state",
        "state_digest",
    }
    nonce = continuation.get("nonce")
    if (
        not isinstance(nonce, str)
        or len(nonce) != 48
        or any(character not in "0123456789abcdef" for character in nonce)
        or any(continuation.get(key) != value for key, value in common.items())
    ):
        raise AuthorityDenied("PackVM capability bridge continuation is invalid")
    if set(continuation) == legacy_fields:
        return dict(continuation)
    if set(continuation) != extended_fields:
        raise AuthorityDenied("PackVM capability bridge continuation is invalid")

    hop = continuation.get("hop")
    max_hops = continuation.get("max_hops")
    predecessor = continuation.get("previous_result_digest")
    state = continuation.get("state")
    state_digest = continuation.get("state_digest")

    def valid_digest(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 71
            and value.startswith("sha256:")
            and all(character in "0123456789abcdef" for character in value[7:])
        )

    try:
        state_bytes = canonical_json(dict(state)) if isinstance(state, Mapping) else ""
        expected_state_digest = (
            canonical_digest(dict(state)) if isinstance(state, Mapping) else None
        )
    except Exception as error:
        raise AuthorityDenied(
            "PackVM capability bridge continuation is invalid"
        ) from error
    if (
        type(hop) is not int
        or type(max_hops) is not int
        or not 1 <= max_hops <= _PACKVM_BRIDGE_MAX_HOPS
        or not 0 <= hop < max_hops
        or (hop == 0 and predecessor is not None)
        or (hop > 0 and not valid_digest(predecessor))
        or not isinstance(state, Mapping)
        or len(state_bytes) > _PACKVM_BRIDGE_MAX_REQUEST_BYTES
        or not valid_digest(state_digest)
        or state_digest != expected_state_digest
    ):
        raise AuthorityDenied("PackVM capability bridge continuation is invalid")
    return dict(continuation)


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


def _provider_unavailable_bridge_result(
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Return the bounded error projection allowed across the guest boundary."""

    del error
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
    generation_budget_template: NativeGenerationBudgetTemplate | None = None,
    native_provider_prepare_planner: NativeProviderPreparePlanner | None = None,
    acceptance_receipts: AcceptanceReceiptPort | None = None,
    chat_continuation_approve: Callable[..., Mapping[str, Any]] | None = None,
    chat_continuation_resume: Callable[..., Mapping[str, Any]] | None = None,
    authority_approval_window_open: (
        Callable[[str], Mapping[str, Any]] | None
    ) = None,
    panel_auth_manager: PanelAuthManager | None = None,
    model_search: (
        Callable[
            [
                Mapping[str, Any],
                list[Mapping[str, Any]],
                list[Mapping[str, Any]],
                Mapping[str, Any],
            ],
            Mapping[str, Any],
        ]
        | None
    ) = None,
) -> V4DispatchSession:
    """Capture ProductionRuntimeV4 and its RequestBroker from verified records."""

    from ..native_provider_prepare_ipc import (
        AI_ROUTING_MINIMUM_MEMORY,
        CONVERSATION_APPEND_MINIMUM_MEMORY,
        CONVERSATION_CONFIRMATION_MINIMUM_MEMORY,
        CONVERSATION_GET_MAX_ID_CHARS,
        CONVERSATION_GET_MINIMUM_MEMORY,
        MODEL_RESOLVE_MINIMUM_MEMORY,
        SAVED_RECEIPT_MAX_ID_CHARS,
        SAVED_RECEIPT_MINIMUM_MEMORY,
        SAVED_ORCHESTRATION_MINIMUM_MEMORY,
        SAVED_LOCAL_CHAT_MAX_INPUT_BYTES,
        SAVED_LOCAL_CHAT_MINIMUM_MEMORY,
        SAVED_TURN_READ_MINIMUM_MEMORY,
        SAVED_TURN_SETTLEMENT_MINIMUM_MEMORY,
        SAVED_TURN_START_MINIMUM_MEMORY,
        ConversationGetCaller,
        ModelResolveRole,
        NativeAiRoutingFailure,
        NativeAiRoutingRequest,
        NativeConversationAppendFailure,
        NativeConversationAppendPublicationUnconfirmed,
        NativeConversationAppendRequest,
        NativeConversationConfirmationFailure,
        NativeConversationConfirmationPublicationUnconfirmed,
        NativeConversationConfirmationRequest,
        NativeConversationGetFailure,
        NativeConversationGetRequest,
        NativeModelResolveFailure,
        NativeModelResolveRequest,
        NativeModelSaveFailure,
        NativeModelSavePublicationUnconfirmed,
        NativeModelSaveRequest,
        NativeModelSettingsRequest,
        NativeProviderPrepareUnavailable,
        NativeSavedReceiptFailure,
        NativeSavedReceiptRequest,
        NativeSavedOrchestrationRequest,
        NativeSavedLocalChatFailure,
        NativeSavedLocalChatRequest,
        NativeSavedTurnReadFailure,
        NativeSavedTurnReadRequest,
        NativeSavedTurnSettlementFailure,
        NativeSavedTurnSettlementPublicationUnconfirmed,
        NativeSavedTurnSettlementRequest,
        NativeSavedTurnStartFailure,
        NativeSavedTurnStartRequest,
        SavedReceiptCaller,
        SavedTurnStartAction,
        capture_launcher_ai_routing_evaluator,
        capture_launcher_conversation_append_writer,
        capture_launcher_conversation_confirmation_writer,
        capture_launcher_conversation_get_reader,
        capture_launcher_model_resolve_reader,
        capture_launcher_model_save_writer,
        capture_launcher_model_settings_reader,
        capture_launcher_provider_prepare_planner,
        capture_launcher_saved_receipt_reader,
        capture_launcher_saved_orchestration_step,
        capture_launcher_saved_local_chat_executor,
        capture_launcher_saved_turn_reader,
        capture_launcher_saved_turn_settlement_writer,
        capture_launcher_saved_turn_start_writer,
        is_canonical_model_resolve_identifier,
        is_native_saved_turn_read_identifier,
        is_native_saved_turn_start_identifier,
    )
    from ..provider_prepare_native_v4 import (
        NativeProviderRegistryRequiresLegacyRead,
        current_provider_prepare_http_scope,
        invoke_native_provider_commit,
    )

    launcher_provider_prepare = native_provider_prepare_planner is None
    if launcher_provider_prepare:
        native_provider_prepare_planner = capture_launcher_provider_prepare_planner()
    native_model_settings_reader = capture_launcher_model_settings_reader()
    native_model_save_writer = capture_launcher_model_save_writer()
    native_conversation_get_reader = capture_launcher_conversation_get_reader()
    native_model_resolve_reader = capture_launcher_model_resolve_reader()
    native_ai_routing_evaluator = capture_launcher_ai_routing_evaluator()
    native_confirmation_writer = capture_launcher_conversation_confirmation_writer()
    native_append_writer = capture_launcher_conversation_append_writer()
    native_saved_receipt_reader = capture_launcher_saved_receipt_reader()
    native_saved_step = capture_launcher_saved_orchestration_step()
    native_saved_local_chat = capture_launcher_saved_local_chat_executor()
    native_saved_manifest = os.environ.get("TOBKIRI_NATIVE_SAVED_ORCHESTRATION_MANIFEST")
    if native_saved_step is not None and native_saved_manifest is not None:
        require_digest(native_saved_manifest, "admitted Saved Orchestration manifest")
    native_saved_turn_reader = capture_launcher_saved_turn_reader()
    native_saved_turn_settlement_writer = capture_launcher_saved_turn_settlement_writer()
    native_saved_turn_start_writer = capture_launcher_saved_turn_start_writer()

    authority_path = authority_store.path.resolve()
    if authority_path.name != "v4.sqlite3" or authority_path.parent.name != "authority":
        raise AuthorityDenied("Authority store path is not canonical")
    authority_user_data = authority_path.parent.parent
    profile_id = str(active.resolved.profile["profile_id"])
    generation_budget_template = owner_startup_generation_budget_template(
        generation_budget_template
    )
    if (generation_budget_template is not None
            and generation_budget_template.profile_id != profile_id):
        raise AuthorityDenied("Host generation budget Profile binding is invalid")
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
    from .external_profile_catalog import catalog_for_verified_active_profile
    from ..retained_bundled_context_v4 import (
        retained_origin_for_verified_active_profile,
    )

    retained_origin = retained_origin_for_verified_active_profile(
        active, user_data_root=authority_user_data
    )
    if retained_origin is None:
        from .profile_capture import baseline_origin_for_verified_active_profile

        retained_origin = baseline_origin_for_verified_active_profile(
            active, user_data_root=authority_user_data,
        )
    if retained_origin is None:
        catalog = catalog_for_verified_active_profile(
            catalog, active, user_data=authority_user_data,
        )
    else:
        catalog = catalog_for_verified_active_profile(
            catalog,
            active,
            user_data=authority_user_data,
            retained_bundled_origin=retained_origin,
        )
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
    shell_principal_ids = frozenset(
        principal.principal_id
        for principals in principals_by_function.values()
        for principal in principals
    )
    for binding in plan["bindings"]:
        principal = FunctionPrincipal.from_dict(binding["function_principal"])
        existing = list(principals_by_function.get(principal.function_id, ()))
        if principal not in existing:
            existing.append(principal)
        principals_by_function[principal.function_id] = tuple(existing)

    # A signed edge names only the caller Function; every admitted Operation
    # of that Function is one exact caller principal.  Functions that never
    # appear as a binding target still have digest-pinned declarations in the
    # admitted manifests, which keep their outgoing Profile edges deniable but
    # representable.  A caller Function with no admitted declaration at all
    # stays denied rather than masked or selected arbitrarily.
    admitted_pack_ids = {
        str(item["identity"]) for item in lock["effective_set"]
    }
    admitted_pack_ids.update(
        str(item["pack_id"]) for item in plan["bindings"]
    )
    admitted_pack_ids.add(shell_id)
    manifest_callers = _manifest_caller_principals(
        catalog, admitted_pack_ids,
        expected_artifact_digests={str(item["identity"]): str(item["artifact_digest"]) for item in lock["effective_set"]},
    )
    edge_caller_function_ids = {
        str(edge["caller_function_id"]) for edge in profile["requested_edges"]
    }
    for function_id in sorted(edge_caller_function_ids):
        existing = list(principals_by_function.get(function_id, ()))
        by_operation = {item.operation_id: item for item in existing}
        for declared in manifest_callers.get(function_id, ()):
            previous = by_operation.get(declared.operation_id)
            if previous is None:
                existing.append(declared)
                continue
            if previous != declared:
                raise AuthorityDenied(
                    "Profile edge caller declaration conflicts with the "
                    "signed ResolvedPlan"
                )
        principals_by_function[function_id] = tuple(existing)

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
        if not callers:
            raise AuthorityDenied(
                "Profile edge caller Function is not declared by the "
                "admitted inventory"
            )
        target = FunctionPrincipal.from_dict(binding["function_principal"])
        if str(edge["target_provider_id"]) != target.function_id:
            raise AuthorityDenied("Profile edge target differs from its ResolvedPlan binding")
        scope = _committed_operation_scope(edge, target)
        if binding["requested_scope_digest"] != canonical_digest(scope.to_dict()):
            raise AuthorityDenied("ResolvedPlan requested scope binding changed")
        authority_mode = _requested_edge_authority_mode(edge, binding)
        axis_ceilings = _authority_ceilings_for_edge(edge, target)
        for caller in callers:
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
    from .profile_capture import runtime_user_data_scope

    retained_root_options = (
        {"origin": retained_origin} if retained_origin is not None else {}
    )
    from ..bundled_host_baseline_v4 import bundled_host_baseline_read

    baseline_scope = (
        bundled_host_baseline_read(retained_origin, bundle_root=bundle_root)
        if retained_origin is not None else nullcontext()
    )
    with runtime_user_data_scope(authority_user_data), baseline_scope:
        pack_roots = resolve_admitted_pack_roots(
            tuple(sorted(binding_pack_ids)),
            ecosystem_root,
            artifact_pins={
                str(row["identity"]): str(row["artifact_digest"])
                for row in lock["effective_set"]
            },
            **retained_root_options,
        )
    captured_pack_root_identities = _pack_root_identities(pack_roots)
    from ..captured_ui_asset_delegation_v4 import CapturedAssetOnlyUIPacks

    # UI-only captures are not Function roots or Broker principals.
    ui_asset_packs = CapturedAssetOnlyUIPacks(
        artifact_pins={
            str(row["identity"]): str(row["artifact_digest"])
            for row in lock["effective_set"]
            if row["role"] == "pack"
        },
        owner_root=authority_user_data,
    )
    retained_pack_roots = {
        pack_id: root
        for pack_id, root in pack_roots.items()
        if retained_origin is not None
        and root.is_relative_to(
            authority_user_data / "retained-bundled-artifacts-v4"
        )
    }

    def assert_retained_root_current(pack_id: str) -> None:
        if pack_id not in retained_pack_roots:
            return
        from ..bundled_artifact_retention_v4 import (
            verify_retained_bundled_artifact,
        )

        assert retained_origin is not None
        checked = verify_retained_bundled_artifact(
            retained_origin,
            pack_id=pack_id,
            artifact_digest=dict(retained_origin.pack_pins)[pack_id],
        )
        if checked != retained_pack_roots[pack_id]:
            raise AuthorityDenied("retained Pack root changed during capture")

    def artifact_resolver(binding: ResolvedOperationBinding) -> Any:
        pack_id = binding.artifact.pack_id
        root = pack_roots.get(pack_id)
        expected_identity = captured_pack_root_identities.get(pack_id)
        if root is None or expected_identity is None:
            raise AuthorityDenied("resolved Pack artifact root is unavailable")
        before = root.lstat()
        if root.is_symlink() or (int(before.st_dev), int(before.st_ino)) != expected_identity:
            raise AuthorityDenied("resolved Pack artifact root identity changed")
        assert_retained_root_current(pack_id)
        artifact = capture_materialized_artifact(root, binding)
        assert_retained_root_current(pack_id)
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
        caller_function_declarations={
            function_id: tuple(
                principal.to_dict() for principal in declared
            )
            for function_id, declared in manifest_callers.items()
        },
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
    owned_packvm_backends: tuple[Any, ...] = ()
    owned_wasm_backends: tuple[Any, ...] = ()
    if backends is None:
        authenticated_backend = _authenticated_packvm_backend(packvm_provisioner)
        if authenticated_backend is not None:
            registered_backends += (authenticated_backend,)
            owned_packvm_backends = (authenticated_backend,)
        if any(
            binding.variant.execution_kind is ExecutionKind.WASM
            for binding in catalog_bindings
        ):
            try:
                from tobkiri_host.wasm_backend import production_wasm_backend

                wasm_backend = production_wasm_backend()
                registered_backends += (wasm_backend,)
                owned_wasm_backends = (wasm_backend,)
            except Exception:
                # A missing or unverified engine leaves the pinned binding
                # unavailable and receives no domain or Authority records.
                pass
    target_backend_digests = dict(target_backend_digests or {})
    from core_runtime.authority.policy_exact_grant import PolicyExactGrantKernel
    from tobkiri_host.policy_exact_authority_adapter import PolicyExactAuthorityV4Adapter

    policy_kernel = PolicyExactGrantKernel(
        authority_store, runtime.composition.resolver, lease_ttl_seconds=300
    )
    policy_kernel.policy_roots = {}
    authority_control = PolicyExactAuthorityV4Adapter(
        policy_kernel, runtime.composition.resolver,
    )
    profile_edge_authority_records: dict[Any, tuple[Any, Any | None]] = {}

    def unavailable_chat_continuation(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        raise PermissionError("chat approval continuation is unavailable")

    chat_approval_continuation = ChatApprovalContinuationController(
        approve=chat_continuation_approve or unavailable_chat_continuation,
        resume=chat_continuation_resume or unavailable_chat_continuation,
    )

    def unavailable_approval_window(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        raise PermissionError("authority approval window is unavailable")

    def bind_approval_window_presentation(
        command: AuthorityApprovalWindowOpenCommand,
    ) -> None:
        """Record the owner-scoped presenter grant for the Launcher window.

        The dedicated approval window is a separate webview surface with its
        own cookie store, so its one-time ``?code=`` exchange mints a fresh
        panel session.  Recording the grant only after the interactive
        approval port proves this caller already is the request's
        presentation owner lets that exact exchange resolve to the journal
        session the request is bound to.  A caller that does not own the
        request records nothing: the window's session then stays foreign and
        presentation validation remains fail-closed.
        """

        context = command.context
        try:
            authority_control.get_interactive_approval(
                InteractiveApprovalGetQuery(
                    context=context,
                    request_id=command.request_id,
                )
            )
        except AuthorityDenied:
            return
        owner_principal = str(context.caller_principal.value)
        owner_session = str(context.caller_session_id)
        session_suffix = (
            f".{owner_principal.removeprefix('sha256:')[:24]}"
            f".{activation_suffix}"
        )
        if (
            command.presentation_owner_principal_id != owner_principal
            or command.presentation_owner_session_id != owner_session
            or not owner_session.endswith(session_suffix)
            or len(owner_session) <= len(session_suffix)
        ):
            raise AuthorityDenied("authority approval window owner is invalid")
        manager = (
            panel_auth_manager
            if panel_auth_manager is not None
            else get_panel_auth_manager()
        )
        manager.record_approval_presenter_grant(
            command.request_id,
            owner_session[: -len(session_suffix)],
            PanelAuthBinding(
                profile_id=str(context.profile_id),
                profile_revision=str(context.profile_revision),
                activation_id=str(context.activation_id),
                plan_digest=str(context.plan_digest),
                security_epoch=int(context.security_epoch),
            ),
        )

    authority_approval_window = AuthorityApprovalWindowController(
        open_window=authority_approval_window_open or unavailable_approval_window,
        bind_presentation_owner=bind_approval_window_presentation,
    )

    def unavailable_model_search(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        raise PermissionError("model search is unavailable")

    model_search_controller = ModelSearchController(
        search_models=model_search or unavailable_model_search,
    )
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
            packvm_readiness_reader=timed_profile_reader(
                packvm_readiness_reader, "packvm_readiness"
            ),
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
            profile_edge_authority_records[captured_edge.binding_key] = _commit_plan_authority(
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
    approval_roots_by_pack = _optional_pack_approval_roots(catalog, optional_pack_ids)
    pack_ids_by_artifact_digest: dict[str, set[str]] = {}
    for item in lock["effective_set"]:
        pack_ids_by_artifact_digest.setdefault(
            str(item["artifact_digest"]),
            set(),
        ).add(str(item["identity"]))

    def is_exact_original_python_edge(
        edge: _CapturedPlanEdge,
        function_id: str,
        mode: str = "profile_grant",
        *,
        backend: str = "tobkiri.python-host-v4",
        execution_kind: str = "host_extension",
        domain_kind: str = "dedicated_process",
    ) -> bool:
        binding = edge.binding
        return (
            edge.authority_mode == mode
            and edge.target.function_id == function_id
            and binding.get("variant_id") == function_id + ".python"
            and binding.get("platform") == binding.get("architecture") == "any"
            and binding.get("runtime_abi") == "python3.13"
            and binding.get("backend") == backend
            and binding.get("execution_kind") == execution_kind
            and binding.get("domain_kind") == domain_kind
            and binding.get("adapter_digests") == []
            and edge.edge.get("requested_scope_template") == _operation_scope(
                binding["contract_id"], binding["operation_id"], edge.target,
            ).to_dict()
        )

    turn_pack = "rumi_turn_runtime_pack"
    saved_pack = "tobkiri_conversation_orchestration_pack"

    def select_original_chat_edge(
        caller_ids: set[str],
        pack_id: str,
        contract_id: str,
        operation_id: str,
        function_id: str,
        effect: EffectClass,
    ) -> _CapturedPlanEdge | None:
        matches = tuple(
            edge for edge in captured_edges
            if edge.caller.principal_id in caller_ids
            and edge.resolved_binding.artifact.pack_id == pack_id
            and edge.binding_key[1:] == (contract_id, operation_id)
            and edge.resolved_binding.operation.effect_class is effect
            and (
                is_exact_original_python_edge(
                    edge, function_id, backend="tobkiri.python-pack-v4",
                    execution_kind="pack_vm", domain_kind="pack_vm",
                ) if pack_id == saved_pack
                else is_exact_original_python_edge(edge, function_id)
            )
        )
        return matches[0] if len(matches) == 1 else None

    ordinary_turn_edge = select_original_chat_edge(
        {
            edge.caller.principal_id for edge in captured_edges
            if edge.caller.principal_id in shell_principal_ids
            and edge.caller.function_id == "shell.tauri.default"
        },
        turn_pack, "tobkiri.action.turn.saved.v1", f"{turn_pack}.turn-saved",
        f"{turn_pack}.turn-runtime.saved", EffectClass.PRIVILEGED,
    )
    ordinary_orchestration_edge = (
        select_original_chat_edge(
            {ordinary_turn_edge.target.principal_id}, saved_pack,
            "conversation.saved-turn.v1", "saved_complete", f"{saved_pack}.saved",
            EffectClass.PURE,
        ) if ordinary_turn_edge is not None else None
    )

    saved_local_chat_routes: dict[str, _SavedLocalChatRoute] = {}
    if (
        native_saved_local_chat is not None
        and ordinary_turn_edge is not None
        and ordinary_orchestration_edge is not None
    ):
        gateway_pack = "rumi_ai_gateway_pack"
        adapter_pack = "rumi_provider_adapters_pack"
        registry_pack = "rumi_provider_registry_pack"
        local_roles: tuple[Literal["generate", "stream"], ...] = ("generate", "stream")
        for role in local_roles:
            gateway_function = f"{gateway_pack}.ai-gateway.{role}"
            gateway = select_original_chat_edge(
                {ordinary_orchestration_edge.target.principal_id}, gateway_pack,
                f"tobkiri.service.ai.{role}.v1", gateway_function, gateway_function,
                EffectClass.PRIVILEGED,
            )
            if gateway is None:
                continue
            adapter = select_original_chat_edge(
                {gateway.target.principal_id}, adapter_pack,
                f"tobkiri.service.ai.provider.{role}.v1", f"{adapter_pack}.provider-{role}",
                f"{adapter_pack}.provider.compatibility.{role}", EffectClass.PRIVILEGED,
            )
            if adapter is None:
                continue
            registry = select_original_chat_edge(
                {adapter.target.principal_id}, registry_pack, LOCAL_REGISTRY_CONTRACT,
                f"{registry_pack}.provider-registry-resource.{role}",
                f"{registry_pack}.provider-registry.resource", EffectClass.PRIVILEGED,
            )
            if registry is not None:
                saved_local_chat_routes[adapter.caller.principal_id] = _SavedLocalChatRoute(
                    role, adapter, registry,
                    (gateway, ordinary_orchestration_edge, ordinary_turn_edge),
                )
    saved_local_chat_keys = {
        (*route.adapter.binding_key[1:], route.adapter.target.principal_id)
        for route in saved_local_chat_routes.values()
    }

    turn_start_edge = (
        select_original_chat_edge(
            {ordinary_turn_edge.target.principal_id},
            turn_pack,
            "tobkiri.action.turn.lifecycle.v1",
            f"{turn_pack}.turn-lifecycle",
            f"{turn_pack}.turn-runtime.lifecycle",
            EffectClass.PRIVILEGED,
        )
        if ordinary_turn_edge is not None and native_saved_turn_start_writer is not None
        else None
    )

    native_saved_execution: NativeWasmBinding | None = None
    native_saved_original_provider: str | None = None
    if (
        native_saved_step is not None
        and native_saved_manifest is not None
        and ordinary_orchestration_edge is not None
    ):
        from tobkiri_host.native_saved_orchestration import NativeSavedOrchestrationBackend

        edge = ordinary_orchestration_edge
        target_suffix = edge.target.principal_id.removeprefix("sha256:")[:24]
        manifest_hex = native_saved_manifest.removeprefix("sha256:")
        native_domain = (
            f"domain.provider.saved-wasm.{target_suffix}.{activation_suffix}.{manifest_hex}"
        )
        wasm_backend = NativeSavedOrchestrationBackend(
            edge.resolved_binding,
            native_domain,
            native_saved_manifest,
            execute=lambda invocation, data, guard: execute_native_saved_step(
                invocation, data, guard,
            ),
            preflight=lambda invocation, proof: saved_preflight(invocation, proof),
            dispatch=lambda invocation, frame, proof: saved_capability_bridge(
                invocation, frame, proof,
            ),
            capture_current=lambda invocation: capture_current_native_saved(invocation),
        )
        native_saved_execution = NativeWasmBinding(
            binding=edge.resolved_binding,
            backend=wasm_backend,
            caller_principal=OpaqueAuthorityRef(edge.caller.principal_id),
            domain_id=native_domain,
            executable_digest=native_saved_manifest,
        )

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
            profile_edge_authority_records[captured_edge.binding_key] = _commit_plan_authority(
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

        if resolved_binding.variant.execution_kind not in {
            ExecutionKind.PACK_VM,
            ExecutionKind.WASM,
        }:
            continue
        physical_wasm = (
            native_saved_execution
            if captured_edge is ordinary_orchestration_edge else None
        )
        if physical_wasm is not None:
            backend = physical_wasm.backend
        else:
            try:
                backend = selected_backend_registry.select(resolved_binding)
            except Exception:
                # A selected isolated backend remains visible in the catalog but
                # receives no authority until it is authenticated and ready.
                continue
            if not callable(getattr(backend, "bind_target_domain_resolver", None)):
                continue
        provenance_valid, approval_pack_id = _packvm_approval_provenance(
            caller_artifact_digest=captured_edge.caller.parent_artifact_digest,
            target_pack_id=resolved_binding.artifact.pack_id,
            optional_pack_ids=optional_pack_ids,
            pack_ids_by_artifact_digest=pack_ids_by_artifact_digest,
            approval_roots_by_pack=approval_roots_by_pack,
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
        execution_kind = backend.status.execution_kind
        authority_label = (
            "profile-pack-vm"
            if resolved_binding.variant.execution_kind is ExecutionKind.PACK_VM
            else "profile-wasm-component"
        )
        target_domain = _execution_domain(
            domain_id=(
                physical_wasm.domain_id if physical_wasm is not None else
                f"domain.provider.{target.principal_id.removeprefix('sha256:')[:24]}.{activation_suffix}"
            ),
            principal=target,
            active=active,
            boundary=(
                DomainBoundary.DEDICATED_PROCESS
                if execution_kind is ExecutionKind.PACK_VM
                else DomainBoundary.WASM_COMPONENT
            ),
            channel_seed=(
                f"{execution_kind.value}-provider:{resolved_binding.operation.contract_id}:"
                f"{resolved_binding.operation.operation_id}"
            ),
        )
        _register_exact_domain(
            authority_store,
            authority_control,
            target_domain,
            session_id=(
                f"session.{physical_wasm.domain_id}" if physical_wasm is not None else
                f"session.provider.{execution_kind.value}."
                f"{target.principal_id.removeprefix('sha256:')[:24]}.{activation_suffix}"
            ),
            principal=target,
        )
        profile_edge_authority_records[captured_edge.binding_key] = _commit_plan_authority(
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
            authority_label=authority_label,
            authority_mode=captured_edge.authority_mode,
            pack_approval_revision=pack_approval_revision,
            execution_provider_label=(
                "profile-saved-wasm-" + physical_wasm.executable_digest[7:31]
                if physical_wasm is not None else None
            ),
        )
        if physical_wasm is None:
            target_backend_digests[target.principal_id] = backend.status.backend_digest
        else:
            _, logical_grant = profile_edge_authority_records[captured_edge.binding_key]
            if logical_grant is None:
                raise AuthorityDenied("native Saved Orchestration original Grant is unavailable")
            native_saved_original_provider = "provider." + logical_grant.grant_id.removeprefix("grant.")

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
            raise AuthorityDenied(
                "production isolated target domain is not registered by Authority"
            )
        return domain_id

    # The bridge is Host-owned and receives only an authenticated outer
    # RequestEnvelope from the VZ supervisor.  The callback is intentionally
    # closed over the immutable capture; it never accepts caller, profile,
    # session, contract, provider, or plan identity from the guest.
    dispatch_holder: list[V4DispatchSession] = []
    bridge_targets = _bridge_targets_by_outer_edge(captured_edges)
    bridge_budget_registry = PackVMBridgeBudgetRegistry()

    def resolve_bridge_outer(outer_request: object) -> _CapturedPlanEdge:
        """Resolve bridge authority only from the captured authenticated outer edge."""
        deadline = getattr(outer_request, "deadline_monotonic", None)
        cancellation = getattr(outer_request, "cancellation_requested", None)
        if (
            not isinstance(deadline, (int, float))
            or isinstance(deadline, bool)
            or not math.isfinite(deadline)
            or deadline <= time.monotonic()
            or type(cancellation) is not threading.Event
            or cancellation.is_set()
        ):
            raise AuthorityDenied("PackVM capability bridge outer budget is invalid")
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
        physical_wasm = (
            native_saved_execution if outer_edge is ordinary_orchestration_edge else None
        )
        if physical_wasm is None:
            expected_target_domain = authority_target_domain(outer_edge.resolved_binding)
            expected_target_backend_digest = target_backend_digests.get(outer_target)
        else:
            expected_target_domain = physical_wasm.domain_id
            expected_target_backend_digest = physical_wasm.backend_digest
        if (
            outer_target != outer_edge.target.principal_id
            or outer_domain != expected_target_domain
            or outer_caller != outer_edge.caller.principal_id
            or getattr(outer_request, "contract_version", None)
            != outer_edge.resolved_binding.operation.contract_version
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
        return outer_edge

    def invoke_bridge_provider(
        outer_request: object,
        outer_edge: _CapturedPlanEdge,
        bridge_edge: _CapturedPlanEdge,
        request: Mapping[str, Any],
        *,
        parent_cancellation_proof: NestedCancellationProof | None,
        result_projector: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Invoke the selected Provider with one Host session and original budget."""
        outer_context = getattr(outer_request, "context", None)
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
        parent_deadline = getattr(outer_request, "deadline_monotonic", None)
        if parent_deadline is None:
            raise AuthorityDenied("PackVM capability bridge outer deadline is missing")
        parent_cancellation = getattr(outer_request, "cancellation_requested", None)
        if type(parent_cancellation) is not threading.Event:
            raise AuthorityDenied("PackVM capability bridge cancellation signal is missing")
        bridge_authority_session_id = bind_nested_session(
            bridge_session_id,
            outer_edge.target.principal_id,
            presentation_owner_for(outer_request),
            parent_invocation=capture_invocation_scope(outer_request),
        )
        bridge_phase = "finite_source"
        try:
            from contextlib import nullcontext
            from tobkiri_host.finite_chat_dispatch import finite_saved_chat_source
            finite_source = (
                finite_saved_chat_source(
                    capture_invocation_scope(outer_request),
                    parent_cancellation_proof,
                    request,
                    presentation_owner_for(outer_request),
                )
                if (bridge_edge.resolved_binding.operation.contract_id,
                    bridge_edge.resolved_binding.operation.operation_id) == (
                        "tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke")
                and request.get("tool_id") == "chat_send_message"
                else nullcontext()
            )
            with finite_source:
                bridge_phase = "provider_dispatch"
                provider_result = dispatch.invoke(
                    bridge_edge.resolved_binding.operation.contract_id,
                    bridge_edge.resolved_binding.operation.operation_id,
                    {**dict(request), "_session_id": bridge_session_id},
                    parent_deadline_monotonic=parent_deadline,
                    parent_cancellation=parent_cancellation,
                    parent_cancellation_proof=parent_cancellation_proof,
                    inline_parent_scope=capture_invocation_scope(outer_request),
                )
            bridge_phase = "result_shape"
            if not isinstance(provider_result, Mapping):
                raise TypeError("verified Provider capability returned a non-object")
            if result_projector is not None:
                bridge_phase = "result_projection"
                provider_result = result_projector(provider_result)
            bridge_phase = "result_encoding"
            result = {"status": "ok", "value": dict(provider_result)}
            if len(canonical_json(result)) > _PACKVM_BRIDGE_MAX_RESULT_BYTES:
                raise ValueError("verified Provider capability result is too large")
        except Exception as error:
            # Do not project provider/backend details through the PackVM ABI.
            # The guest receives a typed, bounded result it can safely render.
            from .bridge_diagnostics import record_bridge_failure

            record_bridge_failure(
                error,
                request_id=request_id,
                phase=bridge_phase,
                contract_id=bridge_edge.resolved_binding.operation.contract_id,
                operation_id=bridge_edge.resolved_binding.operation.operation_id,
            )
            result = _provider_unavailable_bridge_result(error)
        finally:
            release_nested_session(bridge_session_id, bridge_authority_session_id)
        return result

    def capability_bridge(
        outer_request: object,
        bridge_request: Mapping[str, Any],
        parent_cancellation_proof: NestedCancellationProof | None = None,
    ) -> Mapping[str, Any]:
        outer_edge = resolve_bridge_outer(outer_request)

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
        bridge_edge, expected_bridge_target = _resolve_packvm_bridge_target(
            bridge_candidates,
            target,
        )
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

        checked_continuation = _validate_packvm_bridge_continuation(
            continuation,
            operation_id=outer_edge.resolved_binding.operation.operation_id,
            target=expected_bridge_target,
            request_digest=str(bridge_request["request_digest"]),
        )
        nonce = str(checked_continuation["nonce"])

        budget_admission = bridge_budget_registry.admit(
            outer_request,
            bridge_edge.resolved_binding.operation.contract_id,
            request,
        )
        result = invoke_bridge_provider(
            outer_request,
            outer_edge,
            bridge_edge,
            request,
            parent_cancellation_proof=parent_cancellation_proof,
        )
        bridge_budget_registry.record(budget_admission, result)

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

    from .saved_bridge import (
        ALLOWED_TARGETS,
        SavedBridgeCallbacks,
        project_saved_ai_result,
        project_saved_tool_result,
    )

    saved_bridge_cancellation_proof: contextvars.ContextVar[
        NestedCancellationProof | None
    ] = contextvars.ContextVar(
        "saved_bridge_cancellation_proof",
        default=None,
    )

    def saved_target(outer_request: object, target: tuple[str, str]) -> _CapturedPlanEdge:
        outer_edge = resolve_bridge_outer(outer_request)
        if (
            outer_edge.resolved_binding.operation.contract_id != SAVED_CONVERSATION_CONTRACT
            or outer_edge.resolved_binding.operation.operation_id != SAVED_CONVERSATION_OPERATION
            or target not in ALLOWED_TARGETS
        ):
            raise AuthorityDenied("saved bridge outer operation is not selected")
        candidates = tuple(
            edge for edge in bridge_targets[outer_edge.key]
            if (edge.resolved_binding.operation.contract_id,
                edge.resolved_binding.operation.operation_id) == target
        )
        if len(candidates) != 1:
            raise AuthorityDenied("saved bridge target is missing or ambiguous")
        edge = candidates[0]
        if not dispatch_holder:
            raise AuthorityDenied("saved bridge dispatch is not initialized")
        dispatch_holder[0].assert_current()
        authority_target_domain(edge.resolved_binding)
        if target_backend_digests.get(edge.target.principal_id) is None:
            raise AuthorityDenied("saved bridge target backend is unavailable")
        return edge

    def require_saved_targets(outer_request: object, targets: tuple[tuple[str, str], ...]) -> None:
        for target in targets:
            saved_target(outer_request, target)

    def saved_dispatch(
        outer_request: object, target: tuple[str, str], payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        edge = saved_target(outer_request, target)
        if "profile_id" in payload or "_session_id" in payload:
            raise AuthorityDenied("saved bridge payload cannot select Host identity")
        arguments = dict(payload)
        if target[0] == "tobkiri.action.message.manage.v1" and payload.get("operation") == "append":
            initial = getattr(outer_request, "payload", None)
            if not isinstance(initial, Mapping):
                raise AuthorityDenied("saved bridge initial input is missing")
            arguments["operation"] = "append_saved"
            arguments["saved_input"] = validate_saved_conversation_input(initial)
        if target[0] in {"tobkiri.resource.conversation.v1", "tobkiri.action.message.manage.v1", "tobkiri.resource.workspace.v1", "tobkiri.resource.project.state.v1"}:
            arguments["profile_id"] = profile["profile_id"]
        runtime.composition.catalog.validate_input(edge.resolved_binding, arguments)
        return invoke_bridge_provider(
            outer_request, resolve_bridge_outer(outer_request), edge, arguments,
            parent_cancellation_proof=saved_bridge_cancellation_proof.get(),
            result_projector=(
                project_saved_ai_result
                if target[0]
                in {
                    "tobkiri.service.ai.generate.v1",
                    "tobkiri.service.ai.stream.v1",
                    "tobkiri.service.ai.strategy.dispatch.v1",
                }
                else project_saved_tool_result if target[0] == "tobkiri.service.tool.invoke.v1" else None
            ),
        )

    def saved_stream_available(
        outer_request: object, request: Mapping[str, Any], conversation: Mapping[str, Any],
    ) -> bool:
        from tobkiri_protocol.turn_progress_v1 import (
            AI_STREAM, ACTION, ACTION_OPERATION, RESOURCE, RESOURCE_OPERATION,
        )
        try:
            require_saved_targets(outer_request, (
                AI_STREAM, (ACTION, ACTION_OPERATION), (RESOURCE, RESOURCE_OPERATION),
            ))
            outcome = saved_dispatch(outer_request, (ACTION, ACTION_OPERATION), {"phase": "ready"})
            value = outcome.get("value")
            return outcome.get("status") == "ok" and isinstance(value, Mapping) and value.get("ready") is True
        except (AuthorityDenied, PermissionError, RuntimeError, ValueError):
            return False

    from tobkiri_host.saved_workspace_context import SavedWorkspaceExecutionRegistry

    saved_workspace_execution_registry = SavedWorkspaceExecutionRegistry()

    def saved_selected_workspace() -> Mapping[str, Any]:
        from ecosystem.rumi_workspace_mount_pack.runtime.mounts import (
            capture_selected_workspace_binding,
        )

        if not dispatch_holder:
            raise AuthorityDenied("saved workspace capture is unavailable")
        dispatch_holder[0].assert_current()
        return capture_selected_workspace_binding(
            str(profile["profile_id"]), user_data_root=authority_user_data,
        )

    def borrow_bridge_saved_context(outer: object) -> AbstractContextManager[Any]:
        consumer = resolve_bridge_outer(outer)
        conversation = saved_target(outer, (
            "tobkiri.resource.conversation.v1", "rumi_conversation_store_pack.conversation-resource",
        ))
        project_targets = tuple(edge for edge in bridge_targets[consumer.key]
            if (edge.resolved_binding.operation.contract_id, edge.resolved_binding.operation.operation_id)
            == ("tobkiri.resource.project.state.v1", "tobkiri_ui_settings_pack.projects-read"))
        if len(project_targets) > 1:
            raise AuthorityDenied("saved Project selected owner is ambiguous")

        def read_project(payload):
            if payload != {"profile_id": str(profile["profile_id"])}:
                raise AuthorityDenied("saved Project Profile changed")
            outcome = saved_dispatch(outer, ("tobkiri.resource.project.state.v1",
                "tobkiri_ui_settings_pack.projects-read"), {})
            value = outcome.get("value")
            if outcome.get("status") != "ok" or not isinstance(value, Mapping):
                raise AuthorityDenied("saved Project owner is unavailable")
            return value

        return _borrow_selected_saved_context(
            operation=outer, consumer=consumer.resolved_binding, conversation_owner=conversation.resolved_binding,
            project_owner=project_targets[0].resolved_binding if project_targets else None,
            factories=loaded_host_factories, profile_id=str(profile["profile_id"]),
            presentation_owner=lambda: presentation_owner_for(outer), read_project=read_project,
            guard=lambda: saved_target(outer, ("tobkiri.resource.conversation.v1",
                "rumi_conversation_store_pack.conversation-resource")),
        )

    saved_callbacks = SavedBridgeCallbacks(
        saved_dispatch,
        require_saved_targets,
        saved_stream_available,
        saved_workspace_execution_registry,
        saved_selected_workspace,
        borrow_saved_context=borrow_bridge_saved_context,
    )

    def saved_capability_bridge(
        outer_request: object,
        frame: Any,
        parent_cancellation_proof: NestedCancellationProof | None = None,
    ) -> Mapping[str, Any]:
        token = saved_bridge_cancellation_proof.set(parent_cancellation_proof)
        try:
            return saved_callbacks(outer_request, frame)
        finally:
            saved_bridge_cancellation_proof.reset(token)

    def saved_preflight(
        outer_request: object,
        parent_cancellation_proof: NestedCancellationProof | None = None,
    ) -> None:
        token = saved_bridge_cancellation_proof.set(parent_cancellation_proof)
        try:
            saved_callbacks.preflight(outer_request)
        finally:
            saved_bridge_cancellation_proof.reset(token)

    saved_backend_ids = {
        edge.resolved_binding.variant.backend for edge in captured_edges
        if edge.resolved_binding.operation.contract_id == SAVED_CONVERSATION_CONTRACT
        and edge.resolved_binding.operation.operation_id == SAVED_CONVERSATION_OPERATION
        and edge.resolved_binding.variant.execution_kind is ExecutionKind.PACK_VM
    }
    packvm_backend_ids = {
        edge.resolved_binding.variant.backend
        for edge in captured_edges
        if edge.resolved_binding.variant.execution_kind is ExecutionKind.PACK_VM
    }
    isolated_backend_ids = {
        edge.resolved_binding.variant.backend
        for edge in captured_edges
        if edge.resolved_binding.variant.execution_kind
        in {ExecutionKind.PACK_VM, ExecutionKind.WASM}
    }
    for registered_backend in registered_backends:
        if registered_backend.status.backend_id not in isolated_backend_ids:
            continue
        binder = getattr(registered_backend, "bind_artifact_resolver", None)
        if not callable(binder):
            raise AuthorityDenied(
                "production isolated backend cannot bind authenticated artifacts"
            )
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
        if registered_backend.status.backend_id in saved_backend_ids:
            saved_binder = getattr(registered_backend, "bind_saved_capability_bridge", None)
            if not callable(saved_binder):
                raise AuthorityDenied("production PackVM backend cannot bind saved callbacks")
            saved_binder(saved_capability_bridge, saved_preflight)
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
    cancellation_handles = OwnedCancellationHandles()
    cancellation_roles: dict[str, tuple[str, str, str]] = {}
    local_model_bindings: dict[str, LocalModelInvocationBinding] = {}
    close_callbacks.append(cancellation_handles.close)
    invocation_evidence_registry = InvocationEvidenceRegistry()
    close_callbacks.append(invocation_evidence_registry.close)
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
    presentation_owner_refcounts: dict[str, int] = {}
    nested_session_refcounts: dict[str, int] = {}
    if credential_store_binding is not None:
        credential_store_binding = capture_native_generation_budget(
            binding=credential_store_binding, authority_store=authority_store,
            user_data_root=authority_user_data, profile_id=profile_id,
            template=generation_budget_template,
        )
        admission = credential_store_binding.generation_admission
        close_admission = getattr(admission, "close", None)
        if admission is not None and callable(close_admission):
            close_callbacks.append(close_admission)
    elif generation_budget_template is not None:
        raise AuthorityDenied("generation budget requires Host credential storage")

    parent_invocation_scopes = ParentInvocationScopesV4()
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

    def retain_presentation_owner(session_id: str, owner: tuple[str, str]) -> None:
        """Share one Host-derived owner across concurrent nested and resumed calls."""
        with caller_session_bindings_lock:
            if presentation_owner_bindings.get(session_id) not in {None, owner}:
                raise AuthorityDenied("nested Host Provider owner binding changed")
            presentation_owner_bindings[session_id] = owner
            presentation_owner_refcounts[session_id] = (
                presentation_owner_refcounts.get(session_id, 0) + 1
            )

    def release_presentation_owner(session_id: str) -> None:
        with caller_session_bindings_lock:
            remaining = presentation_owner_refcounts[session_id] - 1
            if remaining:
                presentation_owner_refcounts[session_id] = remaining
            else:
                presentation_owner_refcounts.pop(session_id)
                presentation_owner_bindings.pop(session_id)

    @contextmanager
    def pending_effect_owner_scope(
        context: RequestContext, principal_id: str, session_id: str,
    ) -> Iterator[None]:
        """Restore the durable owner with a fresh prepared-dispatch capture scope."""
        from .profile_capture import profile_capture_scope

        # A detached approved dispatch has no caller ContextVar scope. A
        # synchronous one may still carry its caller's earlier snapshot. Give
        # both one fresh operation scope while every live guard still runs.
        with profile_capture_scope(fresh=True):
            retain_presentation_owner(
                context.caller_session_id, (principal_id, session_id)
            )
            try:
                yield
            finally:
                release_presentation_owner(context.caller_session_id)

    from tobkiri_host.saved_tool_entry_guards import SavedToolEntryGuardRegistry

    saved_tool_entry_guards = SavedToolEntryGuardRegistry()
    from core_runtime.policy_invocation_v4 import PolicyInvocationRegistry
    policy_invocation_registry = PolicyInvocationRegistry()
    from core_runtime.saved_tool_policy_selection_v4 import RetainedSelectionDispatchV4
    policy_selection_dispatch = RetainedSelectionDispatchV4()

    def capture_invocation_scope(envelope: Any) -> CapturedInvocationScopeV4:
        with caller_session_bindings_lock:
            parent = parent_invocation_scopes.lookup(envelope.context.caller_session_id)
        saved_tool_entry_guard = saved_tool_entry_guards.capture(envelope)
        saved_workspace_binding = saved_workspace_execution_registry.capture(
            envelope, parent
        )

        def guard() -> None:
            with _capture_guard_traversal(assert_current_capture):
                assert_dispatched_invocation(envelope, authority_store)
                if (
                    native_saved_execution is not None
                    and envelope.context.target_domain_id == native_saved_execution.domain_id
                ):
                    assert_original_saved_provider_current()
                if saved_tool_entry_guard is not None:
                    saved_tool_entry_guard()
                if saved_workspace_binding is not None:
                    # Fresh owner/mount reads remain at workspace authority
                    # and effect boundaries, rather than ambient ancestry checks.
                    saved_workspace_binding.assert_execution_live()
                if parent is not None:
                    parent.assert_current()
                if not dispatch_holder:
                    raise AuthorityDenied("captured dispatch is unavailable")
                dispatch_holder[0].assert_current()

        return CapturedInvocationScopeV4(
            envelope, guard, parent, saved_workspace_binding
        )

    def bind_nested_session(
        session_id: str,
        caller_principal_id: str,
        presentation_owner: tuple[str, str],
        parent_invocation: CapturedInvocationScopeV4 | None = None,
    ) -> str:
        """Atomically bind one stable nested session and retain concurrent users."""

        resolved_session_id = authority_session_id(session_id, caller_principal_id)
        with caller_session_bindings_lock:
            existing_caller = caller_session_bindings.get(session_id)
            if existing_caller not in {None, caller_principal_id}:
                raise AuthorityDenied("nested Host Provider caller binding changed")
            if parent_invocation is not None:
                parent_invocation_scopes.retain(resolved_session_id, parent_invocation)
            retain_presentation_owner(resolved_session_id, presentation_owner)
            caller_session_bindings[session_id] = caller_principal_id
            nested_session_refcounts[session_id] = (
                nested_session_refcounts.get(session_id, 0) + 1
            )
        return resolved_session_id

    def release_nested_session(
        session_id: str, resolved_session_id: str, *, release_parent: bool = True,
    ) -> None:
        """Release one stable nested-session user without racing a peer call."""

        with caller_session_bindings_lock:
            release_presentation_owner(resolved_session_id)
            if release_parent:
                parent_invocation_scopes.release(resolved_session_id)
            remaining = nested_session_refcounts.get(session_id, 0) - 1
            if remaining > 0:
                nested_session_refcounts[session_id] = remaining
                return
            nested_session_refcounts.pop(session_id, None)
            caller_session_bindings.pop(session_id, None)

    @contextmanager
    def retained_effect_resume_scope(scope: CapturedInvocationScopeV4, context: RequestContext) -> Any:
        """Retain only this live coordinator dispatch across its approved execute."""
        scope.assert_current()
        if (scope.envelope.target_principal != context.caller_principal
                or any(getattr(scope.envelope.context, field) != getattr(context, field)
                       for field in ("profile_id", "profile_revision", "activation_id",
                                     "activation_digest", "plan_digest", "profile_authority_digest",
                                     "security_epoch", "fencing_token"))):
            raise AuthorityDenied("effect execute capture or caller changed")
        with caller_session_bindings_lock:
            owner = presentation_owner_bindings.get(scope.envelope.context.caller_session_id)
            if owner is None:
                raise AuthorityDenied("effect execute owner is unavailable")
            parent_invocation_scopes.retain(context.caller_session_id, scope)
        try:
            yield {
                "execution_guard": scope.assert_current,
                "inline_parent_scope": scope,
                "parent_deadline_monotonic": scope.envelope.deadline_monotonic,
                "cancellation_requested": scope.envelope.cancellation_requested,
                "nested_cancellation_proof": nested_cancellation_proof_for(scope.envelope, owner[0], owner[1]),
            }
            scope.assert_current()
        finally:
            with caller_session_bindings_lock:
                parent_invocation_scopes.release(context.caller_session_id)

    def saved_local_chat_lineage(
        context: RequestContext,
        payload: Mapping[str, Any],
        parent: CapturedInvocationScopeV4 | None,
    ) -> _SavedLocalChatRoute | None:
        route = saved_local_chat_routes.get(context.caller_principal.value)
        connection_id = payload.get("provider_connection_id")
        if (
            route is None
            or payload.get("request_surface") != "conversation.saved"
            or payload.get("profile_id") not in (None, context.profile_id)
            or payload.get("credential_handle") is not None
            or not isinstance(connection_id, str)
            or not connection_id
        ):
            return None
        for edge in route.parents:
            if parent is None or not matches_captured_edge(parent.envelope, edge):
                return None
            parent = parent.parent
        return route if parent is None else None

    def is_native_saved_local_chat_route(
        context: RequestContext, payload: Mapping[str, Any],
    ) -> bool:
        return native_provider_runtime is not None and saved_local_chat_lineage(
            context, payload, parent_invocation_scopes.lookup(context.caller_session_id),
        ) is not None

    class _InvocationSession:
        """Bind nested dispatch to the authenticated provider invocation."""

        def __init__(
            self,
            envelope: Any,
            *,
            presentation_owner: tuple[str, str],
            observe_local_registry: Callable[
                [str, str, Mapping[str, Any], Mapping[str, Any]], None
            ] | None = None,
        ) -> None:
            self._envelope = envelope
            self._presentation_owner = presentation_owner
            self._observe_local_registry = observe_local_registry
            self.profile_id = str(profile["profile_id"])
            self.plan_digest = str(plan["plan_digest"])
            self.profile_revision = str(plan["profile_revision"])
            self.activation_id = str(active.activation["activation_id"])

        @property
        def presentation_owner_principal_id(self) -> str:
            return self._presentation_owner[0]

        @property
        def presentation_owner_session_id(self) -> str:
            return self._presentation_owner[1]

        def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
            capture_invocation_scope(self._envelope).assert_current()
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
            idempotency_key: str | None = None,
            timeout_ms: int | None = None,
            expected_payload_digest: str | None = None,
        ) -> Mapping[str, Any]:
            if not dispatch_holder:
                raise AuthorityDenied("Host Provider dispatch is not initialized")
            scope = capture_invocation_scope(self._envelope)
            scope.assert_current()
            independent_saved_cancellation = None
            if (
                (self._envelope.contract_id, self._envelope.operation_id)
                == ("tobkiri.action.chat.message.delivery.v1", "rumi_turn_runtime_pack.chat-message-deliver")
                and (contract_id, operation_id)
                == ("tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved")
            ):
                from ecosystem.rumi_tool_broker_pack.runtime.chat_delivery import (
                    assert_delivery_saved_branch,
                )
                from types import SimpleNamespace
                delivery_scope = SimpleNamespace(
                    envelope=self._envelope,
                    parent_invocation=scope.parent,
                    assert_current=scope.assert_current,
                    presentation_owner_principal_id=self._presentation_owner[0],
                    presentation_owner_session_id=self._presentation_owner[1],
                )
                assert_delivery_saved_branch(delivery_scope, payload)
                independent_saved_cancellation = threading.Event()
            calendar_dispatch_branch = None
            from tobkiri_host.calendar_dispatch_branch import (
                CalendarDispatchBranch, is_calendar_effect_dispatch,
            )
            if is_calendar_effect_dispatch(self._envelope, contract_id, operation_id, payload):
                key = ("rumi_job_action_broker_pack.job-action.broker", contract_id, operation_id)
                selected = resolved_binding_by_edge.get(key)
                if (selected is None or pack_by_principal.get(self._envelope.target_principal.value)
                        != "rumi_job_action_broker_pack"
                        or selected.function.function_id != "rumi_turn_runtime_pack.chat-saved-job-adapter"):
                    raise AuthorityDenied("Calendar selected adapter is unavailable")
                calendar_dispatch_branch = CalendarDispatchBranch(
                    scope, cancellation_handles, self._presentation_owner, selected,
                )
            nested_session_id = execution_session_id(self._envelope)
            nested_authority_session_id = bind_nested_session(
                nested_session_id,
                self._envelope.target_principal.value,
                self._presentation_owner,
                parent_invocation=scope,
            )
            try:
                result = dispatch_holder[0].invoke(
                    contract_id,
                    operation_id,
                    {**dict(payload), "_session_id": nested_session_id},
                    version_range=version_range,
                    parent_deadline_monotonic=self._envelope.deadline_monotonic,
                    parent_cancellation=self._envelope.cancellation_requested,
                    independent_saved_cancellation=independent_saved_cancellation,
                    calendar_dispatch_branch=calendar_dispatch_branch,
                    inline_parent_scope=scope,
                    execution_guard=scope.assert_current,
                    parent_cancellation_proof=nested_cancellation_proof_for(
                        self._envelope,
                        self._presentation_owner[0],
                        self._presentation_owner[1],
                    ),
                    idempotency_key=idempotency_key,
                    timeout_ms=timeout_ms,
                    expected_payload_digest=expected_payload_digest,
                )
                scope.assert_current()
                if self._observe_local_registry is not None:
                    self._observe_local_registry(contract_id, operation_id, payload, result)
                return result
            except Exception as error:
                log_nested_dispatch_failure(contract_id, operation_id, error)
                raise
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
            self._scope = capture_invocation_scope(envelope)
            self._client: GlobalContractClient | None = None
            self._client_binding: tuple[frozenset[str], str, bool] | None = None
            route = saved_local_chat_lineage(
                envelope.context, envelope.payload, self._scope.parent,
            )
            self._local_chat_route = (
                route if route is not None and matches_captured_edge(envelope, route.adapter)
                else None
            )
            self._local_chat_selection: tuple[str, int | None, str] | None = None
            self._local_chat_source_attempted = False
            self._local_chat_attempted = False

        @property
        def envelope(self) -> Any:
            return self._envelope

        @property
        def presentation_owner_principal_id(self) -> str:
            return self._presentation_owner_principal_id

        @property
        def presentation_owner_session_id(self) -> str:
            return self._presentation_owner_session_id

        @property
        def parent_invocation(self) -> CapturedInvocationScopeV4 | None:
            return self._scope.parent

        def _assert_saved_tool_parent_scopes(
            self, scopes: tuple[CapturedInvocationScopeV4, ...],
        ) -> None:
            """Prove supplied recipient joins against retained Host parents."""
            self.assert_current()
            if not scopes or self._scope.parent is not scopes[0]:
                raise PermissionError("saved tool current parent scope changed")
            original: CapturedInvocationScopeV4 | None = self._scope.parent
            for scope in scopes:
                if (not isinstance(scope, CapturedInvocationScopeV4)
                        or scope is not original):
                    raise PermissionError("saved tool retained parent scope changed")
                scope.assert_current()
                original = scope.parent

        @property
        def cancellation(self) -> OwnedCancellationBinding:
            declaration = cancellation_roles.get(self._envelope.target_principal.value)
            if declaration is None:
                raise AuthorityDenied("Host Provider cancellation role is unavailable")
            pack_id, group, role = declaration
            return cancellation_handles.bind(
                group=(pack_id, group), role=role, envelope=self._envelope,
                owner_principal=self.presentation_owner_principal_id,
                owner_session=self.presentation_owner_session_id,
                guard=self.assert_current,
            )

        @property
        def approved_interrupt(self) -> Any:
            """Expose interruption only to exact currently approved delivery."""
            from tobkiri_host.approved_interrupt import ApprovedInterruptBinding
            from core_runtime.owned_chat_message_approval_v4 import assert_chat_message_interrupt_authority
            if ((self._envelope.contract_id, self._envelope.operation_id) != (
                    "tobkiri.action.chat.message.delivery.v1", "rumi_turn_runtime_pack.chat-message-deliver")
                    or pack_by_principal.get(self._envelope.target_principal.value) != "rumi_turn_runtime_pack"):
                raise AuthorityDenied("approved interrupt port is unavailable")
            def authorize(state: Any) -> None:
                _execute, owner = assert_chat_message_interrupt_authority(self, state)
                if owner != self.presentation_owner_principal_id:
                    raise AuthorityDenied("approved interrupt presentation owner changed")
            return ApprovedInterruptBinding(registry=cancellation_handles, envelope=self._envelope,
                presentation_owner_principal_id=self.presentation_owner_principal_id,
                authorize=authorize, guard=self.assert_current)

        @property
        def scheduled_job_cancellation(self) -> Any:
            """Expose exact selected occurrence stop only to JobBroker cancel."""
            from tobkiri_host.scheduled_job_cancellation import (
                ADAPTER, ScheduledJobCancellationBinding,
            )
            if (
                (self._envelope.contract_id, self._envelope.operation_id) != ADAPTER
                or pack_by_principal.get(self._envelope.target_principal.value)
                != "rumi_turn_runtime_pack"
                or cancellation_roles.get(self._envelope.target_principal.value)
                != ("rumi_turn_runtime_pack", "saved-turn", "execute")
            ):
                raise AuthorityDenied("scheduled occurrence stop port is unavailable")
            return ScheduledJobCancellationBinding(
                registry=cancellation_handles, scope=self._scope,
                guard=self.assert_current,
            )

        def contract_client(
            self,
            *,
            allowed_contract_ids: frozenset[str],
            consumer_pack_id: str,
            include_credentials: bool = True,
        ) -> GlobalContractClient:
            expected_pack_id = pack_by_principal.get(self._envelope.target_principal.value)
            if type(include_credentials) is not bool:
                raise AuthorityDenied("Host Provider credential selection is invalid")
            binding = (allowed_contract_ids, consumer_pack_id, include_credentials)
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
                    generation_admission=(
                        credential_store_binding.generation_admission
                    ),
                    consumer_pack_id=consumer_pack_id,
                    parent_guard=self.assert_current,
                )
                if include_credentials and credential_store_binding is not None
                else None
            )
            invocation_session = _InvocationSession(
                self._envelope,
                presentation_owner=(
                    self._presentation_owner_principal_id,
                    self._presentation_owner_session_id,
                ),
                observe_local_registry=(
                    self._retain_local_chat_selection if self._local_chat_route else None
                ),
            )
            local_binding = local_model_bindings.get(provider_principal.principal_id)
            local_transport = create_local_model_transport(
                envelope=self._envelope, principal=provider_principal,
                authority_store=authority_store, user_data_root=authority_user_data,
                registry_snapshot=lambda: invocation_session.invoke(
                    LOCAL_REGISTRY_CONTRACT,
                    local_binding.registry_operation_id,
                    {"profile_id": profile_id},
                ),
                assert_current=self.assert_current,
                binding=local_binding,
            ) if (
                local_binding is not None
                and LOCAL_REGISTRY_CONTRACT in allowed_contract_ids
            ) else None
            self._client = GlobalContractClient(
                session=invocation_session,
                allowed_contract_ids=allowed_contract_ids,
                consumer_pack_id=consumer_pack_id,
                host_credential_transport=transport,
                host_local_model_transport=local_transport,
            )
            self._client_binding = binding
            return self._client

        def _retain_local_chat_selection(
            self,
            contract_id: str,
            operation_id: str,
            query: Mapping[str, Any],
            result: Mapping[str, Any],
        ) -> None:
            route = self._local_chat_route
            context = self._envelope.context
            if (
                route is None
                or (contract_id, operation_id) != route.registry.binding_key[1:]
                or query not in ({}, {"profile_id": context.profile_id})
                or not isinstance(result, Mapping)
                or result.get("profile_id") != context.profile_id
                or not isinstance(result.get("providers"), list)
            ):
                return
            expected = self._envelope.payload["provider_connection_id"]
            records = [
                item for item in result["providers"]
                if isinstance(item, Mapping) and item.get("provider_instance_id") == expected
            ]
            self._local_chat_selection = None
            if len(records) != 1:
                return
            record = records[0]
            revision, endpoint = record.get("record_revision"), record.get("endpoint")
            if (
                record.get("adapter_id") == "local-openai-compatible"
                and record.get("enabled") is True
                and record.get("credential_handle") is None
                and isinstance(endpoint, str) and endpoint
            ):
                native_revision = (
                    revision if type(revision) is int and 1 <= revision <= MAX_SAFE_INTEGER
                    else None
                )
                self._local_chat_selection = expected, native_revision, endpoint

        def post_saved_local_chat(
            self,
            connection: Mapping[str, Any],
            encoded: bytes,
            deadline_monotonic: float,
        ) -> bytes | dict[str, Any] | None:
            """Borrow the live signed local Adapter invocation for one POST."""
            return self._post_saved_local_chat(
                connection, encoded, deadline_monotonic, compile_request=False,
            )

        @property
        def supports_saved_local_chat_request_compilation(self) -> bool:
            """Check the captured route and the Launcher's negotiated input form."""
            return (
                self._local_chat_route is not None
                and native_provider_runtime is not None
                and getattr(native_saved_local_chat, "supports_request_compilation", False)
                is True
            )

        @property
        def supports_saved_local_chat_policy_compilation(self) -> bool:
            """Check the captured route and support for native saved model policy."""
            return (
                self.supports_saved_local_chat_request_compilation
                and getattr(native_saved_local_chat, "supports_policy_compilation", False)
                is True
            )

        def post_saved_local_chat_source(
            self,
            connection: Mapping[str, Any],
            request: Mapping[str, Any],
            deadline_monotonic: float,
            *,
            compile_policy: bool = False,
        ) -> bytes | dict[str, Any] | None:
            """Borrow the same live dispatch authority for a source-form request."""
            if not self.supports_saved_local_chat_request_compilation:
                return None
            if compile_policy and not self.supports_saved_local_chat_policy_compilation:
                return None
            return self._post_saved_local_chat(
                connection, request, deadline_monotonic, compile_request=True,
                compile_policy=compile_policy,
            )

        def _post_saved_local_chat(
            self,
            connection: Mapping[str, Any],
            request: bytes | Mapping[str, Any],
            deadline_monotonic: float,
            *,
            compile_request: bool,
            compile_policy: bool = False,
        ) -> bytes | dict[str, Any] | None:
            route, runtime = self._local_chat_route, native_provider_runtime
            executor = native_saved_local_chat
            if route is None or runtime is None or executor is None:
                return None
            self.assert_current()
            envelope = self._envelope
            selection = self._local_chat_selection
            if (
                selection is None
                or connection.get("adapter_id") != "local-openai-compatible"
                or connection.get("enabled") is not True
                or connection.get("credential_handle") is not None
                or connection.get("provider_instance_id") != selection[0]
                or connection.get("endpoint") != selection[2]
                or self._local_chat_attempted
                or (compile_request and self._local_chat_source_attempted)
            ):
                raise GlobalContractInvocationError(
                    "denied", "local provider connection binding changed",
                )
            if compile_request:
                self._local_chat_source_attempted = True
            instance, revision, endpoint = selection
            if revision is None:
                # Some older Registry documents predate record revisions.
                return None
            if (
                type(connection.get("record_revision")) is not int
                or connection["record_revision"] != revision
            ):
                raise GlobalContractInvocationError(
                    "denied", "local provider connection revision changed",
                )
            reservation = envelope.resource_reservation_id
            if production_admission.native_resources(reservation).memory_bytes < (
                SAVED_LOCAL_CHAT_MINIMUM_MEMORY
            ):
                return None
            deadline = min(envelope.deadline_monotonic, deadline_monotonic)
            dispatch_possible = False

            def guard() -> None:
                self.assert_current()
                if native_provider_runtime is not runtime:
                    raise AuthorityDenied("local provider runtime generation changed")
                child = envelope
                scope = self._scope.parent
                for edge in route.parents:
                    retained = parent_invocation_scopes.lookup(child.context.caller_session_id)
                    if (
                        scope is None or retained is None
                        or retained.envelope is not scope.envelope
                        or not matches_captured_edge(scope.envelope, edge)
                        or scope.envelope.lease.token == child.lease.token
                        or (
                            child.resource_reservation_id is not None
                            and child.resource_reservation_id == scope.envelope.resource_reservation_id
                        )
                        or scope.envelope.deadline_monotonic < child.deadline_monotonic
                    ):
                        raise AuthorityDenied("local provider retained invocation changed")
                    child, scope = scope.envelope, scope.parent
                if scope is not None or parent_invocation_scopes.lookup(
                    child.context.caller_session_id,
                ) is not None:
                    raise AuthorityDenied("local provider saved turn owner changed")
                runtime[1]()
                production_admission.native_resources(reservation)

            def mark_dispatch_possible() -> None:
                nonlocal dispatch_possible
                guard()
                if dispatch_possible:
                    raise AuthorityDenied("local provider dispatch permit was already offered")
                dispatch_possible = True

            self._local_chat_attempted = True
            guard()
            if isinstance(request, bytes):
                encoded = request
            else:
                source_fields = {"model_id", "provider_id", "messages", "parameters", "tools"}
                source = {
                    key: value for key, value in request.items() if key in source_fields
                }
                try:
                    encoded = json.dumps(
                        source, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                    ).encode("utf-8")
                except (TypeError, ValueError, RecursionError):
                    encoded = None
                if encoded is None or len(encoded) > SAVED_LOCAL_CHAT_MAX_INPUT_BYTES:
                    guard()
                    self._local_chat_attempted = False
                    return None
            try:
                result = executor(NativeSavedLocalChatRequest(
                    context=envelope.context, runtime_instance=runtime[0],
                    deadline_monotonic=deadline, assert_current=guard,
                    role=route.role, connection_id=instance, record_revision=revision,
                    endpoint=endpoint, input=encoded, assert_dispatch=guard,
                    mark_dispatch_possible=mark_dispatch_possible,
                    compile_request=compile_request,
                    compile_policy=compile_policy,
                ))
            except NativeProviderPrepareUnavailable:
                guard()
                if not dispatch_possible:
                    if compile_request:
                        # The authenticated exchange declined before a permit,
                        # so offer the compatibility body to the same authority.
                        self._local_chat_attempted = False
                    return None
                raise GlobalContractInvocationError(
                    "provider_unavailable", "local provider dispatch is unconfirmed",
                ) from None
            except NativeSavedLocalChatFailure as error:
                guard()
                failures = {
                    "denied": ("denied", "local provider authorization changed"),
                    "invalid": ("invalid_request", "local provider request is invalid"),
                    "invalid-response": ("invalid_response", "local provider response is too large"),
                }
                if compile_policy and not dispatch_possible:
                    failures.update({
                        "policy-model-denied": (
                            "denied", "model is not permitted by this connection",
                        ),
                        "policy-model-invalid": (
                            "invalid_request", "provider model ID is invalid",
                        ),
                        "policy-parameters-invalid": (
                            "invalid_request",
                            "request parameters cannot override the saved model route",
                        ),
                    })
                code, message = failures.get(
                    error.code, ("provider_unavailable", "local provider is unavailable"),
                )
                raise GlobalContractInvocationError(code, message) from None
            except (AuthorityDenied, PermissionError):
                raise
            except (OSError, RuntimeError):
                raise GlobalContractInvocationError(
                    "provider_unavailable", "local provider is unavailable",
                ) from None
            guard()
            if not dispatch_possible or type(result) not in (bytes, dict):
                raise AuthorityDenied("local provider result is not confirmed")
            return result

        def dispatch_bounded(
            self,
            *,
            allowed_contract_ids: frozenset[str],
            consumer_pack_id: str,
            contract_id: str,
            operation_id: str,
            payload: Mapping[str, Any],
            idempotency_key: str,
            timeout_ms: int | None,
            expected_payload_digest: str,
            evidence_ref: HostInvocationEvidenceReferenceV4 | None = None,
        ) -> Mapping[str, Any]:
            """Dispatch one durable typed attempt through the real Broker.

            Only Host-owned consumers reach this path.  The durable
            idempotency key, the caller's bounded timeout (which can only
            tighten the envelope deadline) and the digest of the pinned
            resolved input all reach the real ``RequestBroker``; credential
            transport stays disabled and declared-contract restrictions are
            identical to ``contract_client``.  No arbitrary frame override
            is accepted.
            """
            expected_pack_id = pack_by_principal.get(
                self._envelope.target_principal.value
            )
            if expected_pack_id != consumer_pack_id:
                raise AuthorityDenied("Host Provider consumer identity is invalid")
            if contract_id not in allowed_contract_ids:
                raise AuthorityDenied("Host Provider contract is not declared")
            if type(idempotency_key) is not str or not idempotency_key:
                raise AuthorityDenied("Host Provider idempotency key is invalid")
            if timeout_ms is not None and (
                type(timeout_ms) is not int or timeout_ms <= 0
            ):
                raise AuthorityDenied("Host Provider timeout bound is invalid")
            if (
                type(expected_payload_digest) is not str
                or not expected_payload_digest
            ):
                raise AuthorityDenied("Host Provider payload digest is invalid")
            session = _InvocationSession(
                self._envelope,
                presentation_owner=(
                    self._presentation_owner_principal_id,
                    self._presentation_owner_session_id,
                ),
            )
            evidence_scope: AbstractContextManager[None] = nullcontext()
            if evidence_ref is not None:
                if not isinstance(evidence_ref, HostInvocationEvidenceReferenceV4):
                    raise AuthorityDenied("Host evidence reference is invalid")
                issuer = principal_by_id.get(self._envelope.target_principal.value)
                if issuer is None:
                    raise AuthorityDenied("Host evidence issuer is unavailable")
                target = resolved_binding_by_edge.get(
                    (issuer.function_id, contract_id, operation_id)
                )
                if target is None:
                    raise AuthorityDenied("Host evidence target is not a selected edge")
                evidence_scope = invocation_evidence_registry.dispatch(
                    kind=evidence_ref.kind,
                    reference=evidence_ref.reference,
                    binding=self._evidence_binding(
                        issuer_principal=self._envelope.target_principal.value,
                        target_principal=target.principal_ref.value,
                        contract_id=contract_id,
                        contract_version=target.operation.contract_version,
                        operation_id=operation_id,
                        payload_digest=expected_payload_digest,
                        idempotency_key=idempotency_key,
                    ),
                    guard=self.assert_current,
                )
            with evidence_scope:
                return session.invoke(
                    contract_id, operation_id, dict(payload),
                    idempotency_key=idempotency_key, timeout_ms=timeout_ms,
                    expected_payload_digest=expected_payload_digest,
                )

        def _evidence_binding(
            self, *, issuer_principal: str, target_principal: str,
            contract_id: str, contract_version: str, operation_id: str, payload_digest: str,
            idempotency_key: str,
        ) -> EvidenceBinding:
            context = self._envelope.context
            return EvidenceBinding(
                issuer_principal=issuer_principal, target_principal=target_principal,
                contract_id=contract_id, contract_version=contract_version,
                operation_id=operation_id,
                payload_digest=payload_digest, idempotency_key=idempotency_key,
                profile_id=context.profile_id, plan_digest=context.plan_digest,
                activation_id=context.activation_id,
                activation_digest=context.activation_digest,
                security_epoch=context.security_epoch,
                presentation_owner_principal=self.presentation_owner_principal_id,
                presentation_owner_session=self.presentation_owner_session_id,
            )

        def invocation_evidence(self, kind: str) -> BoundInvocationEvidence:
            self.assert_current()
            envelope = self._envelope
            return invocation_evidence_registry.receive(
                kind=kind, receiver=envelope, guard=self.assert_current,
                binding=self._evidence_binding(
                    issuer_principal=envelope.context.caller_principal.value,
                    target_principal=envelope.target_principal.value,
                    contract_id=envelope.contract_id,
                    contract_version=envelope.contract_version,
                    operation_id=envelope.operation_id,
                    payload_digest=digest_payload(dict(envelope.payload)),
                    idempotency_key=envelope.idempotency_key or "",
                ),
            )

        def assert_current(self) -> None:
            """Fence durable coordination with actual lease and preserved ancestry."""
            with _capture_guard_traversal(assert_current_capture):
                self._scope.assert_current()
                if (
                    self._envelope.cancellation_requested.is_set()
                    or self._envelope.deadline_monotonic <= time.monotonic()
                ):
                    raise AuthorityDenied("Host Provider invocation is no longer active")
                if not dispatch_holder:
                    raise AuthorityDenied("Host Provider dispatch is not initialized")
                dispatch_holder[0].assert_current()

    def invocation_context(envelope: Any) -> HostProviderInvocationContextV4:
        return _HostInvocation(envelope)

    try:
        host_provider_state_root = SecureDirectory(authority_user_data / "host_provider_state").root
    except (OSError, SecurePersistenceError) as exc:
        raise AuthorityDenied("Host Provider state root is unavailable") from exc

    from core_runtime.host_contract import ExecutionProfileIdentity
    from core_runtime.project_directory_launcher import LauncherDirectoryPickerPort
    from tobkiri_host.directory_picker import CapturedDirectoryPicker
    from tobkiri_host.directory_selections import DirectorySelections

    directory_selections = DirectorySelections()
    directory_picker = CapturedDirectoryPicker(
        LauncherDirectoryPickerPort(ExecutionProfileIdentity(
            profile_id=str(profile["profile_id"]),
            profile_revision=str(plan["profile_revision"]),
            activation_id=str(active.activation["activation_id"]),
            plan_digest=str(plan["plan_digest"]),
        )), directory_selections,
    )
    from core_runtime.project_directory_port import ProjectDirectoryPort
    directory_selection_port = ProjectDirectoryPort(directory_picker, directory_selections)
    close_callbacks.append(directory_selection_port.close)

    from core_runtime.saved_tool_capture_v4 import (
        LOCAL_EXECUTOR_FUNCTION,
        LateBoundSavedToolConsentPortV4,
        optional_captured_consent_route,
        assert_saved_tool_requested_mode,
    )

    saved_tool_consent_port = LateBoundSavedToolConsentPortV4()
    saved_tool_mode_admission_holder: list[Callable[[Any], str]] = [
        assert_saved_tool_requested_mode
    ]

    def saved_tool_mode_admission(invocation: Any) -> str:
        return saved_tool_mode_admission_holder[0](invocation)
    approval_policy_capabilities_holder: list[Any] = []

    def project_approval_policy_capabilities(
        invocation: Any, applicability: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if len(approval_policy_capabilities_holder) != 1:
            raise PermissionError("approval policy capabilities are not captured")
        return approval_policy_capabilities_holder[0](invocation, applicability)

    def borrow_turn_saved_context(invocation: Any) -> AbstractContextManager[Any]:
        invocation.assert_current()
        envelope = invocation.envelope
        matches = []
        for binding in catalog_bindings:
            if (binding.principal_ref.value == envelope.target_principal.value
                    and binding.operation.contract_id == envelope.contract_id
                    and binding.operation.operation_id == envelope.operation_id
                    and binding not in matches):
                matches.append(binding)
        if len(matches) != 1 or envelope.context.profile_id != str(profile["profile_id"]):
            raise AuthorityDenied("saved context invocation owner changed")
        consumer = matches[0]
        caller = consumer.function.function_id
        conversation_owner = resolved_binding_by_edge.get((caller,
            "tobkiri.resource.conversation.v1", "rumi_conversation_store_pack.conversation-resource"))
        project_owner = resolved_binding_by_edge.get((caller,
            "tobkiri.resource.project.state.v1", "tobkiri_ui_settings_pack.projects-read"))

        def read_project(payload):
            invocation.assert_current()
            return _InvocationSession(envelope, presentation_owner=(
                invocation.presentation_owner_principal_id, invocation.presentation_owner_session_id,
            )).invoke("tobkiri.resource.project.state.v1", "tobkiri_ui_settings_pack.projects-read", payload)

        return _borrow_selected_saved_context(
            operation=invocation, consumer=consumer, conversation_owner=conversation_owner, project_owner=project_owner,
            factories=loaded_host_factories, profile_id=str(profile["profile_id"]),
            presentation_owner=lambda: (invocation.presentation_owner_principal_id,
                                        invocation.presentation_owner_session_id),
            read_project=read_project, guard=invocation.assert_current,
        )

    def host_provider_capture_context(
        provider_bindings: tuple[ResolvedOperationBinding, ...],
        *,
        workspace_mutation_port: HostWorkspaceMutationPort | None,
        interactive_effect_port: LateBoundInteractiveEffectPort | None = None,
        declared_pack_data: tuple[CapturedHostPackDataV4, ...] = (),
        wake_port: LateBoundWakePortV4 | None = None,
        directory_port: ProjectDirectoryPort | None = None,
        saved_tool_port: Callable[..., Mapping[str, Any]] | None = None,
        saved_tool_mode_port: Callable[[Any], str] | None = None,
        capabilities_port: Callable[..., Mapping[str, Any]] | None = None,
        selected_tool_policy_port: Callable[[Any], Any] | None = None,
        action_approval_policy_port: Any | None = None,
        workflow_port: LateBoundWorkflowAttemptPortV4 | None = None,
        saved_turn_settlement_port: (
            Callable[[HostProviderInvocationContextV4], SavedTurnSettlementPort | None]
            | None
        ) = None,
        conversation_confirmation_port: (
            Callable[
                [HostProviderInvocationContextV4], SavedConversationConfirmationPort | None
            ] | None
        ) = None,
        saved_message_append_port: (
            Callable[[HostProviderInvocationContextV4], SavedMessageAppendPort | None] | None
        ) = None,
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
            chat_approval_continuation_port=chat_approval_continuation,
            authority_approval_window_port=authority_approval_window,
            model_search_port=model_search_controller,
            interactive_effect_port=interactive_effect_port,
            workspace_mutation_port=workspace_mutation_port,
            directory_selection_port=directory_port,
            declared_pack_data=declared_pack_data,
            wake_port=wake_port,
            saved_tool_consent_port=saved_tool_port,
            saved_tool_mode_admission=saved_tool_mode_port,
            action_approval_policy_capabilities_port=capabilities_port,
            selected_tool_policy_port=selected_tool_policy_port,
            generation_budget_port=(
                credential_store_binding.generation_admission
                if credential_store_binding is not None
                and credential_store_binding.generation_admission is not None
                and all(item.function.function_id in {
                    "rumi_provider_registry_pack.provider-configure.prepare",
                    "rumi_provider_registry_pack.provider-configure.execute",
                    "rumi_credential_broker_pack.credential-broker.manage",
                } for item in provider_bindings)
                else None
            ),
            action_approval_policy_port=action_approval_policy_port,
            workflow_attempt_port=workflow_port,
            outbound_catalog_bindings=tuple(
                binding for key, binding in resolved_binding_by_edge.items()
                if key[0] in {item.function.function_id for item in provider_bindings}
            ),
            borrow_saved_context=(borrow_turn_saved_context
                if len(provider_bindings) == 1 and provider_bindings[0].function.function_id
                == "rumi_turn_runtime_pack.turn-runtime.saved" else None),
            borrow_saved_turn_settlement=saved_turn_settlement_port,
            borrow_saved_conversation_confirmation=conversation_confirmation_port,
            borrow_saved_message_append=saved_message_append_port,
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
        local_request = getattr(factory, "local_model_request", None)
        if local_request is not None:
            if type(local_request) is not LocalModelRequest:
                raise AuthorityDenied("Host local model declaration is invalid")
            for binding in captured_bindings:
                principal = _binding_principal(binding)
                matches = tuple(
                    edge for edge in captured_edges
                    if edge.caller == principal
                    and edge.resolved_binding.operation.contract_id == LOCAL_REGISTRY_CONTRACT
                    and edge.resolved_binding.operation.operation_id == local_request.registry_operation_id
                )
                if (
                    binding.operation.contract_id != local_request.contract_id
                    or len(matches) != 1
                    or matches[0].target != _binding_principal(matches[0].resolved_binding)
                    or principal.principal_id in local_model_bindings
                ):
                    raise AuthorityDenied("Host local model dependency binding is invalid")
                local_model_bindings[principal.principal_id] = capture_local_model_binding(
                    binding, matches[0].resolved_binding,
                )
        cancellation_group = getattr(factory, "cancellation_group", None)
        if cancellation_group is not None:
            role = getattr(factory, "cancellation_role", None)
            if (
                not isinstance(cancellation_group, str)
                or not 0 < len(cancellation_group) <= 128
                or role not in {"execute", "stop"}
            ):
                raise AuthorityDenied("Host Provider cancellation declaration is invalid")
            for binding in captured_bindings:
                cancellation_roles[binding.principal_ref.value] = (
                    binding.artifact.pack_id, cancellation_group, role,
                )

    workflow_assembly = capture_workflow_port_assembly_v4(
        tuple(loaded_host_factories), captured_edges,
    )
    workflow_attempt_port = (
        LateBoundWorkflowAttemptPortV4() if workflow_assembly is not None else None
    )

    interactive_effect_coordinator = _interactive_effect_coordinator_factory(
        tuple(loaded_host_factories)
    )
    interactive_effect_port: LateBoundInteractiveEffectPort | None = None
    if interactive_effect_coordinator is not None:
        interactive_effect_port = LateBoundInteractiveEffectPort()

    native_prepare_edges: tuple[_CapturedPlanEdge, _CapturedPlanEdge] | None = None
    native_execute_edge: _CapturedPlanEdge | None = None
    native_provider_runtime: tuple[str, Callable[[], None]] | None = None

    def publish_native_provider_runtime(
        instance: str, check: Callable[[], None],
    ) -> None:
        nonlocal native_provider_runtime
        native_provider_runtime = (instance, check)

    if (
        native_provider_prepare_planner is not None
        and interactive_effect_coordinator is not None
        and "." not in profile_id
    ):
        coordinator_binding = interactive_effect_coordinator[1][0]
        shell_edges = tuple(
            edge for edge in captured_edges
            if edge.caller.principal_id in shell_principal_ids
            and edge.resolved_binding == coordinator_binding
            and is_exact_original_python_edge(
                edge, "rumi_host_authority_bridge_pack.host-authority.interactive-effect",
            )
        )
        prepare_edges = tuple(
            edge for edge in captured_edges
            if edge.caller.principal_id == coordinator_binding.principal_ref.value
            and edge.resolved_binding.artifact.pack_id == "rumi_provider_registry_pack"
            and edge.resolved_binding.operation.contract_id
            == NATIVE_PROVIDER_PREPARE_CONTRACT
            and edge.resolved_binding.operation.operation_id
            == NATIVE_PROVIDER_PREPARE_OPERATION
            and is_exact_original_python_edge(edge, NATIVE_PROVIDER_PREPARE_FUNCTION)
        )
        if len(shell_edges) == len(prepare_edges) == 1:
            native_prepare_edges = shell_edges[0], prepare_edges[0]
            execute_edges = tuple(
                edge for edge in captured_edges
                if edge.caller.principal_id == coordinator_binding.principal_ref.value
                and edge.resolved_binding.artifact.pack_id
                == "rumi_provider_registry_pack"
                and edge.resolved_binding.operation.contract_id
                == NATIVE_PROVIDER_PREPARE_CONTRACT
                and edge.resolved_binding.operation.operation_id
                == NATIVE_PROVIDER_EXECUTE_OPERATION
                and is_exact_original_python_edge(
                    edge, NATIVE_PROVIDER_EXECUTE_FUNCTION, "interactive_only",
                )
            )
            if len(execute_edges) == 1 and (
                credential_store_binding is None
                or credential_store_binding.generation_admission is None
            ):
                native_execute_edge = execute_edges[0]

    native_model_settings_edge: _CapturedPlanEdge | None = None
    if native_model_settings_reader is not None:
        model_settings_edges = tuple(
            edge for edge in captured_edges
            if edge.caller.principal_id in shell_principal_ids
            and edge.caller.function_id == "shell.tauri.default"
            and edge.resolved_binding.artifact.pack_id == NATIVE_MODEL_SETTINGS_PACK
            and edge.resolved_binding.operation.contract_id
            == NATIVE_MODEL_SETTINGS_CONTRACT
            and edge.resolved_binding.operation.operation_id
            == NATIVE_MODEL_SETTINGS_OPERATION
            and edge.resolved_binding.operation.effect_class is EffectClass.READ
            and is_exact_original_python_edge(edge, NATIVE_MODEL_SETTINGS_FUNCTION)
        )
        if len(model_settings_edges) == 1:
            native_model_settings_edge = model_settings_edges[0]

    native_model_save_edge: _CapturedPlanEdge | None = None
    if native_model_save_writer is not None:
        save_edges = tuple(
            edge for edge in captured_edges
            if edge.caller.principal_id in shell_principal_ids
            and edge.caller.function_id == "shell.tauri.default"
            and edge.resolved_binding.artifact.pack_id == NATIVE_MODEL_SETTINGS_PACK
            and edge.resolved_binding.operation.contract_id == _MODEL_SAVE_CONTRACT
            and edge.resolved_binding.operation.operation_id == _MODEL_SAVE_OPERATION
            and edge.resolved_binding.operation.effect_class is EffectClass.PRIVILEGED
            and is_exact_original_python_edge(edge, _MODEL_SAVE_FUNCTION)
        )
        if len(save_edges) == 1:
            manage = save_edges[0]
            provider_reads = tuple(
                edge for edge in captured_edges
                if edge.caller == manage.target
                and edge.resolved_binding.artifact.pack_id == "rumi_provider_registry_pack"
                and edge.resolved_binding.operation.contract_id == _MODEL_PROVIDER_CONTRACT
                and edge.resolved_binding.operation.operation_id == _MODEL_PROVIDER_OPERATION
                # The original catalog classifies this Registry resource as privileged.
                and edge.resolved_binding.operation.effect_class is EffectClass.PRIVILEGED
                and is_exact_original_python_edge(edge, _MODEL_PROVIDER_FUNCTION)
            )
            if len(provider_reads) == 1:
                native_model_save_edge = manage

    conversation_pack = "rumi_conversation_store_pack"
    conversation_contract = "tobkiri.resource.conversation.v1"
    conversation_operation = "rumi_conversation_store_pack.conversation-resource"
    conversation_function = "rumi_conversation_store_pack.conversation-store.resource"
    native_conversation_get_edges: dict[
        str, tuple[ConversationGetCaller, _CapturedPlanEdge]
    ] = {}
    if native_conversation_get_reader is not None:
        conversation_callers: dict[str, tuple[ConversationGetCaller, str | None]] = {
            "shell.tauri.default": ("shell", None),
            "rumi_turn_runtime_pack.turn-runtime.saved": (
                "saved-turn", "rumi_turn_runtime_pack",
            ),
            "tobkiri_conversation_orchestration_pack.saved": (
                "saved-orchestration", "tobkiri_conversation_orchestration_pack",
            ),
        }
        for function_id, (caller, pack_id) in conversation_callers.items():
            caller_principals = (
                shell_principal_ids
                if pack_id is None
                else {
                    principal_id for principal_id, pack in pack_by_principal.items()
                    if pack == pack_id
                }
            )
            matching = tuple(
                edge for edge in captured_edges
                if edge.caller.function_id == function_id
                and edge.caller.principal_id in caller_principals
                and edge.resolved_binding.artifact.pack_id == conversation_pack
                and edge.resolved_binding.operation.contract_id == conversation_contract
                and edge.resolved_binding.operation.operation_id
                == conversation_operation
                and edge.resolved_binding.operation.effect_class
                is EffectClass.PRIVILEGED
                and is_exact_original_python_edge(edge, conversation_function)
            )
            if len(matching) == 1:
                edge = matching[0]
                native_conversation_get_edges[edge.caller.principal_id] = caller, edge

    def is_native_conversation_get_route(
        context: RequestContext, payload: Mapping[str, Any],
    ) -> bool:
        identifier = payload.get("conversation_id")
        return (
            native_provider_runtime is not None
            and context.caller_principal.value in native_conversation_get_edges
            and payload.get("operation") == "get"
            and payload.get("profile_id") == context.profile_id
            and set(payload) - {"_session_id"}
            == {"profile_id", "operation", "conversation_id"}
            and isinstance(identifier, str)
            and bool(identifier)
            and len(identifier) <= CONVERSATION_GET_MAX_ID_CHARS
        )

    def wrap_native_conversation_get(
        contribution: HostProviderContributionV4,
        reader: Callable[[NativeConversationGetRequest], Mapping[str, Any]],
    ) -> HostProviderContributionV4:
        """Read through the Launcher while the original exact Broker edge is live."""

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native Conversation GET context is unavailable")
            envelope = invocation.envelope
            context = envelope.context
            if not is_native_conversation_get_route(context, payload):
                return contribution.invoke(operation_id, payload, invocation)
            caller, edge = native_conversation_get_edges[context.caller_principal.value]
            parent = invocation.parent_invocation
            parent_envelope = parent.envelope if parent is not None else None
            pinned_runtime = native_provider_runtime
            if (
                pinned_runtime is None
                or operation_id != conversation_operation
                or envelope.contract_id != conversation_contract
                or envelope.target_principal != edge.resolved_binding.principal_ref
                or payload is not envelope.payload
                or (parent is None) != (caller == "shell")
                or (
                    parent_envelope is not None
                    and parent_envelope.target_principal != context.caller_principal
                )
            ):
                raise AuthorityDenied("native Conversation GET lineage changed")

            def guard() -> None:
                invocation.assert_current()
                retained = parent_invocation_scopes.lookup(context.caller_session_id)
                retained_envelope = retained.envelope if retained is not None else None
                if (
                    native_provider_runtime is not pinned_runtime
                    or retained_envelope is not parent_envelope
                ):
                    raise AuthorityDenied("native Conversation GET capture changed")
                pinned_runtime[1]()
                production_admission.native_resources(envelope.resource_reservation_id)

            def legacy_read() -> Mapping[str, Any]:
                guard()
                result = contribution.invoke(operation_id, payload, invocation)
                guard()
                return result

            guard()
            resources = production_admission.native_resources(
                envelope.resource_reservation_id,
            )
            if resources.memory_bytes < CONVERSATION_GET_MINIMUM_MEMORY:
                return legacy_read()
            try:
                result = reader(NativeConversationGetRequest(
                    context=context,
                    runtime_instance=pinned_runtime[0],
                    deadline_monotonic=envelope.deadline_monotonic,
                    assert_current=guard,
                    caller=caller,
                    conversation_id=payload["conversation_id"],
                ))
            except (
                NativeProviderPrepareUnavailable,
                NativeProviderRegistryRequiresLegacyRead,
            ):
                return legacy_read()
            except NativeConversationGetFailure as error:
                guard()
                if error.code == "not-found":
                    raise KeyError("conversation is unavailable") from None
                if error.code == "invalid":
                    raise ValueError(
                        "conversation or message identifier is invalid",
                    ) from None
                if error.code == "denied":
                    raise PermissionError(
                        "conversation read operation is denied",
                    ) from None
                raise
            guard()
            return result

        return replace(contribution, invoke=invoke)

    def matches_captured_edge(
        envelope: RequestEnvelope, edge: _CapturedPlanEdge,
    ) -> bool:
        return (
            envelope.context.caller_principal.value == edge.caller.principal_id
            and envelope.target_principal == edge.resolved_binding.principal_ref
            and (envelope.contract_id, envelope.operation_id) == edge.binding_key[1:]
        )

    signed_saved_parents = tuple(
        edge for edge in captured_edges
        if ordinary_turn_edge is not None
        and edge.target == ordinary_turn_edge.target
        and edge.binding_key[1:] == ordinary_turn_edge.binding_key[1:]
    )

    def native_saved_parent(context: RequestContext) -> CapturedInvocationScopeV4:
        parent = parent_invocation_scopes.lookup(context.caller_session_id)
        if parent is None or not any(
            matches_captured_edge(parent.envelope, edge) for edge in signed_saved_parents
        ):
            raise AuthorityDenied("native Saved Orchestration parent is unavailable")
        return parent

    def assert_original_saved_provider_current() -> None:
        provider_id = native_saved_original_provider
        if provider_id is None or authority_store.is_revoked("provider_authority", provider_id):
            raise AuthorityDenied("original Saved Orchestration Provider was revoked")

    def selected_native_saved_authority(
        context: RequestContext, target: OpaqueAuthorityRef,
    ) -> NativeWasmBinding | None:
        native = native_saved_execution
        if native is None:
            return None
        exact_edge = (
            context.caller_principal == native.caller_principal
            and target == native.binding.principal_ref
        )
        if exact_edge:
            assert_original_saved_provider_current()
            native_saved_parent(context).assert_current()
            return native
        if (
            context.target_domain_id == native.domain_id
            or context.target_backend_digest == native.backend_digest
        ):
            raise AuthorityDenied("native Saved Orchestration authority identity changed")
        return None

    def selected_native_saved_binding(
        context: RequestContext, binding: ResolvedOperationBinding,
    ) -> NativeWasmBinding | None:
        native = selected_native_saved_authority(context, binding.principal_ref)
        if native is not None and binding != native.binding:
            raise AuthorityDenied("native Saved Orchestration signed binding changed")
        return native

    def capture_current_native_saved(envelope: RequestEnvelope) -> Callable[[], None]:
        captured_runtime = native_provider_runtime
        scope = capture_invocation_scope(envelope)
        parent = native_saved_parent(envelope.context)
        if (
            captured_runtime is None
            or scope.parent is None
            or scope.parent.envelope is not parent.envelope
            or resolve_bridge_outer(envelope) is not ordinary_orchestration_edge
            or envelope.lease.token == parent.envelope.lease.token
            or envelope.resource_reservation_id == parent.envelope.resource_reservation_id
            or envelope.deadline_monotonic > parent.envelope.deadline_monotonic
        ):
            raise AuthorityDenied("native Saved Orchestration captured scope changed")

        def guard() -> None:
            if (
                native_provider_runtime is not captured_runtime
                or native_saved_parent(envelope.context).envelope is not parent.envelope
            ):
                raise AuthorityDenied("native Saved Orchestration capture changed")
            scope.assert_current()
            captured_runtime[1]()
            resources = production_admission.native_resources(envelope.resource_reservation_id)
            if resources.memory_bytes < SAVED_ORCHESTRATION_MINIMUM_MEMORY:
                raise AuthorityDenied("native Saved Orchestration capacity is unavailable")

        return guard

    def execute_native_saved_step(
        envelope: RequestEnvelope,
        step_input: Mapping[str, Any],
        guard: Callable[[], None],
    ) -> Mapping[str, Any]:
        launcher_runtime = native_provider_runtime
        guard()
        if native_saved_step is None or launcher_runtime is None:
            raise AuthorityDenied("native Saved Orchestration Launcher is unavailable")
        return native_saved_step(NativeSavedOrchestrationRequest(
            context=envelope.context,
            runtime_instance=launcher_runtime[0],
            deadline_monotonic=envelope.deadline_monotonic,
            assert_current=guard,
            input=step_input,
        ))

    @dataclass(frozen=True)
    class _SavedCompletionRoute:
        """The exact signed edges for saved completion and its nested Turn read."""

        caller: SavedReceiptCaller
        completion: _CapturedPlanEdge
        parent: _CapturedPlanEdge
        resource: _CapturedPlanEdge

    saved_receipt_routes: dict[
        str, tuple[SavedReceiptCaller, _CapturedPlanEdge, _CapturedPlanEdge]
    ] = {}
    saved_turn_routes: dict[
        str, tuple[SavedReceiptCaller, _CapturedPlanEdge]
    ] = {}
    confirmation_routes: dict[str, _SavedCompletionRoute] = {}
    completion_function = "rumi_conversation_store_pack.conversation-store.completion"
    completion_operation = (
        "tobkiri.action.conversation.completion.v1",
        "rumi_conversation_store_pack.conversation-completion",
    )
    if (
        native_saved_receipt_reader is not None
        or native_saved_turn_settlement_writer is not None
        or native_confirmation_writer is not None
        or native_saved_turn_reader is not None
    ):
        turn_pack = "rumi_turn_runtime_pack"
        receipt_callers: tuple[tuple[SavedReceiptCaller, str], ...] = (
            ("saved-turn", "saved"), ("saved-reconcile", "reconcile"),
        )
        for caller, kind in receipt_callers:
            turn_function = f"{turn_pack}.turn-runtime.{kind}"
            parents = tuple(
                edge for edge in captured_edges
                if edge.caller.principal_id in shell_principal_ids
                and edge.caller.function_id == "shell.tauri.default"
                and edge.resolved_binding.artifact.pack_id == turn_pack
                and edge.binding_key[1:] == (
                    f"tobkiri.action.turn.{kind}.v1", f"{turn_pack}.turn-{kind}",
                )
                and edge.resolved_binding.operation.effect_class
                is EffectClass.PRIVILEGED
                and is_exact_original_python_edge(edge, turn_function)
            )
            if len(parents) != 1:
                continue
            parent_edge, = parents
            if native_saved_turn_settlement_writer is not None:
                saved_turn_routes[parent_edge.target.principal_id] = (caller, parent_edge)
            if native_confirmation_writer is not None or native_saved_turn_reader is not None:
                completion_edges = tuple(
                    edge for edge in captured_edges
                    if edge.caller.principal_id == parent_edge.target.principal_id
                    and edge.binding_key[1:] == completion_operation
                    and edge.resolved_binding.artifact.pack_id == conversation_pack
                    and edge.resolved_binding.operation.effect_class is EffectClass.PRIVILEGED
                    and is_exact_original_python_edge(edge, completion_function)
                )
                if len(completion_edges) == 1:
                    completion_edge, = completion_edges
                    resource_edges = tuple(
                        edge for edge in captured_edges
                        if edge.caller.principal_id == completion_edge.target.principal_id
                        and edge.binding_key[1:] == (
                            "tobkiri.resource.turn.v1", f"{turn_pack}.turn-resource",
                        )
                        and edge.resolved_binding.artifact.pack_id == turn_pack
                        and edge.resolved_binding.operation.effect_class
                        is EffectClass.PRIVILEGED
                        and is_exact_original_python_edge(edge, f"{turn_pack}.turn-runtime.resource")
                    )
                    if len(resource_edges) == 1:
                        confirmation_routes[parent_edge.target.principal_id] = _SavedCompletionRoute(
                            caller, completion_edge, parent_edge, resource_edges[0],
                        )
            if native_saved_receipt_reader is None:
                continue
            receipt_edges = tuple(
                edge for edge in captured_edges
                if edge.caller.principal_id == parent_edge.target.principal_id
                and edge.caller.function_id == turn_function
                and edge.resolved_binding.artifact.pack_id == conversation_pack
                and edge.binding_key[1:] == (
                    conversation_contract, conversation_operation,
                )
                and edge.resolved_binding.operation.effect_class
                is EffectClass.PRIVILEGED
                and is_exact_original_python_edge(edge, conversation_function)
            )
            if len(receipt_edges) == 1:
                receipt_edge, = receipt_edges
                saved_receipt_routes[receipt_edge.caller.principal_id] = (
                    caller, receipt_edge, parent_edge,
                )

    saved_owner_publications: WeakValueDictionary[int, RequestEnvelope] = WeakValueDictionary()
    saved_owner_publication_lock = threading.Lock()

    def saved_owner_publication_possible(envelope: RequestEnvelope) -> bool:
        with saved_owner_publication_lock:
            return saved_owner_publications.get(id(envelope)) is envelope

    def record_saved_owner_publication(envelope: RequestEnvelope) -> None:
        with saved_owner_publication_lock:
            saved_owner_publications[id(envelope)] = envelope

    def saved_turn_start_action(
        context: RequestContext,
        payload: Mapping[str, Any],
        parent: CapturedInvocationScopeV4 | None,
    ) -> SavedTurnStartAction | None:
        if (
            turn_start_edge is None
            or ordinary_turn_edge is None
            or parent is None
            or parent.parent is not None
            or not matches_captured_edge(parent.envelope, ordinary_turn_edge)
            or parent.envelope.context.profile_id != context.profile_id
            or context.caller_principal.value != turn_start_edge.caller.principal_id
            or set(payload) - {"_session_id"} != {"operation", "profile_id", "request"}
            or payload["profile_id"] != context.profile_id
        ):
            return None
        action = payload["operation"]
        if action not in ("begin_saved", "claim_saved"):
            return None
        source = parent.envelope.payload.get("request")
        accepted = payload["request"]
        if not isinstance(source, Mapping) or not isinstance(accepted, Mapping):
            return None
        turn_id = accepted.get("turn_id")
        conversation_id = accepted.get("conversation_id")
        revision = accepted.get("conversation_revision")
        if (
            not is_native_saved_turn_start_identifier(turn_id)
            or not is_native_saved_turn_start_identifier(conversation_id)
            or type(revision) is not int
            or not 1 <= revision <= MAX_SAFE_INTEGER
            or turn_id != source.get("turn_id")
            or conversation_id != source.get("conversation_id")
            or revision != source.get("conversation_revision")
        ):
            return None
        return "begin" if action == "begin_saved" else "claim"

    def is_native_saved_turn_start_route(
        context: RequestContext, payload: Mapping[str, Any],
    ) -> bool:
        return native_provider_runtime is not None and saved_turn_start_action(
            context, payload, parent_invocation_scopes.lookup(context.caller_session_id),
        ) is not None

    def wrap_native_saved_turn_start(
        contribution: HostProviderContributionV4,
        edge: _CapturedPlanEdge,
        writer: Callable[[NativeSavedTurnStartRequest], Mapping[str, Any]],
    ) -> HostProviderContributionV4:
        """Borrow the existing ordinary lifecycle lease for one owner transaction."""
        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native saved Turn start context is unavailable")
            envelope = invocation.envelope
            parent = invocation.parent_invocation
            runtime = native_provider_runtime
            action = saved_turn_start_action(envelope.context, payload, parent)
            if action is None or parent is None or runtime is None:
                return contribution.invoke(operation_id, payload, invocation)
            if (
                operation_id != envelope.operation_id
                or payload is not envelope.payload
                or not matches_captured_edge(envelope, edge)
                or envelope.resource_reservation_id is None
                or envelope.resource_reservation_id == parent.envelope.resource_reservation_id
                or envelope.lease.token == parent.envelope.lease.token
                or envelope.deadline_monotonic > parent.envelope.deadline_monotonic
            ):
                raise AuthorityDenied("native saved Turn start lineage changed")
            reservation_id = envelope.resource_reservation_id

            def guard() -> None:
                invocation.assert_current()
                retained = parent_invocation_scopes.lookup(envelope.context.caller_session_id)
                if (
                    native_provider_runtime is not runtime
                    or retained is None
                    or retained.envelope is not parent.envelope
                    or parent_invocation_scopes.lookup(parent.envelope.context.caller_session_id)
                    is not None
                ):
                    raise AuthorityDenied("native saved Turn start capture changed")
                runtime[1]()
                production_admission.native_resources(reservation_id)

            def legacy() -> Mapping[str, Any]:
                guard()
                return contribution.invoke(operation_id, payload, invocation)

            def mark_publication() -> None:
                guard()
                record_saved_owner_publication(envelope)

            if saved_owner_publication_possible(envelope):
                raise HostPublicationUnconfirmed("saved Turn start is unconfirmed")
            guard()
            resources = production_admission.native_resources(reservation_id)
            if resources.memory_bytes < SAVED_TURN_START_MINIMUM_MEMORY:
                return legacy()

            from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict

            source = validate_saved_conversation_input(parent.public_payload())
            accepted = validate_saved_conversation_input({"request": payload["request"]})
            request = accepted["request"]
            # The owner may resolve context and references. Every other field
            # remains the authenticated parent's input.
            projection_fields = {"task_context", "resolved_chat_references"}
            source_user = {
                key: value for key, value in source["request"].items()
                if key not in projection_fields
            }
            accepted_user = {
                key: value for key, value in request.items() if key not in projection_fields
            }
            if (
                "resolved_chat_references" in source["request"]
                or canonical_digest(source_user) != canonical_digest(accepted_user)
            ):
                raise AuthorityDenied("native saved Turn user input changed")
            try:
                result = writer(NativeSavedTurnStartRequest(
                    context=envelope.context,
                    runtime_instance=runtime[0],
                    deadline_monotonic=envelope.deadline_monotonic,
                    assert_current=guard,
                    action=action,
                    turn_id=request["turn_id"],
                    conversation_id=request["conversation_id"],
                    conversation_revision=request["conversation_revision"],
                    input_digest=canonical_digest(accepted),
                    source_digest=canonical_digest(source),
                    mark_publication_possible=mark_publication,
                ))
                guard()
                if (
                    set(result) != {"claimed", "turn"}
                    or type(result["claimed"]) is not bool
                    or not isinstance(result["turn"], Mapping)
                    or result["claimed"] != (
                        action == "claim" and saved_owner_publication_possible(envelope)
                    )
                ):
                    raise ValueError("native saved Turn start result is invalid")
                return result["turn"] if action == "begin" else result
            except Exception as error:
                if saved_owner_publication_possible(envelope):
                    raise HostPublicationUnconfirmed("saved Turn start is unconfirmed") from None
                guard()
                if isinstance(error, (
                    NativeProviderPrepareUnavailable, NativeProviderRegistryRequiresLegacyRead,
                )):
                    return legacy()
                if isinstance(error, NativeSavedTurnStartFailure):
                    if error.code in {"conflict", "not-found"}:
                        raise TurnConflict("saved turn no longer matches its owner") from None
                    if error.code == "invalid":
                        raise ValueError("native saved Turn start input is invalid") from None
                    if error.code == "denied":
                        raise AuthorityDenied("native saved Turn start is denied") from None
                raise

        return replace(contribution, invoke=invoke)

    def requested_saved_turn_id(
        caller: SavedReceiptCaller, payload: Mapping[str, Any],
    ) -> str | None:
        fields = set(payload) - {"_session_id"}
        if caller == "saved-reconcile":
            candidate = payload.get("turn_id") if fields == {"turn_id"} else None
        else:
            request = payload.get("request") if fields == {"request"} else None
            candidate = request.get("turn_id") if isinstance(request, Mapping) else None
        return candidate if isinstance(candidate, str) and 0 < len(candidate) <= 256 else None

    def is_native_saved_turn_settlement_route(
        context: RequestContext,
        payload: Mapping[str, Any],
        *,
        caller: SavedReceiptCaller,
        edge: _CapturedPlanEdge,
    ) -> bool:
        return (
            native_provider_runtime is not None
            and context.caller_principal.value == edge.caller.principal_id
            and parent_invocation_scopes.lookup(context.caller_session_id) is None
            and requested_saved_turn_id(caller, payload) is not None
        )

    def capture_turn_settlement_port(
        caller: SavedReceiptCaller,
        edge: _CapturedPlanEdge,
        writer: Callable[[NativeSavedTurnSettlementRequest], Mapping[str, Any]],
    ) -> Callable[[HostProviderInvocationContextV4], SavedTurnSettlementPort | None]:
        def borrow(invocation: HostProviderInvocationContextV4) -> SavedTurnSettlementPort | None:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native Turn settlement context is unavailable")
            envelope = invocation.envelope
            if not matches_captured_edge(envelope, edge) or invocation.parent_invocation is not None:
                return None
            invocation.assert_current()
            root_turn_id = requested_saved_turn_id(caller, envelope.payload)
            runtime = native_provider_runtime
            if root_turn_id is None or runtime is None:
                return None
            reservation_id = envelope.resource_reservation_id

            def guard() -> None:
                invocation.assert_current()
                if (
                    native_provider_runtime is not runtime
                    or parent_invocation_scopes.lookup(envelope.context.caller_session_id) is not None
                ):
                    raise AuthorityDenied("native Turn settlement capture changed")
                runtime[1]()
                production_admission.native_resources(reservation_id)

            def mark_publication() -> None:
                guard()
                record_saved_owner_publication(envelope)

            def settle(
                turn_id: str, input_digest: str,
                result_reference: Mapping[str, Any], expected_revision: int | None,
            ) -> dict[str, Any] | None:
                if (
                    turn_id != root_turn_id
                    or (caller == "saved-reconcile" and expected_revision is None)
                ):
                    raise AuthorityDenied("native Turn settlement identity changed")
                if saved_owner_publication_possible(envelope):
                    raise HostSavedTurnSettlementUnconfirmed("saved Turn write is unconfirmed")
                guard()
                resources = production_admission.native_resources(reservation_id)
                if (
                    resources.memory_bytes < SAVED_TURN_SETTLEMENT_MINIMUM_MEMORY
                    or (type(expected_revision) is int and expected_revision >= 1 << 64)
                ):
                    return None
                try:
                    result = writer(NativeSavedTurnSettlementRequest(
                        context=envelope.context,
                        runtime_instance=runtime[0],
                        deadline_monotonic=envelope.deadline_monotonic,
                        assert_current=guard,
                        caller=caller,
                        turn_id=turn_id,
                        input_digest=input_digest,
                        expected_revision=expected_revision,
                        result_reference=result_reference,
                        mark_publication_possible=mark_publication,
                    ))
                    guard()
                    return dict(result)
                except Exception as error:
                    if (
                        saved_owner_publication_possible(envelope)
                        or isinstance(error, NativeSavedTurnSettlementPublicationUnconfirmed)
                    ):
                        raise HostSavedTurnSettlementUnconfirmed(
                            "saved Turn write is unconfirmed",
                        ) from None
                    guard()
                    if isinstance(error, (
                        NativeProviderPrepareUnavailable, NativeProviderRegistryRequiresLegacyRead,
                    )):
                        return None
                    if isinstance(error, NativeSavedTurnSettlementFailure):
                        if error.code == "conflict":
                            raise HostSavedTurnSettlementConflict(
                                "saved receipt cannot settle current turn",
                            ) from None
                        if error.code == "not-found":
                            raise KeyError("turn is unknown") from None
                        if error.code == "invalid":
                            raise ValueError("saved Turn settlement is invalid") from None
                        if error.code == "denied":
                            raise PermissionError("saved Turn settlement is denied") from None
                    raise

            return settle

        return borrow

    def confirmation_lineage(
        context: RequestContext,
        payload: Mapping[str, Any],
        parent: CapturedInvocationScopeV4 | None,
    ) -> tuple[SavedReceiptCaller, _CapturedPlanEdge, RequestEnvelope] | None:
        route = confirmation_routes.get(context.caller_principal.value)
        if (
            route is None
            or parent is None
            or parent.parent is not None
            or not matches_captured_edge(parent.envelope, route.parent)
            or set(payload) - {"_session_id"} != {"profile_id", "operation", "turn_id"}
            or payload.get("operation") != "confirm"
            or payload.get("profile_id") != context.profile_id
        ):
            return None
        turn_id = requested_saved_turn_id(route.caller, parent.envelope.payload)
        if turn_id is None or payload["turn_id"] != turn_id:
            return None
        return route.caller, route.completion, parent.envelope

    def is_native_conversation_confirmation_route(
        context: RequestContext, payload: Mapping[str, Any],
    ) -> bool:
        return native_provider_runtime is not None and confirmation_lineage(
            context, payload, parent_invocation_scopes.lookup(context.caller_session_id),
        ) is not None

    def saved_turn_read_lineage(
        context: RequestContext,
        payload: Mapping[str, Any],
        parent: CapturedInvocationScopeV4 | None,
    ) -> tuple[SavedReceiptCaller, _CapturedPlanEdge] | None:
        turn_id = payload.get("turn_id")
        if (
            parent is None
            or set(payload) - {"_session_id"} != {"profile_id", "operation", "turn_id"}
            or payload.get("operation") != "get"
            or payload.get("profile_id") != context.profile_id
            or not is_native_saved_turn_read_identifier(turn_id)
        ):
            return None
        completion = parent.envelope
        route = confirmation_routes.get(completion.context.caller_principal.value)
        if (
            route is None
            or not matches_captured_edge(completion, route.completion)
            or context.caller_principal.value != route.resource.caller.principal_id
            or completion.context.profile_id != context.profile_id
            or completion.payload.get("turn_id") != turn_id
            or confirmation_lineage(
                completion.context, completion.payload, parent.parent,
            ) is None
        ):
            return None
        return route.caller, route.resource

    def is_native_saved_turn_read_route(
        context: RequestContext, payload: Mapping[str, Any],
    ) -> bool:
        return native_provider_runtime is not None and saved_turn_read_lineage(
            context, payload, parent_invocation_scopes.lookup(context.caller_session_id),
        ) is not None

    def wrap_native_saved_turn_read(
        contribution: HostProviderContributionV4,
        reader: Callable[[NativeSavedTurnReadRequest], Mapping[str, Any] | None],
    ) -> HostProviderContributionV4:
        """Read the indexed Turn under the signed completion's own Resource lease."""

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native saved Turn read context is unavailable")
            envelope = invocation.envelope
            context = envelope.context
            parent = invocation.parent_invocation
            lineage = saved_turn_read_lineage(context, payload, parent)
            runtime = native_provider_runtime
            if lineage is None or runtime is None:
                return contribution.invoke(operation_id, payload, invocation)
            caller, edge = lineage
            assert parent is not None and parent.parent is not None
            grandparent = parent.parent
            ancestors = (parent.envelope, grandparent.envelope)
            if (
                operation_id != envelope.operation_id
                or payload is not envelope.payload
                or not matches_captured_edge(envelope, edge)
                or len({item.lease.token for item in (envelope, *ancestors)}) != 3
                or envelope.resource_reservation_id is None
                or any(
                    envelope.resource_reservation_id == item.resource_reservation_id
                    for item in ancestors
                )
                or envelope.deadline_monotonic > ancestors[0].deadline_monotonic
                or ancestors[0].deadline_monotonic > ancestors[1].deadline_monotonic
            ):
                raise AuthorityDenied("native saved Turn read lineage changed")

            def guard() -> None:
                invocation.assert_current()
                retained = parent_invocation_scopes.lookup(context.caller_session_id)
                retained_grandparent = parent_invocation_scopes.lookup(
                    parent.envelope.context.caller_session_id,
                )
                if (
                    retained is None or retained.envelope is not parent.envelope
                    or retained.parent is None
                    or retained.parent.envelope is not grandparent.envelope
                    or retained_grandparent is None
                    or retained_grandparent.envelope is not grandparent.envelope
                    or native_provider_runtime is not runtime
                ):
                    raise AuthorityDenied("native saved Turn read capture changed")
                runtime[1]()
                production_admission.native_resources(envelope.resource_reservation_id)

            def legacy_read() -> Mapping[str, Any]:
                guard()
                result = contribution.invoke(operation_id, payload, invocation)
                guard()
                return result

            guard()
            resources = production_admission.native_resources(
                envelope.resource_reservation_id,
            )
            if resources.memory_bytes < SAVED_TURN_READ_MINIMUM_MEMORY:
                return legacy_read()
            try:
                result = reader(NativeSavedTurnReadRequest(
                    context=context,
                    runtime_instance=runtime[0],
                    deadline_monotonic=envelope.deadline_monotonic,
                    assert_current=guard,
                    caller=caller,
                    turn_id=payload["turn_id"],
                ))
            except (
                NativeProviderPrepareUnavailable,
                NativeProviderRegistryRequiresLegacyRead,
            ):
                return legacy_read()
            except NativeSavedTurnReadFailure as error:
                guard()
                if error.code == "invalid":
                    raise ValueError("persisted turn record is invalid") from None
                if error.code == "denied":
                    raise PermissionError("turn read operation is not permitted") from None
                raise
            guard()
            if result is None:
                raise KeyError("turn is unavailable")
            return result

        return replace(contribution, invoke=invoke)

    def capture_conversation_confirmation_port(
        writer: Callable[[NativeConversationConfirmationRequest], Mapping[str, Any]],
    ) -> Callable[
        [HostProviderInvocationContextV4], SavedConversationConfirmationPort | None
    ]:
        terminal_fields = (
            "id", "profile_id", "conversation_id", "request_id", "status", "revision",
            "conversation_revision", "input_digest", "result_reference",
        )

        def borrow(
            invocation: HostProviderInvocationContextV4,
        ) -> SavedConversationConfirmationPort | None:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native Conversation confirmation context is unavailable")
            envelope = invocation.envelope
            context = envelope.context
            lineage = confirmation_lineage(context, envelope.payload, invocation.parent_invocation)
            runtime = native_provider_runtime
            if lineage is None or runtime is None:
                return None
            caller, edge, parent = lineage
            if (
                not matches_captured_edge(envelope, edge)
                or envelope.lease.token == parent.lease.token
                or envelope.deadline_monotonic > parent.deadline_monotonic
                or envelope.resource_reservation_id == parent.resource_reservation_id
            ):
                raise AuthorityDenied("native Conversation confirmation lineage changed")

            def guard() -> None:
                invocation.assert_current()
                retained = parent_invocation_scopes.lookup(context.caller_session_id)
                if (
                    retained is None or retained.envelope is not parent
                    or parent_invocation_scopes.lookup(parent.context.caller_session_id) is not None
                    or native_provider_runtime is not runtime
                ):
                    raise AuthorityDenied("native Conversation confirmation capture changed")
                runtime[1]()
                production_admission.native_resources(envelope.resource_reservation_id)

            def mark_publication() -> None:
                guard()
                record_saved_owner_publication(envelope)

            def confirm(terminal: Mapping[str, Any]) -> Mapping[str, Any] | None:
                if terminal.get("id") != envelope.payload["turn_id"]:
                    raise AuthorityDenied("native Conversation confirmation Turn changed")
                if saved_owner_publication_possible(envelope):
                    raise HostPublicationUnconfirmed("Conversation confirmation is unconfirmed")
                guard()
                resources = production_admission.native_resources(envelope.resource_reservation_id)
                if (
                    resources.memory_bytes < CONVERSATION_CONFIRMATION_MINIMUM_MEMORY
                    or terminal.get("profile_id") != context.profile_id
                    or terminal.get("status") != "completed"
                    or any(key not in terminal for key in terminal_fields)
                    or any(type(terminal[key]) is not int or not 0 <= terminal[key] < 1 << 63
                           for key in ("revision", "conversation_revision"))
                ):
                    return None
                # Use the credential-free Turn resource's exact fields required
                # by the owner. The completion call supplies only the Turn ID.
                proof = {key: terminal[key] for key in terminal_fields}
                try:
                    result = writer(NativeConversationConfirmationRequest(
                        context=context,
                        runtime_instance=runtime[0],
                        deadline_monotonic=envelope.deadline_monotonic,
                        assert_current=guard,
                        caller=caller,
                        terminal=proof,
                        mark_publication_possible=mark_publication,
                    ))
                    guard()
                    return result
                except Exception as error:
                    if (
                        saved_owner_publication_possible(envelope)
                        or isinstance(error, NativeConversationConfirmationPublicationUnconfirmed)
                    ):
                        raise HostPublicationUnconfirmed(
                            "Conversation confirmation is unconfirmed",
                        ) from None
                    guard()
                    if isinstance(error, (
                        NativeProviderPrepareUnavailable, NativeProviderRegistryRequiresLegacyRead,
                    )):
                        return None
                    if isinstance(error, NativeConversationConfirmationFailure):
                        if error.code == "invalid":
                            raise ValueError("Conversation confirmation input is invalid") from None
                        if error.code == "denied":
                            raise PermissionError("Conversation confirmation is denied") from None
                    raise

            return confirm

        return borrow

    def saved_receipt_lineage(
        context: RequestContext,
        payload: Mapping[str, Any],
        parent: CapturedInvocationScopeV4 | None,
    ) -> tuple[SavedReceiptCaller, _CapturedPlanEdge, RequestEnvelope] | None:
        route = saved_receipt_routes.get(context.caller_principal.value)
        turn_id = payload.get("turn_id")
        if (
            route is None
            or parent is None
            or parent.parent is not None
            or not matches_captured_edge(parent.envelope, route[2])
            or payload.get("operation") != "saved_receipt"
            or payload.get("profile_id") != context.profile_id
            or set(payload) - {"_session_id"} != {"profile_id", "operation", "turn_id"}
            or not isinstance(turn_id, str)
            or len(turn_id) > SAVED_RECEIPT_MAX_ID_CHARS
        ):
            return None
        try:
            turn_id.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return None
        return route[0], route[1], parent.envelope

    def is_native_saved_receipt_route(
        context: RequestContext, payload: Mapping[str, Any],
    ) -> bool:
        return native_provider_runtime is not None and saved_receipt_lineage(
            context, payload,
            parent_invocation_scopes.lookup(context.caller_session_id),
        ) is not None

    def wrap_native_saved_receipt(
        contribution: HostProviderContributionV4,
        reader: Callable[[NativeSavedReceiptRequest], Mapping[str, Any]],
    ) -> HostProviderContributionV4:
        """Borrow the exact saved receipt edge under its live signed Turn parent."""

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native saved receipt context is unavailable")
            envelope = invocation.envelope
            context = envelope.context
            lineage = saved_receipt_lineage(
                context, payload, invocation.parent_invocation,
            )
            pinned_runtime = native_provider_runtime
            if lineage is None or pinned_runtime is None:
                return contribution.invoke(operation_id, payload, invocation)
            caller, edge, parent = lineage
            if (
                operation_id != envelope.operation_id
                or payload is not envelope.payload
                or not matches_captured_edge(envelope, edge)
                or envelope.lease.token == parent.lease.token
                or envelope.deadline_monotonic > parent.deadline_monotonic
                or envelope.resource_reservation_id == parent.resource_reservation_id
            ):
                raise AuthorityDenied("native saved receipt lineage changed")

            def guard() -> None:
                invocation.assert_current()
                retained = parent_invocation_scopes.lookup(context.caller_session_id)
                if (
                    retained is None or retained.envelope is not parent
                    or parent_invocation_scopes.lookup(parent.context.caller_session_id)
                    is not None
                    or native_provider_runtime is not pinned_runtime
                ):
                    raise AuthorityDenied("native saved receipt capture changed")
                pinned_runtime[1]()
                production_admission.native_resources(envelope.resource_reservation_id)

            def legacy_read() -> Mapping[str, Any]:
                guard()
                result = contribution.invoke(operation_id, payload, invocation)
                guard()
                return result

            guard()
            resources = production_admission.native_resources(
                envelope.resource_reservation_id,
            )
            if resources.memory_bytes < SAVED_RECEIPT_MINIMUM_MEMORY:
                return legacy_read()
            try:
                result = reader(NativeSavedReceiptRequest(
                    context=context,
                    runtime_instance=pinned_runtime[0],
                    deadline_monotonic=envelope.deadline_monotonic,
                    assert_current=guard,
                    caller=caller,
                    turn_id=payload["turn_id"],
                ))
            except (
                NativeProviderPrepareUnavailable,
                NativeProviderRegistryRequiresLegacyRead,
            ):
                return legacy_read()
            except NativeSavedReceiptFailure as error:
                guard()
                if error.code == "invalid":
                    raise ValueError(
                        "conversation or message identifier is invalid",
                    ) from None
                if error.code == "denied":
                    raise PermissionError(
                        "conversation read operation is denied",
                    ) from None
                raise
            guard()
            return result

        return replace(contribution, invoke=invoke)

    model_resolve_routes: dict[
        tuple[str, str], tuple[ModelResolveRole, tuple[_CapturedPlanEdge, ...]]
    ] = {}
    ai_routing_routes: dict[
        tuple[str, str], tuple[ModelResolveRole, tuple[_CapturedPlanEdge, ...]]
    ] = {}
    append_edges: tuple[_CapturedPlanEdge, _CapturedPlanEdge, _CapturedPlanEdge] | None = None
    append_function = "rumi_conversation_store_pack.conversation-store.message-manage"
    append_operation = (
        "tobkiri.action.message.manage.v1",
        "rumi_conversation_store_pack.message-manage",
    )
    if (
        native_model_resolve_reader is not None
        or native_ai_routing_evaluator is not None
        or native_append_writer is not None
    ):
        turn_edge, orchestration_edge = ordinary_turn_edge, ordinary_orchestration_edge
        gateway_pack = "rumi_ai_gateway_pack"

        if (
            native_append_writer is not None
            and turn_edge is not None
            and orchestration_edge is not None
        ):
            message_edge = select_original_chat_edge(
                {orchestration_edge.target.principal_id}, "rumi_conversation_store_pack",
                *append_operation, append_function, EffectClass.PRIVILEGED,
            )
            if message_edge is not None:
                append_edges = message_edge, orchestration_edge, turn_edge
        if (
            (native_model_resolve_reader is not None
             or native_ai_routing_evaluator is not None)
            and turn_edge is not None
            and orchestration_edge is not None
        ):
            gateway_roles: tuple[tuple[ModelResolveRole, str, str, str], ...] = (
                ("quote-generate", "tobkiri.resource.ai.route.quote.v1",
                 "route-quote", "generate"),
                ("quote-stream", "tobkiri.resource.ai.route.quote.v1",
                 "route-quote", "stream"),
                ("generate", "tobkiri.service.ai.generate.v1", "generate", "generate"),
                ("stream", "tobkiri.service.ai.stream.v1", "stream", "stream"),
            )
            for role, gateway_contract, gateway_kind, call_kind in gateway_roles:
                gateway_id = f"{gateway_pack}.ai-gateway.{gateway_kind}"
                gateway_edge = select_original_chat_edge(
                    {orchestration_edge.target.principal_id}, gateway_pack,
                    gateway_contract, gateway_id, gateway_id, EffectClass.PRIVILEGED,
                )
                if gateway_edge is None:
                    continue
                gateway_principal = gateway_edge.target.principal_id
                parents = (gateway_edge, orchestration_edge, turn_edge)
                if native_model_resolve_reader is not None:
                    model_operation = f"{NATIVE_MODEL_SETTINGS_OPERATION}.{call_kind}"
                    model_edge = select_original_chat_edge(
                        {gateway_principal}, NATIVE_MODEL_SETTINGS_PACK,
                        NATIVE_MODEL_SETTINGS_CONTRACT, model_operation,
                        NATIVE_MODEL_SETTINGS_FUNCTION, EffectClass.READ,
                    )
                    if model_edge is not None:
                        model_resolve_routes[gateway_principal, model_operation] = (
                            role, (model_edge, *parents),
                        )
                if native_ai_routing_evaluator is not None:
                    router_pack = "rumi_ai_routing_pack"
                    router_operation = f"{router_pack}.ai-route.{call_kind}"
                    router_edge = select_original_chat_edge(
                        {gateway_principal}, router_pack, "tobkiri.service.ai.route.v1",
                        router_operation, f"{router_pack}.ai-routing.default",
                        EffectClass.PRIVILEGED,
                    )
                    if router_edge is not None:
                        ai_routing_routes[gateway_principal, router_operation] = (
                            role, (router_edge, *parents),
                        )

    def saved_append_lineage(
        context: RequestContext,
        payload: Mapping[str, Any],
        parent: CapturedInvocationScopeV4 | None,
    ) -> tuple[RequestEnvelope, RequestEnvelope] | None:
        if append_edges is None or parent is None or parent.parent is None:
            return None
        message_edge, saved_edge, turn_edge = append_edges
        saved, turn = parent.envelope, parent.parent.envelope
        if (
            context.caller_principal.value != message_edge.caller.principal_id
            or not matches_captured_edge(saved, saved_edge)
            or not matches_captured_edge(turn, turn_edge)
            or parent.parent.parent is not None
            or set(payload) - {"_session_id"} != {
                "profile_id", "operation", "conversation_id",
                "expected_conversation_revision", "message", "saved_input",
            }
            or payload["operation"] != "append_saved"
            or payload["profile_id"] != context.profile_id
        ):
            return None
        initial = payload["saved_input"]
        request = initial.get("request") if isinstance(initial, Mapping) else None
        turn_id = requested_saved_turn_id("saved-turn", turn.payload)
        if (
            turn_id is None
            or not isinstance(request, Mapping)
            or request.get("turn_id") != turn_id
            or request.get("conversation_id") != payload["conversation_id"]
        ):
            return None
        return saved, turn

    def is_native_saved_append_route(
        context: RequestContext, payload: Mapping[str, Any],
    ) -> bool:
        return native_provider_runtime is not None and saved_append_lineage(
            context, payload, parent_invocation_scopes.lookup(context.caller_session_id),
        ) is not None

    def capture_saved_append_port(
        edge: _CapturedPlanEdge,
        writer: Callable[[NativeConversationAppendRequest], Mapping[str, Any]],
    ) -> Callable[[HostProviderInvocationContextV4], SavedMessageAppendPort | None]:
        def borrow(
            invocation: HostProviderInvocationContextV4,
        ) -> SavedMessageAppendPort | None:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native saved append context is unavailable")
            envelope = invocation.envelope
            payload = envelope.payload
            parent = invocation.parent_invocation
            ancestors = saved_append_lineage(envelope.context, payload, parent)
            runtime = native_provider_runtime
            if ancestors is None or runtime is None:
                return None
            saved, turn = ancestors
            if (
                not matches_captured_edge(envelope, edge)
                or parent is None
                or len({envelope.lease.token, saved.lease.token, turn.lease.token}) != 3
                or not envelope.deadline_monotonic <= saved.deadline_monotonic <= turn.deadline_monotonic
                or any(envelope.resource_reservation_id == item.resource_reservation_id
                       for item in ancestors)
            ):
                raise AuthorityDenied("native saved append lineage changed")

            def guard() -> None:
                invocation.assert_current()
                child = envelope
                for ancestor in ancestors:
                    retained = parent_invocation_scopes.lookup(child.context.caller_session_id)
                    if retained is None or retained.envelope is not ancestor:
                        raise AuthorityDenied("native saved append parent changed")
                    child = ancestor
                if (
                    native_provider_runtime is not runtime
                    or parent_invocation_scopes.lookup(turn.context.caller_session_id) is not None
                ):
                    raise AuthorityDenied("native saved append capture changed")
                runtime[1]()
                production_admission.native_resources(envelope.resource_reservation_id)

            def mark_publication() -> None:
                guard()
                record_saved_owner_publication(envelope)

            def append() -> Mapping[str, Any] | None:
                if saved_owner_publication_possible(envelope):
                    raise HostPublicationUnconfirmed("Conversation append is unconfirmed")
                guard()
                resources = production_admission.native_resources(envelope.resource_reservation_id)
                revision = payload["expected_conversation_revision"]
                if (
                    resources.memory_bytes < CONVERSATION_APPEND_MINIMUM_MEMORY
                    or type(revision) is not int or not 1 <= revision < 1 << 63
                ):
                    return None
                initial = payload["saved_input"]
                if canonical_digest(initial) != canonical_digest(parent.public_payload()):
                    raise AuthorityDenied("native saved append input changed")
                message = dict(payload["message"])
                # The original owner chooses a missing message timestamp before
                # taking the physical lock. The guest chooses lifecycle and
                # conversation write times inside that lock.
                if not message.get("created_at") and not message.get("timestamp"):
                    message["created_at"] = int(time.time() * 1000)
                request = {
                    "conversation_id": payload["conversation_id"],
                    "expected_conversation_revision": revision,
                    "message": message,
                    "saved_input": initial,
                }
                try:
                    result = writer(NativeConversationAppendRequest(
                        context=envelope.context,
                        runtime_instance=runtime[0],
                        deadline_monotonic=envelope.deadline_monotonic,
                        assert_current=guard,
                        request=request,
                        mark_publication_possible=mark_publication,
                    ))
                    guard()
                    return result
                except Exception as error:
                    if (
                        saved_owner_publication_possible(envelope)
                        or isinstance(error, NativeConversationAppendPublicationUnconfirmed)
                    ):
                        raise HostPublicationUnconfirmed(
                            "Conversation append is unconfirmed",
                        ) from None
                    guard()
                    if isinstance(error, (
                        NativeProviderPrepareUnavailable, NativeProviderRegistryRequiresLegacyRead,
                    )):
                        return None
                    if isinstance(error, NativeConversationAppendFailure):
                        if error.code == "invalid":
                            raise ValueError("Conversation append input is invalid") from None
                        if error.code == "denied":
                            raise PermissionError("Conversation append is denied") from None
                        if error.code == "not-found":
                            raise KeyError("Conversation append owner is unavailable") from None
                    raise

            return append

        return borrow

    def match_saved_gateway_ancestors(
        parent: CapturedInvocationScopeV4 | None,
        parent_edges: list[_CapturedPlanEdge],
    ) -> tuple[RequestEnvelope, ...] | None:
        ancestors: list[RequestEnvelope] = []
        for edge in parent_edges:
            if parent is None or not matches_captured_edge(parent.envelope, edge):
                return None
            ancestors.append(parent.envelope)
            parent = parent.parent
        return tuple(ancestors) if parent is None else None

    def model_resolve_lineage(
        context: RequestContext,
        payload: Mapping[str, Any],
        parent: CapturedInvocationScopeV4 | None,
        operation_id: str,
    ) -> tuple[ModelResolveRole, _CapturedPlanEdge, tuple[RequestEnvelope, ...]] | None:
        route = model_resolve_routes.get((context.caller_principal.value, operation_id))
        if (
            route is None
            or set(payload) != {"identifier"}
            or not is_canonical_model_resolve_identifier(payload["identifier"])
        ):
            return None
        role, (model_edge, *parent_edges) = route
        ancestors = match_saved_gateway_ancestors(parent, parent_edges)
        return (role, model_edge, ancestors) if ancestors is not None else None

    def is_native_model_resolve_route(
        context: RequestContext, payload: Mapping[str, Any], *, operation_id: str,
    ) -> bool:
        return native_provider_runtime is not None and model_resolve_lineage(
            context, payload,
            parent_invocation_scopes.lookup(context.caller_session_id),
            operation_id,
        ) is not None

    def wrap_native_model_resolve(
        contribution: HostProviderContributionV4,
        reader: Callable[[NativeModelResolveRequest], Mapping[str, Any]],
    ) -> HostProviderContributionV4:
        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native Model Resolve context is unavailable")
            envelope = invocation.envelope
            lineage = model_resolve_lineage(
                envelope.context, payload, invocation.parent_invocation, operation_id,
            )
            pinned_runtime = native_provider_runtime
            if lineage is None or pinned_runtime is None:
                return contribution.invoke(operation_id, payload, invocation)
            role, model_edge, ancestors = lineage
            if (
                payload is not envelope.payload
                or not matches_captured_edge(envelope, model_edge)
                or any(
                    envelope.lease.token == ancestor.lease.token
                    for ancestor in ancestors
                )
                or envelope.deadline_monotonic > ancestors[0].deadline_monotonic
                or any(
                    envelope.resource_reservation_id == ancestor.resource_reservation_id
                    for ancestor in ancestors
                )
            ):
                raise AuthorityDenied("native Model Resolve lineage changed")

            def guard() -> None:
                invocation.assert_current()
                child = envelope
                for ancestor in ancestors:
                    retained = parent_invocation_scopes.lookup(
                        child.context.caller_session_id,
                    )
                    if retained is None or retained.envelope is not ancestor:
                        raise AuthorityDenied("native Model Resolve parent changed")
                    child = ancestor
                if (
                    native_provider_runtime is not pinned_runtime
                    or parent_invocation_scopes.lookup(child.context.caller_session_id)
                    is not None
                ):
                    raise AuthorityDenied("native Model Resolve capture changed")
                pinned_runtime[1]()
                production_admission.native_resources(envelope.resource_reservation_id)

            def legacy_read() -> Mapping[str, Any]:
                guard()
                result = contribution.invoke(operation_id, payload, invocation)
                guard()
                return result

            guard()
            resources = production_admission.native_resources(
                envelope.resource_reservation_id,
            )
            if resources.memory_bytes < MODEL_RESOLVE_MINIMUM_MEMORY:
                return legacy_read()
            try:
                result = reader(NativeModelResolveRequest(
                    context=envelope.context,
                    runtime_instance=pinned_runtime[0],
                    deadline_monotonic=envelope.deadline_monotonic,
                    assert_current=guard,
                    role=role,
                    identifier=payload["identifier"],
                ))
            except (
                NativeProviderPrepareUnavailable,
                NativeProviderRegistryRequiresLegacyRead,
            ):
                return legacy_read()
            except NativeModelResolveFailure as error:
                guard()
                if error.code == "not-found":
                    raise KeyError("model profile or alias is unknown") from None
                if error.code == "invalid":
                    raise ValueError("model registry identifier is invalid") from None
                if error.code == "denied":
                    raise PermissionError("model registry resolve is denied") from None
                raise
            guard()
            return result

        return replace(contribution, invoke=invoke)

    def ai_routing_lineage(
        context: RequestContext,
        payload: Mapping[str, Any],
        parent: CapturedInvocationScopeV4 | None,
        operation_id: str,
    ) -> tuple[ModelResolveRole, _CapturedPlanEdge, tuple[RequestEnvelope, ...]] | None:
        route = ai_routing_routes.get((context.caller_principal.value, operation_id))
        if route is None or set(payload) != {
            "models", "execution_providers", "health", "requirements", "decision_time",
        }:
            return None
        role, (router_edge, *parent_edges) = route
        ancestors = match_saved_gateway_ancestors(parent, parent_edges)
        if ancestors is None:
            return None
        if role in ("quote-generate", "quote-stream"):
            mode = ancestors[0].payload.get("delivery_mode", "buffered")
            if mode != ("incremental" if role == "quote-stream" else "buffered"):
                return None
        return role, router_edge, ancestors

    def is_native_ai_routing_route(
        context: RequestContext, payload: Mapping[str, Any], *, operation_id: str,
    ) -> bool:
        return native_provider_runtime is not None and ai_routing_lineage(
            context, payload,
            parent_invocation_scopes.lookup(context.caller_session_id),
            operation_id,
        ) is not None

    def wrap_native_ai_routing(
        contribution: HostProviderContributionV4,
        evaluator: Callable[[NativeAiRoutingRequest], Mapping[str, Any]],
    ) -> HostProviderContributionV4:
        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native AI routing context is unavailable")
            envelope = invocation.envelope
            lineage = ai_routing_lineage(
                envelope.context, payload, invocation.parent_invocation, operation_id,
            )
            runtime = native_provider_runtime
            if lineage is None or runtime is None:
                return contribution.invoke(operation_id, payload, invocation)
            role, edge, ancestors = lineage
            reservation = envelope.resource_reservation_id
            chain = (envelope, *ancestors)
            if (
                operation_id != envelope.operation_id
                or payload is not envelope.payload
                or not matches_captured_edge(envelope, edge)
                or len({item.lease.token for item in chain}) != len(chain)
                or any(
                    child.deadline_monotonic > parent.deadline_monotonic
                    for child, parent in zip(chain, ancestors)
                )
                or any(reservation == item.resource_reservation_id for item in ancestors)
            ):
                raise AuthorityDenied("native AI routing lineage changed")

            def guard() -> None:
                invocation.assert_current()
                for child, parent in zip(chain, ancestors):
                    retained = parent_invocation_scopes.lookup(
                        child.context.caller_session_id,
                    )
                    if retained is None or retained.envelope is not parent:
                        raise AuthorityDenied("native AI routing parent changed")
                if (
                    native_provider_runtime is not runtime
                    or parent_invocation_scopes.lookup(
                        ancestors[-1].context.caller_session_id,
                    ) is not None
                ):
                    raise AuthorityDenied("native AI routing capture changed")
                runtime[1]()
                production_admission.native_resources(reservation)

            def legacy_read() -> Mapping[str, Any]:
                guard()
                result = contribution.invoke(operation_id, payload, invocation)
                guard()
                return result

            guard()
            if production_admission.native_resources(reservation).memory_bytes < (
                AI_ROUTING_MINIMUM_MEMORY
            ):
                return legacy_read()
            try:
                result = evaluator(NativeAiRoutingRequest(
                    context=envelope.context,
                    runtime_instance=runtime[0],
                    deadline_monotonic=envelope.deadline_monotonic,
                    assert_current=guard,
                    role=role,
                    input=payload,
                ))
            except (
                NativeProviderPrepareUnavailable,
                NativeProviderRegistryRequiresLegacyRead,
            ):
                return legacy_read()
            except NativeAiRoutingFailure as error:
                guard()
                if error.code == "invalid":
                    raise ValueError("AI routing input is invalid") from None
                if error.code == "denied":
                    raise PermissionError("AI routing is denied") from None
                raise
            guard()
            return result

        return replace(contribution, invoke=invoke)

    def is_native_model_save_route(
        context: RequestContext, payload: Mapping[str, Any],
    ) -> bool:
        scope = current_model_save_http_scope()
        return (
            native_model_save_edge is not None
            and scope is not None
            and bool(dispatch_holder)
            and scope.session is dispatch_holder[0]
            and payload == scope.normalized_payload
            and context.caller_principal.value == native_model_save_edge.caller.principal_id
            and context.caller_session_id == authority_session_id(
                scope.panel_session_id, context.caller_principal.value,
            )
            and parent_invocation_scopes.lookup(context.caller_session_id) is None
        )

    def model_save_provider(
        invocation: _HostInvocation, request: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot = invocation.contract_client(
            allowed_contract_ids=frozenset({_MODEL_PROVIDER_CONTRACT}),
            consumer_pack_id=NATIVE_MODEL_SETTINGS_PACK,
            include_credentials=False,
        ).invoke(_MODEL_PROVIDER_CONTRACT, _MODEL_PROVIDER_OPERATION, {})
        expected = request["provider_registry_revision"]
        if not isinstance(snapshot, Mapping) or snapshot.get("revision") != expected:
            raise PermissionError("model route provider connection changed")
        rows = snapshot.get("providers")
        if not isinstance(rows, list):
            raise PermissionError("model route provider registry is invalid")
        selected = request["provider_instance_id"]
        matches = []
        for row in rows:
            if (
                isinstance(row, Mapping)
                and row.get("provider_instance_id") == selected
                and row.get("enabled") is True
            ):
                matches.append({"provider_instance_id": selected, "enabled": True})
                if len(matches) == 2:
                    break
        return {"revision": expected, "providers": matches}

    def wrap_native_model_save(
        contribution: HostProviderContributionV4,
        edge: _CapturedPlanEdge,
        writer: Callable[[NativeModelSaveRequest], Mapping[str, Any]],
    ) -> HostProviderContributionV4:
        """Borrow the signed direct save after its ordinary Broker admission."""

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native Model Save context is unavailable")
            scope = current_model_save_http_scope()
            if scope is None:
                return contribution.invoke(operation_id, payload, invocation)
            envelope = invocation.envelope
            if (
                not is_native_model_save_route(envelope.context, payload)
                or invocation.parent_invocation is not None
                or operation_id != _MODEL_SAVE_OPERATION
                or envelope.contract_id != _MODEL_SAVE_CONTRACT
                or payload is not envelope.payload
                or envelope.target_principal != edge.resolved_binding.principal_ref
            ):
                raise AuthorityDenied("native Model Save lineage changed")
            reservation_id = envelope.resource_reservation_id

            def guard() -> None:
                invocation.assert_current()
                scope.assert_current()
                production_admission.native_resources(reservation_id)

            def legacy_save() -> Mapping[str, Any]:
                if scope.publication_possible(envelope):
                    raise HostPublicationUnconfirmed(
                        "native Model Save publication is unconfirmed",
                    )
                guard()
                result = contribution.invoke(operation_id, payload, invocation)
                guard()
                return result

            guard()
            resources = production_admission.native_resources(reservation_id)
            revisions = (
                scope.request["expected_revision"],
                scope.request["provider_registry_revision"],
            )
            if resources.memory_bytes < NATIVE_MODEL_SETTINGS_MEMORY or any(
                type(revision) is int and revision > MAX_SAFE_INTEGER
                for revision in revisions
            ):
                return legacy_save()
            provider = model_save_provider(invocation, scope.request)
            guard()
            try:
                return writer(NativeModelSaveRequest(
                    context=envelope.context,
                    runtime_instance=scope.runtime_instance,
                    deadline_monotonic=envelope.deadline_monotonic,
                    assert_current=guard,
                    request=scope.request,
                    provider=provider,
                    mark_publication_possible=lambda: scope.mark_publication_possible(envelope),
                ))
            except (
                NativeProviderPrepareUnavailable,
                NativeProviderRegistryRequiresLegacyRead,
            ):
                return legacy_save()
            except NativeModelSavePublicationUnconfirmed as error:
                raise HostPublicationUnconfirmed(
                    "native Model Save publication is unconfirmed",
                ) from error
            except NativeModelSaveFailure as error:
                if error.code == "invalid":
                    raise ValueError("model Registry rejected the save") from None
                if error.code == "denied":
                    raise PermissionError("model Registry rejected the save") from None
                raise RuntimeError("model Registry rejected the save") from None

        return replace(contribution, invoke=invoke)

    def is_native_model_settings_route(
        context: RequestContext, payload: Mapping[str, Any],
    ) -> bool:
        http_scope = current_model_settings_http_scope()
        return (
            native_model_settings_edge is not None
            and http_scope is not None
            and bool(dispatch_holder)
            and http_scope.session is dispatch_holder[0]
            and payload == {"profile_id": context.profile_id, "operation": "list"}
            and context.caller_principal.value
            == native_model_settings_edge.caller.principal_id
            and context.caller_session_id == authority_session_id(
                http_scope.panel_session_id, context.caller_principal.value,
            )
            and parent_invocation_scopes.lookup(context.caller_session_id) is None
        )

    def wrap_native_model_settings(
        contribution: HostProviderContributionV4,
        edge: _CapturedPlanEdge,
        reader: Callable[[NativeModelSettingsRequest], Mapping[str, Any]],
    ) -> HostProviderContributionV4:
        """Borrow only the direct Shell model-selector read with live admission."""

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native Model Settings context is unavailable")
            envelope = invocation.envelope
            http_scope = current_model_settings_http_scope()
            if http_scope is None:
                return contribution.invoke(operation_id, payload, invocation)
            if (
                not is_native_model_settings_route(envelope.context, payload)
                or invocation.parent_invocation is not None
                or operation_id != NATIVE_MODEL_SETTINGS_OPERATION
                or envelope.contract_id != NATIVE_MODEL_SETTINGS_CONTRACT
                or payload is not envelope.payload
                or envelope.target_principal != edge.resolved_binding.principal_ref
                or http_scope.runtime_instance is None
            ):
                raise AuthorityDenied("native Model Settings lineage changed")

            reservation_id = envelope.resource_reservation_id

            def guard() -> None:
                invocation.assert_current()
                http_scope.assert_current()
                production_admission.native_resources(reservation_id)

            def legacy_read() -> Mapping[str, Any]:
                guard()
                result = contribution.invoke(operation_id, payload, invocation)
                guard()
                return result

            guard()
            resources = production_admission.native_resources(reservation_id)
            if resources.memory_bytes < NATIVE_MODEL_SETTINGS_MEMORY:
                return legacy_read()

            try:
                projected = reader(NativeModelSettingsRequest(
                    context=envelope.context,
                    runtime_instance=http_scope.runtime_instance,
                    deadline_monotonic=envelope.deadline_monotonic,
                    assert_current=guard,
                ))
            except (
                NativeProviderPrepareUnavailable,
                NativeProviderRegistryRequiresLegacyRead,
            ):
                return legacy_read()
            result = model_settings_presenter_input(projected)
            guard()
            return result

        return replace(contribution, invoke=invoke)

    def is_native_prepare_route(
        context: RequestContext, payload: Mapping[str, Any],
    ) -> bool:
        if native_prepare_edges is None or not is_keyless_local(payload):
            return False
        if launcher_provider_prepare and current_provider_prepare_http_scope() is None:
            return False
        shell_edge, _ = native_prepare_edges
        parent = parent_invocation_scopes.lookup(context.caller_session_id)
        if parent is None:
            return False
        envelope = parent.envelope
        return (
            envelope.context.caller_principal.value == shell_edge.caller.principal_id
            and envelope.target_principal == shell_edge.resolved_binding.principal_ref
            and context.caller_principal == envelope.target_principal
            and envelope.contract_id == INTERACTIVE_EFFECT_COORDINATOR_CONTRACT_ID
            and envelope.operation_id == INTERACTIVE_EFFECT_COORDINATOR_OPERATION_ID
            and envelope.payload.get("phase") == "prepare"
            and envelope.payload.get("effect_kind") == "provider_configure"
        )

    def wrap_native_provider_prepare(
        contribution: HostProviderContributionV4,
        capture: HostProviderCaptureContextV4,
        planner: NativeProviderPreparePlanner,
        prepare_edge: _CapturedPlanEdge,
    ) -> HostProviderContributionV4:
        """Borrow one verified Shell -> coordinator -> Provider preparation."""

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native Provider preparation context is unavailable")
            envelope = invocation.envelope
            if not is_native_prepare_route(envelope.context, payload):
                return contribution.invoke(operation_id, payload, invocation)
            invocation.assert_current()
            parent = invocation.parent_invocation
            if (
                parent is None
                or parent is not parent_invocation_scopes.lookup(envelope.context.caller_session_id)
                or operation_id != NATIVE_PROVIDER_PREPARE_OPERATION
                or payload is not envelope.payload
                or envelope.target_principal != prepare_edge.resolved_binding.principal_ref
                or parent.envelope.payload.get("request") != payload
                or capture.user_data_root is None
                or payload.get("profile_id", capture.profile_id) != capture.profile_id
            ):
                raise AuthorityDenied("native Provider preparation lineage changed")

            from ecosystem.rumi_provider_registry_pack.runtime.configuration import (
                configuration_request,
            )

            normalized = configuration_request(payload)
            http_scope = (
                current_provider_prepare_http_scope() if launcher_provider_prepare else None
            )
            reservation_id = envelope.resource_reservation_id
            resources = production_admission.native_resources(reservation_id)

            def guard() -> None:
                invocation.assert_current()
                if http_scope is not None:
                    http_scope[1]()
                production_admission.native_resources(reservation_id)

            if resources.memory_bytes < planner.memory_bytes:
                guard()
                return contribution.invoke(operation_id, payload, invocation)

            borrow = NativeProviderPrepareBorrow(
                context=envelope.context,
                payload=payload,
                deadline_monotonic=envelope.deadline_monotonic,
                resources=resources,
                guard=guard,
                runtime_instance=http_scope[0] if http_scope is not None else None,
            )
            try:
                plan = invoke_native_provider_prepare(
                    planner, borrow, payload=payload, normalized=normalized,
                )
            except (
                NativeProviderPrepareUnavailable, NativeProviderRegistryRequiresLegacyRead,
            ):
                guard()
                return contribution.invoke(operation_id, payload, invocation)
            guard()
            budget = capture.generation_budget_port
            if budget is not None:
                plan = budget.prepare(plan)
                guard()
            return plan

        return replace(contribution, invoke=invoke)

    def is_native_execute_route(
        context: RequestContext, payload: Mapping[str, Any],
    ) -> bool:
        request, plan = payload.get("request"), payload.get("plan")
        return (
            native_execute_edge is not None
            and native_provider_runtime is not None
            and context.caller_principal.value
            == native_execute_edge.caller.principal_id
            and set(payload) == {"request", "plan"}
            and isinstance(request, Mapping)
            and is_keyless_local(request)
            and isinstance(plan, Mapping)
            and "generation_budget" not in plan
        )

    def wrap_native_provider_execution(
        contribution: HostProviderContributionV4,
        capture: HostProviderCaptureContextV4,
        planner: NativeProviderPreparePlanner,
        edge: _CapturedPlanEdge,
    ) -> HostProviderContributionV4:
        """Delegate publication of a detached approved effect to the live Launcher."""

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if type(invocation) is not _HostInvocation:
                raise AuthorityDenied("native Provider execution context unavailable")
            envelope = invocation.envelope
            context = envelope.context
            if not is_native_execute_route(context, payload):
                return contribution.invoke(operation_id, payload, invocation)
            pinned_runtime = native_provider_runtime
            if (
                pinned_runtime is None
                or operation_id != NATIVE_PROVIDER_EXECUTE_OPERATION
                or payload is not envelope.payload
                or envelope.target_principal != edge.resolved_binding.principal_ref
                or capture.user_data_root is None
                or capture.generation_budget_port is not None
            ):
                raise AuthorityDenied("native Provider execution lineage changed")
            reservation = envelope.resource_reservation_id
            owner = (
                invocation.presentation_owner_principal_id,
                invocation.presentation_owner_session_id,
            )

            def guard() -> None:
                invocation.assert_current()
                if native_provider_runtime is not pinned_runtime:
                    raise AuthorityDenied("native Provider runtime publication changed")
                pinned_runtime[1]()
                with caller_session_bindings_lock:
                    retained_owner = presentation_owner_bindings.get(
                        context.caller_session_id,
                    )
                    if retained_owner != owner:
                        raise AuthorityDenied("native Provider approval owner changed")
                production_admission.native_resources(reservation)

            guard()
            lease, _state, _epoch, _revoked = (
                authority_store.inspect_dispatch_authority(
                    envelope.lease.token.decode("ascii"),
                )
            )
            grant = authority_store.get_grant(lease.grant_id)
            if (
                grant is None
                or grant.lifetime is not GrantLifetime.ONE_SHOT
                or grant.approval_id is None
                or grant.scope.exact_request_digest != envelope.request_digest
                or lease.authorized_scope.exact_request_digest
                != envelope.request_digest
            ):
                raise AuthorityDenied("native Provider execution lacks an exact approval")
            resources = production_admission.native_resources(reservation)
            if resources.memory_bytes < planner.memory_bytes:
                guard()
                return contribution.invoke(operation_id, payload, invocation)

            from ecosystem.rumi_provider_registry_pack.runtime.configuration import (
                configuration_request,
            )

            request = payload["request"]
            normalized = configuration_request(request)
            borrow = NativeProviderPrepareBorrow(
                context=context, payload=request,
                deadline_monotonic=envelope.deadline_monotonic,
                resources=resources, guard=guard, runtime_instance=pinned_runtime[0],
                frozen_plan=payload["plan"],
            )
            try:
                return invoke_native_provider_commit(
                    planner, borrow, payload=request, normalized=normalized,
                )
            except (
                NativeProviderPrepareUnavailable, NativeProviderRegistryRequiresLegacyRead,
            ):
                guard()
                return contribution.invoke(operation_id, payload, invocation)

        return replace(contribution, invoke=invoke)

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

    from core_runtime.host_provider_hooks_v4 import group_host_provider_captures

    host_provider_data = HostProviderDataCaptureV4(lock, ecosystem_root)

    def capture_provider_pack_data(factory: Any) -> tuple[CapturedHostPackDataV4, ...]:
        """Read finite factory data under the captured owner and fresh baseline."""
        scoped_baseline = (
            bundled_host_baseline_read(retained_origin, bundle_root=bundle_root)
            if retained_origin is not None
            else nullcontext()
        )
        with runtime_user_data_scope(authority_user_data), scoped_baseline:
            return host_provider_data.capture_for(factory)

    native_prepare_key: tuple[str, str, str] | None = None
    native_execute_key: tuple[str, str, str] | None = None
    native_model_settings_key: tuple[str, str, str] | None = None
    native_model_save_key: tuple[str, str, str] | None = None
    native_conversation_get_bindings: dict[
        tuple[str, str, str], ResolvedOperationBinding
    ] = {}
    native_saved_receipt_bindings: dict[
        tuple[str, str, str], ResolvedOperationBinding
    ] = {}
    native_turn_settlement_bindings: dict[
        tuple[str, str, str], tuple[SavedReceiptCaller, _CapturedPlanEdge]
    ] = {}
    native_conversation_confirmation_bindings: dict[
        tuple[str, str, str], ResolvedOperationBinding
    ] = {}
    native_saved_turn_read_bindings: dict[
        tuple[str, str, str], ResolvedOperationBinding
    ] = {}
    native_saved_turn_start_key: tuple[str, str, str] | None = None
    native_saved_append_binding: ResolvedOperationBinding | None = None
    native_saved_append_key: tuple[str, str, str] | None = None
    native_model_resolve_bindings: dict[
        tuple[str, str, str], ResolvedOperationBinding
    ] = {}
    native_ai_routing_bindings: dict[
        tuple[str, str, str], ResolvedOperationBinding
    ] = {}
    captured_wake_ports: list[tuple[LateBoundWakePortV4, CapturedWakeDeclarationV4,
                                   ResolvedOperationBinding]] = []
    for captured_bindings, factory, backend_id in group_host_provider_captures(loaded_host_factories):
        declaration = getattr(factory, "wake_declaration", None)
        if declaration is not None and (
            type(declaration) is not CapturedWakeDeclarationV4 or len(captured_bindings) != 1
        ):
            raise AuthorityDenied("captured wake declaration is invalid")
        wake_port = LateBoundWakePortV4() if declaration is not None else None
        declared_pack_data = capture_provider_pack_data(factory)
        turn_settlement_port = None
        if (
            native_saved_turn_settlement_writer is not None
            and getattr(factory, "saved_turn_settlement_v1", False) is True
            and len(captured_bindings) == 1
        ):
            binding = captured_bindings[0]
            turn_route = saved_turn_routes.get(binding.principal_ref.value)
            if turn_route is not None and turn_route[1].resolved_binding == binding:
                caller, edge = turn_route
                turn_settlement_port = capture_turn_settlement_port(
                    caller, edge, native_saved_turn_settlement_writer,
                )
                native_turn_settlement_bindings[(
                    *edge.binding_key[1:], binding.principal_ref.value,
                )] = turn_route
        confirmation_port = None
        if (
            native_confirmation_writer is not None
            and getattr(factory, "saved_conversation_confirmation_v1", False) is True
            and len(captured_bindings) == 1
            and captured_bindings[0].function.function_id == completion_function
        ):
            binding = captured_bindings[0]
            if any(
                route.completion.resolved_binding == binding
                for route in confirmation_routes.values()
            ):
                confirmation_port = capture_conversation_confirmation_port(native_confirmation_writer)
                native_conversation_confirmation_bindings[(
                    *completion_operation, binding.principal_ref.value,
                )] = binding
        saved_append_port = None
        if (
            native_append_writer is not None
            and append_edges is not None
            and getattr(factory, "saved_message_append_v1", False) is True
            and len(captured_bindings) == 1
            and captured_bindings[0].function.function_id == append_function
            and captured_bindings[0] == append_edges[0].resolved_binding
        ):
            binding = captured_bindings[0]
            native_saved_append_binding = binding
            native_saved_append_key = (*append_operation, binding.principal_ref.value)
            saved_append_port = capture_saved_append_port(append_edges[0], native_append_writer)
        provider_capture = host_provider_capture_context(
            captured_bindings,
            workspace_mutation_port=workspace_mutation_port,
            saved_turn_settlement_port=turn_settlement_port,
            conversation_confirmation_port=confirmation_port,
            saved_message_append_port=saved_append_port,
            declared_pack_data=declared_pack_data,
            interactive_effect_port=(
                interactive_effect_port
                if interactive_effect_coordinator is not None
                and factory is interactive_effect_coordinator[2]
                else None
            ),
            workflow_port=(workflow_attempt_port if workflow_assembly is not None
                           and workflow_assembly.supplies(factory) else None),
            wake_port=wake_port,
            saved_tool_port=(saved_tool_consent_port if
                factory.function_id == LOCAL_EXECUTOR_FUNCTION else None),
            saved_tool_mode_port=(saved_tool_mode_admission if
                factory.function_id == LOCAL_EXECUTOR_FUNCTION else None),
            selected_tool_policy_port=(policy_invocation_registry.capture_current if
                factory.function_id in {
                    "rumi_default_tools_pack.file-create-tool",
                    "rumi_host_authority_bridge_pack.host-authority.interactive-effect",
                } else None),
            action_approval_policy_port=(policy_selection_dispatch if
                factory.function_id == ("rumi_host_authority_bridge_pack"
                                        ".host-authority.approval-policy") else None),
            capabilities_port=(project_approval_policy_capabilities if
                factory.function_id == ("rumi_host_authority_bridge_pack"
                                        ".host-authority.approval-policy-capabilities")
                else None),
            directory_port=(directory_selection_port if
                factory.function_id in {
                    "rumi_workspace_mount_pack.project-directory.service",
                    "rumi_workspace_mount_pack.project-workspace-prepare.service",
                } else None),
        )
        captured_provider = factory.capture(provider_capture)
        try:
            _validate_host_provider_contributions(
                captured_bindings,
                captured_provider.contributions,
                dynamic_domain_ids,
            )
        except Exception:
            captured_provider.close()
            raise AuthorityDenied("Host Provider hook contribution set is incomplete")
        try:
            verified_issuers = {item.principal_ref.value for item in captured_bindings}
            declarations = []
            for evidence in captured_provider.evidence_contributions:
                if evidence.issuer_principal_id not in verified_issuers:
                    raise AuthorityDenied("Host evidence issuer is not a verified contribution")
                declarations.append((
                    evidence.issuer_principal_id, evidence.kind, evidence.resolve,
                    evidence.data_fields,
                ))
            invocation_evidence_registry.register_many(tuple(declarations))
        except BaseException:
            # No dispatch exists yet. Revoke evidence before closing stores,
            # including factories captured earlier in this failed bootstrap.
            invocation_evidence_registry.close()
            for close in (*close_callbacks, captured_provider.close):
                try:
                    close()
                except Exception:
                    pass  # Keep the original invalid-capture error primary.
            raise
        contributions = captured_provider.contributions
        if (
            turn_start_edge is not None
            and native_saved_turn_start_writer is not None
            and turn_start_edge.resolved_binding in captured_bindings
        ):
            native_saved_turn_start_key = (
                *turn_start_edge.binding_key[1:], turn_start_edge.target.principal_id,
            )
            contributions = tuple(
                wrap_native_saved_turn_start(
                    item, turn_start_edge, native_saved_turn_start_writer,
                ) if item.key == native_saved_turn_start_key else item
                for item in contributions
            )
        if native_model_resolve_reader is not None:
            resolve_bindings = {
                (*edge.binding_key[1:], edge.target.principal_id): edge.resolved_binding
                for _role, (edge, *_parents) in model_resolve_routes.values()
                if edge.resolved_binding in captured_bindings
            }
            native_model_resolve_bindings.update(resolve_bindings)
            contributions = tuple(
                wrap_native_model_resolve(item, native_model_resolve_reader)
                if item.key in resolve_bindings else item
                for item in contributions
            )
        if native_ai_routing_evaluator is not None:
            routing_bindings = {
                (*edge.binding_key[1:], edge.target.principal_id): edge.resolved_binding
                for _role, (edge, *_parents) in ai_routing_routes.values()
                if edge.resolved_binding in captured_bindings
            }
            native_ai_routing_bindings.update(routing_bindings)
            contributions = tuple(
                wrap_native_ai_routing(item, native_ai_routing_evaluator)
                if item.key in routing_bindings else item
                for item in contributions
            )
        if native_conversation_get_reader is not None:
            conversation_bindings = {
                (
                    conversation_contract,
                    conversation_operation,
                    edge.target.principal_id,
                ): edge.resolved_binding
                for _caller, edge in native_conversation_get_edges.values()
                if edge.resolved_binding in captured_bindings
            }
            native_conversation_get_bindings.update(conversation_bindings)
            contributions = tuple(
                wrap_native_conversation_get(item, native_conversation_get_reader)
                if item.key in conversation_bindings else item
                for item in contributions
            )
        if native_saved_receipt_reader is not None:
            receipt_bindings = {
                (*edge.binding_key[1:], edge.target.principal_id): edge.resolved_binding
                for _, edge, _parent in saved_receipt_routes.values()
                if edge.resolved_binding in captured_bindings
            }
            native_saved_receipt_bindings.update(receipt_bindings)
            contributions = tuple(
                wrap_native_saved_receipt(item, native_saved_receipt_reader)
                if item.key in receipt_bindings else item
                for item in contributions
            )
        if native_saved_turn_reader is not None:
            turn_read_bindings = {
                (*edge.binding_key[1:], edge.target.principal_id): edge.resolved_binding
                for edge in (route.resource for route in confirmation_routes.values())
                if edge.resolved_binding in captured_bindings
            }
            native_saved_turn_read_bindings.update(turn_read_bindings)
            contributions = tuple(
                wrap_native_saved_turn_read(item, native_saved_turn_reader)
                if item.key in turn_read_bindings else item
                for item in contributions
            )
        if (
            native_model_save_edge is not None
            and native_model_save_writer is not None
            and native_model_save_edge.resolved_binding in captured_bindings
        ):
            native_model_save_key = (
                _MODEL_SAVE_CONTRACT, _MODEL_SAVE_OPERATION,
                native_model_save_edge.target.principal_id,
            )
            contributions = tuple(
                wrap_native_model_save(
                    item, native_model_save_edge, native_model_save_writer,
                ) if item.key == native_model_save_key else item
                for item in contributions
            )
        if (
            native_model_settings_edge is not None
            and native_model_settings_reader is not None
            and native_model_settings_edge.resolved_binding in captured_bindings
        ):
            native_model_settings_key = (
                NATIVE_MODEL_SETTINGS_CONTRACT, NATIVE_MODEL_SETTINGS_OPERATION,
                native_model_settings_edge.target.principal_id,
            )
            contributions = tuple(
                wrap_native_model_settings(
                    item, native_model_settings_edge, native_model_settings_reader,
                ) if item.key == native_model_settings_key else item
                for item in contributions
            )
        if native_prepare_edges is not None and native_provider_prepare_planner is not None:
            _, prepare_edge = native_prepare_edges
            if prepare_edge.resolved_binding in captured_bindings:
                native_prepare_key = (
                    NATIVE_PROVIDER_PREPARE_CONTRACT, NATIVE_PROVIDER_PREPARE_OPERATION,
                    prepare_edge.target.principal_id,
                )
                contributions = tuple(
                    wrap_native_provider_prepare(
                        item, provider_capture, native_provider_prepare_planner,
                        prepare_edge,
                    ) if item.key == native_prepare_key else item
                    for item in contributions
                )
            if (
                native_execute_edge is not None
                and native_execute_edge.resolved_binding in captured_bindings
            ):
                native_execute_key = (
                    NATIVE_PROVIDER_PREPARE_CONTRACT, NATIVE_PROVIDER_EXECUTE_OPERATION,
                    native_execute_edge.target.principal_id,
                )
                contributions = tuple(
                    wrap_native_provider_execution(
                        item, provider_capture, native_provider_prepare_planner,
                        native_execute_edge,
                    ) if item.key == native_execute_key else item
                    for item in contributions
                )
        host_contributions_by_backend.setdefault(backend_id, []).extend(contributions)
        close_callbacks.append(captured_provider.close)
        if wake_port is not None and declaration is not None:
            captured_wake_ports.append((wake_port, declaration, captured_bindings[0]))
    for backend_id, contributions in sorted(host_contributions_by_backend.items()):
        registered_backends += (
            ExactHostProviderBackendV4(
                tuple(contributions),
                backend_id=backend_id,
                profile_id=str(profile["profile_id"]),
                plan_digest=str(plan["plan_digest"]),
                security_epoch=int(active.activation["security_epoch"]),
                invocation_context=invocation_context,
                native_model_save_publication_possible=(
                    model_save_publication_possible
                    if any(item.key == native_model_save_key for item in contributions)
                    else None
                ),
                native_turn_settlement_publication_possible=(
                    saved_owner_publication_possible
                    if any(item.key in native_turn_settlement_bindings for item in contributions)
                    else None
                ),
                native_turn_start_publication_possible=(
                    saved_owner_publication_possible
                    if any(item.key == native_saved_turn_start_key for item in contributions)
                    else None
                ),
                native_conversation_confirmation_publication_possible=(
                    saved_owner_publication_possible
                    if any(item.key in native_conversation_confirmation_bindings
                           for item in contributions)
                    else None
                ),
                native_conversation_append_publication_possible=(
                    saved_owner_publication_possible
                    if any(item.key == native_saved_append_key for item in contributions)
                    else None
                ),
                admission_bound_operations=frozenset(
                    item.key for item in contributions
                    if item.key in {
                        native_prepare_key,
                        native_execute_key,
                        native_model_settings_key,
                        native_model_save_key,
                        native_saved_append_key,
                        native_saved_turn_start_key,
                    } or item.key in native_conversation_get_bindings
                    or item.key in native_saved_receipt_bindings
                    or item.key in native_model_resolve_bindings
                    or item.key in native_ai_routing_bindings
                    or item.key in native_turn_settlement_bindings
                    or item.key in native_conversation_confirmation_bindings
                    or item.key in native_saved_turn_read_bindings
                    or item.key in saved_local_chat_keys
                ),
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
    def confirmed_supervisor_release(
        saved_identity: Mapping[str, str],
        reservations: tuple[ResourceReservation, ...],
    ) -> tuple[str, ...]:
        """Return only reservations with an exact supervisor release proof.

        Non-detached charges are owned by the supervisor process that
        journaled them, so a dead recorded owner proves the charge is
        reusable. Detached PackVM charges can outlive their owner and still
        require exact interrupted-allocation reconciliation.
        """

        released: set[str] = set(dead_owner_reservation_ids(reservations))
        recover = getattr(packvm_provisioner, "recover_interrupted_allocation", None)
        current_fencing = int(active.activation["fencing_token"])
        saved_activation_id = saved_identity.get("activation_id", "")
        if (
            not callable(recover)
            or saved_identity.get("profile_id") != profile_id
            or saved_activation_id == activation_id
            or current_fencing <= 1
            or not reservations
            or any(item.profile_id != profile_id for item in reservations)
        ):
            return tuple(sorted(released))
        activation_name = saved_activation_id.removeprefix("activation:")
        if (
            not activation_name
            or len(activation_name) > 255
            or Path(activation_name).name != activation_name
        ):
            return tuple(sorted(released))
        try:
            envelope = json.loads(
                (
                    authority_workspace
                    / "activation"
                    / "activations"
                    / f"{activation_name}.json"
                ).read_text(encoding="utf-8")
            )
            saved_activation = envelope["activation"]
            saved_fencing = saved_activation["fencing_token"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return tuple(sorted(released))
        if (
            not isinstance(saved_activation, Mapping)
            or type(saved_fencing) is not int
            or saved_fencing < 1
            or saved_fencing >= current_fencing
            or any(
                saved_activation.get(field) != saved_identity.get(field)
                for field in (
                    "profile_id",
                    "profile_revision",
                    "activation_id",
                    "plan_digest",
                )
            )
        ):
            return tuple(sorted(released))
        candidates = {
            (
                f"domain.provider."
                f"{binding.principal_ref.value.removeprefix('sha256:')[:24]}."
                f"{saved_fencing}",
                item.reservation_id,
                binding.function.implementation_digest,
            )
            for item in reservations
            for binding in catalog_bindings
            if binding.variant.execution_kind is ExecutionKind.PACK_VM
        }
        for domain_id, reservation_id, executable_digest in sorted(candidates):
            if recover(
                domain_id=domain_id,
                reservation_id=reservation_id,
                executable_digest=executable_digest,
            ):
                released.add(reservation_id)
        return tuple(sorted(released))

    native_admission_rules = [
        (
            binding,
            CONVERSATION_GET_MINIMUM_MEMORY,
            is_native_conversation_get_route,
        )
        for binding in native_conversation_get_bindings.values()
    ]
    native_admission_rules.extend(
        (binding, SAVED_RECEIPT_MINIMUM_MEMORY, is_native_saved_receipt_route)
        for binding in native_saved_receipt_bindings.values()
    )
    native_admission_rules.extend(
        (
            edge.resolved_binding,
            SAVED_TURN_SETTLEMENT_MINIMUM_MEMORY,
            partial(is_native_saved_turn_settlement_route, caller=caller, edge=edge),
        )
        for caller, edge in native_turn_settlement_bindings.values()
    )
    native_admission_rules.extend(
        (
            binding,
            CONVERSATION_CONFIRMATION_MINIMUM_MEMORY,
            is_native_conversation_confirmation_route,
        )
        for binding in native_conversation_confirmation_bindings.values()
    )
    native_admission_rules.extend(
        (binding, SAVED_TURN_READ_MINIMUM_MEMORY, is_native_saved_turn_read_route)
        for binding in native_saved_turn_read_bindings.values()
    )
    if native_saved_turn_start_key is not None and turn_start_edge is not None:
        native_admission_rules.append((
            turn_start_edge.resolved_binding,
            SAVED_TURN_START_MINIMUM_MEMORY,
            is_native_saved_turn_start_route,
        ))
    native_admission_rules.extend(
        (
            binding,
            MODEL_RESOLVE_MINIMUM_MEMORY,
            partial(is_native_model_resolve_route, operation_id=operation_id),
        )
        for (_, operation_id, _), binding in native_model_resolve_bindings.items()
    )
    native_admission_rules.extend(
        (
            binding,
            AI_ROUTING_MINIMUM_MEMORY,
            partial(is_native_ai_routing_route, operation_id=operation_id),
        )
        for (_, operation_id, _), binding in native_ai_routing_bindings.items()
    )
    if native_saved_append_binding is not None:
        native_admission_rules.append((
            native_saved_append_binding,
            CONVERSATION_APPEND_MINIMUM_MEMORY,
            is_native_saved_append_route,
        ))
    if native_saved_execution is not None:
        native_admission_rules.append((
            native_saved_execution.binding,
            SAVED_ORCHESTRATION_MINIMUM_MEMORY,
            lambda context, _: context.target_domain_id == native_saved_execution.domain_id,
        ))
    native_admission_rules.extend(
        (
            route.adapter.resolved_binding,
            SAVED_LOCAL_CHAT_MINIMUM_MEMORY,
            is_native_saved_local_chat_route,
        )
        for route in saved_local_chat_routes.values()
    )
    if native_model_save_key is not None and native_model_save_edge is not None:
        native_admission_rules.append((
            native_model_save_edge.resolved_binding,
            NATIVE_MODEL_SETTINGS_MEMORY,
            is_native_model_save_route,
        ))
    if native_model_settings_key is not None and native_model_settings_edge is not None:
        native_admission_rules.append((
            native_model_settings_edge.resolved_binding,
            NATIVE_MODEL_SETTINGS_MEMORY,
            is_native_model_settings_route,
        ))
    if native_provider_prepare_planner is not None:
        memory = native_provider_prepare_planner.memory_bytes
        if native_prepare_key is not None and native_prepare_edges is not None:
            native_admission_rules.append(
                (
                    native_prepare_edges[1].resolved_binding,
                    memory,
                    is_native_prepare_route,
                )
            )
        if native_execute_key is not None and native_execute_edge is not None:
            native_admission_rules.append(
                (native_execute_edge.resolved_binding, memory, is_native_execute_route)
            )
    production_admission = _PlanAdmission(
        profile_id=profile_id,
        activation_id=activation_id,
        plan=plan,
        state_path=authority_workspace / "admission" / "reservations.json",
        confirmed_supervisor_release=confirmed_supervisor_release,
        native_bindings=tuple(native_admission_rules),
    )
    if native_saved_execution is not None:
        authority_control.install_native_wasm_binding_resolver(selected_native_saved_authority)
    broker = runtime.broker(
        authority_store=authority_store,
        adapters=AdapterPlanner(()),
        adapter_executor=_NoAdapterExecution(),
        backends=backend_registry,
        materialization=MaterializationCoordinator(),
        admission=production_admission,
        reconciliation=InMemoryReconciliationStore(),
        authority_adapter=authority_control,
        acceptance_receipts=acceptance_receipts,
        native_wasm_binding_for=(
            selected_native_saved_binding if native_saved_execution is not None else None
        ),
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
        # An external panel session belongs to the captured Shell, never to
        # an arbitrary Provider which happens to call the same operation.
        # Nested Provider sessions above retain their exact Host binding.
        shell_candidates = tuple(
            edge for edge in candidates if edge.caller.principal_id in shell_principal_ids
        )
        if len(shell_candidates) != 1:
            raise AuthorityDenied(
                "operation does not identify one captured Shell caller edge"
            )
        return shell_candidates[0]

    def context_for(contract_id: str, operation_id: str, session_id: str) -> RequestContext:
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512:
            raise AuthorityDenied("authenticated session binding is invalid")
        captured_edge = select_edge(contract_id, operation_id, session_id)
        caller = captured_edge.caller
        target = captured_edge.target
        target_suffix = target.principal_id.removeprefix("sha256:")[:24]
        native = native_saved_execution if captured_edge is ordinary_orchestration_edge else None
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
            target_domain_id=(
                native.domain_id if native is not None
                else f"domain.provider.{target_suffix}.{activation_suffix}"
            ),
            target_boot_epoch=1,
            target_backend_digest=(
                native.backend_digest if native is not None else target_backend_digests.get(
                    target.principal_id,
                    canonical_digest({
                        "backend": "captured-but-not-materialized",
                        "target": target.principal_id,
                    }),
                )
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
            projected_backend = (
                native_saved_execution.backend
                if native_saved_execution is not None
                and resolved_binding == native_saved_execution.binding
                else broker._backends.select(resolved_binding)
            )
        except Exception as error:
            projected_backend = None
            backend_error = str(error) or "production backend is unavailable"
        function_principal = binding["function_principal"]
        function_id = str(function_principal["function_id"])
        pack_id = str(binding["pack_id"])
        pack_manifest = catalog.packs.get(pack_id)
        pack_identity = (
            pack_manifest.get("pack")
            if isinstance(pack_manifest, Mapping)
            else None
        )
        if not isinstance(pack_identity, Mapping):
            raise AuthorityDenied("selected Provider Pack identity is unavailable")
        pack_display_name = pack_identity.get("display_name")
        pack_description = pack_identity.get("description", "")
        if not isinstance(pack_display_name, str) or not pack_display_name.strip():
            raise AuthorityDenied("selected Provider Pack display name is invalid")
        if not isinstance(pack_description, str):
            raise AuthorityDenied("selected Provider Pack description is invalid")
        provider_prefix = f"{pack_id}."
        provider_instance_id = (
            function_id.removeprefix(provider_prefix)
            if function_id.startswith(provider_prefix)
            else function_id
        )
        if not provider_instance_id:
            raise AuthorityDenied("selected Provider instance identity is invalid")
        providers.setdefault(binding["contract_id"], ())
        providers[binding["contract_id"]] += (
            {
                "provider_id": function_id,
                "provider_instance_id": provider_instance_id,
                "pack_id": pack_id,
                "pack_display_name": pack_display_name.strip(),
                "pack_description": pack_description.strip(),
                "function_id": function_id,
                "principal_id": resolved_binding.principal_ref.value,
                "implementation_digest": resolved_binding.function.implementation_digest,
                "contract_id": binding["contract_id"],
                "operation_id": binding["operation_id"],
                "effect_class": resolved_binding.operation.effect_class.value,
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
        _check_shared_capture(_assert_current_capture)

    def _assert_current_capture() -> None:
        from ..pack_control_v4 import PackControlDenied
        from .profile_capture import capture_active_profile

        # Reuse only the explicit operation-local capture opened by the HTTP
        # boundary or runtime-surface operation. Outside that scope this is
        # still a fresh canonical capture on every assertion.
        with runtime_user_data_scope(authority_user_data):
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
        ui_asset_packs.assert_current()
        for pack_id in retained_pack_roots:
            assert_retained_root_current(pack_id)
        host_provider_data.assert_current()
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

    if workflow_attempt_port is not None and workflow_assembly is not None:
        workflow_binding = workflow_assembly.binding
        workflow_principal = workflow_binding.principal_ref

        def workflow_session_id(invocation: Any, reservation_id: str) -> str:
            # Stable across approval resume, unique across concurrent attempts.
            # Both owner and reservation identity are verified Host inputs.
            return "session.workflow-attempt." + canonical_digest({
                "owner_principal": invocation.presentation_owner_principal_id,
                "owner_session": invocation.presentation_owner_session_id,
                "profile_id": profile_id,
                "activation_id": activation_id,
                "reservation_id": reservation_id,
            }).removeprefix("sha256:")

        def context_for_workflow_attempt(
            route: Any, invocation: Any, reservation_id: str,
        ) -> RequestContext:
            invocation.assert_current()
            if route.caller_principal != workflow_principal:
                raise AuthorityDenied("Workflow caller binding changed")
            session_id = workflow_session_id(invocation, reservation_id)
            owner = (invocation.presentation_owner_principal_id,
                     invocation.presentation_owner_session_id)
            resolved_session_id = bind_nested_session(
                session_id, workflow_principal.value, owner,
            )
            try:
                context = context_for(
                    route.binding.operation.contract_id,
                    route.binding.operation.operation_id,
                    session_id,
                )
                if context.caller_principal != workflow_principal or (
                    context.target_domain_id != authority_target_domain(route.binding)
                ):
                    raise AuthorityDenied("Workflow child context binding changed")
                return context
            finally:
                release_nested_session(session_id, resolved_session_id, release_parent=False)

        @contextmanager
        def workflow_execution_scope(context: RequestContext, invocation: Any) -> Iterator[None]:
            invocation.assert_current()
            parent = capture_invocation_scope(invocation.envelope)
            session_id = workflow_session_id(invocation, context.request_id)
            resolved_session_id = bind_nested_session(
                session_id, workflow_principal.value,
                (invocation.presentation_owner_principal_id,
                 invocation.presentation_owner_session_id),
                parent_invocation=parent,
            )
            try:
                if resolved_session_id != context.caller_session_id:
                    raise AuthorityDenied("Workflow execution session changed")
                parent.assert_current()
                yield
                parent.assert_current()
            finally:
                release_nested_session(session_id, resolved_session_id)

        def workflow_evidence_scope(route: Any, invocation: Any, request: Mapping[str, Any]) -> Any:
            invocation.assert_current()
            context = invocation.envelope.context
            if (invocation.envelope.contract_id, invocation.envelope.operation_id,
                    invocation.envelope.target_principal.value) not in workflow_assembly.admitted_invocations:
                raise AuthorityDenied("Workflow evidence parent is outside captured admission")
            return invocation_evidence_registry.dispatch(
                kind=WORKFLOW_EVIDENCE_KIND,
                reference=str(request["request_id"]),
                binding=EvidenceBinding(
                    issuer_principal=workflow_principal.value,
                    target_principal=route.binding.principal_ref.value,
                    contract_id=route.binding.operation.contract_id,
                    contract_version=route.binding.operation.contract_version,
                    operation_id=route.binding.operation.operation_id,
                    payload_digest=digest_payload(request["input"]),
                    idempotency_key=str(request["idempotency_key"]),
                    profile_id=context.profile_id,
                    plan_digest=context.plan_digest,
                    activation_id=context.activation_id,
                    activation_digest=context.activation_digest,
                    security_epoch=context.security_epoch,
                    presentation_owner_principal=invocation.presentation_owner_principal_id,
                    presentation_owner_session=invocation.presentation_owner_session_id,
                ),
                guard=invocation.assert_current,
            )

        workflow_attempt_port.bind(create_workflow_attempt_service_v4(
            WorkflowAttemptServiceConfigV4(
                broker=broker, authority=authority_control, approvals=authority_control,
                routes=workflow_assembly.routes,
                context_for_attempt=context_for_workflow_attempt,
                execution_scope=workflow_execution_scope,
                presentation_owner_scope=pending_effect_owner_scope,
                assert_current_capture=assert_current_capture,
                state_path=host_provider_state_root / profile_id / "workflow-attempts-v4.sqlite3",
                coordinator_principal=workflow_principal,
                coordinator_publisher_lineage=workflow_binding.artifact.publisher_lineage,
                profile_id=profile_id, activation_id=activation_id,
                activation_digest=activation_digest,
                plan_digest=str(plan["plan_digest"]),
                security_epoch=int(active.activation["security_epoch"]),
                admitted_invocations=tuple(
                    (contract, operation, OpaqueAuthorityRef(principal))
                    for contract, operation, principal in workflow_assembly.admitted_invocations
                ),
                stop_principals=workflow_assembly.stop_principals,
                evidence_scope=workflow_evidence_scope,
            )
        ))

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
                owner = presentation_owner_bindings.get(
                    presentation_context.caller_session_id,
                    (presentation_context.caller_principal.value, presentation_context.caller_session_id),
                )
            resolved_session_id = bind_nested_session(
                session_id, route.coordinator_principal.value, owner,
            )
            try:
                context = context_for(
                    route.spec.execute_contract_id,
                    route.spec.execute_operation_id,
                    session_id,
                )
            finally:
                release_nested_session(session_id, resolved_session_id)
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

        from tobkiri_host.typed_pending_effect_persistence import (
            TypedPendingEffectPersistence,
        )

        from tobkiri_host.policy_effect_authorization import PolicyEffectAuthorizationAdapter
        from core_runtime.owned_file_approval_v4 import assert_file_request_live

        def authenticated_policy_root(selection_id: str) -> Any:
            root = policy_kernel.policy_roots.get(selection_id)
            if root is None:
                raise AuthorityDenied("native selected policy root unavailable")
            root.resolve_current()
            return root

        policy_effect_authorizations = PolicyEffectAuthorizationAdapter(
            authority=authority_control, broker=broker,
            root_for=authenticated_policy_root,
            owned_file_guard=assert_file_request_live,
        )

        interactive_effect_controller = PendingEffectController(
            persistence=TypedPendingEffectPersistence(authority_control),
            approvals=authority_control,
            policy_authorizations=policy_effect_authorizations,
            coordinator_principal=coordinator_principal,
            coordinator_publisher_lineage=coordinator_binding.artifact.publisher_lineage,
            presentation_owner_scope=pending_effect_owner_scope,
        )
        _recover_interactive_effect_controller(interactive_effect_controller)
        interactive_effect_port.bind(
            HostInteractiveEffectService(
                broker=broker,
                controller=interactive_effect_controller,
                policy_effect_authorization_port=policy_effect_authorizations,
                routes=tuple(routes),
                context_for_execute=context_for_interactive_effect,
                resume_invocation_scope=retained_effect_resume_scope,
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

    executor_bindings = tuple(
        binding
        for function_id, bindings, _factory, _backend_id in loaded_host_factories
        if function_id == LOCAL_EXECUTOR_FUNCTION
        for binding in bindings
    )
    consent_route = (optional_captured_consent_route(captured_edges, executor_bindings)
                     if executor_bindings else None)
    ask_saved_tool_execution = None
    if consent_route is not None:
        from tobkiri_host.saved_tool_consent import SavedToolConsentExecution
        from tobkiri_host.saved_tool_context import NestedToolBinding
        from tobkiri_host.consumed_tool_consent import ConsumedConsentVerifier

        def bind_saved_tool_nested(
            invocation: Any, contract_id: str, operation_id: str,
            payload: Mapping[str, Any],
        ) -> NestedToolBinding:
            """Retain the same production ancestry until prepared execution ends."""
            scope = capture_invocation_scope(invocation.envelope)
            scope.assert_current()
            session_id = execution_session_id(invocation.envelope)
            owner = (invocation.presentation_owner_principal_id,
                     invocation.presentation_owner_session_id)
            authority_session_id = bind_nested_session(
                session_id, invocation.envelope.target_principal.value, owner,
                parent_invocation=scope,
            )
            try:
                context = context_for(contract_id, operation_id, session_id)
                ceiling = effect_scope_for(contract_id, operation_id, payload, context)
                if context.caller_principal != executor_bindings[0].principal_ref:
                    raise AuthorityDenied("saved tool nested caller changed")
                return NestedToolBinding(
                    context=context, ceiling=ceiling,
                    caller_publisher_lineage=executor_bindings[0].artifact.publisher_lineage,
                    cancellation_proof=nested_cancellation_proof_for(
                        invocation.envelope, owner[0], owner[1],
                    ),
                    release=lambda: release_nested_session(
                        session_id, authority_session_id,
                    ),
                    inline_parent_scope=scope,
                )
            except BaseException:
                release_nested_session(session_id, authority_session_id)
                raise

        ask_saved_tool_execution = SavedToolConsentExecution(
            broker=broker, authority=authority_control,
            window=authority_approval_window,
            consent_proof=ConsumedConsentVerifier(authority_store),
            entry_guard_registry=saved_tool_entry_guards,
            route=consent_route,
            bind_nested=bind_saved_tool_nested,
        )

    from core_runtime.approval_policy_capabilities_v4 import (
        HostApprovalPolicyCapabilitiesV4,
    )
    def approval_policy_conversation_membership(
        invocation: Any, conversation_id: str, workspace_id: str,
    ) -> bool:
        invocation.assert_current()
        client = invocation.contract_client(
            allowed_contract_ids=frozenset({"tobkiri.resource.conversation.v1"}),
            consumer_pack_id="rumi_host_authority_bridge_pack",
            include_credentials=False,
        )
        result = client.invoke(
            "tobkiri.resource.conversation.v1",
            "rumi_conversation_store_pack.conversation-resource",
            {"profile_id": str(profile["profile_id"]), "operation": "get",
             "conversation_id": conversation_id},
        )
        invocation.assert_current()
        return (isinstance(result, Mapping)
                and isinstance(result.get("conversation"), Mapping)
                and result["conversation"].get("id") == conversation_id
                and isinstance(result["conversation"].get("metadata"), Mapping)
                and result["conversation"]["metadata"].get("workspace_id") == workspace_id)

    policy_projection = None
    if (ask_saved_tool_execution is not None
            and workspace_binding_resolver is not None
            and authority_approval_window_open is not None):
        from core_runtime.bootstrap.saved_tool_policy_composition_v4 import compose_saved_tool_policy_v4

        def current_selected_workspace() -> Mapping[str, Any]:
            return saved_selected_workspace()

        def policy_owner_context(function_id: str) -> HostProviderCaptureContextV4:
            matches = tuple(item for item in loaded_host_factories if item[0] == function_id)
            if len(matches) != 1:
                raise AuthorityDenied("reviewer captured owner source unavailable")
            _, bindings_for_owner, owner_factory, _ = matches[0]
            return host_provider_capture_context(
                bindings_for_owner, workspace_mutation_port=workspace_mutation_port,
                declared_pack_data=capture_provider_pack_data(owner_factory),
            )

        def read_policy_conversation(invocation: Any, conversation_id: str) -> Mapping[str, Any]:
            invocation.assert_current()
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({"tobkiri.resource.conversation.v1"}),
                consumer_pack_id="rumi_tool_local_executor_pack", include_credentials=False,
            )
            result = client.invoke("tobkiri.resource.conversation.v1",
                "rumi_conversation_store_pack.conversation-resource",
                {"profile_id": str(profile["profile_id"]), "operation": "get",
                 "conversation_id": conversation_id})
            invocation.assert_current()
            conversation = result.get("conversation") if isinstance(result, Mapping) else None
            if not isinstance(conversation, Mapping):
                raise AuthorityDenied("policy conversation resource unavailable")
            return conversation

        try:
            composed_policy = compose_saved_tool_policy_v4(
                broker=broker, authority=authority_control, store=authority_store,
                kernel=policy_kernel, catalog=runtime.composition.catalog,
                profile_id=str(profile["profile_id"]), edges=tuple(captured_edges),
                bindings=tuple(catalog_bindings), window=authority_approval_window,
                ask=ask_saved_tool_execution, bind_nested=bind_saved_tool_nested,
                entry_guards=saved_tool_entry_guards,
                policy_invocations=policy_invocation_registry,
                selection_dispatch=policy_selection_dispatch,
                read_conversation=read_policy_conversation,
                resolve_workspace=workspace_binding_resolver,
                selected_workspace=current_selected_workspace,
                host_context=policy_owner_context, invocation_context=invocation_context,
                bind_nested_session=bind_nested_session,
                release_nested_session=release_nested_session,
                context_for=context_for, effect_scope_for=effect_scope_for,
                capture_invocation_scope=capture_invocation_scope,
                nested_cancellation_proof_for=nested_cancellation_proof_for,
                assert_current_capture=assert_current_capture,
                authority_records=profile_edge_authority_records,
                dispatch_capture={"profile_revision": str(plan["profile_revision"]),
                    "plan_digest": str(plan["plan_digest"]),
                    "activation_id": str(active.activation["activation_id"]),
                    "security_epoch": int(active.activation["security_epoch"])},
            )
        except (AuthorityDenied, PermissionError, KeyError, LookupError):
            # Optional topology does not prevent local-first startup or native ask.
            saved_tool_consent_port.bind(ask_saved_tool_execution)
        else:
            saved_tool_consent_port.bind(composed_policy.execute)
            saved_tool_mode_admission_holder[0] = composed_policy.admit
            policy_projection = composed_policy.project
    elif ask_saved_tool_execution is not None:
        saved_tool_consent_port.bind(ask_saved_tool_execution)

    approval_policy_capabilities_holder.append(HostApprovalPolicyCapabilitiesV4(
        profile_id=str(profile["profile_id"]),
        activation_id=str(active.activation["activation_id"]),
        capture={"plan_digest": str(plan["plan_digest"]),
                 "activation_digest": activation_digest,
                 "security_epoch": int(active.activation["security_epoch"])},
        assert_current_capture=assert_current_capture,
        workspace_binding=workspace_binding_resolver,
        policy_projection=policy_projection,
        conversation_membership=approval_policy_conversation_membership,
    ))

    wake_drivers: list[CapturedWakeDriverV4] = []

    def close_wake_drivers() -> None:
        """Fence Host sources before any Broker shutdown or read restart."""
        close_captured_wake_drivers_v4(wake_drivers)

    from ..captured_pack_ui_assets_v4 import CapturedPackUIAssets
    from ..pack_boundary import load_pack_catalog, resolve_pack_root

    ui_baseline_records = load_pack_catalog()
    ui_artifact_pins = {
        str(row["identity"]): str(row["artifact_digest"])
        for row in lock["effective_set"]
    }

    def capture_selected_pack_ui_assets(
        owner_pack_id: str, index_file: str,
    ) -> CapturedPackUIAssets | None:
        """Bind a trusted mount to its exact selected Pack, never its latest revision."""
        root = pack_roots.get(owner_pack_id)
        if root is None:
            dispatch.assert_current()
            return ui_asset_packs.capture(
                owner_pack_id, index_file, assert_current=dispatch.assert_current,
            )
        dispatch.assert_current()
        assert_retained_root_current(owner_pack_id)
        pin = ui_artifact_pins[owner_pack_id]
        baseline = ui_baseline_records.get(owner_pack_id)
        legacy_builtin = (
            baseline is not None
            and root == resolve_pack_root(owner_pack_id, ecosystem_root)
            and compile_pack_root(root).artifact.digest == pin
        )

        def assert_ui_current() -> None:
            dispatch.assert_current()
            assert_retained_root_current(owner_pack_id)

        return CapturedPackUIAssets.capture(
            pack_id=owner_pack_id,
            artifact_digest=pin,
            pack_root=root,
            owner_root=authority_user_data,
            index_file=index_file,
            assert_current=assert_ui_current,
            legacy_builtin=legacy_builtin,
        )

    dispatch = runtime.dispatch_session(
        broker=broker,
        context_for=context_for,
        effect_scope_for=effect_scope_for,
        providers=providers,
        authority_control=authority_control,
        current_capture_check=assert_current_capture,
        pack_ui_assets_for=capture_selected_pack_ui_assets,
        owned_authority_store=authority_store,
        close_callbacks=(
            *close_callbacks,
            *((workspace_mutation_port.close,) if workspace_mutation_port else ()),
            *((control_session.close,) if control_session is not None else ()),
            *tuple(
                backend.close
                for backend in (*owned_packvm_backends, *owned_wasm_backends)
                if callable(getattr(backend, "close", None))
            ),
        ),
        stop_callbacks=(
            close_wake_drivers,
            cancellation_handles.close,
            *((control_session.cancel_pending_reads,) if control_session is not None else ()),
        ),
        before_close_callbacks=(close_wake_drivers,),
        native_provider_runtime_publish=(
            publish_native_provider_runtime
            if (native_execute_key is not None or native_conversation_get_bindings
                or native_saved_receipt_bindings or native_model_resolve_bindings
                or native_ai_routing_bindings
                or native_turn_settlement_bindings or native_conversation_confirmation_bindings
                or native_saved_turn_read_bindings
                or native_saved_turn_start_key is not None
                or native_saved_append_key is not None or native_saved_execution is not None
                or saved_local_chat_routes)
            else None
        ),
    )
    dispatch_holder.append(dispatch)
    for wake_port, declaration, owner_binding in captured_wake_ports:
        owner_id = owner_binding.principal_ref.value
        candidates = tuple(edge for edge in captured_edges
            if edge.caller.principal_id == owner_id
            and edge.resolved_binding.operation.contract_id == declaration.contract_id
            and edge.resolved_binding.operation.operation_id == declaration.operation_id)
        # A declaration creates neither a selected edge nor a Grant. Missing
        # or ambiguous routes keep only the unbound/unavailable public port.
        if len(candidates) != 1:
            continue
        edge = candidates[0]
        target_id = edge.target.principal_id
        identity = {"profile_id": profile_id, "profile_revision": plan["profile_revision"],
            "activation_id": activation_id, "activation_digest": activation_digest,
            "plan_digest": plan["plan_digest"], "security_epoch": active.activation["security_epoch"],
            "owner_artifact": owner_binding.artifact.digest,
            "owner_implementation": owner_binding.function.implementation_digest,
            "target_artifact": edge.resolved_binding.artifact.digest,
            "target_implementation": edge.resolved_binding.function.implementation_digest}
        state_key = canonical_digest({"profile_id": profile_id,
            "function_id": owner_binding.function.function_id}).removeprefix("sha256:")

        @contextmanager
        def wake_context_scope(occurrence: str, *, caller_id: str = owner_id,
                               fixed_declaration: CapturedWakeDeclarationV4 = declaration,
                               fixed_target: str = target_id,
                               session_key: str = state_key) -> Iterator[RequestContext]:
            """Register a dedicated Host clock session for one fresh request."""
            session_id = "wake-" + canonical_digest({"registration": session_key,
                "occurrence": occurrence}).removeprefix("sha256:")
            owner_session = authority_session_id(session_id, caller_id)
            resolved_session = bind_nested_session(session_id, caller_id,
                                                   (caller_id, owner_session))
            try:
                context = context_for(fixed_declaration.contract_id,
                                      fixed_declaration.operation_id, session_id)
                expected_domain = dynamic_domain_ids.get((fixed_declaration.contract_id,
                    fixed_declaration.operation_id, fixed_target))
                if (context.caller_principal.value != caller_id
                        or expected_domain != context.target_domain_id):
                    raise AuthorityDenied("captured wake session edge changed")
                yield context
            finally:
                release_nested_session(session_id, resolved_session)

        def wake_effect_scope(context: RequestContext, *,
                              fixed: CapturedWakeDeclarationV4 = declaration) -> Mapping[str, Any]:
            return effect_scope_for(fixed.contract_id, fixed.operation_id,
                                    fixed.payload, context)

        try:
            wake_drivers.append(bind_production_wake_v4(
                port=wake_port, declaration=declaration, owner_principal_id=owner_id,
                target_principal_id=target_id,
                state_path=host_provider_state_root / "wakes" / f"{state_key}.sqlite3",
                identity=identity, broker=broker, authority=authority_control,
                authority_store=authority_store, context_scope=wake_context_scope,
                effect_scope=wake_effect_scope,
                assert_current=assert_current_capture,
            ))
        except Exception:
            dispatch.close()
            raise
    if control_session is not None and http_contract_bindings:
        if capability_binding_selector is None:
            dispatch.close()
            raise AuthorityDenied("application capability binding selector is unavailable")
        capability_binding = capability_binding_selector(http_contract_bindings)
        if capability_binding is None or capability_binding not in http_contract_bindings:
            dispatch.close()
            raise AuthorityDenied("capability invocation binding is absent or ambiguous")

        @profile_operation_phase("capability_binding")
        def capability_binding_reader() -> tuple[Mapping[str, object], Mapping[str, Any]]:
            from ..pack_control_v4 import capture_pack_catalog_reader

            if capability_binding_snapshot_factory is None:
                raise AuthorityDenied("capability projection factory is unavailable")
            catalog = capture_pack_catalog_reader().read()
            capability = capability_binding_snapshot_factory(
                capability_binding,
                session=dispatch,
                catalog=catalog,
            )
            return capability, catalog

        control_session.bind_capability_reader(capability_binding_reader)
    return dispatch


__all__ = ["capture_production_dispatch"]
