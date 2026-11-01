"""V2 framing cannot change independently captured execution expectations."""

from dataclasses import replace
import json

import pytest

from tobkiri_host.continuation_chain import ChainIdentity, ContinuationChains
from tobkiri_host.continuation_envelope import (
    seal_continuation_intent,
    validate_continuation_request,
    validate_continuation_result,
)
from tobkiri_protocol.canonical import canonical_digest, canonical_json

IDENTITY = ChainIdentity("domain", "request", "sha256:" + "a" * 64, 160.0)
TARGET = ("owned.contract.v1", "owned.operation")


@pytest.mark.parametrize("extra", [
    {"request_id": "injected"}, {"binding_digest": "sha256:" + "c" * 64},
    {"nonce": "c" * 48}, {"previous_digest": None}, {"deadline": 9999},
    {"profile_id": "other"}, {"approved": True}, {"hop": 1}, {"hop": False},
    {"target": {"contract_id": TARGET[0], "operation_id": "other"}},
    {"kind": "tobkiri.packvm.bridge.request.v1"}, {"state": []}, {"payload": []},
])
def test_intent_cannot_supply_root_framing_or_change_captured_step(extra: dict[str, object]) -> None:
    intent = {
        "kind": "tobkiri.packvm.continuation.intent.v2", "hop": 0,
        "target": {"contract_id": TARGET[0], "operation_id": TARGET[1]},
        "payload": {}, "state": {}, **extra,
    }
    with pytest.raises(ValueError):
        seal_continuation_intent(
            canonical_json(intent), identity=IDENTITY, hop=0,
            previous_digest=None, target=TARGET, nonce="b" * 48,
        )


def _frame(**extra: object) -> bytes:
    return canonical_json(
        {
            "kind": "tobkiri.packvm.continuation.request.v2",
            "version": 2,
            "request_id": IDENTITY.request_id,
            "binding_digest": IDENTITY.binding_digest,
            "hop": 0,
            "nonce": "b" * 48,
            "previous_digest": None,
            "target": {"contract_id": TARGET[0], "operation_id": TARGET[1]},
            "payload": {"message": "hello"},
            "state": {},
            **extra,
        }
    )


def _check(encoded: bytes):
    return validate_continuation_request(
        encoded, identity=IDENTITY, hop=0, previous_digest=None, target=TARGET
    )


def test_validated_bytes_enter_the_one_shot_chain() -> None:
    checked = _check(_frame())
    assert checked.digest == canonical_digest(json.loads(checked.frame))
    assert json.loads(checked.payload) == {"message": "hello"}
    chains = ContinuationChains(clock=lambda: 100.0)
    chains.start(checked.identity, frame=checked.frame, nonce=checked.nonce)
    permit = chains.take(IDENTITY, nonce=checked.nonce, result=b"owned-result")
    assert permit.frame == checked.frame
    chains.finish(permit)
    with pytest.raises(ValueError):
        chains.take(IDENTITY, nonce=checked.nonce, result=b"replay")


@pytest.mark.parametrize(
    "extra",
    [
        {"version": 1},
        {"version": True},
        {"kind": "tobkiri.packvm.bridge.request.v1"},
        {"request_id": "other"},
        {"binding_digest": "sha256:" + "c" * 64},
        {"hop": 1},
        {"hop": False},
        {"previous_digest": "sha256:" + "d" * 64},
        {"target": {"contract_id": TARGET[0], "operation_id": "other"}},
        {"nonce": "x" * 48},
        {"payload": []},
        {"state": []},
        {"approved": True},
        {"profile_id": "other"},
        {"deadline": 1000},
    ],
)
def test_request_cannot_widen_captured_binding(extra: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _check(_frame(**extra))


def test_predecessor_is_checked_independently() -> None:
    predecessor = "sha256:" + "d" * 64
    encoded = _frame(hop=1, previous_digest=predecessor)
    assert (
        validate_continuation_request(
            encoded, identity=IDENTITY, hop=1, previous_digest=predecessor, target=TARGET
        ).hop
        == 1
    )
    with pytest.raises(ValueError):
        validate_continuation_request(
            encoded, identity=IDENTITY, hop=1, previous_digest="sha256:" + "e" * 64, target=TARGET
        )
    with pytest.raises(ValueError):
        validate_continuation_request(
            _frame(),
            identity=replace(IDENTITY, binding_digest="sha256:" + "f" * 64),
            hop=0,
            previous_digest=None,
            target=TARGET,
        )


@pytest.mark.parametrize(
    "encoded",
    [
        b'{"version":2,"version":1}',
        b'{"value":NaN}',
        b"\xff",
        b"[" * 20 + b"0" + b"]" * 20,
        b" " * (64 * 1024 + 1),
    ],
)
def test_ambiguous_or_unbounded_json_is_rejected(encoded: bytes) -> None:
    with pytest.raises(ValueError):
        _check(encoded)


@pytest.mark.parametrize(
    "outcome",
    [
        {"status": "ok", "value": {"conversation_revision": 2}},
        {"status": "error", "error": {"code": "UNAVAILABLE", "message": "Unavailable"}},
    ],
)
def test_result_binds_to_request_and_next_predecessor(outcome: dict[str, object]) -> None:
    request = _check(_frame())
    reply = {
        "kind": "tobkiri.packvm.continuation.result.v2",
        "version": 2,
        "request_digest": request.digest,
        "outcome": outcome,
    }
    checked = validate_continuation_result(canonical_json(reply), request=request)
    next_request = _frame(hop=1, previous_digest=checked.digest, nonce="c" * 48)
    assert (
        validate_continuation_request(
            next_request, identity=IDENTITY, hop=1, previous_digest=checked.digest, target=TARGET
        ).hop
        == 1
    )
    reply["request_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError):
        validate_continuation_result(canonical_json(reply), request=request)


@pytest.mark.parametrize(
    "outcome",
    [
        {"status": "ok", "value": {}, "error": {}},
        {"status": "ok"},
        {"status": "ok", "value": []},
        {"status": "error", "value": {}},
        {"status": "pending", "value": {}},
        {"status": "error", "error": {"code": "private detail", "message": "x"}},
        {"status": "error", "error": {"code": "ERROR", "message": "x" * 513}},
    ],
)
def test_ambiguous_outcomes_are_not_success(outcome: dict[str, object]) -> None:
    request = _check(_frame())
    encoded = canonical_json(
        {
            "kind": "tobkiri.packvm.continuation.result.v2",
            "version": 2,
            "request_digest": request.digest,
            "outcome": outcome,
        }
    )
    with pytest.raises(ValueError):
        validate_continuation_result(encoded, request=request)
