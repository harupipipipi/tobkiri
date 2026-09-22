"""Canonical fail-closed Request execution and materialization path."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
import contextvars
from dataclasses import dataclass
import hashlib
import json
import threading
import time
from typing import Any, Mapping, Protocol

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
        binding = self._catalog.resolve(
            frame.contract_id,
            frame.operation_id,
            frame.version_range,
        )
        self._catalog.validate_input(binding, frame.payload)
        if binding.operation.idempotency in {"keyed", "replayable"}:
            if not frame.idempotency_key:
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
        request_digest = _digest(
            {
                "request_id": context.request_id,
                "activation_digest": context.activation_digest,
                "plan_digest": context.plan_digest,
                "target": binding.principal_ref.value,
                "contract_id": binding.operation.contract_id,
                "contract_version": binding.operation.contract_version,
                "operation_id": binding.operation.operation_id,
                "payload": payload,
                "idempotency_key": frame.idempotency_key,
            }
        )
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
        remaining = deadline - time.monotonic()
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
                idempotency_key=frame.idempotency_key,
            )
            return self._dispatch(
                backend,
                envelope,
                binding,
                reservation,
                deadline,
            )
        except Exception:
            if lease_issued:
                self._authority.fence_request(context.request_id)
            raise
        finally:
            self._admission.release(ticket)

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
            operation_context = contextvars.copy_context()
            future = self._executor.submit(
                operation_context.run,
                backend.invoke,
                envelope,
            )
            remaining = max(0.0, deadline - time.monotonic())
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
                or evidence.isolation_profile
                != binding.route.execution_domain_profile
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


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
