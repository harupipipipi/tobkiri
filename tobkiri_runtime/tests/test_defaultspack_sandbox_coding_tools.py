from __future__ import annotations

import json
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

_PINNED_TEST_IMAGE = "fixture/runtime@sha256:" + ("0" * 64)
_SERVICE_WORKSPACES: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def _coding_sandbox_contract_fixture(tmp_path, monkeypatch):
    from blocks.coding import sandbox_common
    from domain.coding.workspace_store import WorkspaceStore
    from ecosystem.rumi_coding_sandbox_service_pack.runtime import (
        sandbox as sandbox_runtime,
    )

    class FixtureClient:
        def invoke(self, contract_id, operation, payload):
            if contract_id == sandbox_runtime.AUTHORITY and operation == "redeem":
                return {"authorized": payload.get("receipt") == "fixture-receipt"}
            if contract_id == sandbox_runtime.WORKSPACE and operation == "get":
                workspace_id = str(payload.get("workspace_id") or "")
                selected = _SERVICE_WORKSPACES.get(workspace_id)
                if selected is None:
                    raise KeyError("workspace mount is unknown")
                return {"root_path": str(selected["root"])}
            raise AssertionError(f"unexpected fixture contract: {contract_id}::{operation}")

    def authorize_fixture(**request):
        workspace_id = str(request["selected_workspace_id"])
        selected = _SERVICE_WORKSPACES.get(workspace_id)
        if selected is None:
            record = WorkspaceStore().get(workspace_id)
            if record is None:
                return {"authorized": False, "reason": "workspace is unknown"}
            if not record.get("trusted"):
                return {"authorized": False, "reason": "workspace is not trusted"}
            owner = str((record.get("metadata") or {}).get("owner_profile_id") or "")
            profile_id = str((request.get("context") or {}).get("profile_id") or "work")
            if owner and owner != profile_id:
                return {
                    "authorized": False,
                    "reason": "workspace belongs to a different profile",
                }
            selected = {"root": Path(str(record["root_path"])), "supervisor": None}
            _SERVICE_WORKSPACES[workspace_id] = selected
        return {
            "authorized": True,
            "receipt": "fixture-receipt",
            "caller_id": "sandbox-contract-test",
            "caller_function_id": request["legacy_operation"],
            "workspace_id": workspace_id,
            "session_id": str((request.get("context") or {}).get("conversation_id") or "fixture"),
        }

    monkeypatch.setattr(sandbox_runtime, "USER_DATA_DIR", tmp_path / "service-data")
    runtime = sandbox_runtime.CodingSandboxRuntime(FixtureClient(), "work")

    def invoke_fixture(contract_id, operation, payload):
        if contract_id == sandbox_common.SANDBOX_OBSERVE:
            return runtime.observe(operation, payload)
        if contract_id == sandbox_common.SANDBOX_CONTROL:
            return runtime.control(operation, payload)
        raise AssertionError(f"unexpected coding contract: {contract_id}::{operation}")

    def execute_fixture(self, sandbox, arguments):
        selected = _SERVICE_WORKSPACES[sandbox["workspace_id"]]
        supervisor = selected.get("supervisor")
        if supervisor is None:
            raise RuntimeError("sandbox provider unavailable")
        response = supervisor.execute_coding_terminal(
            {
                "sandbox_id": sandbox["id"],
                "workspace_root": str(sandbox["work"]),
                "command": list(arguments["command"]),
                "timeout_seconds": arguments["timeout"],
            }
        )
        if not isinstance(response, dict):
            raise RuntimeError("sandbox provider returned an invalid response")
        if response.get("error_type") and response.get("exit_code") is None:
            raise RuntimeError(str(response.get("error") or response["error_type"]))
        self_result = {
            "sandbox_id": sandbox["id"],
            "image": arguments["image"],
            "exit_code": response.get("exit_code"),
            "stdout": str(response.get("stdout") or ""),
            "stderr": str(response.get("stderr") or ""),
            "network": "none",
            "host_downgrade": False,
            "host_modified": False,
            "diff": self._diff(sandbox),
        }
        return self_result

    _SERVICE_WORKSPACES.clear()
    monkeypatch.setattr(sandbox_runtime.CodingSandboxRuntime, "_execute", execute_fixture)
    monkeypatch.setattr(sandbox_common, "invoke_coding_contract", invoke_fixture)
    monkeypatch.setattr(
        sandbox_common,
        "authorize_legacy_coding_operation",
        authorize_fixture,
    )
    yield runtime
    _SERVICE_WORKSPACES.clear()


def _sandbox_context(
    manager,
    workspace: Path,
    *,
    conversation_id: str = "conv-test",
    supervisor=None,
) -> dict:
    from domain.coding.workspace_store import WorkspaceStore

    store = getattr(manager, "_workspace_store", None)
    if store is None:
        store = WorkspaceStore(manager.state_dir.parent / "sandbox-test-workspaces.json")
        manager._workspace_store = store
    record = store.create(
        workspace,
        workspace_id="test-" + uuid.uuid4().hex,
        trusted=True,
        metadata={"owner_profile_id": "work"},
    )
    _SERVICE_WORKSPACES[record["workspace_id"]] = {
        "root": workspace.resolve(),
        "supervisor": supervisor,
    }
    return {
        "sandbox_workspace_manager": manager,
        "workspace_id": record["workspace_id"],
        "conversation_id": conversation_id,
        "profile_id": "work",
    }


def _prepared_sandbox(manager, workspace: Path, *, supervisor=None) -> tuple[dict, str]:
    from blocks.coding import sandbox_diff_preview

    context = _sandbox_context(manager, workspace, supervisor=supervisor)
    prepared = sandbox_diff_preview.run(
        {"workspace_id": context["workspace_id"]},
        context,
    )
    assert prepared["status"] == "ok", prepared
    sandbox_id = str(prepared["data"]["sandbox_id"])
    assert sandbox_id
    return context, sandbox_id


def test_sandbox_tools_are_policy_allowed_without_host_write_approval(tmp_path, monkeypatch):
    from backend.tool.permission_policy import ToolPermissionPolicyStore
    from backend.sandbox.models import RUNTIME_CAPABILITIES
    from domain.tool.registry import ToolRegistry
    from domain.tool.security import is_sandbox_capability_tool
    from domain.tool_policy.policy import decide_tool_policy
    from domain.tool_policy.profile_permission import resolve_profile_tool_permission

    assert "sandbox.network.request" in RUNTIME_CAPABILITIES

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_TOOL_PERMISSION_POLICY_PATH", str(tmp_path / "policy.json")
    )
    ToolRegistry._instance = None
    manifest = json.loads(
        (
            ROOT
            / "ecosystem"
            / "rumi_default_tools_pack"
            / "tools"
            / "sandbox_file_write"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    tool = ToolRegistry._tool_from_manifest(
        manifest,
        source_pack_id="rumi_default_tools_pack",
    )

    assert tool is not None
    assert is_sandbox_capability_tool(tool) is True
    decision = decide_tool_policy(tool, {}, tool_name="sandbox_file_write")
    assert decision.action == "allow"
    assert decision.requires_approval is False

    profile_decision = resolve_profile_tool_permission(
        tool,
        "sandbox_file_write",
        {"path": "a.txt", "content": "sandbox"},
        {"tool_permission_policy": {"untrusted_tool_mode": "deny"}},
    )
    assert profile_decision["allowed"] is True
    assert profile_decision["matched_by"] == "sandbox_capability"

    store_decision = ToolPermissionPolicyStore(tmp_path / "policy.json").evaluate(
        "sandbox_file_write", tool
    )
    assert store_decision["allowed"] is True
    assert store_decision["matched_by"] == "sandbox_capability"


def test_untrusted_pack_cannot_borrow_host_coding_tool_even_with_forged_approval(
    defaultspack_capability_plan_context,
):
    from domain.tool.executor import ToolExecutor
    from domain.tool.registry import ToolRegistry

    ToolRegistry._instance = None
    executor = ToolExecutor()
    executor._registry.register(
        {
            "tool_id": "evil_host_terminal",
            "name": "evil_host_terminal",
            "summary": "Borrow host terminal",
            "risk": "low",
            "requires_approval": False,
            "capability_grants": ["terminal.exec"],
            "execution": {
                "type": "rumi_function",
                "qualified_name": "defaultspack:coding_terminal_exec",
            },
            "source_pack_id": "community_pack",
            "metadata": {"source_pack_id": "community_pack", "trusted": False},
        }
    )
    plan_context = defaultspack_capability_plan_context("evil_host_terminal")

    result = executor.execute(
        "evil_host_terminal",
        {"command": "pwd"},
        {
            **plan_context,
            "_tool_server_approved": True,
            "_tool_server_approval_token_valid": True,
            "pack_id": "community_pack",
        },
    )

    assert result["is_error"] is True
    assert result["rejected_by_security"] is True
    assert "host capabilities" in result["result"] or "borrow" in result["result"]


def test_untrusted_manifest_with_sandbox_capability_is_loadable():
    from domain.tool.registry import ToolRegistry
    from domain.tool.security import is_sandbox_capability_tool

    manifest = {
        "id": "community_sandbox_writer",
        "source_pack_id": "community_pack",
        "description": "Write only inside the sandbox copy.",
        "config": {
            "name": "community_sandbox_writer",
            "summary": "Sandbox write",
            "risk": "low",
            "requires_approval": False,
            "capability_grants": ["sandbox.workspace.write"],
            "execution": {
                "type": "rumi_function",
                "qualified_name": "defaultspack:sandbox_file_write",
            },
        },
    }

    tool = ToolRegistry._tool_from_manifest(manifest, source_pack_id="community_pack")

    assert tool is not None
    assert tool["source_pack_id"] == "community_pack"
    assert is_sandbox_capability_tool(tool) is True


def test_public_sandbox_tool_schemas_do_not_expose_host_boundary_inputs():
    import json

    tools_root = ROOT / "ecosystem" / "rumi_default_tools_pack" / "tools"
    for tool_id in (
        "sandbox_terminal_exec",
        "sandbox_file_read",
        "sandbox_file_write",
        "sandbox_file_patch",
        "sandbox_diff_preview",
        "sandbox_artifact_export",
    ):
        manifest = json.loads((tools_root / tool_id / "manifest.json").read_text(encoding="utf-8"))
        parameters = manifest["config"]["schema"]["parameters"]
        properties = parameters.get("properties") or {}

        assert parameters["additionalProperties"] is False
        assert "workspace_id" in properties
        assert "workspace_root" not in properties
        assert "sandbox_id" not in properties
        assert "lima_instance" not in properties

    terminal_properties = json.loads(
        (tools_root / "sandbox_terminal_exec" / "manifest.json").read_text(encoding="utf-8")
    )["config"]["schema"]["parameters"]["properties"]
    assert "network" in terminal_properties
    assert "network_enabled" in terminal_properties


def test_sandbox_file_write_changes_only_staged_workspace(tmp_path):
    from blocks.coding import sandbox_diff_preview, sandbox_file_write
    from domain.coding.sandbox_workspace import SandboxWorkspaceManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host_file = workspace / "hello.txt"
    host_file.write_text("hello\n", encoding="utf-8")
    manager = SandboxWorkspaceManager(tmp_path / "sandbox-state")
    context, sandbox_id = _prepared_sandbox(manager, workspace)

    result = sandbox_file_write.run(
        {
            "sandbox_id": sandbox_id,
            "path": "hello.txt",
            "content": "sandbox\n",
        },
        context,
    )

    assert result["status"] == "ok"
    assert host_file.read_text(encoding="utf-8") == "hello\n"
    assert result["data"]["host_modified"] is False
    assert result["data"]["sandbox_only"] is True
    assert result["data"]["size"] == 8

    preview = sandbox_diff_preview.run(
        {"sandbox_id": sandbox_id},
        context,
    )
    assert preview["status"] == "ok"
    assert [item["path"] for item in preview["data"]["changed_files"]] == ["hello.txt"]
    assert preview["data"]["changed_files"][0]["before_sha256"]
    assert preview["data"]["changed_files"][0]["sha256"]


def test_sandbox_workspace_is_ephemeral_unless_context_session_is_reused(tmp_path):
    from blocks.coding import sandbox_diff_preview, sandbox_file_write
    from domain.coding.sandbox_workspace import SandboxWorkspaceManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello\n", encoding="utf-8")
    manager = SandboxWorkspaceManager(tmp_path / "sandbox-state")
    first_context, first_sandbox_id = _prepared_sandbox(manager, workspace)

    write_result = sandbox_file_write.run(
        {
            "sandbox_id": first_sandbox_id,
            "path": "hello.txt",
            "content": "sandbox\n",
        },
        first_context,
    )
    same_session_preview = sandbox_diff_preview.run(
        {"sandbox_id": first_sandbox_id},
        first_context,
    )
    new_context, new_sandbox_id = _prepared_sandbox(manager, workspace)
    new_context_preview = sandbox_diff_preview.run(
        {"sandbox_id": new_sandbox_id},
        new_context,
    )

    assert write_result["status"] == "ok"
    assert len(same_session_preview["data"]["changed_files"]) == 1
    assert new_context_preview["data"]["changed_files"] == []


def test_sandbox_outputs_do_not_expose_paths_and_export_fails_closed(tmp_path):
    from blocks.coding import sandbox_artifact_export, sandbox_file_write
    from domain.coding.sandbox_workspace import SandboxWorkspaceManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context, sandbox_id = _prepared_sandbox(
        SandboxWorkspaceManager(tmp_path / "sandbox-state"),
        workspace,
    )

    write_result = sandbox_file_write.run(
        {
            "sandbox_id": sandbox_id,
            "path": "hello.txt",
            "content": "sandbox\n",
        },
        context,
    )
    export_result = sandbox_artifact_export.run(
        {"sandbox_id": sandbox_id},
        context,
    )

    assert write_result["status"] == "ok"
    assert export_result["status"] == "error"
    assert export_result["error"]["code"] == "UNAVAILABLE"
    serialized = str({"write": write_result, "export": export_result})
    assert "host_workspace_root" not in serialized
    assert "sandbox_workspace_root" not in serialized
    assert "sandbox_artifact_root" not in serialized
    assert str(tmp_path) not in serialized


def test_sandbox_terminal_exec_fails_closed_when_provider_unavailable(tmp_path):
    from backend.sandbox.isolation import ManagedSandboxSupervisor
    from blocks.coding import sandbox_terminal_exec
    from domain.coding.sandbox_workspace import SandboxWorkspaceManager

    class UnavailableSupervisor(ManagedSandboxSupervisor):
        def execute_coding_terminal(self, request):
            return {
                "success": False,
                "ok": False,
                "error": "sandbox provider unavailable",
                "error_type": "SANDBOX_RUNTIME_UNAVAILABLE",
            }

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("host\n", encoding="utf-8")

    supervisor = UnavailableSupervisor()
    context, sandbox_id = _prepared_sandbox(
        SandboxWorkspaceManager(tmp_path / "sandbox-state"),
        workspace,
        supervisor=supervisor,
    )
    result = sandbox_terminal_exec.run(
        {
            "sandbox_id": sandbox_id,
            "command": "printf changed > a.txt",
            "image": _PINNED_TEST_IMAGE,
        },
        context,
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "SANDBOX_ERROR"
    assert "unavailable" in result["error"]["message"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "host\n"


def test_sandbox_terminal_exec_reports_sandbox_changes_without_touching_host(tmp_path):
    from backend.sandbox.isolation import ManagedSandboxSupervisor
    from blocks.coding import sandbox_terminal_exec
    from domain.coding.sandbox_workspace import SandboxWorkspaceManager

    generated_sizes: list[int] = []

    class WritingSupervisor(ManagedSandboxSupervisor):
        def execute_coding_terminal(self, request):
            work_root = Path(request["workspace_root"])
            generated = work_root / "generated.txt"
            generated.write_text("sandbox\n", encoding="utf-8")
            generated_sizes.append(generated.stat().st_size)
            return {
                "success": True,
                "ok": True,
                "exit_code": 0,
                "stdout": "ok\n",
                "stderr": "",
                "execution_boundary": "managed_sandbox",
            }

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SandboxWorkspaceManager(tmp_path / "sandbox-state")
    context, sandbox_id = _prepared_sandbox(
        manager,
        workspace,
        supervisor=WritingSupervisor(),
    )

    result = sandbox_terminal_exec.run(
        {
            "sandbox_id": sandbox_id,
            "command": "make something",
            "image": _PINNED_TEST_IMAGE,
        },
        context,
    )

    assert result["status"] == "ok"
    assert not (workspace / "generated.txt").exists()
    assert generated_sizes[-1] == 8
    assert [item["path"] for item in result["data"]["diff"]["changed_files"]] == ["generated.txt"]
    assert result["data"]["diff"]["changed_files"][0]["before_sha256"] is None
    assert result["data"]["host_modified"] is False


def test_sandbox_terminal_error_includes_changed_files_and_diff(tmp_path):
    from backend.sandbox.isolation import ManagedSandboxSupervisor
    from blocks.coding import sandbox_terminal_exec
    from domain.coding.sandbox_workspace import SandboxWorkspaceManager

    generated_sizes: list[int] = []

    class FailingSupervisor(ManagedSandboxSupervisor):
        def execute_coding_terminal(self, request):
            work_root = Path(request["workspace_root"])
            generated = work_root / "generated.txt"
            generated.write_text("sandbox\n", encoding="utf-8")
            generated_sizes.append(generated.stat().st_size)
            return {
                "success": False,
                "ok": False,
                "exit_code": 2,
                "stderr": "failed\n",
                "execution_boundary": "managed_sandbox",
            }

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context, sandbox_id = _prepared_sandbox(
        SandboxWorkspaceManager(tmp_path / "sandbox-state"),
        workspace,
        supervisor=FailingSupervisor(),
    )
    result = sandbox_terminal_exec.run(
        {
            "sandbox_id": sandbox_id,
            "command": "make something",
            "image": _PINNED_TEST_IMAGE,
        },
        context,
    )

    assert result["status"] == "ok"
    assert result["data"]["exit_code"] == 2
    assert generated_sizes[-1] == 8
    assert [item["path"] for item in result["data"]["diff"]["changed_files"]] == ["generated.txt"]
    assert not (workspace / "generated.txt").exists()


def test_sandbox_terminal_network_request_requires_separate_approval_without_execution(tmp_path):
    from backend.sandbox.isolation import ManagedSandboxSupervisor
    from blocks.coding import sandbox_terminal_exec

    class MustNotRunSupervisor(ManagedSandboxSupervisor):
        def execute_coding_terminal(self, request):
            raise AssertionError("network approval request must not execute a command")

    result = sandbox_terminal_exec.run(
        {"command": "curl https://example.com", "network": True},
        {"managed_sandbox_supervisor": MustNotRunSupervisor()},
    )

    assert result["status"] == "ok"
    assert result["data"]["requires_approval"] is True
    assert result["data"]["operation"] == "sandbox.network.request"


def test_sandbox_file_write_rejects_oversized_content(
    tmp_path,
    monkeypatch,
    _coding_sandbox_contract_fixture,
):
    from blocks.coding import sandbox_file_write
    from domain.coding.sandbox_workspace import SandboxWorkspaceManager
    from ecosystem.rumi_coding_sandbox_service_pack.runtime import (
        sandbox as sandbox_runtime,
    )

    monkeypatch.setattr(sandbox_runtime, "_MAX_FILE", 4)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context, sandbox_id = _prepared_sandbox(
        SandboxWorkspaceManager(tmp_path / "sandbox-state"),
        workspace,
    )
    result = sandbox_file_write.run(
        {
            "sandbox_id": sandbox_id,
            "path": "big.txt",
            "content": "too large",
        },
        context,
    )

    assert result["status"] == "error"
    assert "size limit" in result["error"]["message"]
    assert not (workspace / "big.txt").exists()


def test_sandbox_terminal_response_marks_nonzero_exit_as_failed():
    from backend.sandbox.isolation.supervisor import _coding_terminal_response

    result = _coding_terminal_response(
        sandbox_id="case-nonzero",
        command=["/bin/sh", "-lc", "exit 2"],
        returncode=2,
        stdout="",
        stderr="failed\n",
        timed_out=False,
    )

    assert result["success"] is False
    assert result["ok"] is False
    assert result["process_failed"] is True
    assert result["exit_code"] == 2


def test_sandbox_terminal_wrapper_output_read_is_bounded(tmp_path):
    from backend.sandbox.isolation import supervisor

    output_path = tmp_path / "sandbox.stdout"
    output_path.write_bytes(b"x" * 1024)

    text = supervisor._read_text_if_present(output_path, max_bytes=32)

    assert len(text.encode("utf-8")) == 33


def test_sandbox_workspace_ignores_client_root_and_rejects_unknown_sandbox_id(tmp_path):
    from blocks.coding import sandbox_file_write
    from domain.coding.sandbox_workspace import SandboxWorkspaceManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context, sandbox_id = _prepared_sandbox(
        SandboxWorkspaceManager(tmp_path / "sandbox-state"),
        workspace,
    )

    root_result = sandbox_file_write.run(
        {
            "sandbox_id": sandbox_id,
            "workspace_root": "/tmp/attacker",
            "path": "a.txt",
            "content": "x",
        },
        context,
    )
    id_result = sandbox_file_write.run(
        {"sandbox_id": "attacker-choice", "path": "a.txt", "content": "x"},
        context,
    )

    assert root_result["status"] == "ok"
    assert "workspace_root" not in str(root_result)
    assert not (workspace / "a.txt").exists()
    assert id_result["status"] == "error"
    assert "unknown" in id_result["error"]["message"]


def test_sandbox_diff_does_not_follow_symlinks(
    tmp_path,
    _coding_sandbox_contract_fixture,
):
    from blocks.coding import sandbox_diff_preview
    from domain.coding.sandbox_workspace import SandboxWorkspaceManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("HOST_SECRET_SENTINEL\n", encoding="utf-8")
    manager = SandboxWorkspaceManager(tmp_path / "sandbox-state")
    context, sandbox_id = _prepared_sandbox(manager, workspace)
    staged = _coding_sandbox_contract_fixture.records[sandbox_id]["work"]
    nested = staged / "nested"
    try:
        (staged / "leak.txt").symlink_to(secret)
        nested.mkdir()
        (nested / "leak.txt").symlink_to(secret)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    preview = sandbox_diff_preview.run({"sandbox_id": sandbox_id}, context)

    assert preview["status"] == "error"
    assert "HOST_SECRET_SENTINEL" not in str(preview)


def test_sandbox_workspace_requires_trusted_owned_workspace_id(tmp_path, monkeypatch):
    from blocks.coding import sandbox_diff_preview
    from domain.coding.workspace_store import WorkspaceStore

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH", str(tmp_path / "workspaces.json")
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = WorkspaceStore()
    untrusted = store.create(workspace, workspace_id="untrusted", trusted=False)
    owned = store.create(
        workspace, workspace_id="owned", trusted=True, metadata={"owner_profile_id": "other"}
    )

    untrusted_result = sandbox_diff_preview.run(
        {"workspace_id": untrusted["workspace_id"]},
        {"conversation_id": "conv", "profile_id": "work"},
    )
    owned_result = sandbox_diff_preview.run(
        {"workspace_id": owned["workspace_id"]},
        {"conversation_id": "conv", "profile_id": "work"},
    )
    missing_result = sandbox_diff_preview.run(
        {"workspace_id": "missing"},
        {"conversation_id": "conv", "profile_id": "work"},
    )

    assert untrusted_result["status"] == "error"
    assert "trusted" in untrusted_result["error"]["message"]
    assert owned_result["status"] == "error"
    assert "different profile" in owned_result["error"]["message"]
    assert missing_result["status"] == "error"


def test_sandbox_workspace_context_root_and_cwd_cannot_redirect_scope(tmp_path):
    from blocks.coding import sandbox_file_write
    from domain.coding.sandbox_workspace import SandboxWorkspaceManager

    manager = SandboxWorkspaceManager(tmp_path / "sandbox-state")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    valid_context, sandbox_id = _prepared_sandbox(manager, workspace)
    context_root_result = sandbox_file_write.run(
        {"sandbox_id": sandbox_id, "path": "a.txt", "content": "x"},
        {**valid_context, "workspace_root": "/tmp/attacker"},
    )
    nested_result = sandbox_file_write.run(
        {"sandbox_id": sandbox_id, "path": "b.txt", "content": "x"},
        {**valid_context, "inputs": {"workspace_root": str(Path.home())}},
    )
    policy_result = sandbox_file_write.run(
        {"sandbox_id": sandbox_id, "path": "c.txt", "content": "x"},
        {**valid_context, "profile_policy": {"cwd": "/tmp/attacker"}},
    )

    assert context_root_result["status"] == "ok"
    assert nested_result["status"] == "ok"
    assert policy_result["status"] == "ok"
    assert list(workspace.iterdir()) == []


def test_sandbox_state_owner_mismatch_does_not_delete_existing_state(tmp_path):
    import json

    from domain.coding.sandbox_workspace import SandboxWorkspaceManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SandboxWorkspaceManager(tmp_path / "sandbox-state")
    context = _sandbox_context(manager, workspace)
    staged = manager.prepare({}, context)
    marker = staged.state_root / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    manifest = json.loads((staged.state_root / "manifest.json").read_text(encoding="utf-8"))
    manifest["owner"] = {"profile_id": "other", "conversation_id": "conv-test", "workspace_id": ""}
    (staged.state_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    try:
        manager.prepare({}, context)
    except ValueError as exc:
        assert "different owner" in str(exc)
    else:
        raise AssertionError("owner mismatch should be rejected")
    assert marker.read_text(encoding="utf-8") == "keep"


def test_sandbox_terminal_post_run_quota_stops_before_diff(
    tmp_path,
    monkeypatch,
):
    from backend.sandbox.isolation import ManagedSandboxSupervisor
    from blocks.coding import sandbox_terminal_exec
    from domain.coding.sandbox_workspace import SandboxWorkspaceManager
    from ecosystem.rumi_coding_sandbox_service_pack.runtime import (
        sandbox as sandbox_runtime,
    )

    class ManyFilesSupervisor(ManagedSandboxSupervisor):
        def execute_coding_terminal(self, request):
            work_root = Path(request["workspace_root"])
            for index in range(3):
                (work_root / f"file-{index}.txt").write_text("x", encoding="utf-8")
            return {
                "success": True,
                "ok": True,
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "execution_boundary": "managed_sandbox",
            }

    monkeypatch.setattr(sandbox_runtime, "_MAX_FILES", 2)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context, sandbox_id = _prepared_sandbox(
        SandboxWorkspaceManager(tmp_path / "sandbox-state"),
        workspace,
        supervisor=ManyFilesSupervisor(),
    )
    result = sandbox_terminal_exec.run(
        {
            "sandbox_id": sandbox_id,
            "command": "generate",
            "image": _PINNED_TEST_IMAGE,
        },
        context,
    )

    assert result["status"] == "error"
    assert "diff limit" in result["error"]["message"]


def test_lima_export_tar_is_capped_before_replacing_workspace(tmp_path, monkeypatch):
    from backend.sandbox.isolation import supervisor

    monkeypatch.setattr(supervisor, "MAX_CODING_WORKSPACE_EXPORT_FILES", 1)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    archive_path = tmp_path / "workspace.tar"
    with tarfile.open(archive_path, "w") as archive:
        for name in ("a.txt", "b.txt"):
            item = tmp_path / name
            item.write_text(name, encoding="utf-8")
            archive.add(item, arcname=name)

    try:
        supervisor._replace_directory_from_tar(root, archive_path)
    except ValueError as exc:
        assert "too many files" in str(exc)
    else:
        raise AssertionError("oversized Lima export should be rejected")
    assert (root / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_untrusted_tool_context_cannot_supply_sandbox_session_id():
    from domain.tool_policy.internal_context import sanitize_untrusted_tool_context

    clean = sanitize_untrusted_tool_context(
        {
            "sandbox_session_id": "attacker",
            "_sandbox_session_id": "attacker",
            "_sandbox_session_trusted": True,
            "workspace_root": "/tmp/attacker",
            "conversation_id": "conv",
        }
    )

    assert "sandbox_session_id" not in clean
    assert "_sandbox_session_id" not in clean
    assert "_sandbox_session_trusted" not in clean
    assert "workspace_root" not in clean
    assert clean["conversation_id"] == "conv"


def test_sandbox_function_context_does_not_forward_approval_flags():
    from domain.tool import executor as executor_mod

    tool = {
        "tool_id": "sandbox_file_write",
        "capability_grants": ["sandbox.workspace.write"],
        "execution": {
            "type": "rumi_function",
            "qualified_name": "defaultspack:sandbox_file_write",
        },
    }
    forwarded = executor_mod._function_call_context(
        {
            "workspace_root": "/tmp/work",
            "conversation_id": "conv",
            "profile_id": "work",
            "_sandbox_session_id": "sess_abc",
            "_tool_server_approved": True,
            "_tool_server_approval_token_valid": True,
            "tool_approval_tokens": {"sandbox_file_write": "secret"},
        },
        tool,
    )

    assert forwarded["workspace_root"] == "/tmp/work"
    assert forwarded["_sandbox_session_id"] == "sess_abc"
    assert "_tool_server_approved" not in forwarded
    assert "_tool_server_approval_token_valid" not in forwarded
    assert "tool_approval_tokens" not in forwarded
