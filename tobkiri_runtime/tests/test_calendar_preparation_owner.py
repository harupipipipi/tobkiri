"""Calendar model preparation and ordinary saved admission share one owner fence."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from copy import deepcopy
import pytest
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict


def source():
    return {
        "profile_id": "defaults",
        "conversation_id": "target",
        "message": "Scheduled message",
        "model": "chosen-model",
    }


def initial(revision=2):
    return {
        "request": {
            "turn_id": "calendar-one",
            "conversation_id": "target",
            "conversation_revision": revision,
            "content": "Scheduled message",
        }
    }


def reserve(store, task=None, revision=1):
    return store.reserve_calendar_preparation(
        occurrence_key="occurrence",
        source=source() if task is None else task,
        turn_id="calendar-one",
        conversation_id="target",
        conversation_revision=revision,
    )


def test_busy_target_precheck_prevents_model_write(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    store.begin_saved(
        {
            "request": {
                "turn_id": "ui",
                "conversation_id": "target",
                "conversation_revision": 1,
                "content": "User",
            }
        }
    )
    writes = []
    with pytest.raises(TurnConflict):
        reserve(store)
        writes.append("model-write")
    assert writes == []
    assert store.get("calendar-one") is None


def test_ui_and_preparation_compete_atomically_one_winner_no_busy_model_write(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    gate = Barrier(2)
    writes = []

    def scheduled():
        gate.wait()
        try:
            reserve(store)
        except TurnConflict:
            return "rejected"
        writes.append("safe-model-write")
        return "scheduled"

    def ui():
        gate.wait()
        try:
            store.begin_saved(
                {
                    "request": {
                        "turn_id": "ui",
                        "conversation_id": "target",
                        "conversation_revision": 1,
                        "content": "User",
                    }
                }
            )
        except TurnConflict:
            return "rejected"
        return "ui"

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = [pool.submit(scheduled), pool.submit(ui)]
        values = [future.result() for future in result]
    assert values.count("rejected") == 1
    assert len(writes) == ("scheduled" in values)
    assert len(store.list(conversation_id="target")) == 1


def test_reservation_crash_recovery_then_bind_normal_saved_claim(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    first = reserve(store)
    recovered = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    assert reserve(recovered, revision=2) == first
    payload = recovered.bind_calendar_preparation(
        occurrence_key="occurrence", source=source(), payload=initial()
    )
    receipt = recovered.recover_calendar_preparation(
        turn_id="calendar-one", occurrence_key="occurrence", source=source()
    )
    assert receipt["payload"] == payload and receipt["status"] == "bound"
    assert recovered.claim_saved(payload)["claimed"] is True
    assert recovered.claim_saved(payload)["claimed"] is False
    assert (
        recovered.release_calendar_preparation(
            turn_id="calendar-one", occurrence_key="occurrence", source=source()
        )["status"]
        == "reconciliation_required"
    )
    assert recovered.get("calendar-one")["status"] == "running"


def test_bind_rejects_source_context_authority_or_digest_rebinding(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    reserve(store)
    changed = deepcopy(initial())
    changed["request"]["content"] = "Other"
    with pytest.raises(PermissionError):
        store.bind_calendar_preparation(
            occurrence_key="occurrence", source=source(), payload=changed
        )
    store.bind_calendar_preparation(
        occurrence_key="occurrence", source=source(), payload=initial()
    )
    with pytest.raises(TurnConflict):
        store.bind_calendar_preparation(
            occurrence_key="occurrence", source=source(), payload=initial(3)
        )
    with pytest.raises(TurnConflict):
        reserve(store, {**source(), "model": "foreign"})


def test_preclaim_cleanup_releases_ui_and_never_cancels_started(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    reserve(store)
    with pytest.raises(TurnConflict):
        store.mutate(
            "steer", "calendar-one", expected_revision=1, guidance={"prompt": "Human"}
        )
    with pytest.raises(TurnConflict):
        store.mutate(
            "transition", "calendar-one", expected_revision=1, status="running"
        )
    assert (
        store.release_calendar_preparation(
            turn_id="calendar-one", occurrence_key="occurrence", source=source()
        )["status"]
        == "released"
    )
    store.begin_saved(
        {
            "request": {
                "turn_id": "ui",
                "conversation_id": "target",
                "conversation_revision": 1,
                "content": "User",
            }
        }
    )
    assert store.get("ui")["status"] == "queued"


def test_new_destination_zero_revision_reserves_before_create(tmp_path):
    from tobkiri_protocol.canonical import canonical_digest

    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    task = {**source(), "conversation_id": None}
    target = "calendar:" + canonical_digest(["defaults", "occurrence"]).removeprefix(
        "sha256:"
    )
    receipt = store.reserve_calendar_preparation(
        occurrence_key="occurrence",
        source=task,
        turn_id="calendar-one",
        conversation_id=target,
        conversation_revision=0,
    )
    assert receipt["original_revision"] == 0
    payload = initial(1)
    payload["request"]["conversation_id"] = target
    store.bind_calendar_preparation(
        occurrence_key="occurrence", source=task, payload=payload
    )
    assert store.claim_saved(payload)["claimed"] is True


def test_bound_calendar_receives_only_normal_immutable_host_projection(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    task = {
        **source(),
        "chat_references": [{"kind": "chat", "profile_id": "defaults", "id": "ref"}],
    }
    reserve(store, task)
    lean = initial()
    lean["request"]["chat_references"] = task["chat_references"]
    store.bind_calendar_preparation(
        occurrence_key="occurrence", source=task, payload=lean
    )
    captured = deepcopy(lean)
    captured["request"]["resolved_chat_references"] = {
        "kind": "tobkiri.chat.reference.snapshot.v1",
        "profile_id": "defaults",
        "store_revision": 1,
        "project_revision": 1,
        "snapshot_time": 1,
        "expires_at": 600001,
        "next_cursor": None,
        "truncated": False,
        "references": [
            {
                "kind": "chat",
                "id": "ref",
                "label": "Ref",
                "conversation_ids": ["ref"],
                "member_count": 1,
                "membership_complete": True,
                "snapshot_digest": "sha256:" + "a" * 64,
            }
        ],
    }
    assert store.bind_saved_input(lean, captured) == captured
    assert store.saved_input(lean) == captured
    assert store.claim_saved(captured)["claimed"] is True


def test_release_without_reservation_is_idempotent_and_has_no_turn_effect(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    for _ in range(2):
        assert (
            store.release_calendar_preparation(
                turn_id="calendar-one", occurrence_key="occurrence", source=source()
            )["status"]
            == "not_reserved"
        )
    assert store.list(conversation_id="target") == []


def test_calendar_preclaim_readiness_failure_then_same_input_executes_once(tmp_path):
    from tests.test_saved_turn_guidance_owner import _OwnerSession, _client
    from ecosystem.rumi_turn_runtime_pack.runtime.saved import (
        execute_saved_turn,
        RECEIPT_CONTRACT,
    )

    session = _OwnerSession(tmp_path)
    store = session.turns
    task = {
        "profile_id": "defaults",
        "conversation_id": "conversation-1",
        "message": "Scheduled message",
        "model": "model-profile-1",
    }
    store.reserve_calendar_preparation(
        occurrence_key="occurrence",
        source=task,
        turn_id="calendar-one",
        conversation_id="conversation-1",
        conversation_revision=1,
    )
    payload = {
        "request": {
            "turn_id": "calendar-one",
            "conversation_id": "conversation-1",
            "conversation_revision": 1,
            "content": "Scheduled message",
        }
    }
    store.bind_calendar_preparation(
        occurrence_key="occurrence", source=task, payload=payload
    )
    original = session.invoke
    failures = [True]

    def transient_readiness(contract, operation, request, **options):
        if (
            contract == RECEIPT_CONTRACT
            and request.get("operation") == "get"
            and failures
        ):
            failures.pop()
            raise TimeoutError("owner context temporarily unavailable")
        return original(contract, operation, request, **options)

    session.invoke = transient_readiness
    with pytest.raises(TimeoutError):
        execute_saved_turn(store, payload, client=_client(session), guard=lambda: None)
    assert store.get("calendar-one")["status"] == "queued"
    assert session.model_inputs == []
    assert session.conversations.get("conversation-1")["messages"] == []
    result = execute_saved_turn(
        store, payload, client=_client(session), guard=lambda: None
    )
    assert result["turn"]["status"] == "completed"
    assert len(session.model_inputs) == 1
    replay = execute_saved_turn(
        store, payload, client=_client(session), guard=lambda: None
    )
    assert replay["turn"]["status"] == "completed"
    assert len(session.model_inputs) == 1


def test_calendar_missing_model_preclaim_can_release_without_stuck_run(tmp_path):
    from tests.test_saved_turn_guidance_owner import _OwnerSession, _client
    from ecosystem.rumi_turn_runtime_pack.runtime.saved import execute_saved_turn
    from tobkiri_host.errors import SavedTurnNotStartedError

    session = _OwnerSession(tmp_path)
    store = session.turns
    task = {
        "profile_id": "defaults",
        "conversation_id": "conversation-1",
        "message": "Scheduled message",
        "model": "model-profile-1",
    }
    store.reserve_calendar_preparation(
        occurrence_key="occurrence",
        source=task,
        turn_id="calendar-one",
        conversation_id="conversation-1",
        conversation_revision=1,
    )
    session.conversations.update(
        "conversation-1", {"model_reference": None}, expected_conversation_revision=1
    )
    payload = {
        "request": {
            "turn_id": "calendar-one",
            "conversation_id": "conversation-1",
            "conversation_revision": 2,
            "content": "Scheduled message",
        }
    }
    store.bind_calendar_preparation(
        occurrence_key="occurrence", source=task, payload=payload
    )
    with pytest.raises(SavedTurnNotStartedError):
        execute_saved_turn(store, payload, client=_client(session), guard=lambda: None)
    assert store.get("calendar-one")["status"] == "queued"
    assert (
        store.release_calendar_preparation(
            turn_id="calendar-one", occurrence_key="occurrence", source=task
        )["status"]
        == "released"
    )
    assert not session.model_inputs


def test_workspace_preference_is_bound_but_never_copied_into_saved_authority(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    task = {**source(), "workspace_id": "workspace-one"}
    reserve(store, task)
    payload = store.bind_calendar_preparation(
        occurrence_key="occurrence", source=task, payload=initial()
    )
    assert "workspace_id" not in payload["request"]
    with pytest.raises(TurnConflict):
        reserve(store, {**task, "workspace_id": "workspace-two"})


@pytest.mark.parametrize("mode", ["ask", "agent", "full"])
def test_calendar_approval_mode_preference_reserve_bind_recover_is_immutable(
    tmp_path, mode
):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    task = {**source(), "action_approval_mode": mode}
    receipt = reserve(store, task)
    assert receipt["source"]["action_approval_mode"] == mode
    payload = initial()
    payload["request"]["action_approval_mode"] = mode
    assert (
        store.bind_calendar_preparation(
            occurrence_key="occurrence", source=task, payload=payload
        )
        == payload
    )
    recovered = store.recover_calendar_preparation(
        turn_id="calendar-one", occurrence_key="occurrence", source=task
    )
    assert recovered["payload"]["request"]["action_approval_mode"] == mode
    assert store.get("calendar-one")["status"] == "queued"
    with pytest.raises(TurnConflict):
        reserve(
            store, {**task, "action_approval_mode": "full" if mode != "full" else "ask"}
        )
    changed = deepcopy(payload)
    changed["request"]["action_approval_mode"] = "ask" if mode != "ask" else "full"
    with pytest.raises(PermissionError):
        store.bind_calendar_preparation(
            occurrence_key="occurrence", source=task, payload=changed
        )
    assert store.claim_saved(payload)["claimed"] is True
    assert not any(
        key in store.get("calendar-one") for key in ("approved", "grants", "authority")
    )


@pytest.mark.parametrize("mode", [None, "approve", "FULL", True, {"approved": True}])
def test_malformed_calendar_approval_mode_rejected_before_reservation(tmp_path, mode):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    with pytest.raises(ValueError):
        reserve(store, {**source(), "action_approval_mode": mode})
    assert store.get("calendar-one") is None
