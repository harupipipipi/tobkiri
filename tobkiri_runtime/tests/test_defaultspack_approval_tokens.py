from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_signed_approval_token_binds_operation_and_arguments(tmp_path, monkeypatch):
    from blocks.coding.file_write import run as file_write_run
    from domain.safety.approval import approve, reset_approval_state_for_tests
    from tests._coding_contract_fixture import bind_verified_coding_contracts

    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    bind_verified_coding_contracts(monkeypatch, tmp_path)
    reset_approval_state_for_tests()

    (tmp_path / "approved.txt").write_text("before", encoding="utf-8")
    args = {
        "path": "approved.txt",
        "content": "ok",
        "workspace_id": "trusted",
        "workspace_root": str(tmp_path),
    }
    request = file_write_run(args, {})

    assert request["status"] == "ok"
    assert request["data"]["approval_required"] is True
    approval = approve(request["data"]["approval_request_id"])
    assert approval["approved"] is True

    written = file_write_run({**args, "approval_token": approval["token"]}, {})
    assert written["status"] == "ok"
    assert written["data"]["written"] is True
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "ok"

    replay = file_write_run({**args, "approval_token": approval["token"]}, {})
    assert replay["status"] == "error"
    assert replay["error"]["code"] == "APPROVAL_TOKEN_USED"


def test_signed_approval_token_rejects_argument_tampering(tmp_path, monkeypatch):
    from blocks.coding.file_write import run as file_write_run
    from domain.safety.approval import approve, reset_approval_state_for_tests
    from tests._coding_contract_fixture import bind_verified_coding_contracts

    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    bind_verified_coding_contracts(monkeypatch, tmp_path)
    reset_approval_state_for_tests()

    (tmp_path / "approved.txt").write_text("before", encoding="utf-8")
    args = {
        "path": "approved.txt",
        "content": "ok",
        "workspace_id": "trusted",
        "workspace_root": str(tmp_path),
    }
    request = file_write_run(args, {})
    approval = approve(request["data"]["approval_request_id"])

    tampered = file_write_run(
        {**args, "content": "changed", "approval_token": approval["token"]},
        {},
    )

    assert tampered["status"] == "error"
    assert tampered["error"]["code"] == "APPROVAL_ARGUMENTS_CHANGED"
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "before"


def test_terminal_stream_starts_real_read_only_process(tmp_path, monkeypatch):
    from blocks.coding.terminal_stream import run as terminal_stream_run
    from tests._coding_contract_fixture import bind_verified_coding_contracts

    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    bind_verified_coding_contracts(monkeypatch, tmp_path)

    result = terminal_stream_run(
        {"command": "pwd", "workspace_id": "trusted", "workspace_root": str(tmp_path)},
        {},
    )

    assert result["status"] == "ok"
    assert result["data"]["started"] is True
    assert result["data"]["status"] in {"running", "exited"}
    assert result["data"]["workspace_id"] == "trusted"


def test_git_branch_checkout_is_unavailable_without_consuming_approval(
    tmp_path, monkeypatch
):
    from blocks.coding.git_branch import run as git_branch_run
    from domain.safety.approval import reset_approval_state_for_tests
    from tests._coding_contract_fixture import bind_verified_coding_contracts

    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    bind_verified_coding_contracts(monkeypatch, tmp_path)
    reset_approval_state_for_tests()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    args = {
        "action": "switch",
        "branch": "feature/local-first",
        "create": True,
        "workspace_id": "trusted",
        "workspace_root": str(tmp_path),
    }
    unavailable = git_branch_run(args, {})

    assert unavailable["status"] == "error"
    assert unavailable["error"]["code"] == "GIT_UNAVAILABLE"
    assert "exclusive workspace mutation lease" in unavailable["error"]["message"]
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "rev-parse",
                "--verify",
                "--quiet",
                "feature/local-first",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 1
    )
    assert (
        subprocess.check_output(
            ["git", "-C", str(tmp_path), "branch", "--show-current"], text=True
        ).strip()
        == "main"
    )

    with_token = git_branch_run({**args, "approval_token": "not-consumed"}, {})
    assert with_token["status"] == "error"
    assert with_token["error"]["code"] == "GIT_UNAVAILABLE"
