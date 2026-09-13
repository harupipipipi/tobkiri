"""Production registration contract for the request-scoped Wasm backend."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from pathlib import Path
import sys
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
from tobkiri_host.errors import BackendUnavailableError, ProviderExecutionError
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.ports import OpaqueInvocationLease
from tobkiri_host.wasm_backend import WasmComponentBackend, production_wasm_backend
from tobkiri_protocol.canonical import canonical_digest


def _binding_and_bytes(binary: bytes | None = None):
    binary = binary or component(output='{"delivered":true}')
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


def _backend(binary: bytes | None = None, command: tuple[str, ...] | None = None):
    command = command or worker_command()
    backend = WasmComponentBackend(
        command,
        worker_command_digest=canonical_digest(list(command)),
        worker_runtime_digest=canonical_digest({"fixture": "wasmtime-runtime"}),
        memory_reservation_bytes=256 * 1024 * 1024,
    )
    binding, materialized = _binding_and_bytes(binary)
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


def test_concurrent_reservations_execute_in_separate_workers() -> None:
    backend, binding = _backend()
    workers = []
    for index in range(2):
        backend.materialize(binding, f"reservation-{index}")
        workers.append(backend._reservations[f"reservation-{index}"].worker)
    assert workers[0] is not workers[1]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                backend.invoke,
                _envelope(backend, binding, f"reservation-{index}", f"request-{index}"),
            )
            for index in range(2)
        ]
        assert [future.result() for future in futures] == [
            ProviderOutcome({"delivered": True}),
            ProviderOutcome({"delivered": True}),
        ]
    backend.release_materialization("reservation-0")
    backend.release_materialization("reservation-1")
    assert backend._reservations == {}


def test_authenticated_cancel_reaps_an_executing_worker() -> None:
    binary = component("(loop $forever br $forever) unreachable")
    backend, binding = _backend(binary)
    backend.materialize(binding, "reservation-1")
    envelope = _envelope(backend, binding, "reservation-1", "request-1")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(backend.invoke, envelope)
        deadline = time.monotonic() + 5
        while "request-1" not in backend._requests and time.monotonic() < deadline:
            time.sleep(0.001)
        assert backend._requests == {"request-1": "reservation-1"}
        envelope.cancellation_requested.set()
        backend.cancel("request-1")
        with pytest.raises(ProviderExecutionError, match="cancelled"):
            future.result(timeout=5)
    backend.release_materialization("reservation-1")
    assert backend._reservations == {}


def test_abnormal_worker_exit_is_reaped_before_release() -> None:
    command = (
        sys.executable,
        "-I",
        "-B",
        "-c",
        "import sys; sys.stdin.buffer.read(); sys.exit(7)",
    )
    backend, binding = _backend(command=command)
    backend.materialize(binding, "reservation-1")
    worker = backend._reservations["reservation-1"].worker
    with pytest.raises(ProviderExecutionError, match="unsuccessfully"):
        backend.invoke(_envelope(backend, binding, "reservation-1", "request-1"))
    assert worker._process is None
    backend.release_materialization("reservation-1")
    assert backend._reservations == {}


def test_tampered_materialized_component_is_rejected_before_attestation() -> None:
    command = worker_command()
    backend = WasmComponentBackend(
        command,
        worker_command_digest=canonical_digest(list(command)),
        worker_runtime_digest=canonical_digest({"fixture": "wasmtime-runtime"}),
        memory_reservation_bytes=256 * 1024 * 1024,
    )
    binding, materialized = _binding_and_bytes()
    implementation = materialized.files[0]
    implementation.content = implementation.content + b"tampered"
    backend.bind_artifact_resolver(lambda selected: materialized)
    backend.bind_target_domain_resolver(lambda selected: "target-domain")
    with pytest.raises(BackendUnavailableError, match="implementation digest"):
        backend.materialize(binding, "reservation-1")
    assert backend._reservations == {}


def test_released_reservation_gets_a_fresh_worker_for_the_next_request() -> None:
    backend, binding = _backend()
    backend.materialize(binding, "reservation-1")
    first_worker = backend._reservations["reservation-1"].worker
    assert backend.invoke(
        _envelope(backend, binding, "reservation-1", "request-1")
    ) == ProviderOutcome({"delivered": True})
    backend.release_materialization("reservation-1")

    backend.materialize(binding, "reservation-2")
    second_worker = backend._reservations["reservation-2"].worker
    assert second_worker is not first_worker
    assert backend.invoke(
        _envelope(backend, binding, "reservation-2", "request-2")
    ) == ProviderOutcome({"delivered": True})
    backend.release_materialization("reservation-2")
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
    assert backend._worker_command[1:4] == ("-I", "-B", "-c")
    bootstrap = backend._worker_command[4]
    assert "tobkiri_host.wasm_component" in bootstrap
    assert str(Path(__file__).resolve().parents[1]) in bootstrap
