from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _fresh_approval_module(monkeypatch, db_path: Path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_CHAT_STORE_PATH",
        str(db_path.parent / "chat" / "conversations.json"),
    )
    for name in (
        "domain.safety.approval",
        "domain.safety.approval_state_json",
        "domain.safety.approval_store",
    ):
        sys.modules.pop(name, None)
    import domain.safety.approval as approval

    return approval


def test_default_approval_paths_use_launcher_user_data(tmp_path, monkeypatch):
    user_data = tmp_path / "launcher-user-data"
    monkeypatch.delenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", raising=False)
    monkeypatch.delenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", raising=False)
    monkeypatch.delenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", raising=False)
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))

    import domain.safety.approval_state_json as state_json
    import domain.safety.approval_store as approval_store

    assert approval_store.default_approval_db_path() == (
        user_data / "defaultspack" / "shared" / "safety" / "approval.sqlite3"
    )
    assert approval_store.default_approval_secret_path() == (
        user_data / "defaultspack" / "shared" / "safety" / "approval_runtime_secret"
    )
    assert state_json.default_chat_dir() == (
        user_data / "defaultspack" / "shared" / "chat"
    )


def test_explicit_approval_paths_override_launcher_user_data(tmp_path, monkeypatch):
    db_path = tmp_path / "explicit" / "approval.sqlite3"
    secret_path = tmp_path / "explicit" / "runtime-secret"
    chat_store = tmp_path / "explicit-chat" / "conversations.json"
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "launcher-user-data"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", str(db_path))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", str(secret_path))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(chat_store))

    import domain.safety.approval_state_json as state_json
    import domain.safety.approval_store as approval_store

    assert approval_store.default_approval_db_path() == db_path
    assert approval_store.default_approval_secret_path() == secret_path
    assert state_json.default_chat_dir() == chat_store.parent


def test_importing_approval_does_not_write_runtime_state(tmp_path, monkeypatch):
    user_data = tmp_path / "launcher-user-data"
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.delenv("RUMI_DEFAULTSPACK_APPROVAL_DB_PATH", raising=False)
    monkeypatch.delenv("RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH", raising=False)
    monkeypatch.delenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", raising=False)
    for name in (
        "domain.safety.approval",
        "domain.safety.approval_state_json",
        "domain.safety.approval_store",
    ):
        sys.modules.pop(name, None)

    import domain.safety.approval  # noqa: F401

    assert not user_data.exists()


def test_approval_requests_survive_process_restart(tmp_path, monkeypatch):
    approval = _fresh_approval_module(monkeypatch, tmp_path / "approval.sqlite3")
    approval.reset_approval_state_for_tests()

    args = {"command": "git push origin main", "workspace_root": str(tmp_path)}
    request = approval.create_approval_request(
        "terminal.exec",
        "high",
        args,
        details={"command": args["command"], "conversation_id": "conv-audit"},
    )

    mirror_path = tmp_path / "chat" / "approval_state.json"
    mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
    assert [item["request_id"] for item in mirror["requests"]] == [request["request_id"]]
    assert mirror["requests"][0]["status"] == "pending"

    conversation_mirror = tmp_path / "chat" / "conversations" / "conv-audit" / "approval_state.json"
    conversation_payload = json.loads(conversation_mirror.read_text(encoding="utf-8"))
    assert [item["request_id"] for item in conversation_payload["requests"]] == [request["request_id"]]

    approval = _fresh_approval_module(monkeypatch, tmp_path / "approval.sqlite3")
    listed = approval.list_approval_requests()

    assert [item["request_id"] for item in listed] == [request["request_id"]]
    assert listed[0]["status"] == "pending"
    assert listed[0]["operation"] == "terminal.exec"


def test_approval_state_conversation_mirror_retries_transient_replace_failure(tmp_path, monkeypatch):
    import domain.safety.approval_state_json as state_json

    target = tmp_path / "chat" / "conversations" / "conv-audit" / "approval_state.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"schema_version": 1, "requests": []}', encoding="utf-8")
    original_replace = state_json.os.replace
    attempts = []

    def flaky_replace(source, destination):
        source_path = Path(source)
        if (
            Path(destination) == target
            and source_path.name.startswith(".approval_state.json.")
            and len(attempts) < 2
        ):
            attempts.append(source_path.name)
            raise PermissionError(13, "Access is denied")
        return original_replace(source, destination)

    monkeypatch.setattr(state_json.os, "replace", flaky_replace)
    monkeypatch.setattr(state_json.time, "sleep", lambda _seconds: None)

    state_json._atomic_write_json(
        target,
        {
            "schema_version": 1,
            "updated_at": 123,
            "requests": [{"request_id": "apr_retry"}],
        },
    )

    assert len(attempts) == 2
    assert json.loads(target.read_text(encoding="utf-8"))["requests"] == [{"request_id": "apr_retry"}]
    assert list(target.parent.glob(".approval_state.json.*.tmp")) == []


def test_approval_state_replace_failure_is_sanitized_and_cleans_temp(tmp_path, monkeypatch):
    import pytest
    import domain.safety.approval_state_json as state_json

    target = tmp_path / "chat" / "conversations" / "conv-audit" / "approval_state.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"schema_version": 1, "requests": [{"request_id": "old"}]}', encoding="utf-8")
    sleep_calls = []

    def locked_replace(source, destination):
        raise PermissionError(13, "Access is denied", str(source), str(destination))

    monkeypatch.setattr(state_json.os, "replace", locked_replace)
    monkeypatch.setattr(state_json.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    with pytest.raises(PermissionError) as exc_info:
        state_json._atomic_write_json(
            target,
            {
                "schema_version": 1,
                "updated_at": 123,
                "requests": [{"request_id": "apr_secret", "details": {"token": "secret-token"}}],
            },
        )

    message = str(exc_info.value)
    assert message == "approval state file is temporarily locked; retry later"
    assert "secret-token" not in message
    assert str(target) not in message
    assert exc_info.value.__suppress_context__ is True
    assert len(sleep_calls) == state_json._REPLACE_RETRY_ATTEMPTS - 1
    assert json.loads(target.read_text(encoding="utf-8"))["requests"] == [{"request_id": "old"}]
    assert list(target.parent.glob(".approval_state.json.*.tmp")) == []


def test_one_shot_token_replay_is_rejected_after_restart(tmp_path, monkeypatch):
    approval = _fresh_approval_module(monkeypatch, tmp_path / "approval.sqlite3")
    approval.reset_approval_state_for_tests()

    args = {"path": "approved.txt", "content": "ok", "workspace_root": str(tmp_path)}
    request = approval.create_approval_request("file.write", "high", args, details={"path": "approved.txt"})
    decision = approval.approve(request["request_id"])

    assert decision["approved"] is True
    token = decision["token"]
    assert approval.verify_execution_token(token, "file.write", approval.hash_arguments(args)).valid is True

    approval = _fresh_approval_module(monkeypatch, tmp_path / "approval.sqlite3")
    replay = approval.verify_execution_token(token, "file.write", approval.hash_arguments(args))

    assert replay.valid is False
    assert replay.code == "APPROVAL_TOKEN_USED"


def test_empty_approval_args_hash_empty_payload_not_details(tmp_path, monkeypatch):
    approval = _fresh_approval_module(monkeypatch, tmp_path / "approval.sqlite3")
    approval.reset_approval_state_for_tests()

    request = approval.create_approval_request(
        "computer.context",
        "high",
        {},
        details={"function_id": "computer.context", "conversation_id": "conv-empty"},
    )
    decision = approval.approve(request["request_id"])

    assert request["args_hash"] == approval.hash_arguments({})
    assert approval.verify_execution_token(
        decision["token"],
        "computer.context",
        approval.hash_arguments({}),
        conversation_id="conv-empty",
    ).valid is True


def test_json_only_pending_is_listed_for_recovery_and_sqlite_wins_conflicts(tmp_path, monkeypatch):
    approval = _fresh_approval_module(monkeypatch, tmp_path / "approval.sqlite3")
    approval.reset_approval_state_for_tests()

    args = {"path": "winner.txt", "content": "ok", "workspace_root": str(tmp_path)}
    sqlite_request = approval.create_approval_request("file.write", "high", args, details={"path": "winner.txt"})
    decision = approval.approve(sqlite_request["request_id"])
    assert decision["approved"] is True

    json_only = {
        "request_id": "apr_json_only_recovery",
        "operation": "terminal.exec",
        "risk_level": "high",
        "args_hash": approval.hash_arguments({"command": "echo recover"}),
        "details": {"command": "echo recover"},
        "created_at": sqlite_request["created_at"] + 10,
        "expires_at": sqlite_request["expires_at"],
        "status": "pending",
        "decision_at": None,
    }
    stale_conflict = {
        **sqlite_request,
        "operation": "terminal.exec",
        "details": {"command": "stale mirror"},
        "status": "pending",
        "decision_at": None,
    }
    mirror_path = tmp_path / "chat" / "approval_state.json"
    mirror_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": sqlite_request["created_at"],
                "requests": [json_only, stale_conflict],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    listed = approval.list_approval_requests()
    by_id = {item["request_id"]: item for item in listed}

    assert by_id["apr_json_only_recovery"]["status"] == "pending"
    assert by_id[sqlite_request["request_id"]]["status"] == "approved"
    assert by_id[sqlite_request["request_id"]]["operation"] == "file.write"

    pending = approval.list_approval_requests(status="pending")
    pending_ids = {item["request_id"] for item in pending}
    assert "apr_json_only_recovery" in pending_ids
    assert sqlite_request["request_id"] not in pending_ids
