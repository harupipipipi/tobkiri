"""Exercise the Wasm Saved backend with the original guest ABI as a test adapter."""

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from ecosystem.tobkiri_conversation_orchestration_pack.runtime import saved_conversation
from tests.test_saved_bridge_callbacks import _setup
from tobkiri_host.backends import RequestScopedBackend
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.contracts import (
    OperationCatalog,
    OperationRoute,
    ResolvedOperationBinding,
)
from tobkiri_host.errors import (
    BackendUnavailableError,
    RequestCancellationRequestedError,
    SavedTurnRejectedError,
)
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    ExecutionKind,
    FunctionArtifact,
    OpaqueAuthorityRef,
    PackageKind,
    PackArtifact,
    RequestContext,
)
from tobkiri_host.native_saved_orchestration import NativeSavedOrchestrationBackend
from tobkiri_host.operation_cancellation import (
    OwnedCancellationHandles,
    nested_cancellation_proof_for,
)
from tobkiri_host.ports import OpaqueInvocationLease
from tobkiri_protocol.canonical import canonical_digest

_DOMAIN = "domain.saved"
_MANIFEST = canonical_digest("saved-wasm-manifest")


def _logical_binding() -> ResolvedOperationBinding:
    contract_id, operation_id = "conversation.saved-turn.v1", "saved_complete"
    pack_id = "tobkiri_conversation_orchestration_pack"
    operation = ContractOperation(
        contract_id,
        "1.0.0",
        canonical_digest("contract"),
        operation_id,
        {},
        {},
    )
    variant = ArtifactVariant(
        "saved-python",
        canonical_digest("variant"),
        ExecutionKind.PACK_VM,
        "any",
        "any",
        "pack-v4",
        "tobkiri.python-pack-v4",
    )
    function = FunctionArtifact(
        f"{pack_id}.saved",
        canonical_digest("implementation"),
        variant.variant_id,
        (operation,),
    )
    artifact = PackArtifact(
        pack_id,
        "1.0.0",
        canonical_digest("pack"),
        "publisher",
        PackageKind.NORMAL,
        (function,),
        (variant,),
    )
    route = OperationRoute(
        contract_id,
        operation_id,
        artifact.digest,
        function.function_id,
        variant.variant_id,
        "pack-vm",
        "on_demand",
        OpaqueAuthorityRef(canonical_digest("principal")),
    )
    return OperationCatalog((artifact,), (route,)).resolve(
        contract_id, operation_id, None
    )


class _Run:
    def __init__(self, tmp_path: Path, **overrides: Any) -> None:
        self.store, outer, self.calls, callbacks = _setup(tmp_path)
        self.steps: list[str] = []
        self.proofs: list[object] = []
        self.binding = _logical_binding()
        self.handles = OwnedCancellationHandles()

        def preflight(envelope: RequestEnvelope, proof: object) -> None:
            assert envelope is self.envelope
            self.proofs.append(proof)
            self.steps.append("preflight")
            callbacks.preflight(outer)

        def dispatch(envelope: RequestEnvelope, frame: Any, proof: object) -> dict:
            assert envelope is self.envelope
            self.proofs.append(proof)
            self.steps.append(frame.stage)
            return callbacks(outer, frame)

        def execute(envelope: RequestEnvelope, step_input: dict, guard: Any) -> dict:
            assert envelope is self.envelope
            guard()
            self.steps.append("wasm")
            return saved_conversation.tobkiri_packvm_invoke(
                self.binding.operation.operation_id,
                step_input,
            )

        def capture_current(envelope: RequestEnvelope) -> Callable[[], None]:
            assert envelope is self.envelope
            self.steps.append("capture")
            return lambda: None

        callbacks_by_name = {
            "execute": execute,
            "preflight": preflight,
            "dispatch": dispatch,
            "capture_current": capture_current,
        }
        callbacks_by_name.update(overrides)
        self.backend = NativeSavedOrchestrationBackend(
            self.binding,
            _DOMAIN,
            _MANIFEST,
            **callbacks_by_name,
        )
        self.envelope = RequestEnvelope(
            context=RequestContext(
                request_id=outer.context.request_id,
                trace_id="trace",
                caller_principal=OpaqueAuthorityRef("caller"),
                profile_id="defaults",
                activation_id="activation",
                activation_digest=canonical_digest("active"),
                plan_digest=canonical_digest("plan"),
                security_epoch=1,
                caller_session_id="session",
                caller_domain_id="source",
                caller_boot_epoch=1,
                target_domain_id=_DOMAIN,
                target_boot_epoch=1,
                target_backend_digest=self.backend.status.backend_digest,
                profile_authority_digest=canonical_digest("authority"),
                fencing_token=1,
                handle_namespace="test",
            ),
            target_principal=self.binding.principal_ref,
            target_domain=OpaqueAuthorityRef(_DOMAIN),
            contract_id=self.binding.operation.contract_id,
            contract_version=self.binding.operation.contract_version,
            operation_id=self.binding.operation.operation_id,
            payload=outer.payload,
            request_digest=canonical_digest(outer.payload),
            deadline_monotonic=time.monotonic() + 30,
            lease=OpaqueInvocationLease(b"lease"),
            idempotency_key=None,
            resource_reservation_id="reservation",
        )

    def track(self, envelope: RequestEnvelope | None = None) -> Any:
        return self.handles.bind(
            group=("saved", "turn"),
            role="execute",
            envelope=envelope or self.envelope,
            owner_principal="owner",
            owner_session="owner-session",
            guard=lambda: None,
        ).track("turn-1")


def test_native_backend_binds_the_wasm_and_checks_each_owner_ack(
    tmp_path: Path,
) -> None:
    run = _Run(tmp_path)
    backend = run.backend
    assert isinstance(backend, RequestScopedBackend)
    assert backend.status.execution_kind is ExecutionKind.WASM
    assert backend.status.backend_id != run.binding.variant.backend
    assert backend.status.ready_for_production
    evidence = backend.materialize(run.binding, "reservation")
    assert evidence.domain_ref == run.envelope.target_domain
    assert evidence.backend_digest == backend.status.backend_digest
    assert evidence.executable_digest == evidence.guest_artifact_identity == _MANIFEST
    assert evidence.guest_execution_boundary == "wasm-component"
    assert evidence.resource_reservation_id == "reservation"
    parent = replace(
        run.envelope,
        context=replace(run.envelope.context, request_id="parent"),
        contract_id="tobkiri.action.turn.saved.v1",
        operation_id="rumi_turn_runtime_pack.turn-saved",
    )
    with run.track(parent):
        proof = nested_cancellation_proof_for(parent, "owner", "owner-session")
        assert proof is not None
        child = proof.reserve_child(run.envelope)
        future: Future[object] = Future()
        future.set_running_or_notify_cancel()
        proof.bind_child(child, future)
        try:
            outcome = backend.invoke(run.envelope)
            with pytest.raises(BackendUnavailableError, match="not enrolled"):
                backend.invoke(run.envelope)
        finally:
            future.set_result(None)
            proof.record_resource_drain(future)
    assert outcome.payload["status"] == "ok"
    assert outcome.payload["message"]["content"] == "Hi"
    assert run.steps == [
        "capture",
        "preflight",
        "wasm",
        "read",
        "wasm",
        "user",
        "wasm",
        "ai",
        "wasm",
        "assistant",
        "wasm",
    ]
    assert run.proofs == [proof] * 5
    backend.release_materialization("reservation")
    with pytest.raises(BackendUnavailableError, match="unowned"):
        backend.cancel(run.envelope.context.request_id)


@pytest.mark.parametrize(
    ("error", "expected"),
    [(AuthorityDenied("denied"), True), (OSError("lost"), False)],
)
def test_preflight_rejection_settles_only_a_definitive_denial(
    tmp_path: Path,
    error: Exception,
    expected: bool,
) -> None:
    def reject(*_args: object) -> None:
        raise error

    run = _Run(tmp_path, preflight=reject)
    before = run.store.path.read_bytes()
    run.backend.materialize(run.binding, "reservation")
    with run.track(), pytest.raises(BackendUnavailableError) as refused:
        run.backend.invoke(run.envelope)
    assert isinstance(refused.value, SavedTurnRejectedError) is expected
    assert run.steps == ["capture"]
    assert run.store.path.read_bytes() == before
    run.backend.release_materialization("reservation")


def test_guest_cannot_forge_success_after_saved_append(tmp_path: Path) -> None:
    def forge(_envelope: RequestEnvelope, payload: dict, guard: Any) -> dict:
        guard()
        result = saved_conversation.tobkiri_packvm_invoke("saved_complete", payload)
        if result.get("status") == "ok":
            result["message"]["content"] = "forged"
        return result

    run = _Run(tmp_path, execute=forge)
    run.backend.materialize(run.binding, "reservation")
    with run.track(), pytest.raises(ValueError, match="terminal result differs"):
        run.backend.invoke(run.envelope)
    assert run.store.get("conversation-1")["messages"][-1]["content"] == "Hi"
    run.backend.release_materialization("reservation")


def test_lost_runtime_pin_fences_owner_dispatch(tmp_path: Path) -> None:
    generation = object()

    def capture(_envelope: RequestEnvelope) -> Callable[[], None]:
        original = generation

        def guard() -> None:
            if generation is not original:
                raise PermissionError("runtime registration changed")

        return guard

    def execute(_envelope: RequestEnvelope, payload: dict, guard: Any) -> dict:
        nonlocal generation
        guard()
        generation = object()
        return saved_conversation.tobkiri_packvm_invoke("saved_complete", payload)

    run = _Run(tmp_path, execute=execute, capture_current=capture)
    before = run.store.path.read_bytes()
    run.backend.materialize(run.binding, "reservation")
    with run.track(), pytest.raises(PermissionError, match="registration changed"):
        run.backend.invoke(run.envelope)
    assert run.steps == ["preflight"]
    assert run.store.path.read_bytes() == before
    run.backend.release_materialization("reservation")


@pytest.mark.parametrize("proof_kind", ["missing", "copied-envelope"])
def test_request_needs_exact_live_broker_proof(tmp_path: Path, proof_kind: str) -> None:
    run = _Run(tmp_path)
    run.backend.materialize(run.binding, "reservation")
    with pytest.raises(BackendUnavailableError, match="proof"):
        if proof_kind == "missing":
            run.backend.invoke(run.envelope)
        else:
            with run.track(replace(run.envelope)):
                run.backend.invoke(run.envelope)
    assert run.steps == []
    run.backend.release_materialization("reservation")


@pytest.mark.parametrize("stop", ["cancel", "release", "terminate"])
def test_inflight_stop_fences_the_next_step_and_holds_the_reservation(
    tmp_path: Path,
    stop: str,
) -> None:
    started = threading.Event()
    unblock = threading.Event()

    def execute(_envelope: RequestEnvelope, _payload: dict, guard: Any) -> dict:
        started.set()
        assert unblock.wait(5)
        guard()
        raise AssertionError("cancelled guest step must abort")

    run = _Run(tmp_path, execute=execute)
    backend = run.backend
    backend.materialize(run.binding, "reservation")
    with run.track(), ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(copy_context().run, backend.invoke, run.envelope)
        try:
            assert started.wait(5)
            if stop == "cancel":
                backend.cancel(run.envelope.context.request_id)
            elif stop == "terminate":
                backend.terminate(_DOMAIN)
                with pytest.raises(BackendUnavailableError, match="reservation"):
                    backend.materialize(run.binding, "future-reservation")
            with pytest.raises(BackendUnavailableError, match="not drained"):
                backend.release_materialization("reservation")
            assert run.envelope.cancellation_requested.is_set()
        finally:
            unblock.set()
        with pytest.raises(RequestCancellationRequestedError):
            task.result(timeout=5)
    assert run.steps == ["capture", "preflight"]
    backend.release_materialization("reservation")
    with pytest.raises(BackendUnavailableError, match="unowned"):
        backend.cancel(run.envelope.context.request_id)
