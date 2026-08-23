from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.coding.checkout_isolation import (  # noqa: E402
    CheckoutAdmissionError,
    CheckoutLeaseError,
    CheckoutLifecycleError,
    CheckoutProvisioner,
    CheckoutRegistry,
    CheckoutRequest,
    CheckoutSecurityError,
    InvalidWorkspaceMode,
    canonical_mode,
    migrate_checkout_record,
)


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--initial-branch", "main")
    _git(root, "config", "user.email", "checkout-tests@test.invalid")
    _git(root, "config", "user.name", "Checkout Tests")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('base')\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=not-for-copy\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.js").write_text("ignored\n", encoding="utf-8")
    _git(root, "add", "README.md", "src/main.py", ".env", "node_modules/ignored.js")
    _git(root, "commit", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _request(root: Path, registry_root: Path, mode: str, attempt: str, **extra):
    registry_root.mkdir(parents=True, exist_ok=True)
    values = {
        "repository": root,
        "allocation_root": registry_root,
        "destination": registry_root / attempt,
        "mode": mode,
        "attempt_id": attempt,
        "trusted": True,
    }
    values.update(extra)
    return CheckoutRequest.from_values(**values)


def test_canonical_modes_never_map_worktree_to_copy():
    assert canonical_mode("copy") == "isolated_copy"
    assert canonical_mode("isolated") == "isolated_copy"
    assert canonical_mode("worktree") == "git_worktree"
    assert canonical_mode("git_worktree") == "git_worktree"
    with pytest.raises(InvalidWorkspaceMode):
        canonical_mode("directory_copy")


def test_metadata_only_has_no_checkout_or_lease(tmp_path):
    root, commit = _repo(tmp_path)
    registry = CheckoutRegistry(tmp_path / "registry.json")
    record, lease, token = CheckoutProvisioner(registry=registry).provision(
        _request(root, tmp_path / "alloc", "metadata_only", "attempt-meta", base_commit=commit)
    )
    assert record.mode == "metadata_only"
    assert record.path is None
    assert lease is None
    assert token is None
    assert not list((tmp_path / "alloc").glob("attempt-meta*"))


def test_git_worktree_is_real_and_pinned_when_branch_moves(tmp_path):
    root, commit = _repo(tmp_path)
    registry = CheckoutRegistry(tmp_path / "registry.json")
    destination = tmp_path / "alloc" / "attempt-a"
    request = _request(root, tmp_path / "alloc", "git_worktree", "attempt-a", base_ref="main")
    record, lease, token = CheckoutProvisioner(registry=registry).provision(request)

    assert record.mode == "git_worktree"
    assert record.base_commit == commit
    assert record.git_registry_head == commit
    assert destination.is_dir()
    assert (destination / ".git").exists()  # a linked worktree has a .git file
    assert _git(destination, "rev-parse", "HEAD") == commit

    (root / "README.md").write_text("moved\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "move main")
    assert _git(destination, "rev-parse", "HEAD") == commit
    assert lease is not None and token


def test_worktree_request_on_non_git_fails_without_copy_fallback(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "file.txt").write_text("content", encoding="utf-8")
    destination = tmp_path / "alloc" / "attempt"
    request = _request(root, tmp_path / "alloc", "worktree", "attempt")
    with pytest.raises(CheckoutSecurityError):
        CheckoutProvisioner(registry_path=tmp_path / "registry.json").provision(request)
    assert not destination.exists()


def test_writable_checkout_requires_explicit_repository_trust(tmp_path):
    root, _commit = _repo(tmp_path)
    (tmp_path / "alloc").mkdir()
    request = CheckoutRequest.from_values(
        repository=root,
        allocation_root=tmp_path / "alloc",
        destination=tmp_path / "alloc" / "attempt-untrusted",
        mode="git_worktree",
        attempt_id="attempt-untrusted",
        trusted=False,
    )
    with pytest.raises(CheckoutSecurityError, match="trusted repository"):
        CheckoutProvisioner(registry_path=tmp_path / "registry.json").provision(request)


def test_attempt_scoped_leases_allow_concurrent_attempts_by_same_member(tmp_path):
    root, _commit = _repo(tmp_path)
    registry_path = tmp_path / "registry.json"

    def provision(index: int):
        attempt = f"attempt-{index}"
        provisioner = CheckoutProvisioner(registry_path=registry_path)
        return provisioner.provision(_request(root, tmp_path / "alloc", "git_worktree", attempt))

    with ThreadPoolExecutor(max_workers=4) as executor:
        values = list(executor.map(provision, range(4)))
    assert len({record.path for record, _lease, _token in values}) == 4
    assert len({lease.lease_id for _record, lease, _token in values if lease}) == 4
    assert len({lease.fencing_token for _record, lease, _token in values if lease}) == 4


def test_isolated_copy_is_bounded_and_excludes_secret_cache_and_untracked_files(tmp_path):
    root, _commit = _repo(tmp_path)
    (root / "untracked.txt").write_text("do not copy by default\n", encoding="utf-8")
    registry = CheckoutRegistry(tmp_path / "registry.json")
    request = _request(root, tmp_path / "alloc", "isolated_copy", "attempt-copy")
    record, lease, token = CheckoutProvisioner(registry=registry).provision(request)
    destination = Path(record.path or "")

    assert (destination / "README.md").exists()
    assert (destination / "src" / "main.py").exists()
    assert not (destination / ".env").exists()
    assert not (destination / "node_modules").exists()
    assert not (destination / "untracked.txt").exists()
    assert record.excluded_paths[".env"] == "environment_secret"
    assert lease is not None and token


def test_isolated_copy_size_admission_is_atomic(tmp_path):
    root, _commit = _repo(tmp_path)
    registry = CheckoutRegistry(tmp_path / "registry.json")
    request = _request(root, tmp_path / "alloc", "isolated_copy", "attempt-small", max_bytes=1)
    with pytest.raises(CheckoutAdmissionError):
        CheckoutProvisioner(registry=registry).provision(request)
    assert not (tmp_path / "alloc" / "attempt-small").exists()
    record = registry.list()[0]
    assert record.state == "failed"


def test_cleanup_refuses_dirty_checkout_and_reconcile_quarantines_missing_path(tmp_path):
    root, _commit = _repo(tmp_path)
    registry = CheckoutRegistry(tmp_path / "registry.json")
    provisioner = CheckoutProvisioner(registry=registry)
    record, lease, token = provisioner.provision(
        _request(root, tmp_path / "alloc", "git_worktree", "attempt-clean")
    )
    assert lease is not None and token
    (Path(record.path or "") / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(CheckoutLifecycleError):
        provisioner.cleanup(
            record.checkout_id,
            attempt_id=lease.attempt_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            token=token,
        )
    _git(root, "worktree", "remove", "--force", str(record.path))
    findings = provisioner.reconcile()["findings"]
    assert findings[0]["status"] == "quarantine"


def test_legacy_worktree_copy_is_never_migrated_as_git_provenance(tmp_path):
    root, _commit = _repo(tmp_path)
    legacy = tmp_path / "legacy-copy"
    legacy.mkdir()
    migrated = migrate_checkout_record(
        {"mode": "worktree", "workspace_root": str(legacy)},
        repository=root,
    )
    assert migrated["mode"] == "legacy_isolated_copy"
    assert migrated["git_provenance"] is False
    assert "legacy_worktree_not_in_git_registry" in migrated["migration_reason"]


def test_registry_is_json_and_never_persists_lease_secret(tmp_path):
    root, _commit = _repo(tmp_path)
    registry_path = tmp_path / "registry.json"
    record, lease, token = CheckoutProvisioner(registry_path=registry_path).provision(
        _request(root, tmp_path / "alloc", "git_worktree", "attempt-secret")
    )
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload)
    assert token not in serialized
    assert lease is not None
    assert record.lease_token_hash in serialized


def test_multi_agent_worktree_mode_uses_distinct_real_checkouts(tmp_path, monkeypatch):
    from domain.agent.multi import MultiAgentOrchestrator
    from domain.coding.workspace_store import WorkspaceStore

    root, commit = _repo(tmp_path)
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH",
        str(tmp_path / "workspaces.json"),
    )
    WorkspaceStore().create(root, workspace_id="trusted-repo", trusted=True)
    orchestrator = MultiAgentOrchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_ai_complete",
        lambda messages, model, tools: {"status": "ok", "data": {"content": "[DONE]"}},
    )
    result = orchestrator.create_session(
        "inspect",
        [
            {"agent_id": "coder", "name": "coder", "role": "coding", "model": "stub"},
            {"agent_id": "reviewer", "name": "reviewer", "role": "review", "model": "stub"},
        ],
        workspace_id="trusted-repo",
        worktree_mode="worktree",
        base_ref="main",
    )
    assert result["status"] == "created", result
    contexts = result["result"]["agent_contexts"]
    paths = [Path(contexts[name]["workspace"]["workspace_root"]) for name in contexts]
    assert len(set(paths)) == 2
    assert all(path.is_dir() for path in paths)
    assert all(_git(path, "rev-parse", "HEAD") == commit for path in paths)
    assert all(
        contexts[name]["workspace"]["worktree"]["mode"] == "git_worktree"
        for name in contexts
    )


def test_merge_requires_exact_authority_and_preserves_provenance(tmp_path):
    root, commit = _repo(tmp_path)
    registry = CheckoutRegistry(tmp_path / "registry.json")
    provisioner = CheckoutProvisioner(registry=registry)
    record, lease, token = provisioner.provision(
        _request(root, tmp_path / "alloc", "git_worktree", "attempt-merge")
    )
    assert lease is not None and token
    worktree = Path(record.path or "")
    (worktree / "README.md").write_text("merged\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "agent change")
    provisioner.transition(
        record.checkout_id,
        "handoff",
        attempt_id=lease.attempt_id,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        token=token,
    )
    with pytest.raises(CheckoutLeaseError):
        provisioner.merge(
            record.checkout_id,
            authority={"approved": True},
            target_repository=root,
        )
    receipt = provisioner.merge(
        record.checkout_id,
        authority={
            "attempt_id": lease.attempt_id,
            "lease_id": lease.lease_id,
            "fencing_token": lease.fencing_token,
            "lease_token": token,
            "merge_approved": True,
        },
        target_repository=root,
    )
    assert receipt["target_head_before"] == commit
    assert receipt["target_head_after"] == receipt["source_head"]
    assert (root / "README.md").read_text(encoding="utf-8") == "merged\n"
