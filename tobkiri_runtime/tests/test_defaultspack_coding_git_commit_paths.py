"""Focused tests for partial git commits via the coding_git_commit block.

These cover:
* GitOps.commit(paths=...) stages and commits only the requested files.
* GitOps.commit(paths=...) rejects workspace-escaping paths and restricted files.
* GitOps.commit() rejects combining paths with all_tracked.
* The blocks.coding.git_commit run() forwards `paths` through GitOps and
  surfaces INVALID_INPUT for bad input.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _coding_contract_fixture import bind_verified_coding_contracts  # noqa: E402


@pytest.fixture(autouse=True)
def _prefer_defaultspack_domain():
    defaultspack_path = str(DEFAULTSPACK_ROOT)
    while defaultspack_path in sys.path:
        sys.path.remove(defaultspack_path)
    sys.path.insert(0, defaultspack_path)
    domain_module = sys.modules.get("domain")
    domain_file = str(getattr(domain_module, "__file__", "") or "") if domain_module else ""
    domain_path = ";".join(str(item) for item in getattr(domain_module, "__path__", []) or []) if domain_module else ""
    if domain_module is not None and defaultspack_path not in f"{domain_file};{domain_path}":
        for module_name in list(sys.modules):
            if module_name == "domain" or module_name.startswith("domain."):
                sys.modules.pop(module_name, None)


def _init_git_repo(path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is required for partial-commit tests")
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def _commit_all(path: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=path, check=True, capture_output=True, text=True)


def _changed_files(path: Path, ref: str = "HEAD") -> set[str]:
    output = subprocess.run(
        ["git", "show", "--name-only", "--pretty=", ref],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {line for line in output.splitlines() if line.strip()}


def _approved_context() -> dict[str, object]:
    from domain.tool_policy.internal_context import mark_tool_server_approval_context

    return mark_tool_server_approval_context({})


def _git_snapshot(path: Path, mount_revision: int = 1) -> dict[str, object]:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(path), *args],
            text=True,
        ).strip()

    status = subprocess.check_output(
        ["git", "-C", str(path), "status", "--porcelain=v2", "--untracked-files=all"],
        text=True,
    )
    return {
        "expected_head": git("rev-parse", "HEAD"),
        "expected_tree": git("rev-parse", "HEAD^{tree}"),
        "expected_status_hash": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "expected_mount_revision": mount_revision,
    }


def test_git_ops_commit_with_paths_commits_only_selected_files(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path.parent / f"{tmp_path.name}-audit.jsonl"))
    from domain.coding.git_ops import GitOps

    _init_git_repo(tmp_path)
    (tmp_path / "a.txt").write_text("a-clean\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b-clean\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("c-clean\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")

    (tmp_path / "a.txt").write_text("a-dirty\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b-dirty\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("c-dirty\n", encoding="utf-8")

    git = GitOps(tmp_path)
    result = git.commit("partial", paths=["a.txt", "c.txt"])

    assert result["paths"] == ["a.txt", "c.txt"]
    assert result["commit_hash"]
    assert _changed_files(tmp_path) == {"a.txt", "c.txt"}

    status = git.status()
    assert "b.txt" in status["modified"]
    assert "a.txt" not in status["modified"] and "a.txt" not in status["staged"]
    assert "c.txt" not in status["modified"] and "c.txt" not in status["staged"]


def test_git_ops_commit_with_paths_can_add_new_untracked_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path.parent / f"{tmp_path.name}-audit.jsonl"))
    from domain.coding.git_ops import GitOps

    _init_git_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")

    (tmp_path / "new.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "ignored_change.txt").write_text("untouched\n", encoding="utf-8")

    git = GitOps(tmp_path)
    result = git.commit("add new", paths=["new.txt"])

    assert result["paths"] == ["new.txt"]
    assert _changed_files(tmp_path) == {"new.txt"}
    assert (tmp_path / "ignored_change.txt").read_text(encoding="utf-8") == "untouched\n"
    assert "ignored_change.txt" in git.status()["untracked"]


def test_git_ops_commit_with_paths_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path.parent / f"{tmp_path.name}-audit.jsonl"))
    from domain.coding.git_ops import GitOps
    from domain.coding.workspace_jail import WorkspacePathViolation

    _init_git_repo(tmp_path)
    (tmp_path / "in.txt").write_text("clean\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")
    (tmp_path / "in.txt").write_text("dirty\n", encoding="utf-8")

    git = GitOps(tmp_path)
    with pytest.raises(WorkspacePathViolation):
        git.commit("nope", paths=["../escape.txt"])

    # The dirty change must not be committed when path validation fails.
    assert "in.txt" in git.status()["modified"]


def test_git_ops_commit_with_paths_rejects_restricted_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path.parent / f"{tmp_path.name}-audit.jsonl"))
    from domain.coding.git_ops import GitOps
    from domain.coding.workspace_jail import WorkspaceRestrictedPath

    _init_git_repo(tmp_path)
    (tmp_path / "ok.txt").write_text("clean\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=clean\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")
    (tmp_path / ".env").write_text("TOKEN=dirty\n", encoding="utf-8")

    git = GitOps(tmp_path)
    with pytest.raises(WorkspaceRestrictedPath):
        git.commit("leak", paths=[".env"])


def test_git_ops_commit_rejects_combining_paths_and_all_tracked(tmp_path):
    from domain.coding.git_ops import GitOps

    _init_git_repo(tmp_path)
    git = GitOps(tmp_path)
    with pytest.raises(ValueError):
        git.commit("oops", all_tracked=True, paths=["a.txt"])


def test_git_ops_commit_paths_requires_non_empty_string_list(tmp_path):
    from domain.coding.git_ops import GitOps

    _init_git_repo(tmp_path)
    git = GitOps(tmp_path)

    with pytest.raises(ValueError):
        git.commit("msg", paths=[])
    with pytest.raises(ValueError):
        git.commit("msg", paths="a.txt")  # bare string is not allowed
    with pytest.raises(ValueError):
        git.commit("msg", paths=[""])
    with pytest.raises(ValueError):
        git.commit("msg", paths=[123])


def test_block_git_commit_with_paths_commits_only_selected_files(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path.parent / f"{tmp_path.name}-audit.jsonl"))
    from blocks.coding.git_commit import run as git_commit_run

    _init_git_repo(tmp_path)
    (tmp_path / "a.txt").write_text("a-clean\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b-clean\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")
    (tmp_path / "a.txt").write_text("a-dirty\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b-dirty\n", encoding="utf-8")
    bind_verified_coding_contracts(monkeypatch, tmp_path)

    result = git_commit_run(
        {
            "workspace_id": "trusted",
            "message": "partial via block",
            "paths": ["a.txt"],
            **_git_snapshot(tmp_path),
        },
        _approved_context(),
    )

    assert result["status"] == "ok", result
    data = result["data"]
    assert data["paths"] == ["a.txt"]
    assert data["commit_hash"]
    assert data["message"] == "partial via block"
    assert _changed_files(tmp_path) == {"a.txt"}


def test_block_git_commit_files_alias_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path.parent / f"{tmp_path.name}-audit.jsonl"))
    from blocks.coding.git_commit import run as git_commit_run

    _init_git_repo(tmp_path)
    (tmp_path / "x.txt").write_text("x-clean\n", encoding="utf-8")
    (tmp_path / "y.txt").write_text("y-clean\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")
    (tmp_path / "x.txt").write_text("x-dirty\n", encoding="utf-8")
    (tmp_path / "y.txt").write_text("y-dirty\n", encoding="utf-8")
    bind_verified_coding_contracts(monkeypatch, tmp_path)

    result = git_commit_run(
        {
            "workspace_id": "trusted",
            "message": "via files alias",
            "files": ["y.txt"],
            **_git_snapshot(tmp_path),
        },
        _approved_context(),
    )

    assert result["status"] == "ok", result
    assert result["data"]["paths"] == ["y.txt"]
    assert _changed_files(tmp_path) == {"y.txt"}


def test_block_git_commit_rejects_paths_with_all_tracked(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path.parent / f"{tmp_path.name}-audit.jsonl"))
    from blocks.coding.git_commit import run as git_commit_run

    _init_git_repo(tmp_path)
    (tmp_path / "a.txt").write_text("clean\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")
    (tmp_path / "a.txt").write_text("dirty\n", encoding="utf-8")

    result = git_commit_run(
        {
            "workspace_root": str(tmp_path),
            "message": "conflict",
            "paths": ["a.txt"],
            "all_tracked": True,
        },
        {"_tool_server_approved": True},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_INPUT"


def test_block_git_commit_rejects_traversal_path(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path.parent / f"{tmp_path.name}-audit.jsonl"))
    from blocks.coding.git_commit import run as git_commit_run

    _init_git_repo(tmp_path)
    (tmp_path / "a.txt").write_text("clean\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")
    (tmp_path / "a.txt").write_text("dirty\n", encoding="utf-8")
    bind_verified_coding_contracts(monkeypatch, tmp_path)

    result = git_commit_run(
        {
            "workspace_id": "trusted",
            "message": "escape attempt",
            "paths": ["../escape.txt"],
            **_git_snapshot(tmp_path),
        },
        _approved_context(),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "GIT_ERROR"
    assert _changed_files(tmp_path) == {"a.txt"}


def test_block_git_commit_rejects_restricted_path(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path.parent / f"{tmp_path.name}-audit.jsonl"))
    from blocks.coding.git_commit import run as git_commit_run

    _init_git_repo(tmp_path)
    (tmp_path / "ok.txt").write_text("clean\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=clean\n", encoding="utf-8")
    _commit_all(tmp_path, "initial")
    (tmp_path / ".env").write_text("TOKEN=dirty\n", encoding="utf-8")
    bind_verified_coding_contracts(monkeypatch, tmp_path)

    result = git_commit_run(
        {
            "workspace_id": "trusted",
            "message": "leak",
            "paths": [".env"],
            **_git_snapshot(tmp_path),
        },
        _approved_context(),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "GIT_ERROR"
    assert _changed_files(tmp_path) == {"ok.txt", ".env"}


def test_live_provider_tool_definition_exposes_paths_and_files_alias():
    """Regression: the provider tool schema served to MiMo (and other models)
    must expose 'paths' and the 'files' alias so that partial-commit calls
    are reachable from the model. Schema validation must remain non-weakened
    (message stays required).
    """
    import json as _json

    from domain.tool.provider_adapter import adapt_rumi_tools_to_provider_tools
    from domain.tool.registry import ToolRegistry

    manifest_path = (
        ROOT
        / "ecosystem"
        / "rumi_default_tools_pack"
        / "tools"
        / "coding_git_commit"
        / "manifest.json"
    )
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_path"] = str(manifest_path)
    manifest["source_pack_id"] = "rumi_default_tools_pack"

    tool_def = ToolRegistry._tool_from_manifest(
        manifest, source_pack_id="rumi_default_tools_pack"
    )
    assert tool_def is not None, "coding_git_commit manifest should produce a tool"

    schema_params = tool_def["schema"]["parameters"]
    properties = schema_params.get("properties", {})
    assert "paths" in properties, (
        "Manifest schema must expose 'paths' so MiMo can request partial commits"
    )
    assert "files" in properties, (
        "Manifest schema must expose 'files' alias for compatibility with models "
        "that prefer the alternative name"
    )
    assert properties["paths"]["type"] == "array"
    assert properties["paths"]["items"]["type"] == "string"
    assert properties["files"]["type"] == "array"
    assert properties["files"]["items"]["type"] == "string"

    # Validation must not be weakened: 'message' stays required.
    assert "message" in schema_params.get("required", [])

    provider_tools, _, definitions = adapt_rumi_tools_to_provider_tools([tool_def])
    assert len(provider_tools) == 1
    function_payload = provider_tools[0]["function"]
    assert function_payload["name"] == "coding_git_commit"
    fn_props = function_payload["parameters"]["properties"]
    assert "paths" in fn_props, (
        "Provider tool definition must propagate 'paths' to the live schema "
        "served to the model"
    )
    assert "files" in fn_props, (
        "Provider tool definition must propagate 'files' alias to the live schema"
    )
    assert "message" in function_payload["parameters"].get("required", [])

    # The original RumiToolDefinition shape also carries the parameters intact.
    assert "paths" in definitions[0].original.parameters["properties"]
    assert "files" in definitions[0].original.parameters["properties"]
