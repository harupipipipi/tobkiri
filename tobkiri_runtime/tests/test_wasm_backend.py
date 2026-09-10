"""Production registration contract for the request-scoped Wasm backend."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from types import SimpleNamespace
import threading
import time

import pytest

from tests.test_tobkiri_host_execution_integration import (
    FakeAdmission,
    FakeAudit,
    FakeAuthority,
    NoAdapters,
    context as base_context,
    frame,
    fixture_artifact,
    fixture_catalog,
)
from tests.test_wasm_component import component, worker_command
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.broker import RequestBroker, RequestEnvelope
from tobkiri_host.contracts import AdapterPlanner
from tobkiri_host.effects import InMemoryReconciliationStore, ProviderOutcome
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.ports import OpaqueInvocationLease
from tobkiri_host.wasm_backend import WasmComponentBackend, production_wasm_backend
from tobkiri_protocol.canonical import canonical_digest


def _binding_and_bytes():
    binary = component(output='{"delivered":true}')
    executable_digest = "sha256:" + hashlib.sha256(binary).hexdigest()
    original = fixture_artifact(timeout_ms=15_000)
    function = replace(
        original.functions[0],
        implementation_digest=executable_digest,
    )
    variant = replace(
        original.variants[0],
        digest=executable_digest,
        os="any",
        architecture="any",
        backend="tobkiri.wasmtime-pulley-v1",
    )
    artifact = replace(original, functions=(function,), variants=(variant,))
    binding = fixture_catalog(artifact).resolve_pinned(
        "io.tobkiri.notification.v1", "send"
    )
    materialized = SimpleNamespace(
        pack_id=artifact.pack_id,
        artifact_digest=artifact.digest,
        function_id=function.function_id,
        implementation_digest=executable_digest,
        implementation_path="runtime/component.wasm",
        files=(SimpleNamespace(path="runtime/component.wasm", content=binary),),
    )
    return binding, materialized


def _backend():
    command = worker_command()
    backend = WasmComponentBackend(
        command,
        worker_command_digest=canonical_digest(list(command)),
        worker_runtime_digest=canonical_digest({"fixture": "wasmtime-runtime"}),
        memory_reservation_bytes=256 * 1024 * 1024,
    )
    binding, materialized = _binding_and_bytes()
    backend.bind_artifact_resolver(lambda selected: materialized)
    backend.bind_target_domain_resolver(lambda selected: "target-domain")
    return backend, binding


def _envelope(backend, binding, reservation_id: str, request_id: str):
    context = replace(
        base_context(),
        request_id=request_id,
        target_backend_digest=backend.status.backend_digest,
    )
    return RequestEnvelope(
        context=context,
        target_principal=binding.principal_ref,
        target_domain=replace(binding.principal_ref, value="target-domain"),
        contract_id=binding.operation.contract_id,
        contract_version=binding.operation.contract_version,
        operation_id=binding.operation.operation_id,
        payload={"message": "hello"},
        request_digest="request-digest",
        deadline_monotonic=time.monotonic() + 15,
        lease=OpaqueInvocationLease(b"test-lease"),
        idempotency_key="test-key",
        resource_reservation_id=reservation_id,
        cancellation_requested=threading.Event(),
    )


def test_real_worker_is_bound_to_exact_reservation_and_reaped() -> None:
    backend, binding = _backend()
    evidence = backend.materialize(binding, "reservation-1")
    assert evidence.resource_reservation_id == "reservation-1"
    outcome = backend.invoke(_envelope(backend, binding, "reservation-1", "request-1"))
    assert outcome == ProviderOutcome({"delivered": True})
    backend.release_materialization("reservation-1")
    assert backend._reservations == {}


def test_real_worker_executes_through_broker_authority_and_audit() -> None:
    backend, binding = _backend()
    events: list[str] = []
    broker = RequestBroker(
        catalog=fixture_catalog(binding.artifact),
        adapters=AdapterPlanner(()),
        adapter_executor=NoAdapters(),
        backends=BackendRegistry((backend,)),
        materialization=MaterializationCoordinator(),
        admission=FakeAdmission(events),
        authority=FakeAuthority(events),
        audit=FakeAudit(events),
        reconciliation=InMemoryReconciliationStore(),
    )
    request_context = replace(
        base_context(),
        target_backend_digest=backend.status.backend_digest,
    )
    try:
        assert broker.invoke(frame(), request_context, effect_scope={}) == {
            "delivered": True
        }
    finally:
        broker.close()
    assert backend._reservations == {}
    assert events == [
        "authority_static",
        "static_admission",
        "queue_reserved",
        "authority_final",
        "audit_reserved",
        "authority_effect_recheck",
        "audit_dispatched",
        "audit_committed",
        "reservation_released",
    ]


def test_reservation_identity_cannot_select_another_worker() -> None:
    backend, binding = _backend()
    backend.materialize(binding, "reservation-1")
    backend.materialize(binding, "reservation-2")
    forged = _envelope(backend, binding, "reservation-missing", "request-1")
    with pytest.raises(BackendUnavailableError, match="does not own"):
        backend.invoke(forged)
    backend.release_materialization("reservation-1")
    backend.release_materialization("reservation-2")


def test_failed_worker_close_retains_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, binding = _backend()
    backend.materialize(binding, "reservation-1")
    owned = backend._reservations["reservation-1"]

    def fail_close() -> None:
        raise RuntimeError("unconfirmed")

    monkeypatch.setattr(owned.worker, "close", fail_close)
    with pytest.raises(RuntimeError, match="unconfirmed"):
        backend.release_materialization("reservation-1")
    assert backend._reservations["reservation-1"] is owned


def test_worker_command_digest_must_match_trusted_argv() -> None:
    with pytest.raises(ValueError, match="command digest mismatch"):
        WasmComponentBackend(
            worker_command(),
            worker_command_digest="sha256:" + "0" * 64,
            worker_runtime_digest=canonical_digest({"fixture": "wasmtime-runtime"}),
        )


def test_production_factory_captures_pinned_runtime_files() -> None:
    backend = production_wasm_backend()
    assert backend.status.ready_for_production
    assert backend.status.backend_id == "tobkiri.wasmtime-pulley-v1"
    assert backend._worker_command[1:] == (
        "-I",
        "-B",
        "-m",
        "tobkiri_host.wasm_component",
    )
