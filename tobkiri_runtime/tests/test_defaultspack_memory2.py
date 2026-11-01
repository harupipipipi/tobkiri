from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_owner_bindings")

from domain.memory.store import MemoryStore  # noqa: E402
from domain.memory2.flush import flush_memory  # noqa: E402
from domain.memory2.markdown_store import MarkdownMemoryStore  # noqa: E402
from domain.memory2.memos import DEFAULT_PERSONALIZATION_FOLDER_ID, MemoStore  # noqa: E402
from domain.memory2.sqlite_store import MemorySQLiteStore  # noqa: E402


def test_memory2_sqlite_and_markdown_store(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MEMORY2_DIR", str(tmp_path / "memory"))
    MemorySQLiteStore._instance = None

    store = MemorySQLiteStore()
    entry = store.add("Rumi likes durable memory", {"kind": "fact"}, scope="user")
    projection_path = MarkdownMemoryStore().append_memory(
        entry["content"], entry["metadata"]
    )

    results = store.search("durable", limit=3)
    assert entry["id"] in {item["id"] for item in results}
    assert projection_path == tmp_path / "memory" / "MEMORY.md"
    assert not projection_path.exists()


def test_legacy_memory_store_bridges_to_memory2(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MEMORY2_DIR", str(tmp_path / "memory"))
    MemorySQLiteStore._instance = None
    MemoryStore._instance = None
    MemoryStore._initialized = False

    legacy = MemoryStore()
    entry = legacy.store("Project convention: keep APIs compatible", {"scope": "project"})

    assert entry["scope"] == "project"
    assert entry["source"] == "legacy_memory_facade"
    assert legacy.recall("compatible", limit=1)[0]["id"] == entry["id"]


def test_memory_flush_returns_refs(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MEMORY2_DIR", str(tmp_path / "memory"))
    MemorySQLiteStore._instance = None

    refs = flush_memory(["Decision: use SQLite WAL", "NO_REPLY"], scope="session")

    assert len(refs) == 1
    assert refs[0]["scope"] == "session"


def test_memory_sqlite_store_supports_parallel_thread_access(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MEMORY2_DIR", str(tmp_path / "memory"))
    MemorySQLiteStore._instance = None
    store = MemorySQLiteStore()

    def add_and_search(index: int):
        store.add(f"parallel memory {index}", {"token": "secret-value"}, scope="session")
        return len(store.search("parallel", limit=50, scope="session"))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(add_and_search, range(20)))

    assert max(results) >= 1
    assert store.search("secret-value", limit=5) == []


def test_memory2_memo_folders_notes_and_first_party_tools(
    tmp_path, monkeypatch, defaultspack_capability_plan_context
):
    from domain.tool.permission_checker import PermissionChecker
    from domain.tool.executor import ToolExecutor
    from domain.tool.registry import ToolRegistry
    import backend.tool.permission_policy as permission_policy

    monkeypatch.setenv("RUMI_DEFAULTSPACK_MEMORY2_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_TOOL_PERMISSION_POLICY_PATH", str(tmp_path / "permission_policy.json"))
    MemorySQLiteStore._instance = None
    ToolRegistry._instance = None
    permission_policy._POLICY_STORE = None

    memos = MemoStore()
    folders = memos.list_folders()
    assert folders[0]["id"] == DEFAULT_PERSONALIZATION_FOLDER_ID

    note = memos.create_note("Favorite greeting: おはよう", title="Greeting")
    assert note["folder_id"] == DEFAULT_PERSONALIZATION_FOLDER_ID
    assert note["content"] == "Favorite greeting: おはよう"
    assert memos.get_note(note["id"])["title"] == "Greeting"
    assert memos.search_notes("greeting")[0]["id"] == note["id"]

    tools = {tool["tool_id"]: tool for tool in ToolRegistry().list_tools()}
    assert "memo_create_note" in tools
    assert "memo_search_notes" in tools
    assert PermissionChecker().decide("memo_list_notes", tool_def=tools["memo_list_notes"])["allowed"] is True
    plan_context = defaultspack_capability_plan_context(
        "memo_create_note", "memo_note_upsert"
    )

    executed = ToolExecutor().execute(
        "memo_create_note",
        {"title": "Tone", "content": "Use concise, warm replies."},
        {**plan_context, "profile_policy": {"yolo_mode": True}},
    )
    assert executed["is_error"] is False
    assert memos.search_notes("warm replies")

    upserted = ToolExecutor().execute(
        "memo_note_upsert",
        {"title": "Current work", "content": "PR97 live memo write should not dead-end on approval."},
        plan_context,
    )
    assert upserted["is_error"] is False
    assert not (isinstance(upserted.get("widget"), dict) and upserted["widget"].get("type") == "approval_request")
    assert memos.search_notes("live memo write")

    path_upserted = ToolExecutor().execute(
        "memo_note_upsert",
        {"folder_id": "personalization/current_work_2026-05-20", "content": "Path-like memo target works."},
        plan_context,
    )
    assert path_upserted["is_error"] is False
    assert memos.search_notes("Path-like memo target")[0]["title"] == "current_work_2026-05-20"
