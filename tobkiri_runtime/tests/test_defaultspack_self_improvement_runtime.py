from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


@pytest.fixture
def live_contracts(tmp_path, monkeypatch):
    """Stub only selected contracts; keep real coding and approval gates."""
    from blocks.coding import file_patch, file_read, file_search, git_commit, git_status, terminal_exec
    from domain.coding import contract_adapter as adapter
    from domain.safety import approval

    monkeypatch.delenv("RUMI_DEV_ENABLE_SELF_IMPROVEMENT_LIVE", raising=False)
    for name, path in {
        "RUMI_DEFAULTSPACK_APPROVAL_DB_PATH": tmp_path / "approval.sqlite3",
        "RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH": tmp_path / "approval-secret",
        "RUMI_DEFAULTSPACK_AUDIT_PATH": tmp_path / "audit.jsonl",
        "RUMI_ADAPTIVE_STORE_ROOT": tmp_path / "adaptive",
    }.items():
        monkeypatch.setenv(name, str(path))
    approval.reset_approval_state_for_tests()
    mount = {
        "id": "selected-workspace", "root_path": str(tmp_path.resolve()),
        "mount_revision": 1, "metadata": {"trusted": True},
    }
    state = SimpleNamespace(
        mount=mount, selected="selected-workspace", calls=[], approval_required=True,
    )

    def invoke(contract_id, operation, payload):
        state.calls.append((contract_id, operation, dict(payload)))
        if contract_id == adapter.WORKSPACE_RESOURCE:
            if operation == "list":
                return {"selected_workspace_id": state.selected, "mounts": [dict(mount)]}
            assert operation == "get" and payload["workspace_id"] == mount["id"]
            return dict(mount)
        if contract_id == adapter.SHELL_INSPECT:
            assert operation == "classify"
            return {"risk_level": "high", "approval_required": state.approval_required}
        if contract_id == adapter.HOST_AUTHORITY:
            assert operation == "authorize"
            return {"authorized": True, "receipt": "test-host-receipt"}
        assert payload["workspace_id"] == mount["id"]
        if contract_id == adapter.FILE_INSPECT and operation == "read":
            return {"path": payload["path"], "content": (tmp_path / payload["path"]).read_text()}
        if contract_id == adapter.FILE_INSPECT and operation == "search":
            return {"pattern": payload["pattern"], "matches": ["sample.py"]}
        if contract_id == adapter.GIT_READ and operation == "status":
            return {"output": "# branch.head main", "repository_root": str(tmp_path)}
        if contract_id == adapter.SHELL_EXECUTE:
            assert operation == "execute" and payload["authority_receipt"] == "test-host-receipt"
            assert not Path(payload["cwd"]).is_absolute()
            return {"exit_code": 0, "stdout": "test output", "stderr": ""}
        raise AssertionError(f"unexpected contract: {contract_id} {operation}")

    monkeypatch.setattr(adapter, "invoke_coding_contract", invoke)
    monkeypatch.setattr(adapter, "_profile_id", lambda: "defaults")
    for block in (file_patch, file_read, file_search, git_commit, git_status, terminal_exec):
        monkeypatch.setattr(block, "invoke_coding_contract", invoke)
    yield state
    approval.reset_approval_state_for_tests()


def test_mimo_profile_assigns_main_vision_fast_models(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")

    assert runtime.role_map["main"] == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert runtime.role_map["vision"] == "xiaomi-token-plan-sgp/mimo-v2-omni"
    assert runtime.role_map["fast"] == "xiaomi-token-plan-sgp/mimo-v2-flash"


def test_mimo_profile_uses_local_company_profile_role_map(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    status_path = tmp_path / "user_data" / "shared" / "mimo_coding_company" / "codex_manager_status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps(
            {
                "provider": {
                    "models": {
                        "main": "profile/main-current",
                        "vision": "profile/vision-current",
                        "fast": "profile/fast-current",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")

    assert runtime.role_map["main"] == "profile/main-current"
    assert runtime.role_map["vision"] == "profile/vision-current"
    assert runtime.role_map["fast"] == "profile/fast-current"


def test_mimo_profile_status_overrides_stale_company_profile(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    company_path = tmp_path / "ecosystem" / "defaultspack" / "user_data" / "shared" / "companies" / "companies.json"
    company_path.parent.mkdir(parents=True)
    company_path.write_text(
        json.dumps(
            {
                "companies": {
                    "mimo-coding-company": {
                        "metadata": {
                            "main_model": "company/main-stale",
                            "vision_model": "company/vision-stale",
                            "fast_model": "company/fast-stale",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    status_path = tmp_path / "user_data" / "shared" / "mimo_coding_company" / "codex_manager_status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps(
            {
                "provider": {
                    "models": {
                        "main": "status/main-current",
                        "vision": "status/vision-current",
                        "fast": "status/fast-current",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")

    assert runtime.role_map["main"] == "status/main-current"
    assert runtime.role_map["vision"] == "status/vision-current"
    assert runtime.role_map["fast"] == "status/fast-current"


def test_mimo_profile_uses_company_metadata_role_map(tmp_path, monkeypatch):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    company_store = tmp_path / "companies.json"
    company_store.write_text(
        json.dumps(
            {
                "companies": {
                    "mimo-coding-company": {
                        "metadata": {
                            "role_map": {
                                "main": "company/main-current",
                                "vision": "company/vision-current",
                                "fast": "company/fast-current",
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", str(company_store))

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")

    assert runtime.role_map["main"] == "company/main-current"
    assert runtime.role_map["vision"] == "company/vision-current"
    assert runtime.role_map["fast"] == "company/fast-current"


def test_live_self_improvement_defaults_to_current_mimo_models():
    from domain.agent.self_improvement_live_loop import run_live_improvement, run_vision_qa

    assert run_live_improvement.__kwdefaults__["model"] == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert run_vision_qa.__kwdefaults__["model"] == "xiaomi-token-plan-sgp/mimo-v2-omni"


def test_self_improvement_run_defaults_to_current_mimo_main_model(tmp_path, monkeypatch, live_contracts):
    seen: dict[str, object] = {}

    def fake_run_live_improvement(**kwargs):
        seen.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(
        "domain.agent.self_improvement_live_loop.run_live_improvement",
        fake_run_live_improvement,
    )

    from blocks.agent.self_improvement_run import run

    result = run({"action": "single", "model": "", "workspace_root": str(tmp_path)}, {})

    assert result["status"] == "ok"
    assert seen["model"] == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert seen["workspace_id"] == live_contracts.mount["id"]


def test_self_improvement_run_uses_local_profile_default(tmp_path, monkeypatch, live_contracts):
    seen: dict[str, object] = {}
    status_path = tmp_path / "user_data" / "shared" / "mimo_coding_company" / "codex_manager_status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps({"provider": {"models": {"main": "profile/main", "vision": "profile/vision", "fast": "profile/fast"}}}),
        encoding="utf-8",
    )

    def fake_run_live_improvement(**kwargs):
        seen.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(
        "domain.agent.self_improvement_live_loop.run_live_improvement",
        fake_run_live_improvement,
    )

    from blocks.agent.self_improvement_run import run

    result = run({"action": "single", "model": "", "workspace_root": str(tmp_path)}, {})

    assert result["status"] == "ok"
    assert seen["model"] == "profile/main"


def test_mimo_vision_role_uses_current_vision_model(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")

    assert runtime.role_map["vision"] == "xiaomi-token-plan-sgp/mimo-v2-omni"


def test_coding_role_rejects_non_tool_call_model():
    from domain.agent.self_improvement_runtime import validate_model_for_role, ModelRoleValidationError

    with pytest.raises(ModelRoleValidationError, match="tool_calls"):
        validate_model_for_role("main", "some/model", {"tool_calls": False})


def test_browser_qa_role_requires_vision_model():
    from domain.agent.self_improvement_runtime import validate_model_for_role, ModelRoleValidationError

    with pytest.raises(ModelRoleValidationError, match="vision"):
        validate_model_for_role("vision", "some/model", {"vision": False})


def test_coding_role_accepts_tool_call_model():
    from domain.agent.self_improvement_runtime import validate_model_for_role

    validate_model_for_role("main", "some/model", {"tool_calls": True})


def test_vision_role_accepts_vision_model():
    from domain.agent.self_improvement_runtime import validate_model_for_role

    validate_model_for_role("vision", "some/model", {"vision": True})


def test_self_improvement_runtime_is_provider_agnostic(tmp_path):
    from domain.agent.self_improvement_runtime import SelfImprovingDefaultspackRuntime

    runtime = SelfImprovingDefaultspackRuntime(
        profile_id="test.custom_provider",
        role_map={
            "main": "custom/main-model",
            "vision": "custom/vision-model",
            "fast": "custom/fast-model",
        },
        workspace_root=tmp_path,
        state_path=tmp_path / "state.json",
    )

    assert runtime.profile_id == "test.custom_provider"
    assert runtime.role_map["main"] == "custom/main-model"
    manifest = runtime.manifest()
    assert manifest["runtime"] == "SelfImprovingDefaultspackRuntime"


def test_self_improvement_runtime_bootstrap_creates_state(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    status = runtime.bootstrap()

    assert status["running"] is True
    assert status["profile_id"] == "defaultspack.mimo_coding_company"

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["running"] is True


def test_self_improvement_runtime_task_lifecycle(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    runtime.bootstrap()

    runtime.add_task("t1", "Read file", expected_outcome="file read")
    runtime.start_task("t1")
    runtime.record_tool_call("coding_file_read", {"path": "test.py"})
    runtime.complete_task("t1", {"files_read": ["test.py"]})

    status = runtime.status()
    assert status["completed_count"] == 1
    assert status["failed_count"] == 0
    assert status["last_self_improvement_result"]["files_read"] == ["test.py"]


def test_self_improvement_runtime_fail_task(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    runtime.bootstrap()

    runtime.add_task("t2", "Patch file")
    runtime.start_task("t2")
    runtime.fail_task("t2", "permission denied")

    status = runtime.status()
    assert status["failed_count"] == 1
    assert status["current_task"] is None


def test_self_improvement_runtime_pause_resume_stop(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    runtime.bootstrap()

    runtime.pause()
    assert runtime.status()["running"] is False

    runtime.resume()
    assert runtime.status()["running"] is True

    runtime.stop()
    status = runtime.status()
    assert status["running"] is False
    assert status["current_task"] is None


def test_self_improvement_runtime_record_test_result(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    runtime.bootstrap()

    runtime.record_test_result("pytest tests/test_foo.py", 0, "all passed")

    status = runtime.status()
    assert status["last_test_result"]["exit_code"] == 0
    assert status["last_test_result"]["command"] == "pytest tests/test_foo.py"


def test_self_improvement_runtime_record_commit(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    runtime.bootstrap()

    runtime.record_commit("abc1234", "fix: typo", paths=["src/foo.py"])

    status = runtime.status()
    assert status["last_commit"]["commit_hash"] == "abc1234"
    assert status["last_commit"]["paths"] == ["src/foo.py"]


def test_self_improvement_runtime_generate_report(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    runtime.bootstrap()

    runtime.add_task("t1", "Task 1")
    runtime.start_task("t1")
    runtime.complete_task("t1", {"ok": True})
    runtime.add_task("t2", "Task 2")
    runtime.start_task("t2")
    runtime.fail_task("t2", "timeout")

    report = runtime.generate_report()
    assert report["total_tasks"] == 2
    assert report["completed"] == 1
    assert report["failed"] == 1
    assert len(report["friction_points"]) >= 1


def test_self_improvement_runtime_requires_all_roles():
    from domain.agent.self_improvement_runtime import SelfImprovingDefaultspackRuntime, ModelRoleValidationError

    with pytest.raises(ModelRoleValidationError, match="missing role"):
        SelfImprovingDefaultspackRuntime(
            profile_id="test.incomplete",
            role_map={"main": "x"},  # missing vision and fast
        )


def test_mimo_rumi_api_get_allowed(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    assert "rumi_api" in runtime.tool_allowlist


def test_mimo_coding_commit_tool_in_allowlist(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    assert "coding_git_commit" in runtime.tool_allowlist


def test_mimo_profile_manifest_has_role_map(tmp_path):
    from domain.agent.self_improvement_runtime import create_mimo_profile

    runtime = create_mimo_profile(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    manifest = runtime.manifest()

    assert manifest["profile_id"] == "defaultspack.mimo_coding_company"
    assert "main" in manifest["role_map"]
    assert "vision" in manifest["role_map"]
    assert "fast" in manifest["role_map"]
    assert manifest["non_stop"] is True
    assert manifest["can_run_24_7"] is True


@pytest.mark.parametrize("cwd_kind", ["parent", "absolute", "symlink"])
def test_live_terminal_rejects_workspace_escape(tmp_path, live_contracts, cwd_kind):
    from domain.agent import self_improvement_live_loop as live
    from domain.coding import contract_adapter as adapter

    outside = tmp_path.parent
    if cwd_kind == "symlink":
        (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    cwd = {"parent": "..", "absolute": str(outside), "symlink": "escape"}[cwd_kind]
    workspace = live.resolve_live_workspace(tmp_path)
    result = live._execute_tool("coding_terminal_exec", {"command": "pwd", "cwd": cwd}, workspace)
    assert result["status"] == "error"
    assert "inside the workspace" in result["error"]["message"]
    assert all(contract == adapter.WORKSPACE_RESOURCE for contract, _, _ in live_contracts.calls)


def test_live_terminal_uses_real_normal_gate_and_relative_cwd(tmp_path, live_contracts):
    from domain.agent import self_improvement_live_loop as live
    from domain.coding import contract_adapter as adapter

    (tmp_path / "src").mkdir()
    live_contracts.approval_required = False
    result = live._execute_tool(
        "coding_terminal_exec", {"command": "pwd", "cwd": str(tmp_path / "src")},
        live.resolve_live_workspace(tmp_path),
    )
    assert result["status"] == "ok" and result["data"]["exit_code"] == 0
    executed = [payload for contract, _, payload in live_contracts.calls if contract == adapter.SHELL_EXECUTE]
    assert len(executed) == 1
    assert executed[0]["cwd"] == "src"
    assert executed[0]["workspace_id"] == "selected-workspace"
    assert executed[0]["authority_receipt"] == "test-host-receipt"


@pytest.mark.parametrize("name,arguments", [
    ("coding_file_patch", {"path": "sample.py", "old": "old", "new": "new"}),
    ("coding_git_commit", {"message": "test commit", "paths": ["sample.py"]}),
    ("coding_terminal_exec", {"command": "echo test"}),
])
def test_live_tools_cannot_reuse_loop_approval(tmp_path, live_contracts, name, arguments):
    from domain.agent import self_improvement_live_loop as live
    from domain.coding import contract_adapter as adapter
    from domain.safety import approval
    from domain.tool_policy.internal_context import mark_tool_server_approval_context

    trusted_start = mark_tool_server_approval_context({"principal_id": "local-owner"})
    result = live._execute_tool(name, {
        **arguments, "approved": True, "approval_token": "forged",
        "_headers": {"X-Rumi-Approval": "forged"},
    }, live.resolve_live_workspace(tmp_path), trusted_start)
    assert result["status"] == "ok" and result["data"]["approval_required"] is True
    request = approval.get_approval_request(result["data"]["approval_request_id"])
    stored = request["details"]["arguments"]
    assert stored["workspace_id"] == "selected-workspace"
    assert not {"approved", "approval_token", "_headers"}.intersection(stored)
    assert not any(contract in {adapter.FILE_PATCH, adapter.GIT_WRITE, adapter.SHELL_EXECUTE, adapter.HOST_AUTHORITY}
                   for contract, _, _ in live_contracts.calls)


@pytest.mark.parametrize("mismatch", ["id", "root", "context", "untrusted", "unselected", "mount"])
def test_live_binding_rejected_before_provider_or_profile_work(tmp_path, monkeypatch, live_contracts, mismatch):
    from domain.agent import self_improvement_live_loop as live

    args = {"workspace_root": tmp_path}
    if mismatch == "id":
        args["workspace_id"] = "model-invented-workspace"
    elif mismatch == "root":
        args["workspace_root"] = tmp_path.parent
    elif mismatch == "context":
        args["context"] = {"workspace_id": "another-workspace"}
    elif mismatch == "untrusted":
        live_contracts.mount["metadata"]["trusted"] = False
    elif mismatch == "unselected":
        live_contracts.selected = None
    else:
        live_contracts.mount["id"] = "other-mount"

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid binding reached provider/profile work")

    monkeypatch.setattr(live, "create_mimo_profile", forbidden)
    monkeypatch.setattr(live, "_configured_mimo_provider", forbidden)
    result = live.run_live_improvement(**args)
    assert result["success"] is False
    assert result["error_code"] == "WORKSPACE_BINDING_INVALID"


def test_live_model_cannot_replace_selected_workspace(tmp_path, live_contracts):
    from domain.agent import self_improvement_live_loop as live
    from domain.coding import contract_adapter as adapter

    workspace = live.resolve_live_workspace(tmp_path)
    result = live._execute_tool(
        "coding_terminal_exec", {"command": "pwd", "workspace_id": "other-workspace"}, workspace,
    )
    assert result["status"] == "error"
    assert "cannot replace" in result["error"]["message"]
    assert all(contract == adapter.WORKSPACE_RESOURCE for contract, _, _ in live_contracts.calls)


def test_live_pending_stops_once_and_normal_exact_followup_is_one_shot(tmp_path, monkeypatch, live_contracts):
    from blocks.agent.self_improvement_run import run
    from blocks.coding.terminal_exec import run as terminal
    from domain.agent import self_improvement_live_loop as live
    from domain.coding import contract_adapter as adapter
    from domain.safety import approval
    from domain.tool_policy.internal_context import mark_tool_server_approval_context

    (tmp_path / "sample.py").write_text("original content", encoding="utf-8")
    (tmp_path / "src").mkdir()
    calls = []

    def complete(*args):
        calls.append(args)
        return {"content": [
            {"type": "tool_use", "id": "read", "name": "coding_file_read", "input": {"path": "sample.py"}},
            {"type": "tool_use", "id": "test", "name": "coding_terminal_exec", "input": {"command": "echo test", "cwd": "src", "approved": True}},
            {"type": "tool_use", "id": "commit", "name": "coding_git_commit", "input": {"message": "must not run"}},
        ]}

    provider = SimpleNamespace(_api_key="test-only", provider_id="test-provider", complete=complete)
    monkeypatch.setattr(live, "_configured_mimo_provider", lambda: provider)
    args = {"workspace_root": str(tmp_path), "task_id": "pending-once", "state_path": str(tmp_path / "state.json")}
    startup = mark_tool_server_approval_context({"principal_id": "local-owner"})
    response = run(args, startup)
    assert response["status"] == "ok"
    pending = response["data"]
    assert pending["success"] is False and pending["status"] == "approval_pending"
    assert pending["tool_calls_made"] == 2 and pending["files_read"] == ["sample.py"]
    assert pending["files_modified"] == [] and pending["commit_hash"] == ""
    assert len(calls) == 1
    assert pending["tool_name"] == "coding_terminal_exec"
    assert pending["arguments"] == {"command": "echo test", "cwd": "src", "workspace_id": "selected-workspace"}
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["tasks"][0]["status"] == "approval_pending"
    assert state["tasks"][0]["result"] == pending
    assert state["completed_tasks"] == [] and state["failed_tasks"] == []
    assert len(state["approval_waiting"]) == 1

    def forbidden():
        raise AssertionError("task retry contacted the provider")

    monkeypatch.setattr(live, "_configured_mimo_provider", forbidden)
    assert run(args, startup)["data"] == pending
    assert len(approval.list_approval_requests()) == 1
    assert not any(contract in {adapter.SHELL_EXECUTE, adapter.GIT_WRITE} for contract, _, _ in live_contracts.calls)

    # The existing coding followup, rather than the live loop, executes the
    # stored exact arguments with its ordinary one-shot server token.
    token = approval.approve(pending["approval_request_id"])["token"]
    approved = {**pending["arguments"], "approval_token": token}
    changed = terminal({**approved, "cwd": "."}, {"principal_id": "local-owner"})
    assert changed["error"]["code"] == "APPROVAL_ARGUMENTS_CHANGED"
    executed = terminal(approved, {"principal_id": "local-owner"})
    assert executed["status"] == "ok" and executed["data"]["exit_code"] == 0
    replay = terminal(approved, {"principal_id": "local-owner"})
    assert replay["error"]["code"] == "APPROVAL_TOKEN_USED"
    assert run(args, startup)["data"] == pending
    assert len([1 for contract, _, _ in live_contracts.calls if contract == adapter.SHELL_EXECUTE]) == 1


def test_live_multi_stops_on_first_pending_request(tmp_path, monkeypatch, live_contracts):
    from domain.agent import self_improvement_live_loop as live
    from domain.safety import approval

    calls = []

    def complete(*args):
        calls.append(args)
        return {"content": [{"type": "tool_use", "name": "coding_terminal_exec", "id": "one", "input": {"command": "echo test"}}]}

    monkeypatch.setattr(live, "_configured_mimo_provider", lambda: SimpleNamespace(
        _api_key="test-only", provider_id="test-provider", complete=complete,
    ))
    result = live.run_multi_task_dogfood(workspace_root=tmp_path, task_count=3)
    assert result["total_tasks"] == 1 and result["approval_waiting"] == 1
    assert result["successful"] == 0 and result["failed"] == 0
    assert len(calls) == 1 and len(approval.list_approval_requests()) == 1


def test_live_provider_schemas_keep_arguments_and_bind_selected_workspace():
    from domain.agent.self_improvement_live_loop import _adapt_tools

    tools = {item["function"]["name"]: item["function"]["parameters"]
             for item in _adapt_tools("selected-workspace")}
    assert tools["coding_terminal_exec"]["required"] == ["command", "workspace_id"]
    assert "cwd" in tools["coding_terminal_exec"]["properties"]
    assert tools["coding_file_patch"]["required"] == ["path", "old", "new", "workspace_id"]
    assert "paths" in tools["coding_git_commit"]["properties"]
    for schema in tools.values():
        assert schema["properties"]["workspace_id"]["enum"] == ["selected-workspace"]
        assert "workspace_id" in schema["required"]


def test_live_default_state_path_cannot_escape_through_symlink(tmp_path, monkeypatch, live_contracts):
    from domain.agent import self_improvement_live_loop as live

    outside = tmp_path.parent / (tmp_path.name + "-outside-state")
    outside.mkdir()
    (tmp_path / "user_data").symlink_to(outside, target_is_directory=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("escaped state reached profile/provider work")

    monkeypatch.setattr(live, "create_mimo_profile", forbidden)
    monkeypatch.setattr(live, "_configured_mimo_provider", forbidden)
    result = live.run_live_improvement(workspace_root=tmp_path)
    assert result["error_code"] == "WORKSPACE_BINDING_INVALID"
    assert list(outside.iterdir()) == []
