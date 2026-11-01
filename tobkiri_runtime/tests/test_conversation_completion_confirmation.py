"""Real durable settlement, captured confirmation and append-source regressions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.rumi_conversation_store_pack.runtime.completion_host import (
    CONTRACT_ID, FUNCTION_ID, OPERATION_ID, TURN_CONTRACT, TURN_OPERATION,
    ConversationCompletionHostFactoryV4,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.saved import COMPLETION_CONTRACT
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict
from tests.test_saved_turn_coordinator import _Session, _run
from tobkiri_protocol.conversation_lifecycle import completion_source, task_gap_context

BASE_MS = 1_790_899_200_000


def _context(root: Path) -> Any:
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=FUNCTION_ID, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=CONTRACT_ID, operation_id=OPERATION_ID, contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value="completion-owner"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    callers = tuple(SimpleNamespace(
        function=SimpleNamespace(function_id=f"rumi_turn_runtime_pack.turn-runtime.{kind}"),
        principal_ref=SimpleNamespace(value=f"saved-{kind}"),
    ) for kind in ("saved", "reconcile"))
    return SimpleNamespace(
        profile_id="defaults", user_data_root=root, provider_bindings=(binding,),
        catalog_bindings=callers,
        domain_ids={(CONTRACT_ID, OPERATION_ID, "completion-owner"): "domain"},
    )


class _CapturedSession(_Session):
    """Use the captured completion function with an explicit public read adapter."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.ai_outcome["value"]["finish_reason"] = "stop"
        self.read_calls = 0
        self.fail_confirmation = False
        self.confirm = ConversationCompletionHostFactoryV4().capture(
            _context(root),
        ).contributions[0].invoke

    def invocation(self, caller: str = "saved-saved") -> Any:
        def client(**kwargs: Any) -> Any:
            assert kwargs == {
                "allowed_contract_ids": frozenset({TURN_CONTRACT}),
                "consumer_pack_id": "rumi_conversation_store_pack",
                "include_credentials": False,
            }

            def invoke(contract: str, operation: str, payload: Any) -> Any:
                self.read_calls += 1
                assert (contract, operation) == (TURN_CONTRACT, TURN_OPERATION)
                assert payload == {
                    "profile_id": "defaults", "operation": "get", "turn_id": "turn-1",
                }
                return self.turns.get("turn-1")

            return SimpleNamespace(invoke=invoke)

        return SimpleNamespace(
            envelope=SimpleNamespace(context=SimpleNamespace(
                caller_principal=SimpleNamespace(value=caller),
            )),
            assert_current=lambda: None, contract_client=client,
        )

    def invoke(
        self, contract_id: str, operation: str, payload: dict, **kwargs: Any,
    ) -> dict:
        if contract_id == COMPLETION_CONTRACT:
            if self.fail_confirmation:
                raise TimeoutError("confirmation unavailable")
            return self.confirm(operation, payload, self.invocation())
        return super().invoke(contract_id, operation, payload, **kwargs)


def test_confirmed_terminal_preserves_append_clock_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [BASE_MS]
    monkeypatch.setattr(
        "ecosystem.rumi_conversation_store_pack.runtime.store._now_ms", lambda: now[0],
    )
    session = _CapturedSession(tmp_path)

    def observe(value: dict) -> dict:
        source = session.conversations.get("conversation-1")["lifecycle"]
        assert source["state"] == "running"
        assert source["completed_at_ms"] is None
        assert source["completion_candidate"]["completed_at_ms"] == BASE_MS
        now[0] += 3_600_000
        return value

    session.transform = observe
    result = _run(session.turns, session)
    assert result["status"] == result["turn"]["status"] == "completed"
    source = completion_source(session.conversations.get("conversation-1"))
    assert source["state"] == "completed"
    assert source["completed_at_ms"] == BASE_MS
    before = session.conversations.path.read_bytes()
    assert _run(session.turns, session)["status"] == "existing"
    assert session.conversations.path.read_bytes() == before
    assert session.ai_calls == 1
    # Once completed wins the durable CAS, a later stop cannot rewrite it.
    with pytest.raises(TurnConflict):
        session.turns.request_saved_cancellation("turn-1")


def test_cancelled_terminal_never_promotes_candidate(tmp_path: Path) -> None:
    session = _CapturedSession(tmp_path)

    def cancel(value: dict) -> dict:
        session.turns.request_saved_cancellation("turn-1")
        session.turns.confirm_saved_cancellation("turn-1")
        return value

    session.transform = cancel
    result = _run(session.turns, session)
    assert result["turn"]["status"] == "cancelled"
    source = completion_source(session.conversations.get("conversation-1"))
    assert source["state"] != "completed" and source["completed_at_ms"] is None
    assert task_gap_context(session.conversations.get("conversation-1"), BASE_MS * 2) is None
    with pytest.raises(PermissionError, match="durable terminal"):
        session.confirm(OPERATION_ID, {
            "profile_id": "defaults", "operation": "confirm", "turn_id": "turn-1",
        }, session.invocation())


def test_restart_after_terminal_cas_recovers_confirmation_without_ai(tmp_path: Path) -> None:
    session = _CapturedSession(tmp_path)
    session.fail_confirmation = True
    result = _run(session.turns, session)
    assert result["status"] == "reconciliation_required"
    assert result["turn"]["status"] == "completed"
    assert completion_source(session.conversations.get("conversation-1"))["state"] == "running"
    session.conversations = ConversationStore("defaults", user_data_root=tmp_path)
    session.turns = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    session.fail_confirmation = False
    result = _run(session.turns, session)
    assert result["status"] == "existing"
    assert completion_source(session.conversations.get("conversation-1"))["state"] == "completed"
    assert session.ai_calls == session.calls == 1


def test_confirmation_commit_with_lost_reply_recovers_idempotently(tmp_path: Path) -> None:
    session = _CapturedSession(tmp_path)
    original = session.confirm

    def lost_reply(*args: Any) -> Any:
        original(*args)
        raise TimeoutError("reply lost after confirmation commit")

    session.confirm = lost_reply
    result = _run(session.turns, session)
    assert result["status"] == "reconciliation_required"
    assert result["turn"]["status"] == "completed"
    before = session.conversations.path.read_bytes()
    session.confirm = original
    assert _run(session.turns, session)["status"] == "existing"
    assert session.conversations.path.read_bytes() == before
    assert session.ai_calls == 1


@pytest.mark.parametrize("change", ["user", "head", "edit", "delete"])
def test_stale_candidate_cannot_promote_a_new_generation(tmp_path: Path, change: str) -> None:
    session = _CapturedSession(tmp_path)

    def retire(value: dict) -> dict:
        if change == "user":
            session.conversations.append_message(
                "conversation-1", {"id": "next", "role": "user", "content": "Continue"},
                expected_conversation_revision=3,
            )
        elif change == "head":
            session.conversations.update(
                "conversation-1", {"current_node_id": value["user_message_id"]},
                expected_conversation_revision=3,
            )
        elif change == "edit":
            session.conversations.mutate_message(
                "conversation-1", value["message"]["id"], patch={"raw_text": "Edited"},
                expected_conversation_revision=3,
            )
        else:
            session.conversations.delete("conversation-1", expected_conversation_revision=3)
        return value

    session.transform = retire
    result = _run(session.turns, session)
    assert result["status"] == result["turn"]["status"] == "completed"
    conversation = session.conversations.get("conversation-1")
    if conversation is not None:
        assert completion_source(conversation)["state"] != "completed"
        assert completion_source(conversation)["completed_at_ms"] is None


@pytest.mark.parametrize("state", ["waiting_user", "waiting_approval", "failed", "running"])
def test_noncomplete_message_state_never_creates_candidate(tmp_path: Path, state: str) -> None:
    session = _CapturedSession(tmp_path)
    # This adapter is the projected Host AI outcome, whose finite finish field
    # carries the authenticated provider state (raw metadata is never guest input).
    session.ai_outcome["value"]["finish_reason"] = "error" if state == "failed" else state
    result = _run(session.turns, session)
    assert result["turn"]["status"] == "completed"  # Execution, not task completion.
    source = session.conversations.get("conversation-1")["lifecycle"]
    assert source["state"] == state
    assert source["completion_candidate"] is None
    assert source["completed_at_ms"] is None


@pytest.mark.parametrize("patch", [
    {"approved": True}, {"turn": {"status": "completed"}}, {"operation": "promote"},
    {"profile_id": "other"}, {"turn_id": "other"},
])
def test_client_flags_and_foreign_bindings_cannot_confirm(tmp_path: Path, patch: dict) -> None:
    session = _CapturedSession(tmp_path)
    _run(session.turns, session)
    before = session.conversations.path.read_bytes()
    with pytest.raises((PermissionError, ValueError, AssertionError)):
        session.confirm(OPERATION_ID, {
            "profile_id": "defaults", "operation": "confirm", "turn_id": "turn-1",
        } | patch, session.invocation())
    assert session.conversations.path.read_bytes() == before


def test_foreign_caller_is_rejected_before_reading_turn(tmp_path: Path) -> None:
    session = _CapturedSession(tmp_path)
    with pytest.raises(PermissionError, match="captured saved owner"):
        session.confirm(OPERATION_ID, {
            "profile_id": "defaults", "operation": "confirm", "turn_id": "turn-1",
        }, session.invocation("external"))
    assert session.read_calls == 0
    assert not session.conversations.saved_receipt("turn-1")


def test_mismatched_durable_result_cannot_confirm(tmp_path: Path) -> None:
    session = _CapturedSession(tmp_path)
    session.fail_confirmation = True
    result = _run(session.turns, session)
    forged = deepcopy(result["turn"])
    forged["result_reference"]["outcome_digest"] = "sha256:" + "0" * 64
    before = session.conversations.path.read_bytes()
    with pytest.raises(PermissionError, match="receipt does not match"):
        session.conversations.confirm_saved_completion(forged)
    assert session.conversations.path.read_bytes() == before
