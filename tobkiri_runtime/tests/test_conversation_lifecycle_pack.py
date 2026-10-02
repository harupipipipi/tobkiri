"""Deterministic owner/source/consumer tests for optional conversation lifecycle."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from ecosystem.defaultspack.runtime import saved_conversation as saved

from ecosystem.rumi_conversation_store_pack.runtime.store import (
    ConversationConflict,
    ConversationStore,
)
from ecosystem.tobkiri_conversation_lifecycle_pack.runtime.lifecycle import (
    CONVERSATION,
    CONVERSATION_ACTION,
    SCHEDULE,
    SCHEDULE_ACTION,
    ConversationLifecycle,
)
from tobkiri_protocol.conversation_lifecycle import (
    ARCHIVE_MODE,
    LIFECYCLE_VERSION,
    completion_source,
    message_task_state,
    task_gap_context,
)

BASE_MS = 1_790_899_200_000


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConversationStore:
    """Use separated Profile data and an explicit deterministic owner clock."""
    monkeypatch.setattr(
        "ecosystem.rumi_conversation_store_pack.runtime.store._now_ms",
        lambda: BASE_MS,
    )
    store = ConversationStore("fixture", user_data_root=tmp_path)
    store.create(
        {"id": "conversation", "model_reference": "model-profile"},
        expected_revision=0,
    )
    return store


def _append(owner: ConversationStore, message: dict[str, Any]) -> Any:
    current = owner.get("conversation")
    return owner.append_message(
        "conversation",
        message,
        expected_conversation_revision=current["conversation_revision"],
    )


def _saved_user(owner: ConversationStore, turn: str = "turn-1") -> Any:
    request = {
        "turn_id": turn,
        "conversation_id": "conversation",
        "conversation_revision": owner.get("conversation")["conversation_revision"],
        "content": "どう？",
    }
    initial = {"request": request}
    intent = saved.start(request)
    intent = saved.resume(
        intent["state"],
        {
            "status": "ok",
            "value": {"conversation": owner.get("conversation")},
        },
    )
    value = owner.append_message(
        "conversation",
        intent["payload"]["message"],
        expected_conversation_revision=request["conversation_revision"],
        saved_input=initial,
    )
    return saved.resume(intent["state"], {"status": "ok", "value": value}), initial


def _saved_finish(owner: ConversationStore, intent: Any, initial: Any) -> None:
    intent = saved.resume(
        intent["state"],
        {
            "status": "ok",
            "value": {"status": "ok", "output": "done", "finish_reason": "stop"},
        },
    )
    owner.append_message(
        "conversation",
        intent["payload"]["message"],
        expected_conversation_revision=owner.get("conversation")["conversation_revision"],
        saved_input=initial,
    )


def _complete(owner: ConversationStore) -> None:
    _saved_finish(owner, *_saved_user(owner))


class PublicClient:
    """Test only the public operation payload, keeping consumer imports isolated."""

    def __init__(self, owner: ConversationStore) -> None:
        self.owner = owner
        self.before_update: Any = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.schedules: list[dict[str, Any]] = []

    def invoke(self, contract: str, operation: str, payload: Any) -> Any:
        self.calls.append((contract, dict(payload)))
        assert payload["profile_id"] == "fixture"
        if contract == CONVERSATION:
            assert operation == "rumi_conversation_store_pack.conversation-resource"
            if payload["operation"] == "list":
                return self.owner.snapshot()
            return {"conversation": self.owner.get(payload["conversation_id"])}
        if contract == SCHEDULE:
            return {"schedules": self.schedules, "revision": len(self.schedules)}
        if contract == SCHEDULE_ACTION:
            schedule = {
                **dict(payload),
                "id": payload["schedule_id"],
                "status": "active",
            }
            self.schedules.append(schedule)
            return {"schedule": schedule, "revision": len(self.schedules)}
        assert contract == CONVERSATION_ACTION
        assert operation == "rumi_conversation_store_pack.conversation-manage"
        assert "approved" not in payload
        if self.before_update is not None:
            hook, self.before_update = self.before_update, None
            hook()
        return self.owner.update(
            payload["conversation_id"],
            payload["patch"],
            expected_conversation_revision=payload["expected_conversation_revision"],
        )


@pytest.mark.parametrize(
    "finish",
    [
        "streaming",
        "running",
        "tool_calls",
        "approval_required",
        "authority_approval_required",
        "waiting_user",
        "cancelled",
        "error",
        "paused_loop",
        None,
    ],
)
def test_non_success_does_not_create_completion(
    owner: ConversationStore,
    finish: str | None,
) -> None:
    _append(owner, {"id": "user", "role": "user"})
    _append(
        owner,
        {
            "id": "assistant",
            "parent_id": "user",
            "role": "assistant",
            "finish_reason": finish,
        },
    )
    source = completion_source(owner.get("conversation"))
    assert source["state"] != "completed"
    assert source["completed_at_ms"] is None
    assert source["archive_due_at_ms"] is None


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"draft": True}, "running"),
        ({"streaming": True}, "running"),
        ({"waiting_for_user": True}, "waiting_user"),
        ({"approval_required": True}, "waiting_approval"),
        ({"thinking": {"state": "cancelled"}}, "cancelled"),
        ({"task_state": "failed"}, "failed"),
        ({"thinking": {"state": "failed"}}, "failed"),
    ],
)
def test_wait_flags_override_stop(metadata: Any, expected: str) -> None:
    assert (
        message_task_state(
            {
                "role": "assistant",
                "finish_reason": "stop",
                "metadata": metadata,
            }
        )
        == expected
    )


def test_owner_records_completion_and_unrelated_edits_preserve_instant(
    owner: ConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _complete(owner)
    before = owner.get("conversation")
    assert before["lifecycle"]["completed_at_ms"] == BASE_MS
    monkeypatch.setattr(
        "ecosystem.rumi_conversation_store_pack.runtime.store._now_ms",
        lambda: BASE_MS + 20_000,
    )
    owner.mutate_message(
        "conversation",
        before["current_node_id"],
        patch={"content": "edited"},
        expected_conversation_revision=before["conversation_revision"],
    )
    after = owner.get("conversation")
    assert after["messages"][-1]["updated_at"] == BASE_MS + 20_000
    assert after["lifecycle"]["completed_at_ms"] == BASE_MS
    owner.update(
        "conversation",
        {"lifecycle": {"completed_at_ms": 0}, "title": "title"},
        expected_conversation_revision=after["conversation_revision"],
    )
    assert owner.get("conversation")["lifecycle"]["completed_at_ms"] == BASE_MS


@pytest.mark.parametrize("elapsed,has_gap", [(3_599_000, False), (3_600_000, True)])
def test_gap_and_archive_share_exact_boundary(
    owner: ConversationStore,
    elapsed: int,
    has_gap: bool,
) -> None:
    _complete(owner)
    current = owner.get("conversation")
    gap = task_gap_context(current, BASE_MS + elapsed)
    assert (gap is not None) is has_gap
    if gap:
        assert gap["elapsed_seconds"] == 3600
        assert gap["previous_task_completed_at"].endswith("+00:00")
    client = PublicClient(owner)
    runtime = ConversationLifecycle(client, "fixture", clock_ms=lambda: BASE_MS + elapsed)
    runtime.configure("conversation", ARCHIVE_MODE)
    result = runtime.tick()
    assert result["count"] == int(has_gap)
    assert owner.get("conversation")["is_archived"] is has_gap
    assert len(owner.get("conversation")["messages"]) == 2


def test_resume_cancels_due_and_next_completion_resets_deadline(
    owner: ConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _complete(owner)
    runtime = ConversationLifecycle(
        PublicClient(owner), "fixture", clock_ms=lambda: BASE_MS + 4_000_000
    )
    runtime.configure("conversation", ARCHIVE_MODE)
    intent, initial = _saved_user(owner, "turn-2")
    assert runtime.tick()["count"] == 0
    assert completion_source(owner.get("conversation"))["archive_due_at_ms"] is None
    monkeypatch.setattr(
        "ecosystem.rumi_conversation_store_pack.runtime.store._now_ms",
        lambda: BASE_MS + 4_000_000,
    )
    _saved_finish(owner, intent, initial)
    source = completion_source(owner.get("conversation"))
    assert source["archive_due_at_ms"] == BASE_MS + 7_600_000
    assert runtime.tick()["count"] == 0


def test_old_stream_finish_cannot_complete_new_turn(owner: ConversationStore) -> None:
    _append(owner, {"id": "user", "role": "user"})
    _append(
        owner,
        {
            "id": "draft",
            "role": "assistant",
            "parent_id": "user",
            "finish_reason": "streaming",
        },
    )
    _append(owner, {"id": "user2", "role": "user"})
    _append(
        owner,
        {
            "id": "late",
            "role": "assistant",
            "parent_id": "user",
            "finish_reason": "stop",
        },
    )
    source = completion_source(owner.get("conversation"))
    assert source["state"] == "running"
    assert source["completed_at_ms"] is None


def test_archive_resume_race_rejected_by_owner_cas(owner: ConversationStore) -> None:
    _complete(owner)
    client = PublicClient(owner)
    runtime = ConversationLifecycle(client, "fixture", clock_ms=lambda: BASE_MS + 3_600_000)
    runtime.configure("conversation", ARCHIVE_MODE)
    client.before_update = lambda: _append(owner, {"id": "resume", "role": "user"})
    result = runtime.tick()
    assert result["count"] == 0
    assert result["conflicts"] == ["conversation"]
    assert owner.get("conversation")["is_archived"] is False
    assert runtime.tick()["count"] == 0


def test_restart_recovers_due_without_transient_schedule_state(
    owner: ConversationStore,
) -> None:
    _complete(owner)
    client = PublicClient(owner)
    ConversationLifecycle(client, "fixture").configure("conversation", ARCHIVE_MODE)
    restarted = ConversationStore("fixture", user_data_root=owner.root.parents[3])
    runtime = ConversationLifecycle(
        PublicClient(restarted), "fixture", clock_ms=lambda: BASE_MS + 3_600_000
    )
    assert runtime.tick()["count"] == 1
    assert runtime.tick()["count"] == 0
    assert len(restarted.get("conversation")["messages"]) == 2


def test_manual_and_disabled_modes_do_not_archive(owner: ConversationStore) -> None:
    _complete(owner)
    client = PublicClient(owner)
    runtime = ConversationLifecycle(client, "fixture", clock_ms=lambda: BASE_MS + 8_000_000)
    assert runtime.tick()["count"] == 0
    runtime.configure("conversation", ARCHIVE_MODE)
    runtime.enabled = False
    assert runtime.tick()["status"] == "disabled"
    assert runtime.status("conversation")["status"] == "disabled"
    assert owner.get("conversation")["is_archived"] is False
    runtime.enabled = True
    assert runtime.tick()["count"] == 1


def test_reset_does_not_manufacture_completion(owner: ConversationStore) -> None:
    _complete(owner)
    current = owner.get("conversation")
    owner.replace_messages(
        "conversation",
        [],
        expected_conversation_revision=current["conversation_revision"],
    )
    assert completion_source(owner.get("conversation"))["completed_at_ms"] is None


@pytest.mark.parametrize("head", ["prior_user", "empty"])
def test_actual_branch_head_change_invalidates_archive_eligibility(
    owner: ConversationStore, head: str,
) -> None:
    _complete(owner)
    current = owner.get("conversation")
    runtime = ConversationLifecycle(
        PublicClient(owner), "fixture", clock_ms=lambda: BASE_MS + 3_600_000,
    )
    runtime.configure("conversation", ARCHIVE_MODE)
    current = owner.get("conversation")
    owner.update(
        "conversation",
        {"current_node_id": current["messages"][0]["id"] if head == "prior_user" else None},
        expected_conversation_revision=current["conversation_revision"],
    )
    source = completion_source(owner.get("conversation"))
    assert source["state"] == "unknown"
    assert source["completed_at_ms"] == BASE_MS
    assert source["archive_due_at_ms"] is None
    assert runtime.tick()["count"] == 0


def test_same_branch_head_does_not_move_completion_instant(owner: ConversationStore) -> None:
    _complete(owner)
    current = owner.get("conversation")
    owner.update(
        "conversation", {"current_node_id": current["current_node_id"]},
        expected_conversation_revision=current["conversation_revision"],
    )
    assert completion_source(owner.get("conversation"))["state"] == "completed"
    assert completion_source(owner.get("conversation"))["completed_at_ms"] == BASE_MS


def test_timezone_and_dst_are_utc_durations() -> None:
    completed = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0)
    received = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=1)
    conversation = {
        "lifecycle": {
            "version": LIFECYCLE_VERSION,
            "state": "completed",
            "completed_at_ms": int(completed.timestamp() * 1000),
            "completion_message_id": "assistant",
        }
    }
    gap = task_gap_context(conversation, int(received.timestamp() * 1000))
    assert gap["elapsed_seconds"] == 3600


def test_legacy_history_timestamps_are_not_completion_claims() -> None:
    conversation = {
        "messages": [{"role": "assistant", "finish_reason": "stop", "updated_at": BASE_MS}]
    }
    assert task_gap_context(conversation, BASE_MS + 86_400_000) is None


def test_missing_contract_is_truthfully_unavailable(owner: ConversationStore) -> None:
    class Missing:
        def invoke(self, *args: Any) -> Any:
            raise LookupError("missing provider")

    runtime = ConversationLifecycle(Missing(), "fixture")
    assert runtime.tick()["status"] == "unavailable"
    assert runtime.status("conversation")["status"] == "unavailable"


def test_stale_completion_write_does_not_mutate_source(
    owner: ConversationStore,
) -> None:
    current = owner.get("conversation")
    _append(owner, {"id": "user", "role": "user"})
    with pytest.raises(ConversationConflict):
        owner.append_message(
            "conversation",
            {"id": "assistant", "role": "assistant", "finish_reason": "stop"},
            expected_conversation_revision=current["conversation_revision"],
        )
    assert owner.get("conversation")["lifecycle"]["completed_at_ms"] is None


def test_terminal_without_active_turn_parent_is_unknown(
    owner: ConversationStore,
) -> None:
    _append(owner, {"id": "user", "role": "user"})
    _append(owner, {"id": "assistant", "role": "assistant", "finish_reason": "stop"})
    assert completion_source(owner.get("conversation"))["state"] == "running"


def test_unconfirmed_schedule_does_not_save_mode(owner: ConversationStore) -> None:
    class Unconfirmed(PublicClient):
        def invoke(self, contract: str, operation: str, payload: Any) -> Any:
            if contract == SCHEDULE_ACTION:
                return {"status": "accepted"}
            return super().invoke(contract, operation, payload)

    runtime = ConversationLifecycle(Unconfirmed(owner), "fixture")
    with pytest.raises(LookupError, match="unconfirmed"):
        runtime.configure("conversation", ARCHIVE_MODE)
    assert completion_source(owner.get("conversation"))["mode"] == "manual"


def test_captured_adapter_describes_exact_action_and_rejects_claims(
    owner: ConversationStore,
) -> None:
    from types import SimpleNamespace
    from ecosystem.tobkiri_conversation_lifecycle_pack.runtime.host import (
        ARCHIVE_ACTION_ID,
        HOST_PROVIDER_FACTORY,
        JOB_CONTRACT,
        PACK_ID,
    )

    function_id = f"{PACK_ID}.archive-job"
    operation_id = f"{PACK_ID}.archive-job-adapter"
    context = SimpleNamespace(
        profile_id="fixture",
        provider_bindings=(
            SimpleNamespace(
                function=SimpleNamespace(
                    function_id=function_id, implementation_digest="implementation"
                ),
                operation=SimpleNamespace(
                    contract_id=JOB_CONTRACT,
                    operation_id=operation_id,
                    contract_version="2.0.0",
                ),
                principal_ref=SimpleNamespace(value="principal"),
                artifact=SimpleNamespace(digest="artifact"),
            ),
        ),
        domain_ids={(JOB_CONTRACT, operation_id, "principal"): "domain"},
    )
    checks: list[str] = []
    invocation = SimpleNamespace(assert_current=lambda: checks.append("current"))
    invoke = HOST_PROVIDER_FACTORY[function_id].capture(context).contributions[0].invoke
    assert invoke(operation_id, {"profile_id": "fixture", "operation": "describe"}, invocation) == {
        "action_ids": [ARCHIVE_ACTION_ID]
    }
    assert checks == ["current"]
    with pytest.raises(PermissionError):
        invoke(operation_id, {"profile_id": "other", "operation": "describe"}, invocation)
    with pytest.raises(ValueError):
        invoke(
            operation_id,
            {"profile_id": "fixture", "operation": "describe", "approved": True},
            invocation,
        )
    context.provider_bindings[0].operation.contract_version = "3.0.0"
    with pytest.raises(PermissionError):
        HOST_PROVIDER_FACTORY[function_id].capture(context)


def test_captured_manage_client_is_restricted_and_profile_bound(
    owner: ConversationStore,
) -> None:
    from types import SimpleNamespace
    from ecosystem.tobkiri_conversation_lifecycle_pack.runtime.host import (
        HOST_PROVIDER_FACTORY,
        MANAGE_CONTRACT,
        PACK_ID,
    )

    function_id = f"{PACK_ID}.manage"
    operation_id = f"{PACK_ID}.lifecycle-manage"
    context = SimpleNamespace(
        profile_id="fixture",
        provider_bindings=(
            SimpleNamespace(
                function=SimpleNamespace(
                    function_id=function_id, implementation_digest="implementation"
                ),
                operation=SimpleNamespace(
                    contract_id=MANAGE_CONTRACT,
                    operation_id=operation_id,
                    contract_version="1.0.0",
                ),
                principal_ref=SimpleNamespace(value="principal"),
                artifact=SimpleNamespace(digest="artifact"),
            ),
        ),
        domain_ids={(MANAGE_CONTRACT, operation_id, "principal"): "domain"},
    )
    claims: list[dict[str, Any]] = []

    def client(**kwargs: Any) -> PublicClient:
        claims.append(kwargs)
        return PublicClient(owner)

    invocation = SimpleNamespace(assert_current=lambda: None, contract_client=client)
    invoke = HOST_PROVIDER_FACTORY[function_id].capture(context).contributions[0].invoke
    result = invoke(
        operation_id,
        {
            "profile_id": "fixture",
            "operation": "configure",
            "conversation_id": "conversation",
            "mode": ARCHIVE_MODE,
        },
        invocation,
    )
    assert result["status"] == "configured"
    assert claims == [
        {
            "allowed_contract_ids": frozenset(
                {CONVERSATION, CONVERSATION_ACTION, SCHEDULE, SCHEDULE_ACTION}
            ),
            "consumer_pack_id": PACK_ID,
            "include_credentials": False,
        }
    ]


def test_internal_gap_insertion_never_rewrites_user_text(
    owner: ConversationStore,
) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ecosystem/defaultspack"))
    from domain.temporal_context import add_task_gap_context_message

    _complete(owner)
    gap = task_gap_context(owner.get("conversation"), BASE_MS + 3_600_000)
    messages = [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "どう？"},
    ]
    add_task_gap_context_message(messages, gap)
    assert messages[1]["role"] == "system"
    assert "elapsed_seconds: 3600" in messages[1]["content"]
    assert messages[2] == {"role": "user", "content": "どう？"}
