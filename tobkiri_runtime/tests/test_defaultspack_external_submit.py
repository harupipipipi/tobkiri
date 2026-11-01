from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_owner_bindings")

from domain.chat.store import ChatStore  # noqa: E402
from domain.input.envelope import RumiInputEnvelope  # noqa: E402
from domain.input.submit import submit_input  # noqa: E402
from domain.integrations.store import IntegrationConversationStore  # noqa: E402


def _configure_external_submit_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_STORE_PATH", str(tmp_path / "integrations" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_LOCKS_DIR", str(tmp_path / "integrations" / "event_locks"))
    ChatStore._instance = None


def _line_envelope(*, event_id: str) -> RumiInputEnvelope:
    return RumiInputEnvelope(
        input="hello from line",
        role="user",
        chat={
            "external_key": "line:user:Utest",
            "title": "line Utest",
            "model": "stub/default",
        },
        source={
            "provider": "line",
            "event_id": event_id,
            "external_key": "line:user:Utest",
        },
        metadata={},
        params={},
        tools=[],
    )


def test_submit_input_returns_in_progress_for_claimed_external_event(monkeypatch, tmp_path):
    import blocks.chat.send as send_block

    _configure_external_submit_paths(monkeypatch, tmp_path)
    store = IntegrationConversationStore()
    assert store.claim_event("line", "evt-in-progress", metadata={"test": True}) is True

    def fake_send_run(*args, **kwargs):
        raise AssertionError("send_run should not execute while the event is already in progress")

    monkeypatch.setattr(send_block, "run", fake_send_run)

    result = submit_input(_line_envelope(event_id="evt-in-progress"), {})

    assert result["status"] == "in_progress"
    assert result["event_id"] == "evt-in-progress"
    assert ChatStore().list_conversations()[1] == 0


def test_submit_input_marks_duplicate_after_successful_external_event(monkeypatch, tmp_path):
    import blocks.chat.send as send_block

    _configure_external_submit_paths(monkeypatch, tmp_path)

    monkeypatch.setattr(
        send_block,
        "run",
        lambda request, context: {
            "status": "ok",
            "data": {
                "id": "assistant-1",
                "content": [{"type": "text", "text": "sent via chrome"}],
            },
        },
    )

    envelope = _line_envelope(event_id="evt-success")
    first = submit_input(envelope, {})
    second = submit_input(envelope, {})
    store = IntegrationConversationStore()

    assert first["status"] == "ok"
    assert first["assistant_text"] == "sent via chrome"
    assert second["status"] == "duplicate"
    assert store.is_event_processed("line", "evt-success") is True
    assert store.is_event_in_progress("line", "evt-success") is False


def test_submit_input_releases_claim_after_send_error(monkeypatch, tmp_path):
    import blocks.chat.send as send_block

    _configure_external_submit_paths(monkeypatch, tmp_path)
    envelope = _line_envelope(event_id="evt-retry")

    monkeypatch.setattr(send_block, "run", lambda request, context: {"status": "error", "error": "boom"})
    failed = submit_input(envelope, {})

    monkeypatch.setattr(
        send_block,
        "run",
        lambda request, context: {
            "status": "ok",
            "data": {
                "id": "assistant-2",
                "content": [{"type": "text", "text": "retry ok"}],
            },
        },
    )
    retried = submit_input(envelope, {})

    assert failed["status"] == "error"
    assert retried["status"] == "ok"
    assert retried["assistant_text"] == "retry ok"


def test_existing_external_conversation_updates_model_and_metadata(monkeypatch, tmp_path):
    _configure_external_submit_paths(monkeypatch, tmp_path)
    chat_store = ChatStore()
    integration_store = IntegrationConversationStore()

    original = integration_store.get_or_create_conversation(
        provider="line",
        external_key="line:group:Cgroup",
        title="line Cgroup",
        metadata={"source": {"provider": "line"}, "input_profile_id": "line.default"},
        chat_store=chat_store,
        model="stub/default",
    )

    reused = integration_store.get_or_create_conversation(
        provider="line",
        external_key="line:group:Cgroup",
        title="line Cgroup",
        metadata={"input_profile_id": "line.computer_use", "line_mention": {"require_group_mention": True}},
        chat_store=chat_store,
        model="google/gemma-4-31b-it",
    )

    assert reused["id"] == original["id"]
    assert reused["model"] == "google/gemma-4-31b-it"
    assert reused["metadata"]["source"] == {"provider": "line"}
    assert reused["metadata"]["input_profile_id"] == "line.computer_use"
    assert reused["metadata"]["line_mention"] == {"require_group_mention": True}
