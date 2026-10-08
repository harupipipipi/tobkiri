"""Real bridge finite append boundary with exact preference and legacy payloads."""

from copy import deepcopy
import pytest
from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.saved_bridge import SavedBridgeCallbacks
from ecosystem.defaultspack.runtime import saved_conversation as guest


@pytest.mark.parametrize("mode", [None, "ask", "agent", "full"])
def test_bridge_append_exact_capture_and_rebound(mode):
    request = {
        "turn_id": "turn:1",
        "conversation_id": "conversation:1",
        "conversation_revision": 1,
        "content": "hello",
    }
    if mode is not None:
        request["action_approval_mode"] = mode
    initial = guest.start(request)
    user = guest.resume(
        initial["state"],
        {
            "status": "ok",
            "value": {
                "conversation": {
                    "id": "conversation:1",
                    "conversation_revision": 1,
                    "model_reference": "model:1",
                    "messages": [],
                    "current_node_id": None,
                }
            },
        },
    )
    SavedBridgeCallbacks._check_append(request, user["payload"], 1)
    state = deepcopy(user["state"])
    state.update(stage="assistant", assistant="answer", assistant_finish_reason="stop")
    assistant = guest._intent(state)
    SavedBridgeCallbacks._check_append(request, assistant["payload"], 3)
    for payload, hop in ((user["payload"], 1), (assistant["payload"], 3)):
        rebound = deepcopy(payload)
        rebound["message"]["metadata"]["action_approval_mode"] = "ask" if mode != "ask" else "full"
        with pytest.raises(AuthorityDenied):
            SavedBridgeCallbacks._check_append(request, rebound, hop)
        approved = deepcopy(payload)
        approved["message"]["metadata"]["approved"] = True
        with pytest.raises(AuthorityDenied):
            SavedBridgeCallbacks._check_append(request, approved, hop)
        if mode is not None:
            missing = deepcopy(payload)
            del missing["message"]["metadata"]["action_approval_mode"]
            with pytest.raises(AuthorityDenied):
                SavedBridgeCallbacks._check_append(request, missing, hop)


@pytest.mark.parametrize("mode", [None, "ask", "agent", "full"])
def test_real_final_ack_requires_exact_captured_mode(mode):
    from ecosystem.rumi_turn_runtime_pack.runtime.saved import _completed_reference
    from tobkiri_protocol.canonical import canonical_digest

    request = {
        "turn_id": "turn:1",
        "conversation_id": "conversation:1",
        "conversation_revision": 1,
        "content": "hello",
    }
    if mode is not None:
        request["action_approval_mode"] = mode
    state = guest.start(request)["state"]
    state.update(stage="assistant", assistant="answer", assistant_finish_reason="stop")
    message = guest._message(state, "assistant")
    result = {
        "status": "ok",
        "turn_id": "turn:1",
        "conversation_id": "conversation:1",
        "conversation_revision": 3,
        "user_message_id": guest._message_id(request, "user"),
        "message": message,
    }
    reference = _completed_reference(request, result)
    assert reference["outcome_digest"] == canonical_digest(result)
    changed = deepcopy(result)
    changed["message"]["metadata"]["action_approval_mode"] = "full" if mode != "full" else "agent"
    with pytest.raises(ValueError):
        _completed_reference(request, changed)
