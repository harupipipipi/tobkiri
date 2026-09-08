"""Fixed guest continuation routing and cancellation, without provider authority."""

from copy import deepcopy
from typing import Any

import pytest

from tobkiri_host.saved_guest_dispatch import SavedGuestTurns, TARGETS
from tobkiri_protocol.canonical import canonical_digest


def request() -> dict:
    return {"request_id": "turn", "target_domain": "domain",
            "guest_artifact_identity": "sha256:" + "a" * 64,
            "request_digest": "sha256:" + "b" * 64, "deadline_monotonic": "90000",
            "payload": {"request": {"turn_id": "turn", "conversation_id": "conversation",
                                    "conversation_revision": 1, "content": "Hello"}}}


def initial(*args: Any) -> dict:
    args[3]()
    return {"kind": "tobkiri.packvm.continuation.intent.v2", "hop": 0,
            "target": dict(zip(("contract_id", "operation_id"), TARGETS[0])),
            "payload": {}, "state": {"hop": 0}}


def result(pending: dict) -> dict:
    wrapper = deepcopy(pending["host_bridge_request"])
    wrapper["kind"] = "tobkiri.packvm.bridge.host-result.v2"
    wrapper.pop("deadline_monotonic")
    frame = wrapper.pop("bridge_request")
    wrapper["bridge_result"] = {"kind": "tobkiri.packvm.continuation.result.v2",
                                "version": 2, "request_digest": canonical_digest(frame),
                                "outcome": {"status": "ok", "value": {}}}
    return wrapper


@pytest.mark.parametrize("field", ["request_id", "target_domain", "guest_artifact_identity",
                                  "binding_digest", "request_digest", "bridge_request_digest"])
def test_swapped_host_binding_fences_before_any_resume(field: str) -> None:
    ledger = SavedGuestTurns(clock=lambda: 10.0)
    pending = ledger.begin(request(), "sha256:" + "c" * 64, initial)
    reply = result(pending)
    reply[field] = "wrong"
    def forbidden(*args: Any) -> dict:
        pytest.fail("swapped result must not execute")
    with pytest.raises(ValueError, match="binding"):
        ledger.resume("domain", "turn", reply, forbidden)
    with pytest.raises(ValueError, match="unavailable"):
        ledger.resume("domain", "turn", result(pending), forbidden)


def test_cancel_during_initial_or_resumed_execution_cannot_register_next_action() -> None:
    for resumed in (False, True):
        ledger = SavedGuestTurns(clock=lambda: 10.0)
        def cancelled(*args: Any) -> dict:
            ledger.cancel("domain", "turn")
            with pytest.raises(ValueError, match="cancelled"):
                args[3]()
            return initial(args[0], args[1], args[2], lambda: None)
        with pytest.raises(ValueError, match="cancelled"):
            if resumed:
                pending = ledger.begin(request(), "sha256:" + "c" * 64, initial)
                ledger.resume("domain", "turn", result(pending), cancelled)
            else:
                ledger.begin(request(), "sha256:" + "c" * 64, cancelled)
        with pytest.raises(ValueError, match="unavailable"):
            ledger.begin(request(), "sha256:" + "c" * 64, initial)


def test_expiry_rejects_late_first_intent_without_renewing_budget() -> None:
    now = [10.0]
    ledger = SavedGuestTurns(clock=lambda: now[0])
    def delayed(*args: Any) -> dict:
        assert args[2] == 70
        value = initial(*args)
        now[0] = 70
        return value
    with pytest.raises(ValueError, match="expired"):
        ledger.begin(request(), "sha256:" + "c" * 64, delayed)


def test_wrong_initial_target_cannot_be_sealed_or_retried() -> None:
    ledger = SavedGuestTurns(clock=lambda: 10.0)
    def wrong(*args: Any) -> dict:
        value = initial(*args)
        value["target"]["operation_id"] = "foreign"
        return value
    with pytest.raises(ValueError, match="captured step"):
        ledger.begin(request(), "sha256:" + "c" * 64, wrong)
    with pytest.raises(ValueError, match="unavailable"):
        ledger.begin(request(), "sha256:" + "c" * 64, initial)
