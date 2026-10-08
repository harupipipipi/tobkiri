"""Bounded final numeric Host/HTTP/authority regressions, overlay fixtures only."""
from __future__ import annotations

from copy import deepcopy
import http.client
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from core_runtime.workflow_v4.data_codec import (
    compiled_digest, document_digest, outcome_digest, request_digest,
)
from tests.test_pack_api_server import (
    _Dispatch, _panel_session, FrontendContractBinding, HTTPContractTarget,
)
from tests import test_tobkiri_host_execution_integration as broker_support
from tobkiri_host.runtime import V4DispatchSession
from tobkiri_protocol.data_codec import (
    CodecError, decode_record, digest_payload, encode_record,
)
from tests.test_numeric_prepared_replay import broker_case as broker_case
from tests.test_numeric_workflow_paths import assert_float_bits


@pytest.mark.parametrize("value,swapped", [(0.75, 0.5), (-0.0, 0.0)], ids=["fraction", "signed-zero"])
def test_real_dispatch_session_numeric_digest_fence_before_broker(broker_case: Any, value: float, swapped: float) -> None:
    fixture, adapter = broker_case
    session = V4DispatchSession(
        broker=fixture.broker,
        context_for=lambda contract, operation: broker_support.context(),
        effect_scope_for=lambda contract, operation, payload: {},
        providers={}, profile_id="profile-1", plan_digest=broker_support.context().plan_digest,
        profile_revision="revision", activation_id="activation-1",
    )
    observed: list[float] = []
    original = fixture.backend.invoke

    def invoke(envelope: Any) -> Any:
        observed.append(envelope.payload["value"])
        return original(envelope)

    fixture.backend.invoke = invoke
    pinned = digest_payload({"value": value})
    with pytest.raises(ValueError, match="pinned digest"):
        session.invoke(broker_support.frame().contract_id, "send", {"value": swapped}, idempotency_key="numeric-swapped", expected_payload_digest=pinned)
    assert fixture.backend.invocations == 0
    assert adapter.calls == 0
    assert fixture.events == []
    assert session.invoke(broker_support.frame().contract_id, "send", {"value": value}, idempotency_key="numeric-original", expected_payload_digest=pinned) == {"delivered": True}
    assert fixture.backend.invocations == 1
    assert adapter.calls == 1
    assert_float_bits(observed[0], value / 2)
    event_count = len(fixture.events)
    with pytest.raises(ValueError, match="pinned digest"):
        session.invoke(broker_support.frame().contract_id, "send", {"value": swapped}, idempotency_key="numeric-swapped-again", expected_payload_digest=pinned)
    assert fixture.backend.invocations == 1
    assert adapter.calls == 1
    assert len(fixture.events) == event_count


@pytest.mark.parametrize("body", [
    b'{"value":0.375,"value":0.5}',
    b'{"value":NaN}',
    b'{"value":Infinity}',
    b'{"value":-Infinity}',
    b'{"value":9007199254740992}',
    b'{"value":"\\ud800"}',
    b'{"value":"\xff"}',
], ids=["duplicate", "nan", "infinity", "negative-infinity", "unsafe-integer", "unpaired-surrogate", "invalid-utf8"])
def test_raw_loopback_json_rejected_without_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path))
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    dispatch = _Dispatch()
    binding = FrontendContractBinding(
        method="POST", path="/api/test/numeric", presentation="identity",
        route_namespace="test", application_id="test",
        targets=(HTTPContractTarget(contribution_id="test.numeric", contract_id="example.generic.numeric.v1", operation_id="number.echo", provider_id="test.numeric", function_id="test.numeric", allowed_payload_keys=frozenset({"value"})),),
    )
    server = PackAPIServer(port=0, dispatch_session=dispatch, contract_bindings=(binding,), panel_auth_manager=PanelAuthManager(bootstrap_secret="verified-desktop"))
    monkeypatch.setattr(server, "_validate_contract_capture", lambda *args, **kwargs: None)
    server.start()
    try:
        cookie, csrf, origin = _panel_session(server)
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        connection.request("POST", "/api/contracts/test/" + quote("POST /api/test/numeric", safe=""), body=body, headers={"Content-Type": "application/json", "Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf, "X-Tobkiri-Request-ID": "31313131-3131-4313-8313-313131313131"})
        response = connection.getresponse()
        response_body = response.read()
        status = response.status
        connection.close()
        assert status == 400, (status, response_body)
        assert dispatch.calls == []
        assert not server._operation_journal.path.exists(), "invalid data cannot reserve durable work"
    finally:
        server.stop()


BASES = {
    "document": {"name": "numeric", "steps": [{"request": {"operation_id": "echo", "input": {"value": 0.375}}, "retry": {"max_attempts": 1}}]},
    "compiled": {"security_epoch": 7, "steps": [{"contract_request": {"operation_id": "echo", "input": {"value": 0.375}}, "timeout_ms": 1000}]},
    "request": {"contract_id": "example.numeric.v1", "input": {"value": 0.375}, "timeout_ms": 1000, "effect_ceiling": ["pure"]},
    "outcome": {"output": {"value": 0.375}, "error_code": None, "ambiguous_effect": False, "timed_out": False},
}
DIGESTS = {"document": document_digest, "compiled": compiled_digest, "request": request_digest, "outcome": outcome_digest}


@pytest.mark.parametrize("kind,path", [
    ("document", ("name",)),
    ("document", ("steps", 0, "retry", "max_attempts")),
    ("document", ("steps", 0, "request", "operation_id")),
    ("compiled", ("security_epoch",)),
    ("compiled", ("steps", 0, "timeout_ms")),
    ("compiled", ("steps", 0, "contract_request", "operation_id")),
    ("request", ("timeout_ms",)),
    ("request", ("contract_id",)),
    ("request", ("effect_ceiling", 0)),
    ("outcome", ("error_code",)),
    ("outcome", ("ambiguous_effect",)),
    ("outcome", ("timed_out",)),
])
def test_selective_workflow_digest_rejects_authority_metadata_floats(kind: str, path: tuple[Any, ...]) -> None:
    record = deepcopy(BASES[kind])
    helper = DIGESTS[kind]
    assert helper(record).startswith("sha256:")
    target = record
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = 0.375
    with pytest.raises(ValueError):
        helper(record)


@pytest.mark.parametrize("bad_payload", [b"not JSON", b'{"payload":["f","3fd8000000000000"]}'])
def test_authenticated_record_verification_precedes_policy_callback(bad_payload: bytes) -> None:
    key = b"numeric-test-seal-key" * 2
    called: list[Any] = []

    def policy(record: dict[str, Any]) -> tuple[Any, ...]:
        called.append(record)
        return (("payload",),)

    with pytest.raises(CodecError, match="authentication"):
        decode_record(bad_payload, "0" * 64, value_paths=policy, seal_key=key)
    assert called == []
    raw, seal = encode_record({"payload": {"value": 0.375}, "security_epoch": 7}, value_paths=(("payload",),), seal_key=key)
    restored = decode_record(raw, seal, value_paths=policy, seal_key=key)
    assert len(called) == 1
    assert_float_bits(restored["payload"]["value"], 0.375)
    assert restored["security_epoch"] == 7
