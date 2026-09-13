from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from _coding_contract_fixture import bind_verified_coding_contracts  # noqa: E402


def _approved_context():
    from domain.tool_policy.internal_context import mark_tool_server_approval_context

    return mark_tool_server_approval_context({})


@pytest.fixture()
def coding_workspace_store(tmp_path, monkeypatch):
    store_path = tmp_path / "coding_workspaces.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH", str(store_path))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    return store_path


def test_workspace_store_persists_validated_schema(tmp_path, coding_workspace_store):
    from domain.coding.workspace_store import WorkspaceStore

    root = tmp_path / "project"
    root.mkdir()

    store = WorkspaceStore()
    record = store.create(root, workspace_id="ws1")

    assert record["workspace_id"] == "ws1"
    assert record["label"] == "project"
    assert record["root_path"] == str(root.resolve())
    assert record["trusted"] is False
    assert record["trust_granted_at"] is None
    assert record["last_used_at"]
    assert set(record["metadata"]) >= {"git_root", "default_branch"}
    assert WorkspaceStore().get("ws1") == record

    selected = WorkspaceStore().select("ws1")
    assert selected["workspace_id"] == "ws1"
    assert WorkspaceStore().selected_workspace_id() == "ws1"


def test_workspace_store_retries_transient_replace_failure(tmp_path, coding_workspace_store, monkeypatch):
    import domain.coding.workspace_store as workspace_store_module
    from domain.coding.workspace_store import WorkspaceStore

    root = tmp_path / "project"
    root.mkdir()
    store = WorkspaceStore()
    target = store.storage_path
    original_replace = workspace_store_module.Path.replace
    attempts = []

    def flaky_replace(self, destination):
        if destination == target and self.name.startswith(".coding_workspaces.json.") and len(attempts) < 2:
            attempts.append(self.name)
            raise PermissionError("Access is denied")
        return original_replace(self, destination)

    monkeypatch.setattr(workspace_store_module.Path, "replace", flaky_replace)
    monkeypatch.setattr(workspace_store_module.time, "sleep", lambda _seconds: None)

    record = store.create(root, workspace_id="ws1")

    assert record["workspace_id"] == "ws1"
    assert len(attempts) == 2
    assert json.loads(target.read_text(encoding="utf-8"))["workspaces"]["ws1"]["workspace_id"] == "ws1"
    assert list(tmp_path.glob(".coding_workspaces.json.*.tmp")) == []


def test_workspace_store_shares_lock_for_same_storage_path(coding_workspace_store):
    from domain.coding.workspace_store import WorkspaceStore

    first = WorkspaceStore()
    second = WorkspaceStore()

    assert first.storage_path == second.storage_path
    assert first._lock is second._lock


def test_workspace_resolver_prefers_id_then_legacy_root_then_cwd(tmp_path, coding_workspace_store):
    from domain.coding.workspace_resolver import WorkspaceResolver
    from domain.coding.workspace_store import WorkspaceStore

    primary = tmp_path / "primary"
    legacy = tmp_path / "legacy"
    cwd_root = tmp_path / "cwd-root"
    primary.mkdir()
    legacy.mkdir()
    cwd_root.mkdir()
    WorkspaceStore().create(primary, workspace_id="primary")

    resolver = WorkspaceResolver()
    by_id = resolver.resolve({"workspace_id": "primary", "workspace_root": str(legacy)})
    by_root = resolver.resolve({"workspace_root": str(legacy)})
    by_cwd = resolver.resolve({"cwd": str(cwd_root)}, allow_cwd_fallback=True)

    assert by_id.root_path == str(primary.resolve())
    assert by_id.workspace_id == "primary"
    assert by_root.root_path == str(legacy.resolve())
    assert by_root.workspace_id is None
    assert by_cwd.root_path == str(cwd_root.resolve())
    assert by_cwd.source == "cwd"


def test_workspace_resolver_inherits_profile_policy_when_explicit_values_missing(tmp_path, coding_workspace_store):
    from domain.coding.workspace_resolver import WorkspaceResolver
    from domain.coding.workspace_store import WorkspaceStore

    group_root = tmp_path / "group"
    explicit_root = tmp_path / "explicit"
    legacy_root = tmp_path / "legacy"
    group_root.mkdir()
    explicit_root.mkdir()
    legacy_root.mkdir()
    WorkspaceStore().create(group_root, workspace_id="group-ws")
    WorkspaceStore().create(explicit_root, workspace_id="explicit-ws")

    resolver = WorkspaceResolver()
    inherited = resolver.resolve({}, {"profile_policy": {"workspace_id": "group-ws"}})
    inherited_root = resolver.resolve({}, {"profile_policy": {"workspace_root": str(legacy_root)}})
    explicit = resolver.resolve(
        {"workspace_id": "explicit-ws"},
        {"profile_policy": {"workspace_id": "group-ws"}},
    )

    assert inherited.root_path == str(group_root.resolve())
    assert inherited.workspace_id == "group-ws"
    assert inherited.source == "workspace_id"
    assert inherited_root.root_path == str(legacy_root.resolve())
    assert inherited_root.source == "workspace_root"
    assert explicit.root_path == str(explicit_root.resolve())
    assert explicit.workspace_id == "explicit-ws"


def test_workspace_trust_policy_gates_workspace_id_mutations(
    tmp_path, coding_workspace_store, monkeypatch
):
    from blocks.coding.file_create import run as file_create_run
    from blocks.coding.file_write import run as file_write_run
    from blocks.coding.workspace.trust import run as trust_run
    from domain.coding.workspace_store import WorkspaceStore

    root = tmp_path / "project"
    root.mkdir()
    WorkspaceStore().create(root, workspace_id="ws1")
    bind_verified_coding_contracts(
        monkeypatch,
        root,
        workspace_id="ws1",
        trusted=False,
    )

    denied = file_write_run(
        {"workspace_id": "ws1", "path": "note.txt", "content": "blocked"},
        _approved_context(),
    )
    assert denied["status"] == "error"
    assert denied["error"]["code"] == "WORKSPACE_UNTRUSTED"
    assert not (root / "note.txt").exists()

    trusted = trust_run({"workspace_id": "ws1"}, _approved_context())
    assert trusted["status"] == "ok"
    assert trusted["data"]["workspace"]["trusted"] is True
    assert trusted["data"]["workspace"]["trust_granted_at"]

    created = file_create_run(
        {"workspace_id": "ws1", "path": "note.txt", "content": "seed"},
        _approved_context(),
    )
    assert created["status"] == "ok", created

    written = file_write_run(
        {"workspace_id": "ws1", "path": "note.txt", "content": "ok"},
        _approved_context(),
    )
    assert written["status"] == "ok"
    assert (root / "note.txt").read_text(encoding="utf-8") == "ok"


def test_workspace_blocks_crud_envelopes(tmp_path, coding_workspace_store, monkeypatch):
    from blocks.coding.workspace.create import run as create_run
    from blocks.coding.workspace.get import run as get_run
    from blocks.coding.workspace.list import run as list_run
    from blocks.coding.workspace.select import run as select_run
    from blocks.coding.workspace.update import run as update_run

    root = tmp_path / "project"
    root.mkdir()
    bind_verified_coding_contracts(monkeypatch, root, workspace_id="ws1")

    created = create_run(
        {"workspace_id": "ws1", "root_path": str(root)}, _approved_context()
    )
    updated = update_run(
        {"workspace_id": "ws1", "label": "Main Project"}, _approved_context()
    )
    selected = select_run({"workspace_id": "ws1"}, _approved_context())
    fetched = get_run({"workspace_id": "ws1"}, {})
    listed = list_run({}, {})

    assert created["status"] == "ok"
    assert updated["status"] == "ok"
    assert updated["data"]["workspace"]["label"] == "Main Project"
    assert selected["status"] == "ok"
    assert selected["data"]["selected_workspace_id"] == "ws1"
    assert fetched["status"] == "ok"
    assert fetched["data"]["workspace"]["workspace_id"] == "ws1"
    assert listed["status"] == "ok"
    assert listed["data"]["selected_workspace_id"] == "ws1"
    assert [item["workspace_id"] for item in listed["data"]["workspaces"]] == ["ws1"]


def test_file_read_write_with_workspace_id_and_context(
    tmp_path, coding_workspace_store, monkeypatch
):
    from blocks.coding.context import run as context_run
    from blocks.coding.file_create import run as file_create_run
    from blocks.coding.file_read import run as file_read_run
    from blocks.coding.file_write import run as file_write_run
    from domain.coding.workspace_store import WorkspaceStore

    root = tmp_path / "project"
    root.mkdir()
    WorkspaceStore().create(root, workspace_id="ws1", trusted=True)
    bind_verified_coding_contracts(monkeypatch, root, workspace_id="ws1")

    created = file_create_run(
        {"workspace_id": "ws1", "path": "README.md", "content": "seed\n"},
        _approved_context(),
    )
    assert created["status"] == "ok", created

    written = file_write_run(
        {"workspace_id": "ws1", "path": "README.md", "content": "# Hello\n"},
        _approved_context(),
    )
    read = file_read_run({"workspace_id": "ws1", "path": "README.md"}, {})
    context = context_run({"workspace_id": "ws1"}, {})

    assert written["status"] == "ok"
    assert read["status"] == "ok"
    assert read["data"]["content"] == "# Hello\n"
    assert read["data"]["workspace_id"] == "ws1"
    assert context["status"] == "ok"
    assert context["data"]["workspace_id"] == "ws1"
    assert context["data"]["root_folder"] is None
    assert context["data"]["files"] == ["README.md"]


def test_file_read_supports_line_ranges(tmp_path, coding_workspace_store, monkeypatch):
    from blocks.coding.file_read import run as file_read_run
    from domain.coding.workspace_store import WorkspaceStore

    root = tmp_path / "project"
    root.mkdir()
    WorkspaceStore().create(root, workspace_id="ws1", trusted=True)
    bind_verified_coding_contracts(monkeypatch, root, workspace_id="ws1")
    (root / "notes.txt").write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")

    read = file_read_run({"workspace_id": "ws1", "path": "notes.txt", "start_line": 2, "end_line": 3}, {})

    assert read["status"] == "ok"
    assert read["data"]["content"] == "line2\nline3\n"
    assert read["data"]["start_line"] == 2
    assert read["data"]["end_line"] == 3
    assert read["data"]["total_lines"] == 4
    assert read["data"]["end_line"] == 3


def test_file_read_rejects_invalid_line_ranges(tmp_path, coding_workspace_store):
    from blocks.coding.file_read import run as file_read_run
    from domain.coding.workspace_store import WorkspaceStore

    root = tmp_path / "project"
    root.mkdir()
    WorkspaceStore().create(root, workspace_id="ws1", trusted=True)
    (root / "notes.txt").write_text("line1\nline2\n", encoding="utf-8")

    invalid_start = file_read_run({"workspace_id": "ws1", "path": "notes.txt", "start_line": 0}, {})
    invalid_order = file_read_run({"workspace_id": "ws1", "path": "notes.txt", "start_line": 2, "end_line": 1}, {})

    assert invalid_start["status"] == "error"
    assert invalid_start["error"]["code"] == "INVALID_INPUT"
    assert invalid_order["status"] == "error"
    assert invalid_order["error"]["code"] == "INVALID_INPUT"


def test_workspace_id_traversal_and_git_mutation_are_blocked(
    tmp_path, coding_workspace_store, monkeypatch
):
    from blocks.coding.file_read import run as file_read_run
    from blocks.coding.file_write import run as file_write_run
    from domain.coding.workspace_store import WorkspaceStore

    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    WorkspaceStore().create(root, workspace_id="ws1", trusted=True)
    bind_verified_coding_contracts(monkeypatch, root, workspace_id="ws1")

    traversal = file_read_run({"workspace_id": "ws1", "path": "../secret.txt"}, {})
    assert traversal["status"] == "error"
    assert traversal["error"]["code"] == "PATH_RESTRICTED"

    git_mutation = file_write_run(
        {"workspace_id": "ws1", "path": ".git/config", "content": "nope"},
        _approved_context(),
    )
    assert git_mutation["status"] == "error"
    assert not (root / ".git" / "config").exists()


def test_snapshot_and_workspace_mutations_are_sensitive_routes():
    from domain.safety.local_guard import is_sensitive_coding_path

    assert is_sensitive_coding_path("/api/coding/files/snapshot", "POST") is True
    assert is_sensitive_coding_path("/api/coding/checkpoints", "GET") is True
    assert is_sensitive_coding_path("/api/coding/checkpoints", "POST") is True
    assert is_sensitive_coding_path("/api/coding/workspaces", "GET") is False
    assert is_sensitive_coding_path("/api/coding/workspaces", "POST") is True
    assert is_sensitive_coding_path("/api/coding/workspaces/trust", "POST") is True
