"""Real local sockets remain bound to the Broker lease and saved registry."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import socket
import threading
import time
from typing import Any

import pytest

from core_runtime.authority.v4 import AuditUnavailable
from core_runtime.global_contract_dispatch import GlobalContractClient
from core_runtime.local_provider_transport import (
    AuthorizedEnvelopeLocalProviderTransport,
    LocalProviderTransportDenied,
    local_provider_base,
)
from ecosystem.rumi_provider_adapters_pack.runtime.adapter import (
    REGISTRY_CONTRACT,
    REGISTRY_GENERATE_OPERATION,
    create_generate_operation,
)
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from tests.test_credential_broker_pack import _dispatched_envelope, _start_provider
from tests.test_mcp_sse_cancellation import _call, _server


def _port(tmp_path: Path, base: str):
    authority, envelope = _dispatched_envelope(tmp_path / "dispatch")
    envelope = replace(
        envelope,
        contract_id="tobkiri.service.ai.provider.generate.v1",
        deadline_monotonic=time.monotonic() + 60,
        payload={"provider_id": "local-test", "model_id": "fixture-1b"},
    )
    registry = ProviderRegistry(envelope.context.profile_id, user_data_root=tmp_path / "registry")
    registry.save({
        "provider_instance_id": "provider.local-test",
        "adapter_id": "openai-compatible",
        "endpoint": base,
        "enabled": True,
    }, expected_revision=0)
    calls = []

    class RegistrySession:
        profile_id = envelope.context.profile_id
        plan_digest = envelope.context.plan_digest

        def invoke(self, contract_id, operation_id, payload, **_kwargs):
            assert contract_id == REGISTRY_CONTRACT
            assert operation_id == REGISTRY_GENERATE_OPERATION
            assert payload == {"profile_id": self.profile_id}
            calls.append((contract_id, operation_id))
            return registry.snapshot()

        def provider_metadata(self, _contract_id):
            return ()

    session = RegistrySession()
    audits = []
    transport = AuthorizedEnvelopeLocalProviderTransport(
        envelope=envelope,
        provider_principal=authority.target,
        authority_store=authority.store,
        allowed_contract_ids=frozenset({REGISTRY_CONTRACT}),
        registry_reader=session.invoke,
        consumer_pack_id="fixture-local-provider",
        audit_sink=audits.append,
    )
    arguments = {
        "endpoint": base + "/chat/completions",
        "headers": {"Content-Type": "application/json"},
        "body": {"model": "fixture-1b", "messages": [], "temperature": 0.5},
        "provider_instance_id": "provider.local-test",
        "provider_scope": "ai.generate",
        "registry_contract_id": REGISTRY_CONTRACT,
        "registry_operation_id": REGISTRY_GENERATE_OPERATION,
        "deadline": time.time() + 60,
    }
    client = GlobalContractClient(
        session=session, allowed_contract_ids=frozenset({REGISTRY_CONTRACT}),
        consumer_pack_id="fixture-local-provider", host_local_provider_transport=transport,
    )
    return transport, arguments, authority, registry, client, calls, audits


@pytest.mark.parametrize("base", ["http://127.0.0.1:18080/v1", "http://[::1]:18080/v1"])
def test_local_base_accepts_only_canonical_literal_loopback(base: str) -> None:
    assert local_provider_base(base) == base


@pytest.mark.parametrize("base", [
    "http://localhost:18080/v1", "http://127.0.0.2:18080/v1",
    "http://192.168.1.1:18080/v1", "http://8.8.8.8:18080/v1",
    "https://127.0.0.1:18080/v1", "http://127.0.0.1/v1",
    "http://127.0.0.1:18080/admin", "http://127.0.0.1:18080/v1?token=secret",
    "http://127.0.0.1:18080/v1#secret", "http://user:secret@127.0.0.1:18080/v1",
    "http://[::1%25eth0]:18080/v1", "http://127.0.0.1:018080/v1",
])
def test_local_base_rejects_untrusted_or_secret_bearing_targets(base: str) -> None:
    assert not local_provider_base(base)


def test_shared_adapter_uses_registry_owned_local_endpoint_and_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual socket ignores caller endpoint fields and proxy settings."""
    body = b'{"choices":[{"message":{"content":"local answer"}}],"usage":{}}'
    server, handler = _start_provider(body=body)
    try:
        base = f"http://127.0.0.1:{server.server_port}/v1"
        owner, _, authority, _, client, calls, audits = _port(tmp_path, base)
        durable, _ = authority.store.inspect_lease_token(owner._envelope.lease.token.decode("ascii"))
        # Lease TTL bounds admission. Dispatched work keeps its captured request
        # deadline even when slow CPU inference outlives the consumed lease TTL.
        authority.clock.value = durable.expires_at + 1
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9090")
        monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:9090")
        monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: pytest.fail("DNS used"))
        result = create_generate_operation(client)("generate", {
            "profile_id": client.session.profile_id,
            "provider_id": "local-test", "model_id": "fixture-1b",
            "messages": [{"role": "user", "content": "Hello"}],
            "deadline": time.time() + 60,
            "endpoint": "http://untrusted.invalid/v1",
        })
        assert result["output"] == "local answer"
        assert len(calls) == 2  # Adapter selection and independent Host re-read.
        assert handler.received == [{
            "authorization": None, "api_key": None,
            "host": f"127.0.0.1:{server.server_port}", "path": "/v1/chat/completions",
        }]
        assert audits[-1]["status"] == "completed"
        with pytest.raises(LocalProviderTransportDenied):
            owner.post_json(**_port_arguments(base))
        assert len(handler.received) == 1
    finally:
        server.shutdown()
        server.server_close()


def _port_arguments(base: str) -> dict[str, Any]:
    return {
        "endpoint": base + "/chat/completions",
        "headers": {"Content-Type": "application/json"},
        "body": {}, "provider_instance_id": "provider.local-test",
        "provider_scope": "ai.generate", "registry_contract_id": REGISTRY_CONTRACT,
        "registry_operation_id": REGISTRY_GENERATE_OPERATION, "deadline": time.time() + 5,
    }


@pytest.mark.parametrize("change", [
    "endpoint", "scope", "connection", "headers", "credential", "disabled", "epoch", "body",
])
def test_registry_binding_or_authority_change_denies_before_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    owner, arguments, authority, registry, _, _, audits = _port(tmp_path, "http://127.0.0.1:18080/v1")
    if change == "endpoint":
        arguments["endpoint"] = "http://127.0.0.1:18081/v1/chat/completions"
    elif change == "scope":
        arguments["provider_scope"] = "ai.image"
    elif change == "connection":
        arguments["provider_instance_id"] = "provider.other-saved-connection"
    elif change == "headers":
        arguments["headers"]["Authorization"] = "Bearer caller-secret"
    elif change == "body":
        arguments["body"]["temperature"] = float("nan")
    elif change == "epoch":
        authority.store.advance_security_epoch("local provider test fence")
    else:
        record = registry.snapshot()["providers"][0]
        if change == "credential":
            # Registry permits opaque credentials only with HTTPS. A new saved
            # endpoint must never re-use the prior credential-free local call.
            record.update(endpoint="https://provider.example/v1", credential_handle="credential:secret")
        else:
            record["enabled"] = False
        registry.save(record, expected_revision=1)
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: pytest.fail("socket opened"))
    with pytest.raises(LocalProviderTransportDenied):
        owner.post_json(**arguments)
    assert audits[-1]["status"] == "denied"


@pytest.mark.parametrize("stage", ["started", "completed"])
def test_audit_failure_cannot_send_or_return_local_response(tmp_path: Path, stage: str) -> None:
    server, handler = _start_provider(body=b'{"answer":"local result"}')
    try:
        owner, arguments, *_ = _port(tmp_path, f"http://127.0.0.1:{server.server_port}/v1")

        def failing_audit(event):
            if event["status"] == stage:
                raise RuntimeError("audit is unavailable")

        owner._audit_sink = failing_audit
        with pytest.raises(LocalProviderTransportDenied):
            owner.post_json(**arguments)
        assert len(handler.received) == (0 if stage == "started" else 1)
    finally:
        server.shutdown()
        server.server_close()


def test_completion_audit_rechecks_revocation_inside_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, envelope = _dispatched_envelope(tmp_path / "dispatch")
    store = authority.store
    original_connection = store._connection
    revoked = False

    @contextmanager
    def race_connection():
        with original_connection() as connection:
            class Connection:
                def execute(self, sql, *args):
                    nonlocal revoked
                    if sql == "BEGIN IMMEDIATE" and not revoked:
                        revoked = True
                        store.revoke(
                            target_kind="function_principal", target_id=authority.target.principal_id,
                            reason="revoked before transport audit transaction",
                        )
                    return connection.execute(sql, *args)

                def __getattr__(self, name):
                    return getattr(connection, name)

            yield Connection()

    monkeypatch.setattr(store, "_connection", race_connection)
    with pytest.raises(AuditUnavailable):
        store.record_provider_transport(
            envelope.lease.token.decode("ascii"), event_state="completed",
            provider_instance_id="provider.local-test", provider_scope="ai.generate",
            endpoint_origin="http://127.0.0.1:18080",
        )
    assert revoked
    assert not any(event["event_type"] == "local_provider_transport" for event in store.audit_events())


def test_local_redirect_cannot_reach_another_origin(tmp_path: Path) -> None:
    destination, destination_handler = _start_provider()
    target = f"http://127.0.0.1:{destination.server_port}/v1/chat/completions"
    server, handler = _start_provider(location=target)
    try:
        owner, arguments, *_ = _port(tmp_path, f"http://127.0.0.1:{server.server_port}/v1")
        with pytest.raises(LocalProviderTransportDenied):
            owner.post_json(**arguments)
        assert len(handler.received) == 1
        assert destination_handler.received == []
    finally:
        server.shutdown()
        server.server_close()
        destination.shutdown()
        destination.server_close()


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("ending", ["cancel", "deadline", "revoke", "epoch"])
def test_local_blocked_io_observes_lease_fence_without_replay(
    tmp_path: Path, phase: str, ending: str,
) -> None:
    entered, release, server_released = threading.Event(), threading.Event(), threading.Event()
    received = []

    def stall(handler):
        received.append(handler.path)
        if phase == "body":
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.write(b"{")
            handler.wfile.flush()
        entered.set()
        release.wait(30)
        server_released.set()

    with _server(stall) as origin:
        owner, arguments, authority, *_ = _port(tmp_path, origin + "/v1")
        if ending == "deadline":
            owner._envelope = replace(owner._envelope, deadline_monotonic=time.monotonic() + 10)
        worker, done, errors = _call(lambda: owner.post_json(**arguments))
        try:
            assert entered.wait(10), "local request did not reach the controlled blocking point"
            if ending == "cancel":
                owner._envelope.cancellation_requested.set()
            elif ending == "deadline":
                # Keep the original captured request budget. No later read may
                # refresh it; completion has two seconds after that deadline.
                pass
            elif ending == "epoch":
                authority.store.advance_security_epoch("local provider IO fence")
            else:
                authority.store.revoke(
                    target_kind="function_principal", target_id=authority.target.principal_id,
                    reason="local provider IO revoke",
                )
            completion_budget = (
                max(0, owner._envelope.deadline_monotonic - time.monotonic()) + 2
                if ending == "deadline" else 2
            )
            assert done.wait(completion_budget)
            assert not server_released.is_set(), "peer release unblocked the request"
            assert len(errors) == 1 and isinstance(errors[0], LocalProviderTransportDenied)
            with pytest.raises(LocalProviderTransportDenied):
                owner.post_json(**arguments)
        finally:
            release.set()
            worker.join(3)
    assert received == ["/v1/chat/completions"]
    assert not any(thread.name == "tobkiri-http-deadline" for thread in threading.enumerate())
