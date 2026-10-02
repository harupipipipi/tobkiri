"""Deterministic owner/source/consumer tests for optional conversation lifecycle."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ecosystem.rumi_conversation_store_pack.runtime.store import (
    ConversationConflict,
    ConversationStore,
)
from ecosystem.tobkiri_conversation_lifecycle_pack.runtime.lifecycle import (
    CONVERSATION,
    CONVERSATION_ACTION,
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
    store.create({"id": "conversation"}, expected_revision=0)
    return store


def _append(owner: ConversationStore, message: dict[str, Any]) -> Any:
    current = owner.get("conversation")
    return owner.append_message(
        "conversation",
        message,
        expected_conversation_revision=current["conversation_revision"],
    )


def _complete(owner: ConversationStore) -> None:
    _append(owner, {"id": "user", "role": "user", "content": "どう？"})
    _append(
        owner,
        {
            "id": "assistant",
            "parent_id": "user",
            "role": "assistant",
            "finish_reason": "stop",
            "content": "done",
            "created_at": 1,
            "updated_at": 99,
            "completed_at_ms": 0,
        },
    )


class PublicClient:
    """Test only the public operation payload, keeping consumer imports isolated."""

    def __init__(self, owner: ConversationStore) -> None:
        self.owner = owner
        self.before_update: Any = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, contract: str, operation: str, payload: Any) -> Any:
        self.calls.append((contract, dict(payload)))
        assert payload["profile_id"] == "fixture"
        if contract == CONVERSATION:
            assert operation == "rumi_conversation_store_pack.conversation-resource"
            if payload["operation"] == "list":
                return self.owner.snapshot()
            return {"conversation": self.owner.get(payload["conversation_id"])}
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
        "assistant",
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
    runtime = ConversationLifecycle(
        client, "fixture", clock_ms=lambda: BASE_MS + elapsed
    )
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
    _append(owner, {"id": "user2", "role": "user", "content": "continue"})
    assert runtime.tick()["count"] == 0
    assert completion_source(owner.get("conversation"))["archive_due_at_ms"] is None
    monkeypatch.setattr(
        "ecosystem.rumi_conversation_store_pack.runtime.store._now_ms",
        lambda: BASE_MS + 4_000_000,
    )
    _append(
        owner,
        {
            "id": "assistant2",
            "role": "assistant",
            "parent_id": "user2",
            "finish_reason": "stop",
        },
    )
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
    runtime = ConversationLifecycle(
        client, "fixture", clock_ms=lambda: BASE_MS + 3_600_000
    )
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
    runtime = ConversationLifecycle(
        client, "fixture", clock_ms=lambda: BASE_MS + 8_000_000
    )
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


def test_timezone_and_dst_are_utc_durations() -> None:
    completed = datetime(
        2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0
    )
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
        "messages": [
            {"role": "assistant", "finish_reason": "stop", "updated_at": BASE_MS}
        ]
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
