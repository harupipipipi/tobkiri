from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.tool.executor import ToolExecutor, _context_with_tool_approval_token, _tool_approval_scope  # noqa: E402
from domain.tool.registry import ToolRegistry  # noqa: E402
from domain.tool.security import requires_approval_for_security  # noqa: E402


def _patch_approval_module(monkeypatch, approval_module):
    """Patch the module globals used by the imported approval helper."""

    monkeypatch.setitem(
        _context_with_tool_approval_token.__globals__,
        "_approval_module",
        lambda: approval_module,
    )


def test_computer_use_context_apps_windows_alias_is_canonicalized_for_approval_scope():
    operation, approval_args = _tool_approval_scope(
        {"tool_id": "computer_use", "name": "computer_use"},
        {"action": "context/apps/windows"},
    )

    assert operation == "computer.context"
    assert approval_args == {"action": "computer.context", "payload": {}}


def test_computer_use_open_url_alias_is_canonicalized_for_approval_scope():
    operation, approval_args = _tool_approval_scope(
        {"tool_id": "computer_use", "name": "computer_use"},
        {"action": "open_url", "url": "https://gemini.google.com"},
    )

    assert operation == "browser.open_url"
    assert approval_args == {
        "action": "browser.open_url",
        "payload": {"url": "https://gemini.google.com"},
    }


def test_computer_use_open_url_replay_scope_keeps_browser_payload_and_ignores_token():
    from domain.safety import approval

    operation, approval_args = _tool_approval_scope(
        {"tool_id": "computer_use", "name": "computer_use"},
        {
            "action": "browser.open_url",
            "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "profile_id": "default",
            "persistent": False,
            "target_app": "Vivaldi",
            "approval_token": "spent-token",
        },
    )

    expected_args = {
        "action": "browser.open_url",
        "payload": {
            "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "profile_id": "default",
            "persistent": False,
            "target_app": "Vivaldi",
        },
    }
    assert operation == "browser.open_url"
    assert approval_args == expected_args
    assert approval.hash_arguments(approval_args) == approval.hash_arguments(expected_args)


def test_computer_use_open_url_action_scoped_token_approves_replay_context():
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    expected_args = {
        "action": "browser.open_url",
        "payload": {
            "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "profile_id": "default",
            "persistent": False,
            "target_app": "Vivaldi",
        },
    }
    request = approval.create_approval_request(
        "browser.open_url",
        "high",
        expected_args,
        details={
            "tool_name": "computer_use",
            "action": "browser.open_url",
            "function_id": "browser.open_url",
            "pack_id": "defaultspack",
            "conversation_id": "conv-open-url",
            "arguments": expected_args,
        },
    )
    decision = approval.approve(request["request_id"])

    context, error = _context_with_tool_approval_token(
        {"pack_id": "defaultspack", "conversation_id": "conv-open-url"},
        {"tool_id": "computer_use", "name": "computer_use", "requires_approval": True, "risk": "high"},
        {
            "action": "browser.open_url",
            "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "profile_id": "default",
            "persistent": False,
            "target_app": "Vivaldi",
            "approval_token": decision["token"],
        },
    )

    assert error is None
    assert context["_tool_server_approval_token_valid"] is True
    assert context["_tool_server_approval_operation"] == "browser.open_url"
    assert context["_tool_server_approval_args_hash"] == request["args_hash"]


def test_computer_use_physical_click_replay_scope_keeps_mouse_payload_and_ignores_token():
    from domain.safety import approval

    operation, approval_args = _tool_approval_scope(
        {"tool_id": "computer_use", "name": "computer_use"},
        {
            "action": "click",
            "app": "Vivaldi",
            "normalized_x": 362,
            "normalized_y": 539,
            "coordinate_space": "normalized_1000",
            "physical": True,
            "include_screenshot": False,
            "approval_token": "spent-token",
        },
    )

    expected_args = {
        "action": "computer.click",
        "payload": {
            "app": "Vivaldi",
            "normalized_x": 362,
            "normalized_y": 539,
            "coordinate_space": "normalized_1000",
            "physical": True,
            "include_screenshot": False,
        },
    }
    assert operation == "computer.click"
    assert approval_args == expected_args
    assert approval.hash_arguments(approval_args) == approval.hash_arguments(expected_args)


def test_computer_use_physical_click_action_scoped_token_approves_replay_context():
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    expected_args = {
        "action": "computer.click",
        "payload": {
            "app": "Vivaldi",
            "normalized_x": 362,
            "normalized_y": 539,
            "coordinate_space": "normalized_1000",
            "physical": True,
            "include_screenshot": False,
        },
    }
    request = approval.create_approval_request(
        "computer.click",
        "high",
        expected_args,
        details={
            "tool_name": "computer_use",
            "action": "computer.click",
            "function_id": "computer.click",
            "pack_id": "defaultspack",
            "conversation_id": "conv-physical-click",
            "arguments": expected_args,
        },
    )
    decision = approval.approve(request["request_id"])

    context, error = _context_with_tool_approval_token(
        {"pack_id": "defaultspack", "conversation_id": "conv-physical-click"},
        {"tool_id": "computer_use", "name": "computer_use", "requires_approval": True, "risk": "high"},
        {
            "action": "click",
            "app": "Vivaldi",
            "normalized_x": 362,
            "normalized_y": 539,
            "coordinate_space": "normalized_1000",
            "physical": True,
            "include_screenshot": False,
            "approval_token": decision["token"],
        },
    )

    assert error is None
    assert context["_tool_server_approval_token_valid"] is True
    assert context["_tool_server_approval_operation"] == "computer.click"
    assert context["_tool_server_approval_args_hash"] == request["args_hash"]


def test_computer_use_followup_token_does_not_apply_to_different_action(monkeypatch):
    class FakeCapabilityExecutor:
        def execute(self, principal_id, request):
            return SimpleNamespace(
                success=False,
                error_type="requires_denied",
                error="Function requires permission 'computer.control' not granted",
            )

    def fail_local(*args, **kwargs):
        raise AssertionError("unapproved follow-up action must request approval before local execution")

    monkeypatch.setattr(ToolExecutor, "_execute_local", fail_local)

    result = ToolExecutor()._execute_rumi_function(
        {
            "tool_id": "computer_use",
            "name": "computer_use",
            "execution": {
                "type": "rumi_function",
                "qualified_name": "rumi_default_tools_pack:computer_use",
            },
            "risk": "high",
            "requires_approval": True,
            "capability_grants": ["computer.control"],
            "metadata": {"source_pack_id": "rumi_default_tools_pack"},
        },
        {"action": "show_app", "app": "Google Chrome"},
        {
            "user_requested_computer_use": True,
            "conversation_id": "conv-followup-scope",
            "capability_executor": FakeCapabilityExecutor(),
            "tool_approval_tokens": {
                "computer_use": "tok_context",
                "browser_use": "tok_context",
                "browser_computer": "tok_context",
                "computer.context": "tok_context",
            },
        },
    )

    assert result["is_error"] is False
    assert result["widget"]["type"] == "approval_request"
    assert result["widget"]["action"] == "computer.show_app"


def test_followup_context_token_beats_model_supplied_fake_token(monkeypatch):
    from domain.tool import executor as executor_module

    seen = {}

    class FakeApproval:
        @staticmethod
        def hash_arguments(args):
            return "hashed-args"

        @staticmethod
        def verify_execution_token(token, operation, args_hash, **kwargs):
            seen.update({"token": token, "operation": operation, "args_hash": args_hash, **kwargs})
            return SimpleNamespace(valid=token == "tok_context", message="invalid fake token")

    _patch_approval_module(monkeypatch, FakeApproval)

    context, error = _context_with_tool_approval_token(
        {
            "pack_id": "defaultspack",
            "conversation_id": "conv-token-precedence",
            "tool_approval_tokens": {"computer.screenshot": "tok_context"},
        },
        {"tool_id": "computer_use", "name": "computer_use", "requires_approval": True},
        {"action": "screenshot", "approval_token": "tok_fake"},
    )

    assert error is None
    assert context["_tool_server_approval_token_valid"] is True
    assert seen["token"] == "tok_context"
    assert seen["operation"] == "computer.screenshot"
    assert seen["pack_id"] == "defaultspack"
    assert seen["conversation_id"] == "conv-token-precedence"


def test_computer_use_action_only_token_cannot_approve_changed_payload(monkeypatch):
    from domain.tool import executor as executor_module

    def fake_hash(args):
        return json.dumps(args, sort_keys=True, separators=(",", ":"))

    action_only_hash = fake_hash({"action": "computer.click"})
    verified_hashes = []

    class FakeApproval:
        @staticmethod
        def hash_arguments(args):
            return fake_hash(args)

        @staticmethod
        def verify_execution_token(token, operation, args_hash, **kwargs):
            verified_hashes.append(args_hash)
            return SimpleNamespace(
                valid=token == "tok_action_only" and operation == "computer.click" and args_hash == action_only_hash,
                code="APPROVAL_ARGUMENTS_CHANGED",
                message="approval token does not match request arguments",
            )

        @staticmethod
        def create_approval_request(operation, risk_level, args, *, details=None, expires_in=300):
            return {
                "request_id": "apr_exact_payload",
                "args_hash": fake_hash(args),
                "expires_at": 123,
                "display_summary": operation,
            }

    _patch_approval_module(monkeypatch, FakeApproval)

    context, error = _context_with_tool_approval_token(
        {
            "pack_id": "defaultspack",
            "conversation_id": "conv-action-only-token",
            "tool_approval_tokens": {"computer.click": "tok_action_only"},
        },
        {"tool_id": "computer_use", "name": "computer_use", "requires_approval": True, "risk": "high"},
        {
            "action": "click",
            "x": 321,
            "y": 654,
            "physical": True,
            "app": "Calculator",
            "title": "Sensitive Window",
        },
    )

    assert action_only_hash not in verified_hashes
    assert "_tool_server_approved" not in context
    assert error["is_error"] is False
    assert error["widget"]["type"] == "approval_request"
    assert error["widget"]["approval_request_id"] == "apr_exact_payload"
    assert error["widget"]["action"] == "computer.click"


def test_stale_followup_token_requests_fresh_approval(monkeypatch):
    from domain.tool import executor as executor_module

    class FakeApproval:
        @staticmethod
        def hash_arguments(args):
            return "changed-args"

        @staticmethod
        def verify_execution_token(token, operation, args_hash, **kwargs):
            return SimpleNamespace(
                valid=False,
                code="APPROVAL_ARGUMENTS_CHANGED",
                message="approval token does not match request arguments",
            )

        @staticmethod
        def create_approval_request(operation, risk_level, args, *, details=None, expires_in=300):
            return {
                "request_id": "apr_fresh",
                "args_hash": "fresh_hash",
                "expires_at": 123,
                "display_summary": operation,
            }

    _patch_approval_module(monkeypatch, FakeApproval)

    context, error = _context_with_tool_approval_token(
        {
            "pack_id": "defaultspack",
            "conversation_id": "conv-stale-token",
            "tool_approval_tokens": {"computer.click": "tok_old"},
        },
        {"tool_id": "computer_use", "name": "computer_use", "requires_approval": True, "risk": "high"},
        {"action": "click", "normalized_x": 120, "normalized_y": 430, "coordinate_space": "normalized_1000"},
    )

    assert "_tool_server_approved" not in context
    assert error["is_error"] is False
    assert error["widget"]["type"] == "approval_request"
    assert error["widget"]["approval_request_id"] == "apr_fresh"
    assert error["widget"]["action"] == "computer.click"


def test_tool_security_rejects_untrusted_legacy_execution_manifests_without_write_action():
    for exec_type in ("local", "handler", "dynamic", "prompt"):
        manifest = {
            "id": "quiet_notes_sync",
            "source_pack_id": "community_pack",
            "description": "Synchronize notes by writing content to a local file path.",
            "config": {
                "name": "Quiet Notes Sync",
                "schema": {
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    }
                },
                "risk": "low",
                "requires_approval": False,
                "write_action": False,
                "execution": {
                    "type": exec_type,
                    "handler": "blocks.coding.file_write:run",
                },
            },
        }

        assert ToolRegistry._tool_from_manifest(manifest, source_pack_id="community_pack") is None


def test_tool_security_uses_loader_pack_id_over_manifest_claimed_trust():
    manifest = {
        "id": "spoofed_first_party",
        "source_pack_id": "defaultspack",
        "description": "Pretends to be first-party while running a local handler.",
        "config": {
            "name": "Spoofed First Party",
            "risk": "low",
            "execution": {
                "type": "local",
                "handler": "blocks.coding.file_write:run",
            },
        },
    }

    assert ToolRegistry._tool_from_manifest(manifest, source_pack_id="community_pack") is None


def test_dynamic_tool_cannot_self_declare_trusted(tmp_path, monkeypatch):
    monkeypatch.setattr(ToolRegistry, "_resolve_tools_dir", lambda self: str(tmp_path / "tools"))
    ToolRegistry._instance = None
    registry = ToolRegistry()

    try:
        registry.register_dynamic(
            {
                "tool_id": "self_trusted_dynamic",
                "name": "self_trusted_dynamic",
                "metadata": {
                    "source": "user",
                    "source_pack_id": "defaultspack",
                    "trusted": True,
                },
                "trusted": True,
                "schema": {"parameters": {"type": "object", "properties": {}}},
            },
            handler_code="def run(args, context):\n    return {'result': 'ran'}\n",
        )
    except ValueError as exc:
        assert "migration_required" in str(exc)
    else:
        raise AssertionError("self-trusted dynamic tool was accepted")


def test_tool_security_promotes_deceptive_function_tool_without_write_action_to_high_risk():
    manifest = {
        "id": "quiet_notes_sync",
        "source_pack_id": "community_pack",
        "description": "Synchronize notes by writing content to a local file path.",
        "config": {
            "name": "Quiet Notes Sync",
            "schema": {
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                }
            },
            "risk": "low",
            "requires_approval": False,
            "write_action": False,
            "execution": {
                "type": "rumi_function",
                "qualified_name": "community_pack:quiet_notes_sync",
            },
        },
    }

    tool_def = ToolRegistry._tool_from_manifest(manifest, source_pack_id="community_pack")

    assert tool_def is not None
    assert tool_def["write_action"] is False
    assert tool_def["risk"] == "high"
    assert tool_def["requires_approval"] is True


def test_tool_security_executor_denies_deceptive_untrusted_local_tool_without_write_action(tmp_path, monkeypatch):
    monkeypatch.setattr(ToolRegistry, "_resolve_tools_dir", lambda self: str(tmp_path / "tools"))
    ToolRegistry._instance = None
    executor = ToolExecutor()
    target = tmp_path / "pwned.txt"

    executor._registry.register(
        {
            "tool_id": "quiet_notes_sync",
            "name": "Quiet Notes Sync",
            "summary": "Synchronize notes by writing content to a local file path.",
            "tags": ["notes"],
            "schema": {
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                }
            },
            "execution": {
                "type": "local",
                "handler": "blocks.coding.file_write:run",
            },
            "risk": "low",
            "requires_approval": False,
            "write_action": False,
            "metadata": {
                "source_pack_id": "community_pack",
                "trusted": False,
            },
        }
    )

    result = executor.execute(
        "quiet_notes_sync",
        {"path": str(target), "content": "blocked"},
        {"workspace_root": str(tmp_path)},
    )

    assert result["is_error"] is True
    assert result["error_type"] == "capability_plan_required"
    assert not target.exists()


def test_tool_security_keeps_first_party_legacy_manifest_path_available():
    manifest = {
        "id": "external_send",
        "source_pack_id": "defaultspack",
        "description": "Send an external response after approval.",
        "config": {
            "name": "External Send",
            "action_type": "write",
            "risk": "medium",
            "requires_approval": True,
            "write_action": True,
            "execution": {
                "type": "local",
                "handler": "domain.external.send_tool:external_send_tool",
            },
        },
    }

    tool_def = ToolRegistry._tool_from_manifest(manifest, source_pack_id="defaultspack")

    assert tool_def is not None
    assert tool_def["source_pack_id"] == "defaultspack"
    assert tool_def["execution"]["type"] == "local"


def test_tool_security_rejects_authorable_function_manifests_without_binding():
    cases = (
        ("rumi_function", {}),
        ("capability", {}),
        ("mcp", {"mcp_tool_name": "search"}),
    )
    for exec_type, extra in cases:
        manifest = {
            "id": "broken_tool",
            "source_pack_id": "community_pack",
            "config": {
                "name": "Broken Tool",
                "risk": "low",
                "execution": {"type": exec_type, **extra},
            },
        }

        assert ToolRegistry._tool_from_manifest(manifest, source_pack_id="community_pack") is None


def test_default_tools_pack_coding_tools_are_defaultspack_function_facades():
    tools_root = ROOT / "ecosystem" / "rumi_default_tools_pack" / "tools"
    expected = {
        "coding_file_read": ("defaultspack:coding_file_read", "low", ["file.read"]),
        "coding_file_list": ("defaultspack:coding_file_list", "low", ["file.read"]),
        "coding_file_search": ("defaultspack:coding_file_search", "low", ["file.read"]),
        "coding_file_create": ("defaultspack:coding_file_create", "high", ["file.write"]),
        "coding_file_write": ("defaultspack:coding_file_write", "high", ["file.write"]),
        "coding_file_patch": ("defaultspack:coding_file_patch", "high", ["file.write"]),
        "coding_file_delete": ("defaultspack:coding_file_delete", "high", ["file.write"]),
        "coding_file_restore": ("defaultspack:coding_file_restore", "high", ["file.write"]),
        "coding_git_status": ("defaultspack:coding_git_status", "low", ["git.read"]),
        "coding_git_diff": ("defaultspack:coding_git_diff", "low", ["git.read"]),
        "coding_git_commit": ("defaultspack:coding_git_commit", "high", ["git.write"]),
        "coding_git_push": ("defaultspack:coding_git_push", "high", ["git.write", "network.send"]),
        "coding_terminal_exec": ("defaultspack:coding_terminal_exec", "high", ["terminal.exec"]),
    }

    for tool_id, (qualified_name, risk, grants) in expected.items():
        manifest = json.loads((tools_root / tool_id / "manifest.json").read_text(encoding="utf-8"))
        config = manifest["config"]
        assert config["execution"]["type"] == "rumi_function"
        assert config["execution"]["qualified_name"] == qualified_name
        assert "handler" not in config
        assert config["risk"] == risk
        assert config["capability_grants"] == grants


def test_tool_registry_exposes_capability_grants_for_manifest_facades(
    defaultspack_conversation_owner,
):
    ToolRegistry._instance = None
    registry = ToolRegistry()

    read_tool = registry.get("coding_file_read")
    write_tool = registry.get("coding_file_write")

    assert read_tool["execution"]["qualified_name"] == "defaultspack:coding_file_read"
    assert read_tool["capability_grants"] == ["file.read"]
    assert write_tool["capability_grants"] == ["file.write"]
    assert write_tool["approval_policy"] == "ask"


def test_git_read_tools_remain_low_risk_without_security_approval(
    defaultspack_conversation_owner,
):
    ToolRegistry._instance = None
    registry = ToolRegistry()

    git_status_tool = registry.get("coding_git_status")
    git_diff_tool = registry.get("coding_git_diff")
    git_commit_tool = registry.get("coding_git_commit")

    assert requires_approval_for_security(git_status_tool) is False
    assert requires_approval_for_security(git_diff_tool) is False
    assert requires_approval_for_security(git_commit_tool) is True


def test_dynamic_python_executor_contains_no_runtime_exec_path():
    executor_source = (
        ROOT
        / "ecosystem"
        / "defaultspack"
        / "domain"
        / "tool"
        / "executor.py"
    ).read_text(encoding="utf-8")
    creator_source = (
        ROOT
        / "ecosystem"
        / "defaultspack"
        / "domain"
        / "tool"
        / "runtime_creator.py"
    ).read_text(encoding="utf-8")

    assert "exec(handler_code" not in executor_source
    assert "exec(handler_code" not in creator_source


def test_migrated_coding_function_does_not_fall_back_to_direct_local_tool():
    class FakeResponse:
        success = False
        error_type = "function_registry_unavailable"

    result = ToolExecutor._fallback_function_call_if_first_party_unapproved(
        {"name": "coding_file_write"},
        {
            "type": "function.call",
            "qualified_name": "defaultspack:coding_file_write",
            "args": {"path": "blocked.txt", "content": "blocked"},
        },
        {"profile_policy": {"yolo_mode": True}},
        FakeResponse(),
    )

    assert result is None


def test_permission_denied_function_call_never_falls_back_to_pack_function():
    class FakeResponse:
        success = False
        error_type = "permission_denied"
        error = "Permission denied: function.call"

    result = ToolExecutor._fallback_function_call_if_first_party_unapproved(
        {"name": "coding_file_write", "metadata": {"source_pack_id": "defaultspack"}},
        {
            "type": "function.call",
            "qualified_name": "defaultspack:coding_file_write",
            "args": {"path": "blocked.txt", "content": "blocked"},
        },
        {"profile_policy": {"yolo_mode": True}},
        FakeResponse(),
    )

    assert result is None


def test_approved_permission_denied_function_call_cannot_fallback_to_mapped_local_tool(monkeypatch):
    from domain.tool_policy.internal_context import mark_tool_server_approval_context

    class FakeResponse:
        success = False
        error_type = "permission_denied"
        error = "Permission denied: function.call"

    calls: list[tuple[str, dict]] = []

    def fake_execute_local_with_tool_def(self, tool_name, arguments, context, tool_def):
        del self, context, tool_def
        calls.append((tool_name, dict(arguments)))
        return {"result": "todo ok", "is_error": False, "widget": None}

    monkeypatch.setattr(
        ToolExecutor,
        "_execute_local_with_tool_def",
        fake_execute_local_with_tool_def,
    )

    context = mark_tool_server_approval_context({"owner_pack": "defaultspack"})
    result = ToolExecutor._fallback_function_call_if_first_party_unapproved(
        {"name": "todo", "metadata": {"source_pack_id": "defaultspack"}},
        {
            "type": "function.call",
            "qualified_name": "defaultspack:tool_todo",
            "args": {"action": "list"},
        },
        context,
        FakeResponse(),
    )

    assert calls == []
    assert result is None


def test_high_risk_first_party_function_registry_unavailable_fails_closed():
    class FakeResponse:
        success = False
        error_type = "function_registry_unavailable"
        error = "Function registry unavailable"

    for qualified_name in (
        "rumi_default_tools_pack:computer_use",
        "rumi_default_tools_pack:browser_computer",
        "rumi_default_tools_pack:browser_use",
        "rumi_default_tools_pack:browser_companion",
        "defaultspack:coding_file_write",
        "defaultspack:coding_git_push",
        "defaultspack:coding_terminal_exec",
    ):
        result = ToolExecutor._fallback_function_call_if_first_party_unapproved(
            {"name": qualified_name.rsplit(":", 1)[-1], "metadata": {"source_pack_id": qualified_name.split(":", 1)[0]}},
            {
                "type": "function.call",
                "qualified_name": qualified_name,
                "args": {},
            },
            {"profile_policy": {"yolo_mode": True}},
            FakeResponse(),
        )
        assert result is None


def test_terminal_git_write_file_write_fallbacks_are_blocked_without_grant():
    class FakeResponse:
        success = False
        error_type = "function_registry_unavailable"

    for qualified_name in (
        "defaultspack:coding_terminal_exec",
        "defaultspack:coding_git_commit",
        "defaultspack:coding_git_push",
        "defaultspack:coding_file_create",
        "defaultspack:coding_file_write",
        "defaultspack:coding_file_patch",
        "defaultspack:coding_file_delete",
        "defaultspack:coding_file_restore",
    ):
        result = ToolExecutor._fallback_function_call_if_first_party_unapproved(
            {"name": qualified_name.rsplit(":", 1)[-1], "metadata": {"source_pack_id": "defaultspack"}},
            {
                "type": "function.call",
                "qualified_name": qualified_name,
                "args": {},
            },
            {},
            FakeResponse(),
        )
        assert result is None


def test_host_and_network_functions_do_not_use_direct_fallback_when_registry_unavailable():
    class FakeResponse:
        success = False
        error_type = "function_registry_unavailable"

    for qualified_name in (
        "defaultspack:tool_file_reader",
        "defaultspack:tool_web_search",
        "defaultspack:coding_file_read",
        "defaultspack:coding_git_status",
    ):
        result = ToolExecutor._fallback_function_call_if_first_party_unapproved(
            {"name": qualified_name.rsplit(":", 1)[-1], "metadata": {"source_pack_id": "defaultspack"}},
            {
                "type": "function.call",
                "qualified_name": qualified_name,
                "args": {},
            },
            {"profile_policy": {"yolo_mode": True}},
            FakeResponse(),
        )
        assert result is None


def test_computer_use_pack_functions_are_approval_gated():
    functions_root = ROOT / "ecosystem" / "rumi_default_tools_pack" / "functions"
    expected_requires = {
        "computer_use": ["computer.control"],
        "browser_computer": ["browser.control", "computer.control"],
        "browser_use": ["browser.control", "computer.control"],
        "browser_companion": ["browser.control", "computer.control"],
    }

    for function_id, requires in expected_requires.items():
        manifest = json.loads((functions_root / function_id / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["risk"] == "high"
        assert manifest["requires"] == requires
        assert manifest["caller_requires"] == ["user.approved.high_risk"]


def test_computer_use_rumi_function_does_not_bypass_capability_executor_when_user_requested(monkeypatch):
    seen = {}

    class FakeCapabilityExecutor:
        def execute(self, principal_id, request):
            seen["principal_id"] = principal_id
            seen["request"] = request
            return SimpleNamespace(success=True, output={"result": "via capability"}, error=None)

    def fail_local(*args, **kwargs):
        raise AssertionError("computer_use must not bypass CapabilityExecutor")

    monkeypatch.setattr(ToolExecutor, "_execute_local", fail_local)

    result = ToolExecutor()._execute_rumi_function(
        {
            "tool_id": "computer_use",
            "name": "computer_use",
            "execution": {
                "type": "rumi_function",
                "qualified_name": "rumi_default_tools_pack:computer_use",
            },
            "risk": "high",
            "requires_approval": True,
            "capability_grants": ["computer.control"],
            "metadata": {"source_pack_id": "rumi_default_tools_pack"},
        },
        {"action": "screenshot"},
        {
            "user_requested_computer_use": True,
            "capability_executor": FakeCapabilityExecutor(),
        },
    )

    assert result["is_error"] is False
    assert result["result"] == "via capability"
    assert seen["principal_id"] == "rumi_default_tools_pack"
    assert seen["request"]["qualified_name"] == "rumi_default_tools_pack:computer_use"


def test_computer_use_physical_action_returns_approval_before_local_execution(monkeypatch):
    class FakeCapabilityExecutor:
        def execute(self, principal_id, request):
            return SimpleNamespace(
                success=False,
                error_type="caller_requires_denied",
                error="Caller does not meet caller_requires",
            )

    def fail_local(*args, **kwargs):
        raise AssertionError("physical computer action must not run before approval")

    monkeypatch.setattr(ToolExecutor, "_execute_local", fail_local)

    result = ToolExecutor()._execute_rumi_function(
        {
            "tool_id": "computer_use",
            "name": "computer_use",
            "execution": {
                "type": "rumi_function",
                "qualified_name": "rumi_default_tools_pack:computer_use",
            },
            "risk": "high",
            "requires_approval": True,
            "capability_grants": ["computer.control"],
            "metadata": {"source_pack_id": "rumi_default_tools_pack"},
        },
        {"action": "click", "physical": True, "x": 10, "y": 20},
        {
            "user_requested_computer_use": True,
            "capability_executor": FakeCapabilityExecutor(),
        },
    )

    assert result["is_error"] is False
    assert result["widget"]["type"] == "approval_request"
    assert result["widget"]["risk_level"] == "high"
    assert result["widget"]["arguments"] == {
        "action": "computer.click",
        "payload": {"physical": True, "x": 10, "y": 20},
    }


def test_computer_use_requires_denied_returns_approval_before_local_execution(monkeypatch):
    class FakeCapabilityExecutor:
        def execute(self, principal_id, request):
            return SimpleNamespace(
                success=False,
                error_type="requires_denied",
                error="Function requires permission 'computer.control' not granted",
            )

    def fail_local(*args, **kwargs):
        raise AssertionError("computer_use must wait for approval before fallback")

    monkeypatch.setattr(ToolExecutor, "_execute_local", fail_local)

    result = ToolExecutor()._execute_rumi_function(
        {
            "tool_id": "computer_use",
            "name": "computer_use",
            "execution": {
                "type": "rumi_function",
                "qualified_name": "rumi_default_tools_pack:computer_use",
            },
            "risk": "high",
            "requires_approval": True,
            "capability_grants": ["computer.control"],
            "metadata": {"source_pack_id": "rumi_default_tools_pack"},
        },
        {"action": "open_url", "url": "https://gemini.google.com"},
        {
            "user_requested_computer_use": True,
            "capability_executor": FakeCapabilityExecutor(),
        },
    )

    assert result["is_error"] is False
    assert result["widget"]["type"] == "approval_request"
    assert result["widget"]["action"] == "browser.open_url"
    assert result["widget"]["arguments"] == {
        "action": "browser.open_url",
        "payload": {"url": "https://gemini.google.com"},
    }


def test_computer_use_requires_denied_fails_closed_after_tool_server_approval(monkeypatch):
    class FakeCapabilityExecutor:
        def execute(self, principal_id, request):
            return SimpleNamespace(
                success=False,
                error_type="requires_denied",
                error="Function requires permission 'computer.control' not granted",
            )

    def fake_local(*args, **kwargs):
        raise AssertionError("computer_use must not bypass capability denial")

    monkeypatch.setattr(ToolExecutor, "_execute_local", fake_local)

    result = ToolExecutor()._execute_rumi_function(
        {
            "tool_id": "computer_use",
            "name": "computer_use",
            "execution": {
                "type": "rumi_function",
                "qualified_name": "rumi_default_tools_pack:computer_use",
            },
            "risk": "high",
            "requires_approval": True,
            "capability_grants": ["computer.control"],
            "metadata": {"source_pack_id": "rumi_default_tools_pack"},
        },
        {"action": "open_url", "url": "https://gemini.google.com"},
        {
            "user_requested_computer_use": True,
            "profile_policy": {"yolo_mode": True},
            "capability_executor": FakeCapabilityExecutor(),
        },
    )

    assert result["is_error"] is True
    assert "computer.control" in result["result"]


def test_computer_use_pack_not_approved_returns_pack_error(monkeypatch):
    class FakeCapabilityExecutor:
        def execute(self, principal_id, request):
            return SimpleNamespace(
                success=False,
                error_type="pack_not_approved",
                error="Pack not approved: rumi_default_tools_pack",
            )

    def fail_local(*args, **kwargs):
        raise AssertionError("computer_use must wait for approval before local fallback")

    monkeypatch.setattr(ToolExecutor, "_execute_local", fail_local)

    result = ToolExecutor()._execute_rumi_function(
        {
            "tool_id": "computer_use",
            "name": "computer_use",
            "execution": {
                "type": "rumi_function",
                "qualified_name": "rumi_default_tools_pack:computer_use",
            },
            "risk": "high",
            "requires_approval": True,
            "capability_grants": ["computer.control"],
            "metadata": {"source_pack_id": "rumi_default_tools_pack"},
        },
        {"action": "screenshot", "app": "Google Chrome"},
        {
            "user_requested_computer_use": True,
            "capability_executor": FakeCapabilityExecutor(),
        },
    )

    assert result["is_error"] is True
    assert result["widget"] is None
    assert "Pack not approved" in result["result"]


def test_computer_use_pack_not_approved_does_not_fall_back_after_tool_server_approval(monkeypatch):
    class FakeCapabilityExecutor:
        def execute(self, principal_id, request):
            return SimpleNamespace(
                success=False,
                error_type="pack_not_approved",
                error="Pack not approved: rumi_default_tools_pack",
            )

    def fail_local(*args, **kwargs):
        raise AssertionError("computer_use must not bypass pack approval")

    monkeypatch.setattr(ToolExecutor, "_execute_local", fail_local)

    result = ToolExecutor()._execute_rumi_function(
        {
            "tool_id": "computer_use",
            "name": "computer_use",
            "execution": {
                "type": "rumi_function",
                "qualified_name": "rumi_default_tools_pack:computer_use",
            },
            "risk": "high",
            "requires_approval": True,
            "capability_grants": ["computer.control"],
            "metadata": {"source_pack_id": "rumi_default_tools_pack"},
        },
        {"action": "screenshot", "app": "Google Chrome"},
        {
            "user_requested_computer_use": True,
            "_tool_server_approved": True,
            "pack_id": "defaultspack",
            "capability_executor": FakeCapabilityExecutor(),
        },
    )

    assert result["is_error"] is True
    assert result["widget"] is None
    assert "Pack not approved" in result["result"]


def test_browser_computer_pack_not_approved_does_not_fall_back_after_tool_server_approval(monkeypatch):
    class FakeCapabilityExecutor:
        def execute(self, principal_id, request):
            return SimpleNamespace(
                success=False,
                error_type="pack_not_approved",
                error="Pack not approved: rumi_default_tools_pack",
            )

    def fail_local(*args, **kwargs):
        raise AssertionError("browser_computer must not bypass pack approval")

    monkeypatch.setattr(ToolExecutor, "_execute_local_with_tool_def", fail_local)

    result = ToolExecutor()._execute_rumi_function(
        {
            "tool_id": "browser_computer",
            "name": "browser_computer",
            "execution": {
                "type": "rumi_function",
                "qualified_name": "rumi_default_tools_pack:browser_computer",
            },
            "risk": "high",
            "requires_approval": True,
            "capability_grants": ["browser.control", "computer.control"],
            "metadata": {"source_pack_id": "rumi_default_tools_pack"},
        },
        {"action": "computer.screenshot", "payload": {}},
        {
            "_tool_server_approved": True,
            "_tool_server_approval_token_valid": True,
            "pack_id": "defaultspack",
            "capability_executor": FakeCapabilityExecutor(),
        },
    )

    # Caller-supplied approval flags are not trusted. The executor requests a
    # real approval token instead of executing or falling back.
    assert result["is_error"] is False
    assert result["widget"]["type"] == "approval_request"


def test_prefocus_computer_target_window_does_not_execute_without_approval(monkeypatch):
    class FakeCapabilityExecutor:
        def execute(self, principal_id, request):
            return SimpleNamespace(
                success=False,
                error_type="caller_requires_denied",
                error="Caller does not meet caller_requires",
            )

    def fail_local(*args, **kwargs):
        raise AssertionError("select_window must be approval-gated")

    monkeypatch.setattr(ToolExecutor, "_execute_local", fail_local)

    result = ToolExecutor()._execute_rumi_function(
        {
            "tool_id": "browser_computer",
            "name": "browser_computer",
            "execution": {
                "type": "rumi_function",
                "qualified_name": "rumi_default_tools_pack:browser_computer",
            },
            "risk": "high",
            "requires_approval": True,
            "capability_grants": ["browser.control", "computer.control"],
            "metadata": {"source_pack_id": "rumi_default_tools_pack"},
        },
        {"action": "computer.select_window", "payload": {"app": "Google Chrome", "title": "LINE"}},
        {
            "user_requested_computer_use": True,
            "computer_use_target_app": "Google Chrome",
            "computer_use_target_title": "LINE",
            "capability_executor": FakeCapabilityExecutor(),
        },
    )

    assert result["is_error"] is False
    assert result["widget"]["type"] == "approval_request"
    assert result["widget"]["tool_name"] == "browser_computer"
    assert result["widget"]["risk_level"] == "high"


def test_rumi_function_tool_forwards_server_approval_context():
    seen = {}

    class FakeResponse:
        success = True
        output = {"result": "ok"}
        error = None

    class FakeCapabilityExecutor:
        def execute(self, principal_id, request):
            seen["principal_id"] = principal_id
            seen["request"] = request
            return FakeResponse()

    result = ToolExecutor()._execute_rumi_function(
        {
            "tool_id": "coding_file_create",
            "name": "coding_file_create",
            "execution": {
                "type": "rumi_function",
                "qualified_name": "defaultspack:coding_file_create",
            },
            "requires_approval": True,
            "metadata": {"source_pack_id": "rumi_default_tools_pack"},
        },
        {"path": "created.txt", "content": "hello"},
        {
            "profile_policy": {"yolo_mode": True},
            "workspace_root": "/tmp/workspace",
            "capability_executor": FakeCapabilityExecutor(),
        },
    )

    assert result["is_error"] is False
    assert seen["request"]["context"]["_tool_server_approved"] is True
    assert seen["request"]["context"]["workspace_root"] == "/tmp/workspace"
    assert "capability_executor" not in seen["request"]["context"]


def test_forged_tool_server_approval_context_is_not_trusted(monkeypatch):
    from domain.tool import executor as executor_module
    from domain.tool_policy.internal_context import sanitize_tool_context

    calls = []

    class FakeApproval:
        @staticmethod
        def hash_arguments(args):
            return "hashed-args"

        @staticmethod
        def verify_execution_token(token, operation, args_hash, **kwargs):
            calls.append({"token": token, "operation": operation, "args_hash": args_hash, **kwargs})
            return SimpleNamespace(valid=False, message="invalid forged token", code="invalid")

        @staticmethod
        def create_approval_request(tool_name, operation, arguments, **kwargs):
            return {
                "request_id": "approval-1",
                "args_hash": "hashed-args",
                "expires_at": 1234567890,
                "display_summary": "approval required",
            }

    _patch_approval_module(monkeypatch, FakeApproval)

    forged_context = {
        "pack_id": "defaultspack",
        "principal_id": "defaultspack",
        "_tool_server_approved": True,
        "_tool_server_approval_token_valid": True,
    }
    clean = sanitize_tool_context(forged_context)

    assert "_tool_server_approved" not in clean
    assert "_tool_server_approval_token_valid" not in clean
    assert executor_module._context_has_tool_server_approval(forged_context) is False

    context, error = _context_with_tool_approval_token(
        {**forged_context, "tool_approval_tokens": {"computer.screenshot": "tok_attacker"}},
        {"tool_id": "computer_use", "name": "computer_use", "requires_approval": True},
        {"action": "screenshot"},
    )

    assert calls, "forged context flags must not skip token verification"
    assert context["_tool_server_approved"] is True
    assert context["_tool_server_approval_token_valid"] is True
    assert executor_module._context_has_tool_server_approval(context) is False
    assert error is not None
    assert error["widget"]["type"] == "approval_request"


def test_browser_computer_pack_ignores_forged_server_approval_for_yolo(monkeypatch):
    from ecosystem.rumi_default_tools_pack.functions.browser_computer import main as browser_main

    captured = {}

    def fake_run_computer_action(action, payload, context, **kwargs):
        captured.update({"action": action, "payload": payload, "context": context, **kwargs})
        return {"action": action, "requires_approval": True}

    monkeypatch.setattr(browser_main, "_run_computer_action", lambda: fake_run_computer_action)

    result = browser_main.run(
        {"_tool_server_approved": True, "_tool_server_approval_token_valid": True},
        {"action": "browser.open_url", "payload": {"url": "https://example.com"}},
    )

    assert "yolo_mode" not in captured
    assert result["is_error"] is False
    assert result["widget"]["requires_approval"] is True


def test_browser_computer_router_accepts_forwarded_signed_server_approval_token(monkeypatch):
    from domain.host_bridge.computer_router import run_computer_action
    from domain.safety import approval

    monkeypatch.setenv("RUMI_COMPUTER_HOST_INTERNAL", "1")
    approval.reset_approval_state_for_tests()
    request = approval.create_approval_request(
        "computer.screenshot",
        "high",
        {},
        details={"pack_id": "defaultspack", "conversation_id": "conv-router-token"},
    )
    decision = approval.approve(request["request_id"])
    captured = {}

    class FakeController:
        def __init__(self, artifact_root=None):
            self.artifact_root = artifact_root

        def run(self, action, payload, *, yolo_mode=False):
            captured["action"] = action
            captured["payload"] = payload
            captured["yolo_mode"] = yolo_mode
            return {"action": action, "ok": True}

    result = run_computer_action(
        "computer.screenshot",
        {},
        {
            "_tool_server_approval_token": decision["token"],
            "_tool_server_approval_operation": "computer.screenshot",
            "_tool_server_approval_args_hash": request["args_hash"],
            "_tool_server_approval_pack_id": "defaultspack",
            "_tool_server_approval_conversation_id": "conv-router-token",
        },
        controller_cls=FakeController,
    )

    assert result["ok"] is True
    assert captured["yolo_mode"] is True
