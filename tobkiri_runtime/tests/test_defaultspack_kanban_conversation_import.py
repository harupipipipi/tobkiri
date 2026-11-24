from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _reset_stores(monkeypatch, tmp_path):
    kanban_db = tmp_path / "kanban.db"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_KANBAN_DB_PATH", str(kanban_db))

    from domain.chat.store import ChatStore
    from domain.kanban.store import KanbanStore

    ChatStore._instance = None
    KanbanStore._instance = None
    return ChatStore, kanban_db


def _kanban_service(db_path):
    from domain.kanban.service import KanbanService
    from ecosystem.rumi_kanban_state_store_pack.runtime.store import KanbanStateStore

    return KanbanService(db_path=db_path, state_store_factory=KanbanStateStore)


def test_import_conversation_creates_group_board_cards_and_prompt_note(tmp_path, monkeypatch):
    ChatStore, kanban_db = _reset_stores(monkeypatch, tmp_path)

    from domain.kanban.service import append_kanban_system_prompt_note

    chat_store = ChatStore()
    conversation = chat_store.create_conversation(model="stub/default", group_id="group-alpha")
    conversation = chat_store.update_conversation(
        conversation["id"],
        {
            "title": "Launch checklist",
            "metadata": {"group_id": "group-alpha", "workspace_id": "workspace-1"},
        },
    )
    chat_store.add_message(
        conversation["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "TODO: write release notes"}],
            "raw_text": "TODO: write release notes",
        },
    )

    service = _kanban_service(kanban_db)
    board = service.bootstrap_board({"scope_type": "group", "scope_id": "group-alpha"})["board"]
    imported = service.import_conversation(board["board_id"], {"conversation_id": conversation["id"], "use_ai": False})

    assert imported["board"]["scope_type"] == "group"
    assert imported["cards"][0]["conversation_id"] == conversation["id"]
    assert imported["cards"][0]["source_type"] == "conversation"
    assert imported["cards"][0]["title"] == "write release notes"
    assert imported["cards"][0]["metadata"]["conversation_group_id"] == "group-alpha"

    updated = chat_store.get_conversation(conversation["id"])
    assert updated["metadata"]["kanban"]["added"] is True
    assert updated["metadata"]["kanban"]["board_id"] == board["board_id"]
    prompt = append_kanban_system_prompt_note("base prompt", updated)
    assert "base prompt" in prompt
    assert "Kanbanに追加されています" in prompt


def test_synced_conversation_updates_existing_kanban_board(tmp_path, monkeypatch):
    ChatStore, kanban_db = _reset_stores(monkeypatch, tmp_path)

    from domain.kanban.chat_sync import sync_conversation_kanban
    from ecosystem.rumi_kanban_state_store_pack.runtime.store import KanbanStateStore

    chat_store = ChatStore()
    conversation = chat_store.create_conversation(model="stub/default", group_id="group-alpha")
    conversation = chat_store.update_conversation(
        conversation["id"],
        {
            "title": "Task sync",
            "metadata": {"group_id": "group-alpha"},
        },
    )
    chat_store.add_message(
        conversation["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "TODO: initial task"}],
            "raw_text": "TODO: initial task",
        },
    )

    service = _kanban_service(kanban_db)
    board = service.bootstrap_board({"scope_type": "group", "scope_id": "group-alpha"})["board"]
    first = service.import_conversation(board["board_id"], {"conversation_id": conversation["id"], "use_ai": False})
    assert [card["title"] for card in first["cards"]] == ["initial task"]

    chat_store.add_message(
        conversation["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "TODO: browser QA"}],
            "raw_text": "TODO: browser QA",
        },
    )
    synced = sync_conversation_kanban(
        conversation["id"],
        reason="test",
        db_path=kanban_db,
        state_store_factory=KanbanStateStore,
    )

    assert synced is not None
    titles = [card["title"] for card in synced["cards"]]
    assert "initial task" in titles
    assert "browser QA" in titles


def test_import_conversation_ignores_provider_error_diagnostics(tmp_path, monkeypatch):
    ChatStore, kanban_db = _reset_stores(monkeypatch, tmp_path)

    chat_store = ChatStore()
    conversation = chat_store.create_conversation(model="openai/gpt-5.4")
    conversation = chat_store.update_conversation(conversation["id"], {"title": "Provider error review"})
    chat_store.add_message(
        conversation["id"],
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "APIエラーでこのタスクを終了しました。\n"
                        "- モデル: openai/gpt-5.4\n"
                        "- 原因: AI provider HTTP 401 (invalid_request_error, invalid_api_key)\n"
                        "- 内容: Incorrect API key provided: sk-test. You can find your API key at "
                        "https://platform.openai.com/account/api-keys.\n"
                        "- 次に試すこと: APIキー、OAuth、モデル利用権限、またはローカル承認が拒否されています。"
                    ),
                }
            ],
            "raw_text": (
                "APIエラーでこのタスクを終了しました。\n"
                "- モデル: openai/gpt-5.4\n"
                "- 原因: AI provider HTTP 401 (invalid_request_error, invalid_api_key)\n"
                "- 内容: Incorrect API key provided: sk-test. You can find your API key at "
                "https://platform.openai.com/account/api-keys.\n"
                "- 次に試すこと: APIキー、OAuth、モデル利用権限、またはローカル承認が拒否されています。"
            ),
        },
    )

    service = _kanban_service(kanban_db)
    board = service.bootstrap_board({"scope_type": "conversation", "scope_id": conversation["id"]})["board"]
    imported = service.import_conversation(board["board_id"], {"conversation_id": conversation["id"], "use_ai": False})

    assert [card["title"] for card in imported["cards"]] == ["Provider error review"]
    assert all("openai/gpt-5.4" not in card["title"] for card in imported["cards"])
    assert "openai/gpt-5.4" not in str(imported["cards"][0]["description"])
    assert "モデル/API" not in str(imported["cards"][0]["description"])


def test_reimport_removes_stale_conversation_cards(tmp_path, monkeypatch):
    ChatStore, kanban_db = _reset_stores(monkeypatch, tmp_path)

    chat_store = ChatStore()
    conversation = chat_store.create_conversation(model="stub/default")
    conversation = chat_store.update_conversation(conversation["id"], {"title": "Shrink import"})

    service = _kanban_service(kanban_db)
    board = service.bootstrap_board({"scope_type": "conversation", "scope_id": conversation["id"]})["board"]
    first = service.import_conversation(
        board["board_id"],
        {
            "conversation_id": conversation["id"],
            "tasks": [{"title": "First task"}, {"title": "Stale task"}],
        },
    )
    assert [card["title"] for card in first["cards"]] == ["First task", "Stale task"]

    second = service.import_conversation(
        board["board_id"],
        {
            "conversation_id": conversation["id"],
            "tasks": [{"title": "First task"}],
        },
    )

    assert [card["title"] for card in second["cards"]] == ["First task"]
    snapshot = service.get_board(board["board_id"])
    assert [card["title"] for card in snapshot["cards"]] == ["First task"]


def test_import_conversation_passes_provider_timeout_and_falls_back(tmp_path, monkeypatch):
    ChatStore, kanban_db = _reset_stores(monkeypatch, tmp_path)

    from domain.ai_client.client import AIClient

    def timeout_complete(self, model, messages, tools=None, params=None):
        del self, model, messages, tools, params
        raise TimeoutError("provider timed out after requested timeout")

    monkeypatch.setattr(AIClient, "complete", timeout_complete)

    chat_store = ChatStore()
    conversation = chat_store.create_conversation(model="stub/default", group_id="group-alpha")
    conversation = chat_store.update_conversation(conversation["id"], {"title": "Timeout fallback"})
    chat_store.add_message(
        conversation["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "TODO: keep UI responsive"}],
            "raw_text": "TODO: keep UI responsive",
        },
    )

    service = _kanban_service(kanban_db)
    board = service.bootstrap_board({"scope_type": "conversation", "scope_id": conversation["id"]})["board"]
    imported = service.import_conversation(
        board["board_id"],
        {
            "conversation_id": conversation["id"],
            "model": "stub/default",
            "ai_timeout_seconds": 0.01,
            "_authority_context": {"test": True},
        },
    )

    assert imported["cards"][0]["title"] == "keep UI responsive"
    assert imported["cards"][0]["metadata"]["conversation_import"]["extraction"]["source"] == "fallback"
    assert "timed out" in imported["cards"][0]["metadata"]["conversation_import"]["extraction"]["error"]


def test_import_conversation_passes_request_timeout_to_provider(tmp_path, monkeypatch):
    ChatStore, kanban_db = _reset_stores(monkeypatch, tmp_path)

    from domain.ai_client.client import AIClient

    seen_params = {}

    def complete(self, model, messages, tools=None, params=None):
        del self, model, messages, tools
        seen_params.update(params or {})
        return {"text": "{\"tasks\": [{\"title\": \"Imported by AI\"}]}"}

    monkeypatch.setattr(AIClient, "complete", complete)

    chat_store = ChatStore()
    conversation = chat_store.create_conversation(model="stub/default")
    conversation = chat_store.update_conversation(conversation["id"], {"title": "Provider timeout"})

    service = _kanban_service(kanban_db)
    board = service.bootstrap_board({"scope_type": "conversation", "scope_id": conversation["id"]})["board"]
    imported = service.import_conversation(
        board["board_id"],
        {
            "conversation_id": conversation["id"],
            "model": "stub/default",
            "ai_timeout_seconds": 3,
            "_authority_context": {"test": True},
        },
    )

    assert imported["cards"][0]["title"] == "Imported by AI"
    assert seen_params["request_timeout"] == 3.0
    assert seen_params["timeout"] == 3.0


def test_import_conversation_without_authority_context_skips_ai(tmp_path, monkeypatch):
    ChatStore, kanban_db = _reset_stores(monkeypatch, tmp_path)

    from domain.ai_client.client import AIClient

    def fail_complete(self, model, messages, tools=None, params=None):
        del self, model, messages, tools, params
        raise AssertionError("AI should not run without authority context")

    monkeypatch.setattr(AIClient, "complete", fail_complete)

    chat_store = ChatStore()
    conversation = chat_store.create_conversation(model="stub/default")
    conversation = chat_store.update_conversation(conversation["id"], {"title": "Authority fallback"})
    chat_store.add_message(
        conversation["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "TODO: stay local first"}],
            "raw_text": "TODO: stay local first",
        },
    )

    service = _kanban_service(kanban_db)
    board = service.bootstrap_board({"scope_type": "conversation", "scope_id": conversation["id"]})["board"]
    imported = service.import_conversation(
        board["board_id"],
        {"conversation_id": conversation["id"], "model": "stub/default"},
    )

    extraction = imported["cards"][0]["metadata"]["conversation_import"]["extraction"]
    assert imported["cards"][0]["title"] == "stay local first"
    assert extraction["source"] == "fallback"
    assert extraction["reason"] == "authority_context_missing"
