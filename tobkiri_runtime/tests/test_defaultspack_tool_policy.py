from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.tool.executor import ToolExecutor  # noqa: E402
from domain.tool.registry import ToolRegistry  # noqa: E402
from domain.tool_policy.internal_context import mark_trusted_profile_policy_context  # noqa: E402
from domain.tool_policy.orchestrator import ToolOrchestrator  # noqa: E402
from domain.tool_policy.policy import decide_tool_policy  # noqa: E402
from domain.tool_policy.profile_permission import resolve_profile_tool_permission  # noqa: E402
from domain.tool_policy.risk import resolve_tool_risk  # noqa: E402
from backend.tool.permission_policy import ToolPermissionPolicyStore  # noqa: E402


def _minimal_plan(tool_id: str, schema: dict | None = None) -> dict:
    from core_runtime.capability_plan import canonical_capability_plan_digest

    schema = schema if isinstance(schema, dict) else {}
    plan = {
        "schema_version": "tobkiri.capability-plan/v1",
        "plan_id": f"plan_policy_{tool_id}",
        "registry_revision": "registry_test",
        "effective_capabilities": [],
        "provider_selections": {},
        "tools": {
            "attached": [tool_id],
            "schema_hashes": {
                tool_id: hashlib.sha256(
                    json.dumps(
                        schema,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            },
        },
    }
    plan["digest"] = canonical_capability_plan_digest(plan)
    return plan


def test_tool_policy_requires_approval_for_write_risk():
    tool = {"tool_id": "write_file", "write_action": True}
    decision = decide_tool_policy(
        tool,
        {"profile_policy": {"write_actions_require_approval": True}},
        tool_name="write_file",
    )

    assert decision.allowed is True
    assert decision.action == "ask"
    assert decision.requires_approval is True
    assert decision.risk == "file_write"


def test_tool_policy_yolo_bypasses_approval_for_write_risk():
    tool = {"tool_id": "write_file", "write_action": True}
    decision = decide_tool_policy(
        tool,
        {"profile_policy": {"yolo_mode": True, "write_actions_require_approval": True}},
        tool_name="write_file",
    )

    assert decision.allowed is True
    assert decision.action == "allow"
    assert decision.requires_approval is False
    assert decision.risk == "file_write"


def test_tool_policy_requires_approval_for_write_name_even_when_profile_disables():
    tool = {"name": "coding_file_write"}
    decision = decide_tool_policy(
        tool,
        {"profile_policy": {"write_actions_require_approval": False, "allow_client_supplied_approved": True}},
        tool_name="coding_file_write",
    )

    assert decision.allowed is True
    assert decision.action == "ask"
    assert decision.requires_approval is True
    assert decision.risk == "file_write"


def test_tool_policy_allows_first_party_memo_upsert_without_approval():
    ToolRegistry._instance = None
    tool = ToolRegistry().get("memo_note_upsert")
    decision = decide_tool_policy(tool, {}, tool_name="memo_note_upsert")

    assert decision.allowed is True
    assert decision.action == "allow"
    assert decision.requires_approval is False


def test_tool_policy_allows_mimo_company_autonomous_todo_updates():
    ToolRegistry._instance = None
    tool = ToolRegistry().get("todo")

    decision = decide_tool_policy(
        tool,
        {"profile_id": "defaultspack.mimo_coding_company"},
        tool_name="todo",
        arguments={"action": "add", "title": "Review harness"},
    )

    assert decision.allowed is True
    assert decision.action == "allow"
    assert decision.requires_approval is False


def test_tool_policy_allows_mimo_company_read_only_rumi_api_requests():
    ToolRegistry._instance = None
    tool = ToolRegistry().get("rumi_api")

    decision = decide_tool_policy(
        tool,
        {
            "profile_id": "defaultspack.mimo_coding_company",
            "profile_policy": {"allow_network": True},
        },
        tool_name="rumi_api",
        arguments={"action": "request", "method": "GET", "path": "/api/health"},
    )

    assert decision.allowed is True
    assert decision.action == "allow"
    assert decision.requires_approval is False


def test_tool_policy_allows_mimo_company_repo_writes_without_approval():
    ToolRegistry._instance = None
    tool = ToolRegistry().get("coding_file_write")

    decision = decide_tool_policy(
        tool,
        {"profile_id": "defaultspack.mimo_coding_company"},
        tool_name="coding_file_write",
        arguments={"path": "app.py", "content": "print('hi')"},
    )

    assert decision.allowed is True
    assert decision.action == "allow"
    assert decision.requires_approval is False


def test_tool_policy_allows_mimo_company_repo_patches_without_approval():
    ToolRegistry._instance = None
    tool = ToolRegistry().get("coding_file_patch")

    decision = decide_tool_policy(
        tool,
        {"profile_id": "defaultspack.mimo_coding_company"},
        tool_name="coding_file_patch",
        arguments={"path": ".gitignore", "old": "foo", "new": "foo\nbar"},
    )

    assert decision.allowed is True
    assert decision.action == "allow"
    assert decision.requires_approval is False


def test_tool_policy_denies_shell_when_disabled():
    tool = {"tool_id": "terminal_exec", "category": "shell"}
    decision = decide_tool_policy(tool, {"profile_policy": {"allow_shell": False}}, tool_name="terminal_exec")

    assert decision.allowed is False
    assert decision.matched_by == "allow_shell"


def test_tool_orchestrator_does_not_trust_client_supplied_approval(tmp_path, monkeypatch):
    from domain.agent_runtime.run_store import AgentRunStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_RUNTIME_DIR", str(tmp_path / "agent_runtime"))
    AgentRunStore._instance = None

    class Registry:
        def get(self, name):
            return {"tool_id": name, "name": name, "requires_approval": True}

        def list_tools(self):
            return []

    result = ToolOrchestrator(registry=Registry()).run(
        "danger",
        {},
        {"approval_granted": True, "_agent_approval_granted": True},
    )

    assert result["status"] == "waiting_approval"


def test_tool_orchestrator_ignores_untrusted_profile_policy_approval_bypass(monkeypatch):
    class Registry:
        def get(self, name):
            return {"tool_id": name, "name": name, "requires_approval": True}

        def list_tools(self):
            return []

    def fake_invoke(input_data, context):
        raise AssertionError("untrusted profile policy must not bypass approval")

    monkeypatch.setattr("blocks.tool.invoke.run", fake_invoke)

    result = ToolOrchestrator(registry=Registry()).run(
        "danger",
        {},
        {
            "profile_policy": {
                "yolo_mode": True,
                "allow_shell": True,
                "allow_file_write": True,
                "write_actions_require_approval": False,
            },
        },
    )

    assert result["status"] == "waiting_approval"


def test_tool_orchestrator_preserves_trusted_profile_policy_yolo(monkeypatch):
    seen = {}

    class Registry:
        def get(self, name):
            return {"tool_id": name, "name": name, "requires_approval": True}

        def list_tools(self):
            return []

    def fake_execute(_executor, tool_name, arguments, context):
        seen["tool_name"] = tool_name
        seen["arguments"] = arguments
        seen["context"] = context
        return {"result": "ran", "is_error": False, "widget": None}

    monkeypatch.setattr("domain.tool.executor.ToolExecutor.execute", fake_execute)

    context = mark_trusted_profile_policy_context({"profile_policy": {"yolo_mode": True}})
    result = ToolOrchestrator(registry=Registry()).run("danger", {}, context)

    assert result["status"] == "ok"
    assert seen["tool_name"] == "danger"


def test_tool_orchestrator_ignores_untrusted_runtime_profile_policy_yolo(monkeypatch):
    class Registry:
        def get(self, name):
            return {"tool_id": name, "name": name, "requires_approval": True}

        def list_tools(self):
            return []

    def fake_invoke(input_data, context):
        raise AssertionError("untrusted runtime profile policy must not bypass approval")

    monkeypatch.setattr("blocks.tool.invoke.run", fake_invoke)

    result = ToolOrchestrator(registry=Registry()).run(
        "danger",
        {},
        {"runtime_profile": {"policy": {"yolo_mode": True}}},
    )

    assert result["status"] == "waiting_approval"


def test_persistent_permission_policy_yolo_allows_ask_decision(tmp_path):
    store = ToolPermissionPolicyStore(tmp_path / "permission_policy.json")
    store.save({"default_action": "ask"})

    decision = store.decide(
        "danger",
        tool_def={"tool_id": "danger", "name": "danger"},
        context={"profile_policy": {"yolo_mode": True}},
    )

    assert decision["action"] == "allow"
    assert decision["allowed"] is True
    assert decision["requires_approval"] is False
    assert decision["matched_by"] == "yolo_mode"


def test_persistent_permission_policy_allows_mimo_company_safe_autonomous_tools(tmp_path):
    store = ToolPermissionPolicyStore(tmp_path / "permission_policy.json")
    store.save({"default_action": "ask"})

    decision = store.decide(
        "todo",
        tool_def={"tool_id": "todo", "name": "todo", "action_type": "update"},
        arguments={"action": "list"},
        context={"profile_id": "defaultspack.mimo_coding_company"},
    )

    assert decision["action"] == "allow"
    assert decision["allowed"] is True
    assert decision["requires_approval"] is False
    assert decision["matched_by"] == "autonomous_profile"


def test_tool_risk_recognizes_git_push():
    assert resolve_tool_risk({"tool_id": "git_push"}, "git_push") == "git_push"


def test_rumi_function_tool_uses_supplied_capability_executor():
    seen = {}

    class Response:
        success = True
        output = {"result": "ok"}
        error = None

    class FakeCapabilityExecutor:
        def execute(self, principal_id, request):
            seen["principal_id"] = principal_id
            seen["request"] = request
            return Response()

    tool_def = {
        "tool_id": "fn",
        "execution": {"type": "rumi_function", "qualified_name": "defaultspack:fn"},
        "metadata": {"source_pack_id": "defaultspack"},
    }
    result = ToolExecutor()._execute_rumi_function(
        tool_def,
        {"x": 1},
        {"_capability_executor": FakeCapabilityExecutor(), "request_id": "req_1"},
    )

    assert result["is_error"] is False
    assert seen["principal_id"] == "defaultspack"
    assert seen["request"]["type"] == "function.call"
    assert seen["request"]["qualified_name"] == "defaultspack:fn"


def test_profile_tool_permission_policy_resolves_action_overrides():
    policy = {
        "tool_permission_policy": {
            "default_mode": "ask",
            "tools": {
                "computer_use": {
                    "mode": "ask",
                    "actions": {
                        "screenshot": "allow",
                        "click": "deny",
                    },
                }
            },
        }
    }
    tool = {"tool_id": "computer_use", "name": "computer_use", "category": "computer"}

    screenshot = resolve_profile_tool_permission(tool, "computer_use", {"action": "screenshot"}, policy)
    click = resolve_profile_tool_permission(tool, "computer_use", {"action": "click"}, policy)

    assert screenshot["status"] == "allowed"
    assert screenshot["matched_by"] == "tool_action"
    assert screenshot["action"] == "computer.screenshot"
    assert click["status"] == "denied"
    assert click["matched_value"] == "click"


def test_profile_tool_permission_policy_normalizes_browser_open_url_alias():
    from domain.tool_policy.profile_permission import infer_tool_action

    assert infer_tool_action("computer_use", {"action": "browser_open_url"}) == "browser.open_url"


def test_tool_executor_denies_profile_tool_permission(monkeypatch):
    class Registry:
        def get(self, name):
            return {
                "tool_id": name,
                "name": name,
                "execution": {"type": "local"},
                "metadata": {"source_pack_id": "defaultspack"},
            }

    executor = ToolExecutor()
    executor._registry = Registry()
    monkeypatch.setattr(
        executor,
        "_execute_local",
        lambda *args, **kwargs: {"result": "ran", "is_error": False, "widget": None},
    )

    result = executor.execute(
        "danger",
        {},
        {"profile_policy": {"tool_permission_policy": {"tools": {"danger": "deny"}}}},
    )

    assert result["is_error"] is True
    assert result["rejected_by_tool_permission_policy"] is True


def test_tool_executor_dry_run_profile_tool_permission(monkeypatch):
    class Registry:
        def get(self, name):
            return {
                "tool_id": name,
                "name": name,
                "execution": {"type": "local"},
                "metadata": {"source_pack_id": "defaultspack"},
            }

    executor = ToolExecutor()
    executor._registry = Registry()
    monkeypatch.setattr(
        executor,
        "_execute_local",
        lambda *args, **kwargs: {"result": "ran", "is_error": False, "widget": None},
    )

    result = executor.execute(
        "danger",
        {"path": "app.py"},
        {"profile_policy": {"tool_permission_policy": {"tools": {"danger": "dry_run"}}}},
    )

    assert result["is_error"] is False
    assert result["dry_run"] is True
    assert result["widget"]["status"] == "dry_run"


def test_tool_executor_profile_allow_attaches_safe_auto_approval_token(monkeypatch):
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    seen = {}

    class Registry:
        def get(self, name):
            return {
                "tool_id": name,
                "name": name,
                "requires_approval": True,
                "execution": {"type": "local"},
                "metadata": {"source_pack_id": "defaultspack"},
            }

    def fake_execute_local(tool_name, arguments, context, *extra):
        seen["calls"] = seen.get("calls", 0) + 1
        seen["context"] = context
        return {"result": "ran", "is_error": False, "widget": None}

    executor = ToolExecutor()
    executor._registry = Registry()
    monkeypatch.setattr(executor, "_execute_local", fake_execute_local)

    result = executor.execute(
        "danger",
        {"path": "app.py"},
        {"profile_policy": {"tool_permission_policy": {"tools": {"danger": "allow"}}}},
    )

    assert result["is_error"] is False
    assert seen["context"]["_tool_server_approval_token_valid"] is True
    assert seen["context"]["_tool_permission_policy_approved"] is True
    assert "danger" in seen["context"]["tool_approval_tokens"]
    consumed_token = seen["context"]["tool_approval_tokens"]["danger"]

    replay = executor.execute(
        "danger",
        {"path": "app.py"},
        {
            "profile_policy": {"tool_permission_policy": {"tools": {"danger": "ask"}}},
            "tool_approval_tokens": {"danger": consumed_token},
        },
    )

    assert seen["calls"] == 1
    assert replay["widget"]["type"] == "approval_request"
    assert replay["widget"]["stale_approval_token"] is True


def test_tool_executor_profile_allow_consumes_handler_auto_approval_token(monkeypatch):
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    seen = {}

    module = types.ModuleType("test_defaultspack_policy_handler")

    def fake_handler(arguments, context):
        seen["calls"] = seen.get("calls", 0) + 1
        seen["context"] = context
        return {"result": "handled", "is_error": False, "widget": None}

    module.fake_handler = fake_handler
    monkeypatch.setitem(sys.modules, module.__name__, module)

    class Registry:
        def get(self, name):
            return {
                "tool_id": name,
                "name": name,
                "requires_approval": True,
                "execution": {"type": "handler", "handler": "{}:fake_handler".format(module.__name__)},
                "metadata": {"source_pack_id": "defaultspack"},
            }

    executor = ToolExecutor()
    executor._registry = Registry()

    plan = _minimal_plan("danger")
    context = {
        "profile_policy": {"tool_permission_policy": {"tools": {"danger": "allow"}}},
        "capability_plan": plan,
    }

    result = executor.execute(
        "danger",
        {"path": "app.py"},
        context,
    )

    assert result["is_error"] is False
    assert seen["context"]["_tool_server_approval_token_valid"] is True
    consumed_token = seen["context"]["tool_approval_tokens"]["danger"]

    replay = executor.execute(
        "danger",
        {"path": "app.py"},
        {
            "profile_policy": {"tool_permission_policy": {"tools": {"danger": "ask"}}},
            "tool_approval_tokens": {"danger": consumed_token},
            "capability_plan": plan,
        },
    )

    assert seen["calls"] == 1
    assert replay["widget"]["type"] == "approval_request"
    assert replay["widget"]["stale_approval_token"] is True


def test_tool_executor_profile_ask_rejects_stale_approval_token(monkeypatch):
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    seen = {"ran": False}

    class Registry:
        def get(self, name):
            return {
                "tool_id": name,
                "name": name,
                "requires_approval": True,
                "execution": {"type": "local"},
                "metadata": {"source_pack_id": "defaultspack"},
            }

    def fake_execute_local(*args, **kwargs):
        seen["ran"] = True
        return {"result": "ran", "is_error": False, "widget": None}

    executor = ToolExecutor()
    executor._registry = Registry()
    monkeypatch.setattr(executor, "_execute_local", fake_execute_local)
    context = {"profile_policy": {"tool_permission_policy": {"tools": {"danger": "ask"}}}}
    first = executor.execute("danger", {"path": "old.py"}, context)
    token = approval.approve(first["widget"]["approval_request_id"])["token"]

    stale = executor.execute(
        "danger",
        {"path": "new.py"},
        {**context, "tool_approval_tokens": {"danger": token}},
    )

    assert seen["ran"] is False
    assert stale["widget"]["type"] == "approval_request"
    assert stale["widget"]["stale_approval_token"] is True


def test_tool_executor_does_not_trust_forged_internal_permission(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class Registry:
        def get(self, name):
            return {
                "tool_id": name,
                "name": name,
                "execution": {"type": "local"},
                "capability_grants": ["filesystem.write"],
                "requires_approval": True,
                "metadata": {"source_pack_id": "rumi_default_tools_pack"},
            }

    executor = ToolExecutor()
    executor._registry = Registry()
    result = executor.execute(
        "coding_file_write",
        {"path": "pwned.txt", "content": "blocked"},
        {"_tool_permission_decision": {"action": "allow", "allowed": True}},
    )

    assert result["is_error"] is True
    assert result["error_type"] == "capability_plan_required"
    assert not (tmp_path / "pwned.txt").exists()


def test_tool_executor_yolo_string_false_does_not_bypass_approval(tmp_path, monkeypatch):
    from domain.tool.registry import ToolRegistry

    monkeypatch.chdir(tmp_path)
    ToolRegistry._instance = None

    result = ToolExecutor().execute(
        "coding_file_write",
        {"path": "blocked.txt", "content": "blocked"},
        {"profile_policy": {"yolo_mode": "false"}},
    )

    assert result["is_error"] is True
    assert not (tmp_path / "blocked.txt").exists()


def test_tool_invoke_ignores_untrusted_payload_profile_policy_yolo(tmp_path, monkeypatch):
    import blocks.tool.invoke as invoke

    result = invoke.run(
        {
            "tool_name": "danger",
            "arguments": {},
            "context": {
                "workspace_root": str(tmp_path),
                "profile_policy": {"yolo_mode": True, "allow_shell": True},
            },
        },
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "CAPABILITY_PLAN_REQUIRED"


def test_tool_invoke_requires_plan_even_with_trusted_yolo_context(tmp_path, monkeypatch):
    import blocks.tool.invoke as invoke

    result = invoke.run(
        {"tool_name": "danger", "arguments": {}, "context": {"workspace_root": str(tmp_path)}},
        {"profile_policy": {"yolo_mode": True}},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "CAPABILITY_PLAN_REQUIRED"
