"""Production Gateway dispatch keeps local adapter drain inside root Stop."""

from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import select
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from core_runtime.authority.v4 import AuthorityStore, FunctionPrincipal
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.host_provider_backend_v4 import ExactHostProviderBackendV4
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from ecosystem.rumi_ai_gateway_pack.runtime.gateway import GENERATE_PROVIDER_CONTRACT
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from tests.test_live_production_v4_dispatch import (
    _CapturedBackend,
    _ai_bridge_request,
    _bundle_root,
    _digest,
)
from tests.test_local_provider_http_lifetime import _reply, _server, _watchers
from tests.test_local_provider_http_lifetime import owned_io as owned_io
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.broker import AdmissionTicket, RequestEnvelope
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.errors import RequestCancellationRequestedError
from tobkiri_host.operation_cancellation import (
    OwnedCancellationHandles,
    nested_cancellation_proof_for,
)


_GATEWAY_CONTRACT = "tobkiri.service.ai.generate.v1"
_GATEWAY_OPERATION = "rumi_ai_gateway_pack.ai-gateway.generate"
_ADAPTER_OPERATION = "rumi_provider_adapters_pack.provider-generate"


def test_root_stop_waits_for_real_gateway_adapter_future_and_broker_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owned_io: SimpleNamespace,
) -> None:
    """The real restricted Gateway client enrolls and drains its local socket."""

    user_data = tmp_path / "gateway-local-cancellation"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    active = capture_default_profile(
        confirmation=prepare_default_profile_confirmation()
    )
    conversation_binding = next(
        item for item in active.resolved.plan["bindings"]
        if item["contract_id"] == "conversation.turn.v1"
        and item["operation_id"] == "complete"
    )
    target = FunctionPrincipal.from_dict(conversation_binding["function_principal"])
    backend = _CapturedBackend(_digest("local-cancellation-bridge-backend"))
    backend.target_executable_digest = target.function_implementation_digest
    handles = OwnedCancellationHandles()
    root = SimpleNamespace()
    root_ready, root_exited, post_entered, peer_disconnected = (
        threading.Event() for _ in range(4)
    )
    adapter_io_closed, allow_adapter_exit, adapter_release_entered = (
        threading.Event() for _ in range(3)
    )
    allow_adapter_release, adapter_released, gateway_released, release_server = (
        threading.Event() for _ in range(4)
    )
    envelopes: dict[str, RequestEnvelope] = {}
    reservations: dict[str, str] = {}
    received: list[tuple[str, str | None]] = []
    caller_errors: list[BaseException] = []
    caller_results: list[Any] = []
    before_watchers = _watchers()

    def stall_headers(handler: BaseHTTPRequestHandler) -> None:
        received.append((handler.path, handler.headers.get("Authorization")))
        post_entered.set()
        while not release_server.is_set():
            readable, _, _ = select.select([handler.connection], [], [], 0.025)
            if readable:
                try:
                    closed = handler.connection.recv(1) == b""
                except ConnectionResetError:
                    closed = True
                if closed:
                    peer_disconnected.set()
                    return
        try:
            _reply(handler, json.dumps({
                "choices": [{"message": {"content": "late result"}}],
                "usage": {},
            }).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass

    original_host_invoke = ExactHostProviderBackendV4.invoke

    def observe_host_invoke(
        host: ExactHostProviderBackendV4, envelope: RequestEnvelope,
    ) -> ProviderOutcome:
        if envelope.contract_id in {_GATEWAY_CONTRACT, GENERATE_PROVIDER_CONTRACT}:
            envelopes[envelope.contract_id] = envelope
        if envelope.contract_id != GENERATE_PROVIDER_CONTRACT:
            return original_host_invoke(host, envelope)
        try:
            return original_host_invoke(host, envelope)
        finally:
            # Keep the actual Broker Future pending after HttpRequestLifetime
            # has closed/joined its IO, so root/Gateway exit ordering is exact.
            adapter_io_closed.set()
            assert allow_adapter_exit.wait(5), "adapter Future exit was not released"

    monkeypatch.setattr(ExactHostProviderBackendV4, "invoke", observe_host_invoke)

    def invoke_guest(envelope: RequestEnvelope) -> ProviderOutcome:
        owner = (envelope.context.caller_principal.value,
                 envelope.context.caller_session_id)
        execute = handles.bind(
            group=("fixture.defaultspack", "conversation"), role="execute",
            envelope=envelope, owner_principal=owner[0], owner_session=owner[1],
            guard=session.assert_current,
        )
        root.cancel = handles.bind(
            group=("fixture.defaultspack", "conversation"), role="stop",
            envelope=replace(envelope, cancellation_requested=threading.Event()),
            owner_principal=owner[0], owner_session=owner[1],
            guard=session.assert_current,
        )
        root.envelope = envelope
        try:
            with execute.track("turn"):
                root.proof = nested_cancellation_proof_for(envelope, *owner)
                assert root.proof is not None
                root_ready.set()
                # Replace only guest transport. The authenticated production
                # capability bridge and its saved-proof handoff stay real.
                response = backend.capability_bridge(
                    envelope,
                    _ai_bridge_request({
                        "profile_id": envelope.context.profile_id,
                        "messages": envelope.payload["messages"],
                        "requirements": {
                            "preferred_model_id": envelope.payload["model"],
                            "preferred_provider_instance_id":
                                "provider.compatibility.generate",
                        },
                        "deadline": int(time.time()) + 30,
                    }),
                    root.proof,
                )
                value = response["result"].get("value", {})
                return ProviderOutcome({"output": value.get("output", "")})
        finally:
            root_exited.set()

    monkeypatch.setattr(backend, "invoke", invoke_guest)

    with _server(stall_headers) as endpoint:
        ProviderRegistry("defaults", user_data_root=user_data).save({
            "provider_instance_id": "provider.production-test",
            "adapter_id": "local-openai-compatible",
            "credential_handle": None,
            "endpoint": endpoint,
            "enabled": True,
        }, expected_revision=0)
        session = capture_production_dispatch(
            active,
            bundle_root=_bundle_root(),
            ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
            authority_store=AuthorityStore(user_data / "authority" / "v4.sqlite3"),
            activation_snapshot_loader=defaultspack_activation_snapshot_loader,
            runtime_surface_factory=create_runtime_surface_services,
            backends=BackendRegistry((backend,)),
            target_backend_digests={target.principal_id: backend.status.backend_digest},
        )
        admission = session.broker._admission
        original_acquire, original_release = admission.acquire, admission.release

        def observe_acquire(
            scope: Any, estimate: Any, timeout: float,
        ) -> AdmissionTicket:
            ticket = original_acquire(scope, estimate, timeout)
            for operation in (_GATEWAY_OPERATION, _ADAPTER_OPERATION):
                if scope.binding_id.endswith(f":{operation}"):
                    reservations[operation] = ticket.reservation.reservation_id
            return ticket

        def gate_release(ticket: AdmissionTicket) -> None:
            reservation_id = ticket.reservation.reservation_id
            if reservation_id == reservations.get(_ADAPTER_OPERATION):
                adapter_release_entered.set()
                assert allow_adapter_release.wait(5), "adapter charge was not released"
            original_release(ticket)
            if reservation_id == reservations.get(_ADAPTER_OPERATION):
                adapter_released.set()
            if reservation_id == reservations.get(_GATEWAY_OPERATION):
                gateway_released.set()

        monkeypatch.setattr(admission, "acquire", observe_acquire)
        monkeypatch.setattr(admission, "release", gate_release)

        def invoke_conversation() -> None:
            try:
                caller_results.append(session.invoke(
                    "conversation.turn.v1", "complete", {
                        "_session_id": "session.panel.local-cancellation",
                        "messages": [{"role": "user", "content": "hello"}],
                        "model": "production-test/model",
                    },
                ))
            except BaseException as error:
                caller_errors.append(error)

        caller = threading.Thread(target=invoke_conversation)
        caller.start()
        try:
            assert root_ready.wait(5), "root cancellation scope was not entered"
            assert post_entered.wait(5), "real Gateway did not reach local HTTP"
            gateway = envelopes[_GATEWAY_CONTRACT]
            adapter = envelopes[GENERATE_PROVIDER_CONTRACT]
            assert (
                gateway.cancellation_requested
                is root.envelope.cancellation_requested
            )
            assert adapter.cancellation_requested is gateway.cancellation_requested
            assert adapter.deadline_monotonic <= gateway.deadline_monotonic
            with handles._lock:
                adapter_children = [
                    child for child in root.proof._children.values()
                    if child.envelope is adapter
                ]
                assert len(adapter_children) == 1
                adapter_child = adapter_children[0]
                assert adapter_child.future is not None
                assert not adapter_child.future.done()
            observation = root.cancel.request("turn")
            assert peer_disconnected.wait(2), "local TCP peer remained connected"
            assert adapter_io_closed.wait(2), "captured adapter IO did not close"
            assert root_exited.wait(2), "root did not leave after Stop"
            assert observation.completed.is_set()
            assert gateway_released.wait(2), "Gateway Broker resources did not drain"
            assert not observation.wait_for_verified_drain(time.monotonic() + 0.01)
            allow_adapter_exit.set()
            assert adapter_release_entered.wait(2), "adapter release was not reached"
            assert adapter_child.future.done()
            assert not observation.wait_for_verified_drain(time.monotonic() + 0.01)
            allow_adapter_release.set()
            assert adapter_released.wait(2), "adapter admission charge did not release"
            assert observation.wait_for_verified_drain(time.monotonic() + 0.5)
            assert not handles._records
        finally:
            if hasattr(root, "envelope"):
                root.envelope.cancellation_requested.set()
            allow_adapter_exit.set()
            allow_adapter_release.set()
            release_server.set()
            caller.join(5)
            session.close()
        assert not caller.is_alive()
        assert caller_results == []
        assert len(caller_errors) == 1
        assert isinstance(caller_errors[0], RequestCancellationRequestedError)
        assert received == [("/v1/chat/completions", None)]
        assert len(owned_io.sockets) == 1
        assert owned_io.sockets[0].fileno() == -1
        assert all(response.isclosed() for response in owned_io.responses)
        assert _watchers() == before_watchers
        assert not session.broker.has_undrained_requests()
