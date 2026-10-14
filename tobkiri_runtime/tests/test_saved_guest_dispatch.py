"""Fixed guest continuation routing and cancellation, without provider authority."""

from copy import deepcopy
from typing import Any, Callable

import pytest

from tobkiri_host.saved_guest_dispatch import SavedGuestTurns, TARGETS
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import MAX_SAVED_TURN_LIFETIME_SECONDS
from tobkiri_protocol.turn_progress_v1 import AI_STREAM


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
        assert args[2] == 10 + MAX_SAVED_TURN_LIFETIME_SECONDS
        value = initial(*args)
        now[0] = 10 + MAX_SAVED_TURN_LIFETIME_SECONDS
        return value
    with pytest.raises(ValueError, match="expired"):
        ledger.begin(request(), "sha256:" + "c" * 64, delayed)


@pytest.mark.parametrize("malformed", [None, [], "invalid"])
def test_nonobject_result_fences_the_pending_request(malformed: object) -> None:
    ledger = SavedGuestTurns(clock=lambda: 10.0)
    pending = ledger.begin(request(), "sha256:" + "c" * 64, initial)
    with pytest.raises(ValueError, match="binding"):
        ledger.resume("domain", "turn", malformed, initial)
    with pytest.raises(ValueError, match="unavailable"):
        ledger.resume("domain", "turn", result(pending), initial)


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


def test_saved_guest_retains_pending_strategy_roundtrip_for_shared_budget() -> None:
    """A generic strategy response can arrive after the former 60-second cap."""
    now = [10.0]
    ledger = SavedGuestTurns(clock=lambda: now[0])
    pending = ledger.begin(request(), "sha256:" + "c" * 64, initial)

    def next_action(*args: Any) -> dict:
        args[3]()
        return {
            "kind": "tobkiri.packvm.continuation.intent.v2",
            "hop": 1,
            "target": dict(zip(("contract_id", "operation_id"), TARGETS[1])),
            "payload": {},
            "state": {"hop": 1},
        }

    now[0] = 10 + 120
    resumed = ledger.resume("domain", "turn", result(pending), next_action)

    assert resumed["state"] == "pending"
    assert ledger.contains("domain", "turn") is True


@pytest.mark.parametrize("mode", ["auto", "manual"])
@pytest.mark.parametrize("replacement", [None, TARGETS[2], ("unknown", "unknown")])
def test_enabled_stream_plan_seals_only_acknowledged_ai_target(
    mode: str, replacement: tuple[str, str] | None,
) -> None:
    """Real guest ABI and ledger keep stream selection inside the exact seal."""
    from ecosystem.tobkiri_conversation_orchestration_pack.runtime import (
        saved_conversation,
    )

    transport = request()
    transport["payload"]["request"]["tool_selection"] = {"mode": mode}
    ledger = SavedGuestTurns(clock=lambda: 10.0)
    deadlines: list[float] = []

    def execute(
        _transport: dict[str, Any], arguments: dict[str, Any],
        deadline: float, guard: Callable[[], None],
    ) -> dict[str, Any]:
        guard()
        deadlines.append(deadline)
        intent = saved_conversation.tobkiri_packvm_invoke("saved_complete", arguments)
        if intent["hop"] == 2 and replacement is not None:
            intent["target"] = dict(
                zip(("contract_id", "operation_id"), replacement)
            )
        return intent

    pending = ledger.begin(transport, "sha256:" + "c" * 64, execute)
    read = result(pending)
    read["bridge_result"]["outcome"]["value"] = {
        "conversation": {
            "id": "conversation", "conversation_revision": 1,
            "model_reference": "model", "messages": [], "current_node_id": None,
        },
        "delivery_mode": "incremental",
    }
    pending = ledger.resume("domain", "turn", read, execute)
    append = result(pending)
    append["bridge_result"]["outcome"]["value"] = {
        "action": "message_appended",
        "message": pending["host_bridge_request"]["bridge_request"]["payload"][
            "message"
        ],
        "conversation_revision": 2,
    }
    if replacement is not None:
        with pytest.raises(ValueError, match="captured step"):
            ledger.resume("domain", "turn", append, execute)
        with pytest.raises(ValueError, match="unavailable"):
            ledger.resume("domain", "turn", append, execute)
    else:
        pending = ledger.resume("domain", "turn", append, execute)
        frame = pending["host_bridge_request"]["bridge_request"]
        assert frame["hop"] == 2
        assert frame["target"] == dict(
            zip(("contract_id", "operation_id"), AI_STREAM)
        )
        assert frame["state"]["ai_mode"] == "incremental"
    assert deadlines == [10 + MAX_SAVED_TURN_LIFETIME_SECONDS] * 3
