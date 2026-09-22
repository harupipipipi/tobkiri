from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_owner_bindings")


def test_provider_trace_redacts_api_keys_and_images(tmp_path, monkeypatch):
    from domain.ai_client.provider_trace import write_provider_trace
    from domain.chat.store import ChatStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"))
    ChatStore._instance = None
    store = ChatStore()
    conv = store.create_conversation(model="stub/default")

    meta = write_provider_trace(
        conversation_id=conv["id"],
        request_id="req",
        provider="openai",
        model="gpt",
        api_family="openai_chat",
        ir_schema_version="rumi.chat.ir.v2",
        capability_summary={},
        planning_metadata={"token": "secret-value"},
        dropped_features=[{"feature": "image"}],
        bridge_actions=[],
        warnings=[],
        compiled_payload={"headers": {"Authorization": "Bearer sk-secret"}, "image": "data:image/png;base64,abcd"},
        response_summary={"finish_reason": "stop"},
        store=store,
    )
    payload = json.loads(Path(meta["trace_path"]).read_text(encoding="utf-8"))

    assert payload["planning_metadata"]["token"] == "[REDACTED]"
    assert payload["trace_mode"] == "summary"
    assert payload["compiled_payload"]["mode"] == "summary"
    assert "headers" not in payload["compiled_payload"]
    assert payload["dropped_features"][0]["feature"] == "image"
    ChatStore._instance = None


def test_provider_trace_full_mode_redacts_and_truncates_payload(tmp_path, monkeypatch):
    from domain.ai_client.provider_trace import write_provider_trace
    from domain.chat.store import ChatStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_PROVIDER_TRACE", "full")
    ChatStore._instance = None
    store = ChatStore()
    conv = store.create_conversation(model="stub/default")

    long_text = "x" * 5000
    meta = write_provider_trace(
        conversation_id=conv["id"],
        request_id="req-full",
        provider="openai",
        model="gpt",
        api_family="openai_chat",
        ir_schema_version="rumi.chat.ir.v2",
        capability_summary={},
        planning_metadata={},
        dropped_features=[],
        bridge_actions=[],
        warnings=[],
        compiled_payload={
            "headers": {"Authorization": "Bearer sk-secret"},
            "image": "data:image/png;base64,abcd",
            "legacy_messages": [{"role": "user", "content": long_text}],
        },
        response_summary={"finish_reason": "stop"},
        store=store,
    )
    payload = json.loads(Path(meta["trace_path"]).read_text(encoding="utf-8"))

    assert payload["trace_mode"] == "full"
    assert payload["compiled_payload"]["headers"]["Authorization"] == "[REDACTED]"
    assert payload["compiled_payload"]["image"] == "data:image/png;base64,[REDACTED:4 chars]"
    assert "[TRUNCATED " in payload["compiled_payload"]["legacy_messages"][0]["content"]
    ChatStore._instance = None
