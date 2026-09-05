"""Canonical fail-closed Request execution and materialization path."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
import contextvars
from dataclasses import dataclass
import hashlib
import json
import math
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from .admission import AdmissionEstimate, QueueScope, ResourceReservation
from .backends import BackendRegistry, ExecutionBackend
from .contracts import (
    AdapterExecutor,
    AdapterPlanner,
    OperationCatalog,
    ResolvedOperationBinding,
)
from .effects import (
    EffectDisposition,
    ProviderOutcome,
    ReconciliationStore,
    raise_ambiguous,
)
from .errors import (
    AmbiguousEffectError,
    AuditUnavailableError,
    AuthorizationError,
    ProviderExecutionError,
    RequestTimedOutError,
    ResolutionError,
)
from .models import (
    EffectClass,
    InvocationFrame,
    OpaqueAuthorityRef,
    RequestContext,
    RuntimeEvidence,
)
from .materialization import MaterializationCoordinator, WorkloadInstanceKey
from .ports import (
    AuditPort,
    AuthorityPort,
    FinalAuthorizationQuery,
    OpaqueAuditReservation,
    OpaqueInvocationLease,
    StaticAuthorityQuery,
)
from tobkiri_protocol.canonical import strict_loads


@dataclass(frozen=True)
class AdmissionTicket:
    """Reservation returned after bounded queue admission."""

    reservation: ResourceReservation


class RequestAdmissionPort(Protocol):
    """Static metadata validation and bounded fair admission interface.

    Exact required methods:

    * ``estimate`` validates only signed, non-executable metadata and returns
      the measured/declaration/floor/Profile/backend inputs used for charging.
    * ``acquire`` applies global/Profile/caller/Pack/binding quotas, waits no
      longer than the supplied budget, and returns an owned reservation.
    * ``release`` idempotently releases queue/workload accounting.
    """

    def estimate(
        self,
        context: RequestContext,
        binding: ResolvedOperationBinding,
        payload: Mapping[str, Any],
    ) -> AdmissionEstimate:
        """Validate static metadata without importing or executing Pack code."""

    def acquire(
        self,
        scope: QueueScope,
        estimate: AdmissionEstimate,
        wait_timeout_seconds: float,
    ) -> AdmissionTicket:
        """Return an accepted queue reservation or fail before materialization."""

    def release(self, ticket: AdmissionTicket) -> None:
        """Release all queue/workload charges for the ticket."""


@dataclass(frozen=True)
class RequestEnvelope:
    """Host-generated provider envelope; caller identity is never payload data."""

    context: RequestContext
    target_principal: OpaqueAuthorityRef
    target_domain: OpaqueAuthorityRef
    contract_id: str
    contract_version: str
    operation_id: str
    payload: Mapping[str, Any]
    request_digest: str
    deadline_monotonic: float
    lease: OpaqueInvocationLease
    idempotency_key: str | None


@dataclass(frozen=True)
class PreparedInvocation:
    """Host-only, side-effect-free result of request normalization.

    ``normalized_payload`` is an immutable JSON-shaped snapshot.  The Broker
    thaws a private copy immediately before dispatch, so caller-owned mutable
    payload objects cannot alter a request after its digest is calculated.
    This type is intentionally not a Pack contract or UI response.
    """

    binding: ResolvedOperationBinding
    normalized_payload: Mapping[str, Any]
    request_digest: str
    timeout_ms: int
    deadline_monotonic: float
    idempotency_key: str | None
    binding_fingerprint: Mapping[str, Any]
    context_fingerprint: Mapping[str, Any]
    allow_lossy_adapters: bool

    def to_snapshot(self) -> "PreparedInvocationSnapshot":
        """Create a JSON-only Host-private snapshot for durable pending work."""

        return PreparedInvocationSnapshot(
            contract_id=self.binding.operation.contract_id,
            contract_version=self.binding.operation.contract_version,
            operation_id=self.binding.operation.operation_id,
            normalized_payload=_thaw_payload(self.normalized_payload),
            request_digest=self.request_digest,
            timeout_ms=self.timeout_ms,
            idempotency_key=self.idempotency_key,
            binding_fingerprint=_thaw_payload(self.binding_fingerprint),
            context_fingerprint=_thaw_payload(self.context_fingerprint),
            allow_lossy_adapters=self.allow_lossy_adapters,
        )


@dataclass(frozen=True)
class PreparedInvocationSnapshot:
    """JSON-serializable Host-private input for one durable pending effect.

    A future Host store must encrypt this object at rest: normalized payloads
    may contain secrets.  It is not a Pack contract and must never be returned
    through a Pack, UI, or approval status response.
    """

    contract_id: str
    contract_version: str
    operation_id: str
    normalized_payload: Mapping[str, Any]
    request_digest: str
    timeout_ms: int
    idempotency_key: str | None
    binding_fingerprint: Mapping[str, Any]
    context_fingerprint: Mapping[str, Any]
    allow_lossy_adapters: bool

    def __post_init__(self) -> None:
        """Canonicalize every durable field and reject malformed snapshots."""

        _snapshot_text(self.contract_id, "snapshot contract_id")
        _snapshot_text(self.contract_version, "snapshot contract_version")
        _snapshot_text(self.operation_id, "snapshot operation_id")
        _snapshot_digest(self.request_digest, "snapshot request_digest")
        if (
            isinstance(self.timeout_ms, bool)
            or not isinstance(self.timeout_ms, int)
            or self.timeout_ms <= 0
        ):
            raise ValueError("snapshot timeout_ms is invalid")
        if self.idempotency_key is not None:
            _snapshot_text(self.idempotency_key, "snapshot idempotency_key")
        if not isinstance(self.allow_lossy_adapters, bool):
            raise ValueError("snapshot adapter policy is invalid")
        object.__setattr__(
            self,
            "normalized_payload",
            _snapshot_mapping(self.normalized_payload, "snapshot payload"),
        )
        object.__setattr__(
            self,
            "binding_fingerprint",
            _snapshot_mapping(
                self.binding_fingerprint,
                "snapshot binding fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "context_fingerprint",
            _snapshot_mapping(
                self.context_fingerprint,
                "snapshot context fingerprint",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return an encrypted-store-ready JSON document without clock state."""

        return {
            "schema": "tobkiri.prepared-invocation.v1",
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "operation_id": self.operation_id,
            "normalized_payload": _thaw_payload(self.normalized_payload),
            "request_digest": self.request_digest,
            "timeout_ms": self.timeout_ms,
            "idempotency_key": self.idempotency_key,
            "binding_fingerprint": _thaw_payload(self.binding_fingerprint),
            "context_fingerprint": _thaw_payload(self.context_fingerprint),
            "allow_lossy_adapters": self.allow_lossy_adapters,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "PreparedInvocationSnapshot":
        """Load one exact Host-private snapshot after durable JSON parsing."""

        expected = {
            "schema",
            "contract_id",
            "contract_version",
            "operation_id",
            "normalized_payload",
            "request_digest",
            "timeout_ms",
            "idempotency_key",
            "binding_fingerprint",
            "context_fingerprint",
            "allow_lossy_adapters",
        }
        if set(value) != expected or value.get("schema") != "tobkiri.prepared-invocation.v1":
            raise ValueError("prepared invocation snapshot shape is invalid")
        return cls(
            contract_id=value["contract_id"],
            contract_version=value["contract_version"],
            operation_id=value["operation_id"],
            normalized_payload=value["normalized_payload"],
            request_digest=value["request_digest"],
            timeout_ms=value["timeout_ms"],
            idempotency_key=value["idempotency_key"],
            binding_fingerprint=value["binding_fingerprint"],
            context_fingerprint=value["context_fingerprint"],
            allow_lossy_adapters=value["allow_lossy_adapters"],
        )


class RequestBroker:
    """The only public Pack operation dispatch path in the v4 host package."""

    def __init__(
        self,
        *,
        catalog: OperationCatalog,
        adapters: AdapterPlanner,
        adapter_executor: AdapterExecutor,
        backends: BackendRegistry,
        materialization: MaterializationCoordinator,
        admission: RequestAdmissionPort,
        authority: AuthorityPort,
        audit: AuditPort,
        reconciliation: ReconciliationStore,
        production: bool = True,
        max_workers: int = 16,
    ) -> None:
        self._catalog = catalog
        self._adapters = adapters
        self._adapter_executor = adapter_executor
        self._backends = backends
        self._materialization = materialization
        self._admission = admission
        self._authority = authority
        self._audit = audit
        self._reconciliation = reconciliation
        # Host-owned test/conformance mode; never caller-controlled request data.
        self._production = production
        self._lifecycle_lock = threading.RLock()
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="tobkiri-v4-request",
        )

    def invoke(
        self,
        frame: InvocationFrame,
        context: RequestContext,
        *,
        effect_scope: Mapping[str, Any],
        allow_lossy_adapters: bool = False,
    ) -> Mapping[str, Any]:
        """Resolve, admit, materialize, authorize, dispatch, and validate."""
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("request broker is closed")
        prepared = self._prepare_invocation(
            frame,
            context,
            allow_lossy_adapters=allow_lossy_adapters,
        )
        return self._execute_prepared(
            prepared,
            context,
            effect_scope=effect_scope,
            monotonic_clock=time.monotonic,
            before_dispatch=None,
        )

    def invoke_prepared(
        self,
        snapshot: PreparedInvocationSnapshot,
        context: RequestContext,
        effect_scope: Mapping[str, Any],
        *,
        execute_not_after_wall: float,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        before_dispatch: Callable[[], None] | None = None,
    ) -> Mapping[str, Any]:
        """Execute one durable Host snapshot without re-running adapters.

        The snapshot's encrypted future store is responsible for durability;
        this Broker validates it against the currently pinned catalog and the
        original Host context before entering the same effect pipeline used by
        an immediate invocation.
        """

        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("request broker is closed")
        if not isinstance(snapshot, PreparedInvocationSnapshot):
            raise TypeError("prepared invocation snapshot is required")
        prepared = self._prepared_from_snapshot(snapshot, context)
        deadline = _fresh_prepared_deadline(
            timeout_ms=prepared.timeout_ms,
            execute_not_after_wall=execute_not_after_wall,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        return self._execute_prepared(
            PreparedInvocation(
                binding=prepared.binding,
                normalized_payload=prepared.normalized_payload,
                request_digest=prepared.request_digest,
                timeout_ms=prepared.timeout_ms,
                deadline_monotonic=deadline,
                idempotency_key=prepared.idempotency_key,
                binding_fingerprint=prepared.binding_fingerprint,
                context_fingerprint=prepared.context_fingerprint,
                allow_lossy_adapters=prepared.allow_lossy_adapters,
            ),
            context,
            effect_scope=effect_scope,
            monotonic_clock=monotonic_clock,
            before_dispatch=before_dispatch,
        )

    def _execute_prepared(
        self,
        prepared: PreparedInvocation,
        context: RequestContext,
        *,
        effect_scope: Mapping[str, Any],
        monotonic_clock: Callable[[], float],
        before_dispatch: Callable[[], None] | None,
    ) -> Mapping[str, Any]:
        """Run the shared static-auth through dispatch pipeline once."""

        binding = prepared.binding
        payload = _thaw_payload(prepared.normalized_payload)
        request_digest = prepared.request_digest
        deadline = prepared.deadline_monotonic
        try:
            self._authority.check_static_path(
                StaticAuthorityQuery(
                    context=context,
                    target_principal=binding.principal_ref,
                    request_digest=request_digest,
                    effect_scope=effect_scope,
                )
            )
        except Exception as exc:
            raise AuthorizationError("static authorization failed") from exc
        estimate = self._admission.estimate(context, binding, payload)
        remaining = deadline - monotonic_clock()
        if remaining <= 0:
            raise RequestTimedOutError("deadline expired before queue admission")
        scope = QueueScope(
            profile_id=context.profile_id,
            caller_id=context.caller_principal.value,
            pack_id=binding.artifact.pack_id,
            binding_id=(
                f"{binding.artifact.digest}:{binding.function.function_id}:"
                f"{binding.operation.operation_id}"
            ),
        )
        ticket = self._admission.acquire(
            scope,
            estimate,
            min(30.0, remaining),
        )
        lease_issued = False
        try:
            backend = self._backends.select(binding, production=self._production)
            workload_key = WorkloadInstanceKey(
                profile_id=context.profile_id,
                activation_id=context.activation_id,
                target_principal=binding.principal_ref,
                execution_domain_profile=binding.route.execution_domain_profile,
                security_epoch=context.security_epoch,
            )
            evidence = self._materialization.materialize(
                workload_key,
                backend,
                binding,
                ticket.reservation.reservation_id,
            )
            self._validate_evidence(binding, backend, evidence)
            lease = self._authorize(
                context,
                binding.principal_ref,
                request_digest,
                effect_scope,
                evidence,
            )
            lease_issued = True
            reservation = self._reserve_audit(
                context,
                binding,
                request_digest,
            )
            envelope = RequestEnvelope(
                context=context,
                target_principal=binding.principal_ref,
                target_domain=evidence.domain_ref,
                contract_id=binding.operation.contract_id,
                contract_version=binding.operation.contract_version,
                operation_id=binding.operation.operation_id,
                payload=payload,
                request_digest=request_digest,
                deadline_monotonic=deadline,
                lease=lease,
                idempotency_key=prepared.idempotency_key,
            )
            return self._dispatch(
                backend,
                envelope,
                binding,
                reservation,
                deadline,
                monotonic_clock,
                before_dispatch,
            )
        except Exception:
            if lease_issued:
                self._authority.fence_request(context.request_id)
            raise
        finally:
            self._admission.release(ticket)

    def prepare(
        self,
        frame: InvocationFrame,
        context: RequestContext,
        *,
        allow_lossy_adapters: bool = False,
    ) -> PreparedInvocation:
        """Resolve and normalize an invocation without exercising authority.

        This Host-only API intentionally stops before admission,
        materialization, audit, backend selection, or provider dispatch.  A
        future durable pending-effect Host primitive can consume this exact
        snapshot, but no Pack receives it directly.
        """

        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("request broker is closed")
        return self._prepare_invocation(
            frame,
            context,
            allow_lossy_adapters=allow_lossy_adapters,
        )

    def _prepare_invocation(
        self,
        frame: InvocationFrame,
        context: RequestContext,
        *,
        allow_lossy_adapters: bool,
    ) -> PreparedInvocation:
        """Build one immutable Host snapshot using the legacy digest formula."""

        binding = self._catalog.resolve(
            frame.contract_id,
            frame.operation_id,
            frame.version_range,
        )
        self._catalog.validate_input(binding, frame.payload)
        if binding.operation.idempotency in {"keyed", "replayable"} and not frame.idempotency_key:
            raise ResolutionError("operation requires an idempotency key")
        timeout_ms = frame.timeout_ms or binding.operation.timeout_default_ms
        timeout_ms = min(timeout_ms, binding.operation.timeout_hard_max_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        adapter_plan = self._adapters.plan(
            binding.route.adapter_ids,
            allow_lossy=allow_lossy_adapters,
        )
        payload = self._adapters.execute(
            adapter_plan,
            frame.payload,
            self._adapter_executor,
        )
        request_digest = _request_digest(
            context=context,
            binding=binding,
            payload=payload,
            idempotency_key=frame.idempotency_key,
        )
        return PreparedInvocation(
            binding=binding,
            normalized_payload=_freeze_payload(payload),
            request_digest=request_digest,
            timeout_ms=timeout_ms,
            deadline_monotonic=deadline,
            idempotency_key=frame.idempotency_key,
            binding_fingerprint=_binding_fingerprint(binding, adapter_plan),
            context_fingerprint=_context_fingerprint(context),
            allow_lossy_adapters=allow_lossy_adapters,
        )

    def _prepared_from_snapshot(
        self,
        snapshot: PreparedInvocationSnapshot,
        context: RequestContext,
    ) -> PreparedInvocation:
        """Revalidate an encrypted durable snapshot without adapter execution."""

        if _thaw_payload(snapshot.context_fingerprint) != _thaw_payload(
            _context_fingerprint(context)
        ):
            raise AuthorizationError("prepared invocation context changed")
        binding = self._catalog.resolve(
            snapshot.contract_id,
            snapshot.operation_id,
            f"=={snapshot.contract_version}",
        )
        if binding.operation.contract_version != snapshot.contract_version:
            raise ResolutionError("prepared invocation Contract version changed")
        if (
            binding.operation.idempotency in {"keyed", "replayable"}
            and not snapshot.idempotency_key
        ):
            raise AuthorizationError("prepared invocation idempotency key is missing")
        adapter_plan = self._adapters.plan(
            binding.route.adapter_ids,
            allow_lossy=snapshot.allow_lossy_adapters,
        )
        if _thaw_payload(snapshot.binding_fingerprint) != _thaw_payload(
            _binding_fingerprint(binding, adapter_plan)
        ):
            raise AuthorizationError("prepared invocation binding changed")
        if snapshot.timeout_ms > binding.operation.timeout_hard_max_ms:
            raise AuthorizationError("prepared invocation timeout exceeds binding")
        payload = _thaw_payload(snapshot.normalized_payload)
        self._catalog.validate_input(binding, payload)
        request_digest = _request_digest(
            context=context,
            binding=binding,
            payload=payload,
            idempotency_key=snapshot.idempotency_key,
        )
        if request_digest != snapshot.request_digest:
            raise AuthorizationError("prepared invocation request digest changed")
        return PreparedInvocation(
            binding=binding,
            normalized_payload=snapshot.normalized_payload,
            request_digest=snapshot.request_digest,
            timeout_ms=snapshot.timeout_ms,
            deadline_monotonic=0.0,
            idempotency_key=snapshot.idempotency_key,
            binding_fingerprint=snapshot.binding_fingerprint,
            context_fingerprint=snapshot.context_fingerprint,
            allow_lossy_adapters=snapshot.allow_lossy_adapters,
        )

    def _authorize(
        self,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        request_digest: str,
        effect_scope: Mapping[str, Any],
        evidence: RuntimeEvidence,
    ) -> OpaqueInvocationLease:
        try:
            return self._authority.authorize_and_issue_lease(
                FinalAuthorizationQuery(
                    context=context,
                    target_principal=target,
                    request_digest=request_digest,
                    effect_scope=effect_scope,
                    evidence=evidence,
                )
            )
        except Exception as exc:
            raise AuthorizationError("final authorization failed") from exc

    def _reserve_audit(
        self,
        context: RequestContext,
        binding: ResolvedOperationBinding,
        request_digest: str,
    ) -> OpaqueAuditReservation:
        try:
            return self._audit.reserve_effect(context, binding, request_digest)
        except Exception as exc:
            raise AuditUnavailableError("authoritative audit reservation failed") from exc

    def _dispatch(
        self,
        backend: ExecutionBackend,
        envelope: RequestEnvelope,
        binding: ResolvedOperationBinding,
        audit_reservation: OpaqueAuditReservation | None,
        deadline: float,
        monotonic_clock: Callable[[], float],
        before_dispatch: Callable[[], None] | None,
    ) -> Mapping[str, Any]:
        future: Future[object] | None = None
        try:
            self._authority.recheck_effect_boundary(
                envelope.context,
                envelope.target_principal,
                envelope.lease,
            )
            if audit_reservation is not None:
                self._audit.mark_dispatched(audit_reservation)
            if before_dispatch is not None:
                before_dispatch()
            operation_context = contextvars.copy_context()
            future = self._executor.submit(
                operation_context.run,
                backend.invoke,
                envelope,
            )
            remaining = max(0.0, deadline - monotonic_clock())
            raw = future.result(timeout=remaining)
            if not isinstance(raw, ProviderOutcome):
                raise TypeError("provider did not return ProviderOutcome")
            if raw.disposition in {
                EffectDisposition.ACCEPTED,
                EffectDisposition.UNKNOWN,
            }:
                self._record_audit_failure(audit_reservation, ambiguous=True)
                raise_ambiguous(
                    self._reconciliation,
                    request_id=envelope.context.request_id,
                    target_ref=envelope.target_principal.value,
                    idempotency_key=envelope.idempotency_key,
                    reconcile_operation=binding.operation.reconcile_operation,
                    receipt=raw.receipt,
                )
            payload = dict(raw.payload or {})
            self._catalog.validate_output(binding, payload)
            if audit_reservation is not None:
                self._audit.commit_effect(
                    audit_reservation,
                    _digest(payload),
                )
            return payload
        except TimeoutError as exc:
            cancellation_error: Exception | None = None
            try:
                backend.cancel(envelope.context.request_id)
            except Exception as cancel_exc:
                cancellation_error = cancel_exc
            ambiguous = binding.operation.effect_class is EffectClass.EXTERNAL_EFFECT
            self._record_audit_failure(audit_reservation, ambiguous=ambiguous)
            if ambiguous:
                raise_ambiguous(
                    self._reconciliation,
                    request_id=envelope.context.request_id,
                    target_ref=envelope.target_principal.value,
                    idempotency_key=envelope.idempotency_key,
                    reconcile_operation=binding.operation.reconcile_operation,
                )
            if cancellation_error is not None:
                raise ProviderExecutionError(
                    "local execution timed out and authenticated cancellation failed"
                ) from cancellation_error
            raise RequestTimedOutError("local execution exceeded deadline") from exc
        except AmbiguousEffectError:
            raise
        except Exception as exc:
            self._record_audit_failure(audit_reservation, ambiguous=False)
            raise ProviderExecutionError("provider execution failed") from exc
        finally:
            # A completed Future retains the provider exception and traceback until
            # its last reference is released.  A timed-out Future may still be
            # queued even after authenticated backend cancellation.  Always drop
            # the broker-side work item promptly; ``cancel`` is harmless once the
            # invocation has completed or started running.
            if future is not None:
                future.cancel()

    def _record_audit_failure(
        self,
        reservation: OpaqueAuditReservation | None,
        *,
        ambiguous: bool,
    ) -> None:
        if reservation is not None:
            self._audit.fail_effect(
                reservation,
                "ambiguous_effect" if ambiguous else "provider_failed",
                ambiguous,
            )

    @staticmethod
    def _validate_evidence(
        binding: ResolvedOperationBinding,
        backend: ExecutionBackend,
        evidence: RuntimeEvidence,
    ) -> None:
        mismatch = (
            evidence.executable_digest != binding.function.implementation_digest
            or evidence.backend_digest != backend.status.backend_digest
            or not evidence.authenticated_channel
            or not evidence.nonce_fresh
        )
        if backend.status.requires_platform_attestation:
            mismatch = mismatch or (
                evidence.platform != backend.status.platform
                or evidence.isolation_profile != binding.route.execution_domain_profile
                or evidence.attestation_digest is None
                or evidence.domain_lease_id is None
                or evidence.resource_reservation_id is None
            )
        if mismatch:
            backend.terminate(evidence.domain_ref.value)
            raise AuthorizationError("runtime evidence mismatch")

    def close(self) -> None:
        """Idempotently stop accepting work without waiting on hostile providers."""

        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "RequestBroker":
        """Return this open Broker for explicit scoped ownership."""

        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("request broker is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Close the Broker when its ownership scope exits."""

        del exc_type, exc_value, traceback
        self.close()


def _request_digest(
    *,
    context: RequestContext,
    binding: ResolvedOperationBinding,
    payload: Mapping[str, Any],
    idempotency_key: str | None,
) -> str:
    """Return the exact legacy request-digest binding for one normalized call."""

    return _digest(
        {
            "request_id": context.request_id,
            "profile_revision": context.profile_revision,
            "activation_digest": context.activation_digest,
            "plan_digest": context.plan_digest,
            "target": binding.principal_ref.value,
            "contract_id": binding.operation.contract_id,
            "contract_version": binding.operation.contract_version,
            "operation_id": binding.operation.operation_id,
            "payload": payload,
            "idempotency_key": idempotency_key,
        }
    )


def _binding_fingerprint(
    binding: ResolvedOperationBinding,
    adapter_plan: Sequence[Any],
) -> Mapping[str, Any]:
    """Capture every catalog identity a durable effect must revalidate."""

    route = binding.route
    return _snapshot_mapping(
        {
            "target_principal": binding.principal_ref.value,
            "artifact": {
                "pack_id": binding.artifact.pack_id,
                "version": binding.artifact.version,
                "digest": binding.artifact.digest,
                "publisher_lineage": binding.artifact.publisher_lineage,
                "catalog_digest": binding.artifact.catalog_digest,
            },
            "function": {
                "function_id": binding.function.function_id,
                "implementation_digest": binding.function.implementation_digest,
                "variant_id": binding.function.variant_id,
            },
            "operation": {
                "contract_id": binding.operation.contract_id,
                "contract_version": binding.operation.contract_version,
                "operation_id": binding.operation.operation_id,
                "revision_digest": binding.operation.revision_digest,
                "input_schema_digest": _digest(binding.operation.input_schema),
                "output_schema_digest": _digest(binding.operation.output_schema),
                "error_schema_digest": _digest(binding.operation.error_schema or {}),
                "progress_schema_digest": _digest(binding.operation.progress_schema or {}),
                "effect_class": binding.operation.effect_class.value,
                "timeout_default_ms": binding.operation.timeout_default_ms,
                "timeout_hard_max_ms": binding.operation.timeout_hard_max_ms,
                "idempotency": binding.operation.idempotency,
                "reconcile_operation": binding.operation.reconcile_operation,
            },
            "variant": {
                "variant_id": binding.variant.variant_id,
                "digest": binding.variant.digest,
                "execution_kind": binding.variant.execution_kind.value,
                "os": binding.variant.os,
                "architecture": binding.variant.architecture,
                "runtime_abi": binding.variant.runtime_abi,
                "backend": binding.variant.backend,
                "domain_kind": binding.variant.domain_kind,
            },
            "route": {
                "contract_id": route.contract_id,
                "operation_id": route.operation_id,
                "artifact_digest": route.artifact_digest,
                "function_id": route.function_id,
                "variant_id": route.variant_id,
                "execution_domain_profile": route.execution_domain_profile,
                "materialization_mode": route.materialization_mode,
                "target_principal": route.target_principal_ref.value,
                "adapter_ids": list(route.adapter_ids),
                "catalog_digest": route.catalog_digest,
                "platform": route.platform,
                "architecture": route.architecture,
                "runtime_abi": route.runtime_abi,
                "backend": route.backend,
                "execution_kind": route.execution_kind,
                "domain_kind": route.domain_kind,
            },
            "adapters": [
                {
                    "adapter_id": adapter.adapter_id,
                    "artifact_digest": adapter.artifact_digest,
                    "source_schema_digest": adapter.source_schema_digest,
                    "target_schema_digest": adapter.target_schema_digest,
                }
                for adapter in adapter_plan
            ],
        },
        "binding fingerprint",
    )


def _context_fingerprint(context: RequestContext) -> Mapping[str, Any]:
    """Capture the full Host context needed to resume an exact effect safely."""

    return _snapshot_mapping(
        {
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "caller_principal": context.caller_principal.value,
            "profile_id": context.profile_id,
            "activation_id": context.activation_id,
            "activation_digest": context.activation_digest,
            "plan_digest": context.plan_digest,
            "profile_revision": context.profile_revision,
            "security_epoch": context.security_epoch,
            "caller_session_id": context.caller_session_id,
            "caller_domain_id": context.caller_domain_id,
            "caller_boot_epoch": context.caller_boot_epoch,
            "target_domain_id": context.target_domain_id,
            "target_boot_epoch": context.target_boot_epoch,
            "target_backend_digest": context.target_backend_digest,
            "profile_authority_digest": context.profile_authority_digest,
            "fencing_token": context.fencing_token,
            "handle_namespace": context.handle_namespace,
            "delegation_chain": [item.value for item in context.delegation_chain],
        },
        "context fingerprint",
    )


def _fresh_prepared_deadline(
    *,
    timeout_ms: int,
    execute_not_after_wall: float,
    wall_clock: Callable[[], float],
    monotonic_clock: Callable[[], float],
) -> float:
    """Translate durable wall expiry into a fresh bounded monotonic deadline."""

    if (
        isinstance(execute_not_after_wall, bool)
        or not isinstance(execute_not_after_wall, (int, float))
        or not math.isfinite(float(execute_not_after_wall))
    ):
        raise ValueError("prepared invocation wall expiry is invalid")
    wall_now = float(wall_clock())
    monotonic_now = float(monotonic_clock())
    if not math.isfinite(wall_now) or not math.isfinite(monotonic_now):
        raise ValueError("Host clock is invalid")
    remaining_wall = float(execute_not_after_wall) - wall_now
    if remaining_wall <= 0:
        raise RequestTimedOutError("prepared invocation expired before execution")
    return monotonic_now + min(timeout_ms / 1000, remaining_wall)


def _snapshot_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """Deep-copy and strict-JSON-validate one bounded durable object."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        encoded = json.dumps(
            _thaw_payload(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8", errors="strict")
        parsed = strict_loads(encoded, max_bytes=10 * 1024 * 1024)
    except Exception as exc:
        raise ValueError(f"{label} must be bounded canonical JSON") from exc
    if not isinstance(parsed, Mapping):  # pragma: no cover - parsed object guard
        raise ValueError(f"{label} must be an object")
    return _freeze_payload(parsed)


def _snapshot_text(value: Any, label: str) -> str:
    """Validate one bounded text field in a Host-private snapshot."""

    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > 4_096
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _snapshot_digest(value: Any, label: str) -> str:
    """Validate the exact SHA-256 digest syntax used by request snapshots."""

    text = _snapshot_text(value, label)
    if (
        len(text) != 71
        or not text.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in text[7:])
    ):
        raise ValueError(f"{label} is invalid")
    return text


def _freeze_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Freeze a JSON-shaped adapter result before retaining it past prepare."""

    frozen = _freeze_value(payload)
    if not isinstance(frozen, Mapping):  # pragma: no cover - protocol guard
        raise TypeError("adapter result must be a mapping")
    return frozen


def _freeze_value(value: Any) -> Any:
    """Recursively remove mutable container references from a JSON value."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _thaw_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create the private mutable JSON copy supplied to an effect provider."""

    value = _thaw_value(payload)
    if not isinstance(value, dict):  # pragma: no cover - protocol guard
        raise TypeError("prepared payload must be a mapping")
    return value


def _thaw_value(value: Any) -> Any:
    """Copy a frozen JSON container without sharing caller-owned references."""

    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
