from __future__ import annotations

import sys
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_conversation_owner")


def test_chat_search_returns_spotlight_conversation_results(tmp_path, monkeypatch):
    from blocks.chat.search import run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    weather = store.create_conversation(model="stub/default")
    shopping = store.create_conversation(model="stub/default")
    store.update_conversation(weather["id"], {"title": "Google Chrome 天気検索"})
    store.update_conversation(shopping["id"], {"title": "買い物メモ"})
    store.add_message(weather["id"], {"role": "user", "content": "Google Chromeで今日の天気を検索して"})
    store.add_message(shopping["id"], {"role": "user", "content": "牛乳と卵を買う"})

    result = run({"query": "天気", "mode": "conversations", "limit": 5}, {})

    assert result["status"] == "ok"
    assert result["data"]["total"] == 1
    assert result["data"]["results"][0]["conversation_id"] == weather["id"]
    assert result["data"]["results"][0]["exact_score"] == 1.0
    assert result["data"]["results"][0]["matches"][0]["exact"] is True
    ChatStore._instance = None


def test_legacy_chat_search_still_returns_matching_messages(tmp_path, monkeypatch):
    from blocks.chat.search import run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    store.add_message(conversation["id"], {"role": "user", "content": "完全一致検索"})

    result = run({"query": "完全一致"}, {})

    assert result["status"] == "ok"
    assert result["data"]["results"][0]["conversation_id"] == conversation["id"]
    assert result["data"]["results"][0]["raw_text"] == "完全一致検索"
    ChatStore._instance = None
