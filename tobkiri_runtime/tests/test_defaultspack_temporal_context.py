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
