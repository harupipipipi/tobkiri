from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from blocks.agent.run_subagent import run as run_subagent_block  # noqa: E402
from domain.chat.store import ChatStore  # noqa: E402
from domain.function_runtime.dispatcher import run_defaultspack_function  # noqa: E402
from domain.tool.executor import ToolExecutor  # noqa: E402
from ecosystem.rumi_default_tools_pack.domain.tool.subagent import SubagentController  # noqa: E402


def _configure_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_STORE_PATH", str(tmp_path / "integrations" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_LOCKS_DIR", str(tmp_path / "integrations" / "event_locks"))
    ChatStore._instance = None


def _parent_conversation() -> dict:
    ChatStore._instance = None
    return ChatStore().create_conversation(model="stub/default")


def test_legacy_run_subagent_wrapper_uses_dispatcher_or_delegate(monkeypatch):
    seen: dict[str, object] = {}

    def fake_call_model(*args, **kwargs):
        seen["called"] = True
        return {"status": "ok", "output": {"recommended_tools": [{"tool_id": "search_docs"}]}}

    monkeypatch.setattr("domain.agent.subagent_orchestrator.call_model", fake_call_model)

    result = run_subagent_block({"role_id": "tool_selector", "payload": {"candidate_tools": [{"tool_id": "search_docs"}]}}, {"call_handler": object()})

    assert result["status"] == "ok"
    assert seen["called"] is True


def test_agent_run_subagent_compat_routes_to_delegate_or_utility(monkeypatch):
    monkeypatch.setattr(
        "domain.agent.subagent_orchestrator.call_model",
        lambda *args, **kwargs: {"status": "ok", "output": {"recommended_tools": [{"tool_id": "search_docs"}]}},
    )

    result = run_defaultspack_function(
        "agent_run_subagent",
        {"role_id": "tool_selector", "payload": {"candidate_tools": [{"tool_id": "search_docs"}]}},
        {"call_handler": object()},
    )

    assert result["status"] == "ok"
    assert result["data"]["output"]["recommended_tools"][0]["tool_id"] == "search_docs"


def test_agent_run_subagent_compat_task_payload_routes_through_agent_delegate(monkeypatch):
    seen: dict[str, object] = {}

    def fake_dispatch(envelope, context):
        seen["action_id"] = envelope.delivery.get("action_id")
        seen["input"] = envelope.input
        seen["conversation_id"] = envelope.target.get("conversation_id")
        return {"status": "ok", "delegate": {"execution_id": "run_1"}, "result": {"status": "queued"}}

    monkeypatch.setattr("domain.input.dispatcher.dispatch_input", fake_dispatch)

    result = run_subagent_block(
        {"role_id": "delegate", "payload": {"task": "delegate this", "tools": ["browser"]}},
        {"conversation_id": "conv_1"},
    )

    assert result["status"] == "ok"
    assert seen["action_id"] == "agent.delegate"
    assert seen["input"] == "delegate this"
    assert seen["conversation_id"] == "conv_1"


def test_tool_subagent_compat_returns_structured_result(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    parent = _parent_conversation()
    monkeypatch.setattr(
        "blocks.chat.send.run",
        lambda request, context: {
            "status": "ok",
            "data": {"id": "assistant-1", "content": [{"type": "text", "text": "done"}]},
        },
    )

    result = run_defaultspack_function(
        "tool_subagent",
        {"task": "hello from child"},
        {"conversation_id": parent["id"], "model": "stub/default"},
    )

    assert result["status"] == "ok"
    assert result["data"]["widget"]["type"] == "subagent"


def test_rumi_default_tools_subagent_compat_uses_dispatcher(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    parent = ChatStore().create_conversation(
        model="stub/default",
        system_prompt_id="mimo_coding_company",
        group_id="company:mimo-coding-company",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )
    seen: dict[str, object] = {}

    def fake_dispatch(envelope, context):
        seen["called"] = envelope.target["conversation_id"]
        seen["input"] = envelope.input
        seen["tools"] = envelope.tools
        seen["params"] = envelope.params
        seen["metadata"] = envelope.metadata
        return {"status": "ok", "assistant_text": "done"}

    monkeypatch.setattr("ecosystem.rumi_default_tools_pack.domain.tool.subagent.dispatch_input", fake_dispatch)

    result = SubagentController().run(
        {"task": "delegate this"},
        {
            "conversation_id": parent["id"],
            "model": "stub/default",
            "profile_id": "defaultspack.mimo_coding_company",
            "profile_policy": {"profile_id": "defaultspack.mimo_coding_company"},
            "capability_graph": {"connected_tools": ["todo", "coding_file_search"]},
        },
    )

    assert result["summary"] == "done"
    assert seen["called"]
    assert seen["input"].startswith("Use the connected tools directly.")
    assert seen["tools"] == ["todo", "coding_file_search"]
    assert seen["params"]["tool_policy"]["profile_id"] == "defaultspack.mimo_coding_company"
    assert seen["metadata"]["profile_id"] == "defaultspack.mimo_coding_company"
    child = ChatStore().get_conversation(result["child_conversation_id"])
    assert child["system_prompt_id"] == "mimo_coding_company"
    assert child["group_id"] == "company:mimo-coding-company"
    assert child["metadata"]["profile_id"] == "defaultspack.mimo_coding_company"
    assert child["metadata"]["company_id"] == "mimo-coding-company"


def test_subagent_mentions_resolve_mimo_target_persona(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    parent = ChatStore().create_conversation(
        model="stub/default",
        metadata={"profile_id": "defaultspack.mimo_coding_company"},
    )

    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.subagent.dispatch_input",
        lambda _envelope, _context: {"status": "ok", "assistant_text": "coded"},
    )

    result = SubagentController().run(
        {"task": "@coding_engineer patch the focused failing test"},
        {"conversation_id": parent["id"], "model": "stub/default"},
    )

    assert result["summary"] == "coded"
    assert result["target_agent_id"] == "coding_engineer"
    child = ChatStore().get_conversation(result["child_conversation_id"])
    assert child["agent_id"] == "coding_engineer"
    assert child["metadata"]["subagent"]["target"]["source"] == "mention"


def test_subagent_unknown_target_returns_structured_error(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    parent = _parent_conversation()

    result = ToolExecutor()._execute_local(
        "subagent",
        {"task": "@ghost_worker take this"},
        {"conversation_id": parent["id"], "model": "stub/default"},
    )

    assert result["is_error"] is True
    assert result["delegation_error"]["category"] == "target"
    assert result["delegation_error"]["code"] == "SUBAGENT_TARGET_UNKNOWN"


def test_subagent_route_failure_returns_structured_error(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    parent = _parent_conversation()

    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.subagent.dispatch_input",
        lambda _envelope, _context: {
            "status": "error",
            "code": "UNKNOWN_INPUT_ACTION",
            "error": "unknown input action",
        },
    )

    with pytest.raises(Exception) as raised:
        SubagentController().run({"task": "delegate this"}, {"conversation_id": parent["id"], "model": "stub/default"})

    error = raised.value
    assert getattr(error, "category") == "route"
    assert getattr(error, "code") == "SUBAGENT_UNKNOWN_INPUT_ACTION"
    assert error.to_error()["details"]["route"] == "chat.message"


def test_subagent_timeout_failure_returns_structured_tool_error(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    parent = _parent_conversation()

    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.subagent.dispatch_input",
        lambda _envelope, _context: {
            "status": "error",
            "code": "TIMEOUT",
            "error": "child route timed out",
        },
    )

    result = ToolExecutor()._execute_local(
        "subagent",
        {"task": "delegate this"},
        {"conversation_id": parent["id"], "model": "stub/default"},
    )

    assert result["is_error"] is True
    assert result["delegation_error"]["category"] == "timeout"
    assert result["widget"]["delegation_error"]["actionable_hint"]


def test_subagent_function_response_preserves_structured_error():
    class Response:
        success = True
        error = None
        output = {
            "result": "child route timed out",
            "is_error": True,
            "widget": {
                "type": "subagent",
                "delegation_error": {
                    "type": "subagent_delegation_error",
                    "category": "timeout",
                    "code": "SUBAGENT_TIMEOUT",
                    "message": "child route timed out",
                    "details": {"route": "function.call"},
                },
            },
        }

    result = ToolExecutor._tool_response_from_capability(Response(), {"name": "subagent"})

    assert result["is_error"] is True
    assert result["delegation_error"]["category"] == "timeout"


def test_agent_run_subagent_delegate_queue_status_is_structured(monkeypatch):
    def fake_dispatch(envelope, context):
        return {
            "status": "ok",
            "delegate": {"execution_id": "agent_queued", "status": "queued"},
            "result": {"status": "queued"},
        }

    monkeypatch.setattr("domain.input.dispatcher.dispatch_input", fake_dispatch)

    result = run_subagent_block(
        {"role_id": "delegate", "payload": {"task": "delegate this", "agent_id": "coding_engineer"}},
        {"conversation_id": "conv_1"},
    )

    assert result["status"] == "ok"
    assert result["data"]["delegation_status"]["category"] == "queue"
    assert result["data"]["delegation_status"]["execution_id"] == "agent_queued"


def test_agent_run_subagent_delegate_policy_error_is_structured(monkeypatch):
    def fake_dispatch(envelope, context):
        return {
            "status": "error",
            "code": "PERMISSION_DENIED",
            "error": "agent delegation denied by policy",
        }

    monkeypatch.setattr("domain.input.dispatcher.dispatch_input", fake_dispatch)

    result = run_subagent_block(
        {"role_id": "delegate", "payload": {"task": "delegate this", "agent_id": "coding_engineer"}},
        {"conversation_id": "conv_1"},
    )

    assert result["status"] == "error"
    details = result["error"]["details"]
    assert details["delegation_error"]["category"] == "policy"
    assert details["delegation_error"]["code"] == "SUBAGENT_PERMISSION_DENIED"


def test_agent_run_subagent_hides_untrusted_delegation_error_fields(monkeypatch):
    def fake_subagent_compat(*args, **kwargs):
        return {
            "status": "error",
            "code": "SUBAGENT_TIMEOUT",
            "error": "backend failed with api_key=not-for-users",
            "raw_response": {"authorization": "Bearer not-for-users"},
            "delegation_error": {
                "category": "timeout",
                "code": "SUBAGENT_TIMEOUT",
                "message": "backend failed with api_key=not-for-users",
                "details": {
                    "route": "agent.delegate",
                    "target_agent_id": "coding_engineer",
                    "authorization": "Bearer not-for-users",
                },
            },
        }

    monkeypatch.setattr(
        "blocks.agent.run_subagent.run_subagent_compat", fake_subagent_compat
    )

    result = run_subagent_block(
        {"role_id": "delegate", "payload": {"task": "delegate this"}}, {}
    )

    rendered = str(result)
    assert result["status"] == "error"
    assert result["error"]["code"] == "SUBAGENT_TIMEOUT"
    assert result["error"]["details"]["delegation_error"] == {
        "type": "subagent_delegation_error",
        "category": "timeout",
        "code": "SUBAGENT_TIMEOUT",
        "details": {
            "route": "agent.delegate",
            "target_agent_id": "coding_engineer",
        },
        "actionable_hint": (
            "Increase the subagent timeout or reduce the delegated task scope; "
            "the child run did not return in time."
        ),
    }
    assert "not-for-users" not in rendered
    assert "authorization" not in rendered


def test_tool_selector_no_longer_depends_on_special_subagent_only_path(monkeypatch):
    seen: dict[str, object] = {}

    def fake_model_call(*args, **kwargs):
        seen["called"] = True
        return {"status": "ok", "output": {"recommended_tools": [{"tool_id": "search_docs", "confidence": 0.9, "reason": "fits"}]}}

    monkeypatch.setattr("domain.chat.tool_selection_orchestrator.call_model", fake_model_call)

    from domain.chat.tool_selection_orchestrator import ToolSelectionOrchestrator

    result = ToolSelectionOrchestrator().select(
        "search docs",
        [{"tool_id": "search_docs", "summary": "Search docs"}],
        selected_model_capabilities={"supports_tool_calling": True},
    )

    assert seen["called"] is True
    assert result["recommended_tools"][0]["tool_id"] == "search_docs"


def test_docs_do_not_present_subagent_as_primary_architecture():
    source = (ROOT / "docs" / "subagents.md").read_text(encoding="utf-8").lower()
    functions_doc = (ROOT / "docs" / "defaultspack-functions.md").read_text(encoding="utf-8").lower()

    assert "compatibility" in source
    assert "no longer treats \"subagent\" as a primary architecture concept" in source
    assert "utility subagents" not in functions_doc


def test_multi_agent_boundary_documented_and_not_broken():
    source = (ROOT / "docs" / "subagents.md").read_text(encoding="utf-8")

    assert "agent.delegate" in source
    assert "multi-agent" in source


def test_subagent_alias_does_not_bypass_tool_policy_or_approval():
    result = ToolExecutor().execute(
        "subagent",
        {"task": "hello"},
        {"profile_policy": {"disabled_tools": ["subagent"]}},
    )

    assert result["is_error"] is True
    assert result["rejected_by_policy"] is True
    assert result["delegation_error"]["category"] == "policy"
