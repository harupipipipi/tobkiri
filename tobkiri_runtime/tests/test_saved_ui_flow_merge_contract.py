"""UI owner context and Flow admission must retain their combined invariants."""
from copy import deepcopy
from dataclasses import replace

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.saved_bridge import _messages
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict
from ecosystem.tobkiri_conversation_orchestration_pack.runtime.messages import (
    tobkiri_packvm_invoke,
)
from tests.test_durable_turn_runtime import SAVED_PAYLOAD
from tests.test_saved_workflow_admission import admission
from tests.test_saved_workspace_context import resolve, conversation
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.conversation_lifecycle import (
    LIFECYCLE_VERSION, active_task_gap_context, task_gap_prompt,
)
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input
from tobkiri_protocol.saved_messages import SavedMessageContextError, build_saved_model_messages


def test_host_workspace_capability_survives_shared_assembly_but_not_wire_copy():
    source = {**conversation(), "messages": [], "current_node_id": None}
    captured = resolve(source)
    assert _messages(captured) == []
    with pytest.raises(AuthorityDenied, match="workspace resolution"):
        _messages(dict(captured))
    with pytest.raises(SavedMessageContextError, match="workspace resolution"):
        tobkiri_packvm_invoke("messages_build", {
            "conversation": dict(captured), "system_prompt": None,
        })


def test_shared_assembly_keeps_owner_bound_task_gap_after_system_prompt():
    source = {
        "system_prompt_id": "system", "current_node_id": "user",
        "messages": [{"id": "user", "parent_id": None, "role": "user",
                      "status": "complete", "content": "Follow-up"}],
        "lifecycle": {
            "version": LIFECYCLE_VERSION, "state": "running",
            "resumed_completed_at_ms": 1000,
            "active_user_received_at_ms": 4000000,
            "active_user_message_id": "user", "completion_message_id": "prior",
        },
    }
    before = deepcopy(source)
    prompt = {"prompt_id": "system", "body": "Instructions"}
    expected = [
        {"role": "system", "content": "Instructions"},
        {"role": "system", "content": task_gap_prompt(active_task_gap_context(source))},
        {"role": "user", "content": "Follow-up"},
    ]
    assert build_saved_model_messages(source, system_prompt=prompt) == expected
    assert _messages(source, system_prompt=prompt, flatten_text_blocks=True) == expected
    assert source == before
    source["lifecycle"]["active_user_message_id"] = "absent"
    assert build_saved_model_messages(source, system_prompt=prompt) == [expected[0], expected[2]]


@pytest.mark.parametrize("workflow", [False, True])
def test_both_claim_paths_preserve_guidance_arriving_before_locked_read(tmp_path, monkeypatch, workflow):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    other = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    original = store.begin_saved
    updated = None

    def steer_after_read(payload):
        nonlocal updated
        snapshot = original(payload)
        if updated is None:
            updated = other.mutate(
                "steer", "turn", expected_revision=snapshot["revision"],
                guidance={"prompt": "Latest guidance"}, guidance_id="guidance.merge",
            )
        return snapshot

    monkeypatch.setattr(store, "begin_saved", steer_after_read)
    result = (store.claim_saved_workflow(SAVED_PAYLOAD, admission()) if workflow
              else store.claim_saved(SAVED_PAYLOAD))
    assert result["claimed"] is True
    assert result["turn"]["guidance"] == updated["guidance"]
    assert result["turn"]["revision"] == updated["revision"] + 1


@pytest.mark.parametrize("workflow", [False, True])
def test_both_claim_paths_refuse_another_active_conversation_turn(tmp_path, workflow):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    store.claim_saved(SAVED_PAYLOAD)
    candidate = deepcopy(SAVED_PAYLOAD)
    candidate["request"]["turn_id"] = "turn.second"
    captured = replace(
        admission(), turn_id="turn.second",
        input_digest=canonical_digest(validate_saved_conversation_input(candidate)),
    )
    with pytest.raises(TurnConflict, match="already has an active turn"):
        if workflow:
            store.claim_saved_workflow(candidate, captured)
        else:
            store.claim_saved(candidate)
    pending = store.get("turn.second")
    # Idle admission rejects before creating a second durable owner row.
    assert pending is None
