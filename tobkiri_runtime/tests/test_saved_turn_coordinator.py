"""Real durable/conversation owners with explicit dispatch and AI adapters."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from core_runtime.global_contract_dispatch import GlobalContractClient
from ecosystem.defaultspack.runtime import saved_conversation as application
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.saved import (
    RECEIPT_CONTRACT, RECEIPT_OPERATION, SAVED_CONTRACTS, execute_saved_turn,
)
from tests.test_saved_conversation_steps import _owner, _setup
from tobkiri_protocol.saved_conversation import (
    SAVED_CONVERSATION_CONTRACT as CONTRACT,
    SAVED_CONVERSATION_OPERATION as OPERATION,
)


class _Session:
    profile_id = "defaults"
    plan_digest = "test-adapter"

    def __init__(self, root: Path) -> None:
        self.conversations, request = _setup(root)
        self.initial = {"request": request}
        self.calls = 0
        self.transform = lambda value: value

    def provider_metadata(self, contract_id: str) -> tuple:
        return ()

    def invoke(self, contract_id: str, operation: str, payload: dict, **kwargs: Any) -> dict:
        if (contract_id, operation) == (RECEIPT_CONTRACT, RECEIPT_OPERATION):
            assert payload == {
                "profile_id": "defaults", "operation": "saved_receipt", "turn_id": "turn-1",
            }
            return {"receipt": self.conversations.saved_receipt(payload["turn_id"])}
        assert (contract_id, operation) == (CONTRACT, OPERATION)
        assert payload == self.initial
        self.calls += 1
        intent = application.start(payload["request"])
        for hop in range(4):
            outcome = (
                {"status": "ok", "value": {"status": "ok", "output": "Hi"}}
                if hop == 2 else _owner(self.conversations, intent, saved_input=self.initial)
            )
            intent = application.resume(intent["state"], outcome)
        return self.transform(intent)


def _run(store: DurableTurnRuntime, session: _Session, guard=lambda: None) -> dict:
    client = GlobalContractClient(
        session=session, allowed_contract_ids=SAVED_CONTRACTS,
        consumer_pack_id="rumi_turn_runtime_pack",
    )
    return execute_saved_turn(store, session.initial, client=client, guard=guard)


def test_saved_execution_completes_once_and_keeps_transcript_in_conversation_owner(
    tmp_path: Path,
) -> None:
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    result = _run(store, session)
    assert result["status"] == "completed"
    assert result["turn"]["status"] == "completed"
    messages = session.conversations.get("conversation-1")["messages"]
    reference = result["turn"]["result_reference"]
    assert reference["user_message_id"] == messages[0]["id"]
    assert reference["assistant_message_id"] == messages[1]["id"]
    assert reference["conversation_revision"] == 3
    assert "Hello" not in json.dumps(result)
    assert "Hi" not in json.dumps(result)
    reopened = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    assert _run(reopened, session) == {"status": "existing", "turn": result["turn"]}
    assert session.calls == 1


def test_lost_result_never_replays_committed_messages(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)

    def lose(value: dict) -> dict:
        raise TimeoutError("private provider diagnostic")

    session.transform = lose
    result = _run(store, session)
    assert result["status"] == "reconciliation_required"
    assert result["turn"]["status"] == "waiting"
    assert "private provider diagnostic" not in json.dumps(result)
    assert len(session.conversations.get("conversation-1")["messages"]) == 2
    reopened = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    recovered = _run(reopened, session)
    assert recovered["status"] == "completed"
    assert recovered["turn"]["result_reference"]["conversation_revision"] == 3
    assert session.calls == 1


@pytest.mark.parametrize("field,value", [
    ("status", "error"), ("turn_id", "other"), ("conversation_id", "other"),
    ("conversation_revision", True), ("conversation_revision", 1),
    ("user_message_id", "other"), ("message", {}), ("extra", "unrecognized"),
])
def test_invalid_acknowledgement_never_marks_completed(
    tmp_path: Path, field: str, value: Any,
) -> None:
    session = _Session(tmp_path)
    session.transform = lambda result: {**result, field: value}
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    result = _run(store, session)
    assert result["status"] == "reconciliation_required"
    assert result["turn"]["result_reference"] is None
    # A forged guest ACK is rejected; only a new, captured owner read can
    # establish that the independent commit actually succeeded.
    assert _run(store, session)["status"] == "completed"
    assert session.calls == 1


@pytest.mark.parametrize("guard_call", [1, 2, 3])
def test_cancellation_and_deadline_guards_fence_dispatch_and_completion(
    tmp_path: Path, guard_call: int,
) -> None:
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == guard_call:
            raise TimeoutError("original invocation is no longer active")

    if guard_call == 1:
        with pytest.raises(TimeoutError):
            _run(store, session, guard)
        assert not store.path.exists()
    else:
        result = _run(store, session, guard)
        assert result["status"] == "reconciliation_required"
        assert result["turn"]["status"] == "waiting"
    assert session.calls == (1 if guard_call == 3 else 0)


def test_duplicate_request_can_reconcile_commit_without_reexecuting_live_turn(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    entered, release = Event(), Event()

    def pause(value: dict) -> dict:
        entered.set()
        assert release.wait(timeout=10)
        return value

    session.transform = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(_run, store, session)
        try:
            assert entered.wait(timeout=10)
            before = deepcopy(store.get("turn-1"))
            repeated = _run(store, session)
            assert repeated["status"] == "completed"
            assert before["status"] == "running"
            assert store.get("turn-1")["revision"] == before["revision"] + 1
        finally:
            release.set()
        assert running.result(timeout=10)["status"] == "completed"
    assert session.calls == 1


def test_concurrent_cancellation_is_not_overwritten_by_late_success(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)

    def cancel(value: dict) -> dict:
        store.mutate("transition", "turn-1", expected_revision=2, status="cancelled")
        return value

    session.transform = cancel
    result = _run(store, session)
    assert result["status"] == "reconciliation_required"
    assert result["turn"]["status"] == "cancelled"
    assert result["turn"]["result_reference"] is None


@pytest.mark.parametrize("change", ["profile", "consumer", "contracts", "credential"])
def test_mismatched_client_is_rejected_before_claim(tmp_path: Path, change: str) -> None:
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    if change == "profile":
        session.profile_id = "other"
    client = GlobalContractClient(
        session=session,
        allowed_contract_ids=SAVED_CONTRACTS | ({"extra"} if change == "contracts" else set()),
        consumer_pack_id="other" if change == "consumer" else "rumi_turn_runtime_pack",
        host_credential_transport=object() if change == "credential" else None,
    )
    with pytest.raises(PermissionError):
        execute_saved_turn(store, session.initial, client=client, guard=lambda: None)
    assert not store.path.exists()
    assert session.calls == 0


def test_assistant_commit_before_owner_reply_is_recovered_after_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    write = session.conversations._write

    def lost_reply(state: dict) -> None:
        write(state)
        if len(state["conversations"]["conversation-1"]["messages"]) == 2:
            raise OSError("owner reply lost after commit")

    monkeypatch.setattr(session.conversations, "_write", lost_reply)
    assert _run(store, session)["status"] == "reconciliation_required"
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    session.conversations = ConversationStore("defaults", user_data_root=tmp_path)
    receipt = session.conversations.saved_receipt("turn-1")
    assert "Hello" not in json.dumps(receipt) and "Hi" not in json.dumps(receipt)
    result = _run(DurableTurnRuntime("defaults", user_data_root=tmp_path), session)
    assert result["status"] == "completed"
    assert result["turn"]["result_reference"] == receipt["result_reference"]
    assert session.calls == 1


def test_message_edit_cannot_change_the_saved_completion_evidence(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    session.transform = lambda _: (_ for _ in ()).throw(TimeoutError())
    _run(store, session)
    before = session.conversations.saved_receipt("turn-1")
    session.conversations.mutate_message(
        "conversation-1", before["assistant_message_id"],
        expected_conversation_revision=3,
        patch={"content": "edited after completion", "metadata": {"turn_id": "forged"}},
    )
    assert session.conversations.saved_receipt("turn-1") == before
    result = _run(store, session)
    assert result["status"] == "completed"
    assert result["turn"]["result_reference"] == before["result_reference"]
    assert result["turn"]["result_reference"]["conversation_revision"] == 3
    assert session.calls == 1


@pytest.mark.parametrize("field,value", [
    ("input_digest", "sha256:" + "0" * 64), ("turn_id", "other"),
    ("initial_revision", True), ("user_revision", True),
    ("result_reference", {"conversation_id": "other"}),
])
def test_substituted_receipt_cannot_complete_a_waiting_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any,
) -> None:
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    session.transform = lambda _: (_ for _ in ()).throw(TimeoutError())
    _run(store, session)
    before = store.get("turn-1")
    original = session.invoke

    def substitute(contract_id: str, operation: str, payload: dict, **kwargs: Any) -> dict:
        result = original(contract_id, operation, payload, **kwargs)
        if contract_id == RECEIPT_CONTRACT:
            result["receipt"][field] = value
        return result

    monkeypatch.setattr(session, "invoke", substitute)
    with pytest.raises(ValueError, match="saved receipt"):
        _run(store, session)
    assert store.get("turn-1") == before
    assert session.calls == 1
