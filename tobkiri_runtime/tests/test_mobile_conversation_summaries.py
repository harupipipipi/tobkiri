from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.contract


def _module():
    from blocks.mobile import conversations

    return conversations


class _Store:
    def __init__(self, conversations: list[dict[str, Any]], total: int) -> None:
        self.conversations = conversations
        self.total = total
        self.include_messages: bool | None = None

    def list_conversations(self, *, include_messages: bool):
        self.include_messages = include_messages
        return self.conversations, self.total


def test_mobile_list_returns_allowlisted_summary_and_owner_count(monkeypatch):
    module = _module()
    store = _Store(
        [
            {
                "id": "conversation-1",
                "title": "Daily notes",
                "created_at": 1_700_000_000_000,
                "updated_at": "1_700_000_000_123",
                "conversation_revision": "7",
                "is_pinned": True,
                "message_count": 11,
                "metadata": {"do_not_return": "private"},
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "latest answer"},
                ],
            }
        ],
        1,
    )
    monkeypatch.setattr(module, "_store", lambda: store)

    result = module.list_conversations({}, None)

    assert result == {
        "status": "ok",
        "data": {
            "count": 1,
            "conversations": [
                {
                    "id": "conversation-1",
                    "title": "Daily notes",
                    "message_count": 11,
                    "updated_at": 1_700_000_000_123,
                    "created_at": 1_700_000_000_000,
                    "pinned": True,
                    "revision": 7,
                    "preview": "latest answer",
                }
            ],
        },
    }
    assert store.include_messages is True
    assert "messages" not in result["data"]["conversations"][0]
    assert "metadata" not in result["data"]["conversations"][0]


def test_mobile_preview_skips_private_internal_and_tool_messages(monkeypatch):
    module = _module()
    store = _Store(
        [
            {
                "id": "conversation-privacy",
                "title": "Privacy",
                "message_count": 8,
                "updated_at": 42,
                "messages": [
                    {"role": "user", "content": "safe older message"},
                    {"role": "system", "content": "system prompt"},
                    {"role": "tool", "content": "tool result"},
                    {
                        "role": "assistant",
                        "content": "private assistant text",
                        "metadata": {"private": True},
                    },
                    {
                        "role": "user",
                        "content": "hidden user text",
                        "hidden": True,
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_result", "text": "tool secret"}
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": "sensitive assistant text",
                        "metadata": {"visibility": "sensitive"},
                    },
                ],
            }
        ],
        1,
    )
    monkeypatch.setattr(module, "_store", lambda: store)

    result = module.list_conversations({}, None)

    assert result["status"] == "ok"
    summary = result["data"]["conversations"][0]
    assert summary["preview"] == "safe older message"
    assert summary["message_count"] == 8
    assert "private assistant text" not in str(result)
    assert "tool secret" not in str(result)
    assert "sensitive assistant text" not in str(result)


def test_mobile_preview_normalizes_redacts_and_caps_text():
    module = _module()
    long_text = (
        "  Keep   this\ntext  "
        "api_key=super-secret-value "
        "Bearer abcdefghijklmnop "
        "sk-12345678901234567890 "
        + ("tail " * 100)
    )

    preview = module._latest_safe_preview(
        [{"role": "assistant", "content": long_text}]
    )

    assert len(preview) <= module.MAX_PREVIEW_LENGTH
    assert preview.endswith("…")
    assert "  " not in preview
    assert "\n" not in preview
    assert "super-secret-value" not in preview
    assert "abcdefghijklmnop" not in preview
    assert "12345678901234567890" not in preview
    assert "Keep this" in preview


def test_mobile_list_returns_stable_error_without_legacy_fallback(monkeypatch):
    module = _module()

    class _BrokenStore:
        def list_conversations(self, *, include_messages: bool):
            assert include_messages is True
            raise RuntimeError("owner unavailable")

    monkeypatch.setattr(module, "_store", _BrokenStore)

    result = module.list_conversations({}, None)

    assert result == {
        "status": "error",
        "error": {
            "code": "CONVERSATION_LIST_FAILED",
            "message": "conversation list unavailable",
        },
    }
