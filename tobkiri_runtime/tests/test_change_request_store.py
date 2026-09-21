from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _init_git_repo(path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is required for change_request snapshot tests")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def _git_commit_all(path: Path, message: str = "initial") -> None:
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=path, check=True, capture_output=True, text=True)


def _change_request_store_class() -> type[Any]:
    module = pytest.importorskip(
        "domain.change_request.store",
        reason="change_request backend implementation is not present yet",
    )
    store_cls = getattr(module, "ChangeRequestStore", None)
    assert store_cls is not None, "domain.change_request.store must expose ChangeRequestStore"
    return store_cls


def _make_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    store_root = tmp_path / "external-review-store" / "change_requests.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHANGE_REQUEST_STORE_PATH", str(store_root))
    store_cls = _change_request_store_class()
    try:
        store = store_cls(store_root=store_root)
    except TypeError:
        try:
            store = store_cls(root=store_root)
        except TypeError:
            store = store_cls(store_root)
    service_module = pytest.importorskip(
        "domain.change_request.service",
        reason="change_request service implementation is not present yet",
    )
    service_cls = getattr(service_module, "ChangeRequestService", None)
    if service_cls is None:
        return store
    return service_cls(store=store)


def _create_change_request(store: Any, workspace_root: Path, **kwargs: Any) -> dict[str, Any]:
    payload = {"workspace_root": str(workspace_root), "title": "Phase 1 review shell"}
    payload.update(kwargs)
    for method_name in ("create", "create_change_request", "create_review"):
        method = getattr(store, method_name, None)
        if method is None:
            continue
        try:
            result = method(**payload)
        except TypeError:
            result = method(str(workspace_root), title=payload["title"])
        assert isinstance(result, dict), f"{method_name} must return a dict payload"
        return result
    raise AssertionError("ChangeRequestStore must expose create() or create_change_request()")


def _get_change_request_id(record: dict[str, Any]) -> str:
    for key in ("change_request_id", "id", "review_id"):
        value = record.get(key)
        if value:
            return str(value)
    raise AssertionError("change_request record must include change_request_id")


def _reload_change_request(store: Any, record: dict[str, Any]) -> dict[str, Any]:
    change_request_id = _get_change_request_id(record)
    for method_name in ("get", "get_change_request", "load"):
        method = getattr(store, method_name, None)
        if method is not None:
            result = method(change_request_id)
            assert isinstance(result, dict), f"{method_name} must return a dict payload"
            return result
    return record


def _snapshot(record: dict[str, Any]) -> dict[str, Any]:
    for key in ("snapshot", "review_snapshot", "diff_seal", "latest_snapshot", "initial_snapshot"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
    return record


def _file_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = _snapshot(record)
    for key in ("files", "file_entries", "entries", "manifest", "file_stats"):
        value = snapshot.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [
                {"path": path, **item}
                if isinstance(item, dict)
                else {"path": path, "value": item}
                for path, item in value.items()
            ]
    raise AssertionError("change_request snapshot must expose file entries")


def _paths(record: dict[str, Any]) -> set[str]:
    return {str(entry.get("path") or entry.get("name") or "") for entry in _file_entries(record)}


def _status_by_path(record: dict[str, Any]) -> dict[str, str]:
    status: dict[str, str] = {}
    for entry in _file_entries(record):
        path = str(entry.get("path") or "")
        state = entry.get("status") or entry.get("change") or entry.get("state") or entry.get("kind")
        if path and state:
            status[path] = str(state)
    git_status = _snapshot(record).get("git_status") or _snapshot(record).get("status")
    if isinstance(git_status, dict):
        for state, paths in git_status.items():
            if isinstance(paths, list):
                for path in paths:
                    status.setdefault(str(path), str(state))
    return status


def _diff_for_path(record: dict[str, Any], path: str) -> str:
    for entry in _file_entries(record):
        if entry.get("path") == path:
            for key in ("diff", "unified_diff", "patch"):
                value = entry.get(key)
                if isinstance(value, str):
                    return value
    snapshot = _snapshot(record)
    normalized_patch = snapshot.get("normalized_patch")
    if isinstance(normalized_patch, str) and f"diff --git a/{path} b/{path}" in normalized_patch:
        return normalized_patch
    diffs = snapshot.get("diffs") or snapshot.get("file_diffs")
    if isinstance(diffs, dict) and isinstance(diffs.get(path), str):
        return diffs[path]
    if isinstance(diffs, list):
        for item in diffs:
            if isinstance(item, dict) and item.get("path") == path:
                value = item.get("diff") or item.get("unified_diff") or item.get("patch")
                if isinstance(value, str):
                    return value
    raise AssertionError(f"change_request snapshot must expose a unified diff for {path}")


def _working_tree_hash(record: dict[str, Any]) -> str:
    snapshot = _snapshot(record)
    for source in (snapshot, record, snapshot.get("diff_seal"), record.get("diff_seal")):
        if isinstance(source, dict):
            value = source.get("working_tree_hash") or source.get("worktree_hash")
            if value:
                return str(value)
    raise AssertionError("change_request snapshot must expose working_tree_hash")


def _revision_payload(service: Any, record: dict[str, Any], *, key: str) -> dict[str, Any]:
    current = service.get(_get_change_request_id(record))
    assert isinstance(current, dict)
    return {
        "expected_revision": record["revision"],
        "expected_snapshot_working_tree_hash": _working_tree_hash(record),
        "expected_current_working_tree_hash": current.get("current_working_tree_hash") or _working_tree_hash(record),
        "idempotency_key": key,
    }


def _detect_drift(store: Any, record: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    change_request_id = _get_change_request_id(record)
    for method_name in ("check_drift", "detect_drift", "drift_status", "refresh"):
        method = getattr(store, method_name, None)
        if method is None:
            continue
        try:
            result = method(change_request_id, workspace_root=str(workspace_root))
        except TypeError:
            result = method(change_request_id)
        assert isinstance(result, dict), f"{method_name} must return a dict payload"
        return result
    raise AssertionError("ChangeRequestStore must expose check_drift() or detect_drift()")


def _is_path_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def test_store_persists_outside_reviewed_repo_and_snapshots_dirty_files(tmp_path, monkeypatch):
    workspace = tmp_path / "reviewed-repo"
    workspace.mkdir()
    _init_git_repo(workspace)
    (workspace / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git_commit_all(workspace)
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (workspace / "untracked.txt").write_text("new\n", encoding="utf-8")

    store = _make_store(tmp_path, monkeypatch)
    created = _create_change_request(store, workspace)
    persisted = _reload_change_request(store, created)

    assert not (workspace / ".rumi_review").exists()
    assert not (workspace / ".rumi_reviews").exists()
    assert not (workspace / ".rumi_change_requests").exists()
    raw_store = getattr(store, "store", store)
    storage_path = (
        persisted.get("storage_path")
        or persisted.get("store_path")
        or str(getattr(raw_store, "storage_path", "") or "")
    )
    if storage_path:
        assert not _is_path_under(Path(storage_path), workspace)

    assert {"tracked.txt", "untracked.txt"} <= _paths(persisted)
    statuses = _status_by_path(persisted)
    assert statuses.get("tracked.txt") in {"modified", "dirty", "M", "changed"}
    assert statuses.get("untracked.txt") in {"untracked", "new", "A", "added"}


def test_revision_mutation_does_not_block_unrelated_change_request(tmp_path):
    store_cls = _change_request_store_class()
    store = store_cls(tmp_path / "change_requests.json")
    for change_request_id in ("cr_slow", "cr_fast"):
        store.create(
            {
                "id": change_request_id,
                "title": change_request_id,
                "revision": 1,
                "latest_snapshot": {"working_tree_hash": "tree-1"},
            }
        )

    slow_started = threading.Event()
    release_slow = threading.Event()

    def slow_mutator(record):
        slow_started.set()
        assert release_slow.wait(timeout=5)
        record["description"] = "slow complete"
        return record, {"completed": True}

    slow_thread = threading.Thread(
        target=lambda: store.mutate_with_revision(
            "cr_slow",
            expected_revision=1,
            expected_snapshot_working_tree_hash="tree-1",
            expected_current_working_tree_hash="tree-1",
            current_working_tree_hash="tree-1",
            idempotency_key="slow-key",
            fingerprint="slow-fingerprint",
            mutator=slow_mutator,
        )
    )
    slow_thread.start()
    assert slow_started.wait(timeout=2)

    started_at = time.monotonic()
    fast, result, replayed = store.mutate_with_revision(
        "cr_fast",
        expected_revision=1,
        expected_snapshot_working_tree_hash="tree-1",
        expected_current_working_tree_hash="tree-1",
        current_working_tree_hash="tree-1",
        idempotency_key="fast-key",
        fingerprint="fast-fingerprint",
        mutator=lambda record: (record, {"completed": True}),
    )
    elapsed = time.monotonic() - started_at

    assert elapsed < 1
    assert fast["revision"] == 2
    assert result == {"completed": True}
    assert replayed is False
    release_slow.set()
    slow_thread.join(timeout=2)
    assert not slow_thread.is_alive()


def test_store_normalizes_legacy_check_full_log_to_bounded_refs(tmp_path):
    store_cls = _change_request_store_class()
    store = store_cls(tmp_path / "change_requests.json")
    large_log = "BEGIN_FULL_LOG" + ("x" * 13000)
    log_ref = "store://change_request/cr_test/checks/chk_test/log"

    created = store.create(
        {
            "id": "cr_test",
            "title": "Legacy check",
            "checks": [
                {
                    "id": "chk_test",
                    "name": "pytest",
                    "status": "passed",
                    "stdout_tail": large_log,
                    "stderr_tail": large_log,
                    "full_log": large_log,
                    "log_ref": log_ref,
                }
            ],
        }
    )
    check = created["checks"][0]
    persisted = json.loads(store.storage_path.read_text(encoding="utf-8"))
    stored_check = persisted["change_requests"]["cr_test"]["checks"][0]

    assert "full_log" not in check
    assert "full_log" not in stored_check
    assert check["full_log_ref"] == log_ref
    assert stored_check["full_log_ref"] == log_ref
    assert "BEGIN_FULL_LOG" not in stored_check["stdout_tail"]
    assert "BEGIN_FULL_LOG" not in stored_check["stderr_tail"]
    assert "BEGIN_FULL_LOG" not in stored_check["log_tail"]
    assert len(stored_check["stdout_tail"]) <= 12000
    assert len(stored_check["stderr_tail"]) <= 12000
    assert len(stored_check["log_tail"]) <= 12000


def test_store_mutate_applies_read_modify_write_under_single_lock(tmp_path):
    store_cls = _change_request_store_class()
    store = store_cls(tmp_path / "change_requests.json")
    store.create({"id": "cr_test", "title": "Atomic update", "comments": []})

    def add_comment(body: str):
        def mutate(record: dict[str, Any]) -> dict[str, Any]:
            comments = list(record.get("comments") or [])
            comments.append({"id": body, "thread_id": body, "kind": "comment", "body": body})
            record["comments"] = comments
            return record

        return store.mutate("cr_test", mutate)

    add_comment("first")
    updated = add_comment("second")

    assert [comment["body"] for comment in updated["comments"]] == ["first", "second"]


def test_synthetic_untracked_diff_looks_like_new_file_diff(tmp_path, monkeypatch):
    workspace = tmp_path / "reviewed-repo"
    workspace.mkdir()
    _init_git_repo(workspace)
    (workspace / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git_commit_all(workspace)
    (workspace / "notes.md").write_text("# Notes\n\nFresh file\n", encoding="utf-8")

    store = _make_store(tmp_path, monkeypatch)
    created = _create_change_request(store, workspace)
    diff = _diff_for_path(created, "notes.md")

    assert "diff --git a/notes.md b/notes.md" in diff
    assert "new file mode" in diff
    assert "--- /dev/null" in diff
    assert "+++ b/notes.md" in diff
    assert "+# Notes" in diff
    assert "+Fresh file" in diff


def test_working_tree_hash_changes_when_content_changes_after_creation(tmp_path, monkeypatch):
    workspace = tmp_path / "reviewed-repo"
    workspace.mkdir()
    _init_git_repo(workspace)
    (workspace / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git_commit_all(workspace)
    (workspace / "tracked.txt").write_text("first dirty state\n", encoding="utf-8")

    store = _make_store(tmp_path, monkeypatch)
    first = _create_change_request(store, workspace)
    first_hash = _working_tree_hash(first)

    (workspace / "tracked.txt").write_text("second dirty state\n", encoding="utf-8")
    second = _create_change_request(store, workspace, title="After local edit")
    second_hash = _working_tree_hash(second)

    assert first_hash != second_hash


def test_stale_drift_detection_reports_snapshot_mismatch(tmp_path, monkeypatch):
    workspace = tmp_path / "reviewed-repo"
    workspace.mkdir()
    _init_git_repo(workspace)
    (workspace / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git_commit_all(workspace)
    (workspace / "tracked.txt").write_text("reviewed dirty state\n", encoding="utf-8")

    store = _make_store(tmp_path, monkeypatch)
    created = _create_change_request(store, workspace)
    original_hash = _working_tree_hash(created)
    (workspace / "tracked.txt").write_text("drift after review shell opened\n", encoding="utf-8")

    drift = store.refresh(
        _get_change_request_id(created),
        _revision_payload(store, created, key="test-drift-refresh"),
    )

    drift_payload = drift.get("drift") if isinstance(drift.get("drift"), dict) else drift
    assert (
        drift_payload.get("stale") is True
        or drift_payload.get("has_drift") is True
        or drift_payload.get("mismatched") is True
        or drift_payload.get("changed") is True
    )
    current_hash = drift_payload.get("current_working_tree_hash") or drift_payload.get("current_worktree_hash")
    snapshot_hash = (
        drift_payload.get("snapshot_working_tree_hash")
        or drift_payload.get("snapshot_worktree_hash")
        or drift_payload.get("previous_working_tree_hash")
        or drift_payload.get("previous_worktree_hash")
    )
    if current_hash and snapshot_hash:
        assert current_hash != snapshot_hash
        assert snapshot_hash == original_hash
    mismatch_paths = set(drift_payload.get("mismatch_paths") or drift_payload.get("changed_paths") or [])
    assert not mismatch_paths or "tracked.txt" in mismatch_paths


def test_service_payloads_hide_absolute_roots_and_drop_patch_metadata(tmp_path, monkeypatch):
    workspace = tmp_path / "reviewed-repo"
    workspace.mkdir()
    _init_git_repo(workspace)
    (workspace / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git_commit_all(workspace)
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    service = _make_store(tmp_path, monkeypatch)
    created = _create_change_request(
        service,
        workspace,
        metadata={"source": "working_tree", "leaked_path": str(workspace.resolve())},
    )
    change_request_id = _get_change_request_id(created)
    patched = service.update_metadata(
        change_request_id,
        {"metadata": {"leaked_path": str(workspace.resolve()), "arbitrary": {"nested": "value"}}},
    )
    listed = service.list(workspace_root=str(workspace))
    fetched = service.get(change_request_id)
    refreshed = service.refresh(
        change_request_id,
        _revision_payload(service, fetched, key="test-redacted-refresh"),
    )

    for payload in (created, patched, listed, fetched, refreshed):
        encoded = json.dumps(payload, sort_keys=True)
        assert str(workspace.resolve()) not in encoded
        assert "/Users/" not in encoded

    assert created["workspace_id"].startswith("ws_")
    assert created["latest_snapshot"]["workspace_root"] == "."
    assert created["latest_snapshot"]["git_root"] == "."
    assert listed[0]["workspace_id"] == created["workspace_id"]
    assert listed[0]["latest_snapshot"]["workspace_root"] == "."
    assert listed[0]["latest_snapshot"]["git_root"] == "."
    assert listed[0]["latest_snapshot"]["file_stats"]
    assert "normalized_patch" not in listed[0]["latest_snapshot"]
    assert "normalized_patch" in fetched["latest_snapshot"]
    assert "leaked_path" not in (patched.get("metadata") or {})
    assert "arbitrary" not in (patched.get("metadata") or {})


def test_large_untracked_files_are_summarized_without_reading_body(tmp_path, monkeypatch):
    workspace = tmp_path / "reviewed-repo"
    workspace.mkdir()
    _init_git_repo(workspace)
    (workspace / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git_commit_all(workspace)
    large_path = workspace / "large.bin"
    large_path.write_bytes(b"x" * (1024 * 1024 + 1))

    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path == large_path:
            raise AssertionError("large untracked files must not be read into memory")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    service = _make_store(tmp_path, monkeypatch)
    created = _create_change_request(service, workspace)
    patch = _diff_for_path(created, "large.bin")

    stat = next(item for item in _file_entries(created) if item.get("path") == "large.bin")
    assert stat["binary"] is True
    assert stat["additions"] == 0
    assert "Binary files /dev/null and b/large.bin differ" in patch


def test_rename_snapshot_preserves_previous_path_for_reviewed_commit_paths(tmp_path, monkeypatch):
    workspace = tmp_path / "reviewed-repo"
    workspace.mkdir()
    _init_git_repo(workspace)
    (workspace / "old_name.txt").write_text("clean\n", encoding="utf-8")
    _git_commit_all(workspace)
    subprocess.run(["git", "mv", "old_name.txt", "new_name.txt"], cwd=workspace, check=True)
    (workspace / "new_name.txt").write_text("clean\nrenamed\n", encoding="utf-8")

    store = _make_store(tmp_path, monkeypatch)
    created = _create_change_request(store, workspace)
    snapshot = _snapshot(created)
    stat = next(item for item in snapshot["file_stats"] if item["path"] == "new_name.txt")

    from domain.change_request.service import selected_snapshot_paths

    assert stat["status"] == "renamed"
    assert stat["previousPath"] == "old_name.txt"
    assert selected_snapshot_paths(snapshot) == ["old_name.txt", "new_name.txt"]
