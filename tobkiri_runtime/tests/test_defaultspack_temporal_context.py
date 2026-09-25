from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _settings_owner(tmp_path: Path) -> Any:
    """Return an isolated explicit owner for model runtime settings."""
    from ecosystem.tobkiri_ui_settings_pack.runtime.store import FrontendSettingsStore

    return FrontendSettingsStore(tmp_path / "frontend_settings.json")


def test_temporal_context_prompt_uses_configured_timezone():
    from domain.temporal_context import current_datetime_context, temporal_context_prompt

    temporal = current_datetime_context(
        {"timezone": "Asia/Tokyo"},
        now=datetime(2026, 6, 5, 15, 54, tzinfo=timezone.utc),
    )
    prompt = temporal_context_prompt(temporal_context=temporal)

    assert temporal["date"] == "2026-06-06"
    assert temporal["utc_offset"] == "+09:00"
    assert "Current date/time: 2026-06-06T00:54:00+09:00" in prompt
    assert "Today is 2026-06-06." in prompt


def test_ai_complete_injects_temporal_context(monkeypatch, tmp_path: Path):
def test_task_gap_context_uses_latest_completed_assistant_message():
    from domain.temporal_context import task_gap_context

    messages = [
        {
            "role": "assistant",
            "finish_reason": "stop",
            "created_at": 1_787_792_400_000,
            "updated_at": 1_787_792_400_000,
        },
        {
            "role": "assistant",
            "finish_reason": "stop",
            "created_at": 1_787_806_800_000,
            "updated_at": 1_787_806_800_000,
        },
        {
            "role": "assistant",
            "finish_reason": "streaming",
            "created_at": 1_787_814_000_000,
            "updated_at": 1_787_814_000_000,
            "metadata": {"draft": True},
        },
    ]

    gap = task_gap_context(
        messages,
        {"timezone": "Asia/Tokyo"},
        now=datetime(2026, 8, 27, 7, 1, tzinfo=timezone.utc),
    )

    assert gap == {
        "previous_task_completed_at": "2026-08-27T14:00:00+09:00",
        "current_user_message_at": "2026-08-27T16:01:00+09:00",
        "elapsed_seconds": 7_260,
        "elapsed": "2h 1m",
        "threshold_seconds": 3_600,
    }


def test_task_gap_context_threshold_is_exactly_one_hour():
    from domain.temporal_context import task_gap_context

    completed_at = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
    messages = [
        {
            "role": "assistant",
            "finish_reason": "stop",
            "updated_at": int(completed_at.timestamp() * 1000),
        }
    ]

    assert task_gap_context(
        messages,
        {"timezone": "UTC"},
        now=completed_at.replace(minute=59, second=59),
    ) is None
    assert task_gap_context(
        messages,
        {"timezone": "UTC"},
        now=completed_at.replace(hour=1),
    )["elapsed_seconds"] == 3_600


def test_task_gap_context_elapsed_is_dst_independent():
    from domain.temporal_context import task_gap_context

    completed_at = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    messages = [
        {
            "role": "assistant",
            "finish_reason": "stop",
            "updated_at": int(completed_at.timestamp() * 1000),
        }
    ]

    gap = task_gap_context(
        messages,
        {"timezone": "America/New_York"},
        now=datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc),
    )

    assert gap["elapsed_seconds"] == 7_200
    assert gap["previous_task_completed_at"] == "2026-11-01T01:30:00-04:00"
    assert gap["current_user_message_at"] == "2026-11-01T02:30:00-05:00"


def test_add_task_gap_context_message_is_internal_system_context():
    from domain.temporal_context import add_task_gap_context_message

    messages = [{"role": "user", "content": "How about now?"}]
    gap = {
        "previous_task_completed_at": "2026-08-27T10:01:00+09:00",
        "current_user_message_at": "2026-08-27T16:01:00+09:00",
        "elapsed_seconds": 21_600,
        "elapsed": "6h 0m",
        "threshold_seconds": 3_600,
    }

    prompt = add_task_gap_context_message(messages, gap)

    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "How about now?"}
    assert prompt.startswith("[Temporal context]\n")
    assert "The previous task completed 6h 0m ago." in prompt
    assert "previous_task_completed_at: 2026-08-27T10:01:00+09:00" in prompt


def test_ai_complete_injects_temporal_context(monkeypatch):
    from blocks.ai.complete import run

    captured: dict[str, object] = {}

    def fake_complete(self, request):
        captured["request"] = request
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr("blocks.ai.complete.LLMGateway.complete", fake_complete)

    result = run(
        {
            "model": "stub/default",
            "messages": [{"role": "user", "content": "What happened today?"}],
        },
        {"timezone": "Asia/Tokyo"},
        settings_owner=_settings_owner(tmp_path),
    )

    messages = captured["request"]["messages"]
    assert result["status"] == "ok"
    assert messages[0]["role"] == "system"
    assert "Current date/time:" in messages[0]["content"]
    assert "Today is " in messages[0]["content"]


def test_ai_complete_passes_profile_authority_context(
    monkeypatch, tmp_path: Path
):
    from blocks.ai.complete import run

    captured: dict[str, object] = {}

    def fake_complete(self, request):
        captured["request"] = request
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr("blocks.ai.complete.LLMGateway.complete", fake_complete)

    result = run(
        {
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "messages": [{"role": "user", "content": "hello"}],
            "conversation_id": "conv-1",
        },
        {"profile_id": "defaultspack.mimo_coding_company"},
        settings_owner=_settings_owner(tmp_path),
    )

    assert result["status"] == "ok"
    assert captured["request"]["authority_context"] == {
        "profile_id": "defaultspack.mimo_coding_company",
        "conversation_id": "conv-1",
        "principal_id": "profile:defaultspack.mimo_coding_company",
    }


def test_ai_complete_does_not_synthesize_principal_from_payload_profile(
    monkeypatch, tmp_path: Path
):
    from blocks.ai.complete import run

    captured: dict[str, object] = {}

    def fake_complete(self, request):
        captured["request"] = request
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr("blocks.ai.complete.LLMGateway.complete", fake_complete)

    result = run(
        {
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "messages": [{"role": "user", "content": "hello"}],
            "profile_id": "payload-profile",
            "principal_id": "profile:payload-spoof",
        },
        {},
        settings_owner=_settings_owner(tmp_path),
    )

    assert result["status"] == "ok"
    authority_context = captured["request"].get("authority_context", {})
    assert authority_context.get("profile_id") == "payload-profile"
    assert "principal_id" not in authority_context


def test_llm_gateway_injects_temporal_context_once():
    from domain.ai_client.gateway import LLMGateway

    class Client:
        def __init__(self):
            self.requests = []

        def complete(self, model, messages, tools=None, params=None):
            self.requests.append(
                {"model": model, "messages": messages, "tools": tools or [], "params": params or {}}
            )
            return {"content": [{"type": "text", "text": "ok"}]}

    client = Client()
    gateway = LLMGateway(client=client)
    gateway.complete(
        {
            "model": "stub/default",
            "messages": [
                {"role": "system", "content": "Current date/time: already present."},
                {"role": "user", "content": "today?"},
            ],
            "params": {"timezone": "Asia/Tokyo"},
        }
    )

    messages = client.requests[0]["messages"]
    assert sum("Current date/time:" in message.get("content", "") for message in messages) == 1


def test_model_call_injects_temporal_context(monkeypatch):
    from domain.ai_client.model_call import call_model

    class Decision:
        selected_model = "stub/default"

        def to_dict(self):
            return {"selected_model": self.selected_model}

    captured: dict[str, object] = {}

    def fake_complete(self, request):
        captured["request"] = request
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr("domain.ai_client.model_call.route_model_request", lambda request: Decision())
    monkeypatch.setattr("domain.ai_client.model_call.get_model_capabilities", lambda model: {})
    monkeypatch.setattr("domain.ai_client.model_call.LLMGateway.complete", fake_complete)

    result = call_model({"question": "What happened today?"}, {"timezone": "Asia/Tokyo"})

    messages = captured["request"]["messages"]
    assert result["status"] == "ok"
    assert messages[0]["role"] == "system"
    assert "Current date/time:" in messages[0]["content"]
