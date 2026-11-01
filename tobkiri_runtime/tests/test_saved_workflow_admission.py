"""Saved graph identity is persisted only by the atomic turn-claim owner."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from tobkiri_protocol.saved_workflow_admission import SavedWorkflowAdmission
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input
from tests.test_durable_turn_runtime import SAVED_PAYLOAD


def admission(run="run.one"):
    return SavedWorkflowAdmission(
        profile_id="defaults", turn_id="turn", conversation_id="conversation",
        input_digest=canonical_digest(validate_saved_conversation_input(SAVED_PAYLOAD)),
        run_id=run, definition_id="chat", revision_digest="sha256:" + "1" * 64,
        policy_digest="sha256:" + "2" * 64, plan_digest="sha256:" + "3" * 64,
        activation_id="activation", activation_digest="sha256:" + "4" * 64,
        security_epoch=1, workflow_principal_ids=("workflow.execute",),
        owner_principal_id="owner", owner_session_id="session",
    )


def test_saved_workflow_claim_persists_exact_binding_and_never_rebinds(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    first = store.claim_saved_workflow(SAVED_PAYLOAD, admission())
    assert first["claimed"] is True
    assert first["turn"]["saved_workflow"] == admission().to_mapping()
    reopened = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    assert reopened.claim_saved_workflow(SAVED_PAYLOAD, admission("another")) == {
        "claimed": False, "turn": first["turn"],
    }
    assert reopened.claim_saved(SAVED_PAYLOAD)["turn"] == first["turn"]


def test_legacy_claim_cannot_acquire_workflow_admission_later(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    initial = store.claim_saved(SAVED_PAYLOAD)
    later = store.claim_saved_workflow(SAVED_PAYLOAD, admission())
    assert later == {"claimed": False, "turn": initial["turn"]}
    assert "saved_workflow" not in later["turn"]


def test_competing_graph_claims_have_one_immutable_winner(tmp_path):
    barrier = Barrier(2)
    def claim(run):
        store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
        barrier.wait(timeout=10)
        return store.claim_saved_workflow(SAVED_PAYLOAD, admission(run))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["run.one", "run.two"]))
    assert sorted(value["claimed"] for value in results) == [False, True]
    winner = next(value for value in results if value["claimed"])
    assert all(value["turn"]["saved_workflow"] == winner["turn"]["saved_workflow"] for value in results)
    assert winner["turn"]["revision"] == 2


@pytest.mark.parametrize("field,value", [
    ("profile_id", "other"), ("turn_id", "other"),
    ("conversation_id", "other"), ("input_digest", "sha256:" + "f" * 64),
])
def test_wrong_input_binding_is_rejected_before_creating_state(tmp_path, field, value):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    with pytest.raises(PermissionError):
        store.claim_saved_workflow(SAVED_PAYLOAD, replace(admission(), **{field: value}))
    assert not store.path.exists()


def test_public_payload_cannot_supply_owner_admission(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    with pytest.raises(PermissionError):
        store.claim_saved_workflow(SAVED_PAYLOAD, admission().to_mapping())
    with pytest.raises((ValueError, PermissionError)):
        store.begin({"saved_workflow": admission().to_mapping()})
    assert not store.path.exists()


def test_binding_survives_ordinary_lifecycle_transitions(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    store.claim_saved_workflow(SAVED_PAYLOAD, admission())
    waiting = store.mutate("transition", "turn", expected_revision=2, status="waiting")
    assert waiting["saved_workflow"] == admission().to_mapping()
    assert store.get("turn")["saved_workflow"] == admission().to_mapping()


def test_admission_and_claim_roll_back_together(tmp_path, monkeypatch):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    store.begin_saved(SAVED_PAYLOAD)
    save = store._save
    def interrupted(connection, record):
        save(connection, record)
        if "saved_workflow" in record:
            raise RuntimeError("interrupted before commit")
    monkeypatch.setattr(store, "_save", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        store.claim_saved_workflow(SAVED_PAYLOAD, admission())
    current = DurableTurnRuntime("defaults", user_data_root=tmp_path).get("turn")
    assert current["status"] == "queued"
    assert current["revision"] == 1
    assert "saved_workflow" not in current


def test_lost_admission_reply_does_not_reexecute_claim(tmp_path, monkeypatch):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    claim = store._claim_saved_workflow
    def lose_reply(*args):
        claim(*args)
        raise RuntimeError("reply lost after commit")
    monkeypatch.setattr(store, "_claim_saved_workflow", lose_reply)
    with pytest.raises(RuntimeError, match="reply lost"):
        store.claim_saved_workflow(SAVED_PAYLOAD, admission())
    reopened = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    result = reopened.claim_saved_workflow(SAVED_PAYLOAD, admission("run.replacement"))
    assert result["claimed"] is False
    assert result["turn"]["saved_workflow"]["run_id"] == "run.one"


@pytest.mark.parametrize("workflow", [False, True])
def test_saved_claim_refuses_a_prior_cancellation_without_rewriting_state(tmp_path, workflow):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    store.begin_saved(SAVED_PAYLOAD)
    stopped = store.request_saved_cancellation("turn")
    result = (store.claim_saved_workflow(SAVED_PAYLOAD, admission()) if workflow
              else store.claim_saved(SAVED_PAYLOAD))
    assert result == {"claimed": False, "turn": stopped}
    assert store.get("turn") == stopped
    assert "saved_workflow" not in stopped
    assert not any(event["name"] == "turn.running" for event in stopped["events"])


@pytest.mark.parametrize("workflow", [False, True])
def test_cancellation_winning_after_claim_read_fences_both_claim_paths(tmp_path, monkeypatch, workflow):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    other = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    store.begin_saved(SAVED_PAYLOAD)
    original = store.begin_saved
    stopped = None
    def cancel_after_read(payload):
        nonlocal stopped
        value = original(payload)
        if stopped is None:
            stopped = other.request_saved_cancellation("turn")
        return value
    monkeypatch.setattr(store, "begin_saved", cancel_after_read)
    result = (store.claim_saved_workflow(SAVED_PAYLOAD, admission()) if workflow
              else store.claim_saved(SAVED_PAYLOAD))
    assert result == {"claimed": False, "turn": stopped}
    assert other.get("turn") == stopped
    assert "saved_workflow" not in result["turn"]
    assert not any(event["name"] == "turn.running" for event in result["turn"]["events"])


def test_workflow_and_legacy_claims_cannot_both_win(tmp_path):
    barrier = Barrier(2)
    def claim(workflow):
        store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
        barrier.wait(timeout=10)
        return (store.claim_saved_workflow(SAVED_PAYLOAD, admission()) if workflow
                else store.claim_saved(SAVED_PAYLOAD))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [False, True]))
    assert sorted(value["claimed"] for value in results) == [False, True]
    winner = next(value["turn"] for value in results if value["claimed"])
    assert all(value["turn"] == winner for value in results)
