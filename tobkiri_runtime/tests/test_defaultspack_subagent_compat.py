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

from blocks.agent.run_subagent import run as run_subagent_block  # noqa: E402
from domain.chat.store import ChatStore  # noqa: E402
from domain.chat.subagent_durability import (  # noqa: E402
    SUBAGENT_DURABLE_DRAFT_FLAG,
    SUBAGENT_FAILED_TEXT,
    SUBAGENT_PENDING_TEXT,
    has_completed_assistant_text,
    subagent_durable_draft_metadata,
)
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


def test_agent_run_subagent_delegate_forwards_profile_authority_context(monkeypatch):
    seen: dict[str, object] = {}

    def fake_dispatch(envelope, context):
        seen["context"] = context
        seen["params"] = envelope.params
        seen["metadata"] = envelope.metadata
        seen["target"] = envelope.target
        return {"status": "ok", "assistant_text": "done"}

    monkeypatch.setattr("domain.input.dispatcher.dispatch_input", fake_dispatch)

    result = run_subagent_block(
        {
            "prompt": "MiMo monitor smoke",
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "profile_id": "defaultspack.mimo_coding_company",
            "conversation_id": "conv_1",
            "company_id": "mimo-coding-company",
        },
        {
            "profile_id": "defaultspack.mimo_coding_company",
            "authority_principal_id": "profile:defaultspack.mimo_coding_company",
        },
    )

    assert result["status"] == "ok"
    assert seen["context"]["profile_id"] == "defaultspack.mimo_coding_company"
    assert seen["context"]["principal_id"] == "profile:defaultspack.mimo_coding_company"
    assert seen["params"]["profile_id"] == "defaultspack.mimo_coding_company"
    assert seen["metadata"]["profile_id"] == "defaultspack.mimo_coding_company"
    assert seen["target"]["conversation_id"] == "conv_1"


def test_agent_run_subagent_delegate_does_not_promote_untrusted_top_level_profile_authority(monkeypatch):
    seen: dict[str, object] = {}

    def fake_dispatch(envelope, context):
        seen["context"] = context
        seen["params"] = envelope.params
        return {"status": "ok", "assistant_text": "done"}

    monkeypatch.setattr("domain.input.dispatcher.dispatch_input", fake_dispatch)

    result = run_subagent_block(
        {
            "task": "Reply with exactly OK_MIMO_CONTEXT.",
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "profile_id": "defaultspack.mimo_coding_company",
            "principal_id": "profile:payload-spoof",
            "authority_principal_id": "profile:payload-spoof",
        },
        {},
    )

    assert result["status"] == "ok"
    assert "profile_id" not in seen["context"]
    assert "principal_id" not in seen["context"]
    assert "authority_principal_id" not in seen["context"]
    assert seen["params"]["profile_id"] == "defaultspack.mimo_coding_company"


def test_agent_run_subagent_delegate_does_not_trust_payload_principal(monkeypatch):
    seen: dict[str, object] = {}

    def fake_dispatch(envelope, context):
        seen["context"] = context
        seen["params"] = envelope.params
        return {"status": "ok", "assistant_text": "done"}

    monkeypatch.setattr("domain.input.dispatcher.dispatch_input", fake_dispatch)

    result = run_subagent_block(
        {
            "prompt": "MiMo monitor smoke",
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "profile_id": "profile-from-payload",
            "principal_id": "profile:payload-spoof",
            "authority_principal_id": "profile:payload-spoof",
        },
        {
            "profile_id": "trusted-profile",
            "authority_principal_id": "profile:trusted-profile",
        },
    )

    assert result["status"] == "ok"
    assert seen["context"]["profile_id"] == "trusted-profile"
    assert seen["context"]["principal_id"] == "profile:trusted-profile"
    assert seen["context"]["authority_principal_id"] == "profile:trusted-profile"
    assert seen["params"]["profile_id"] == "profile-from-payload"


def test_agent_run_subagent_delegate_authority_approval_surfaces_status(monkeypatch):
    def fake_execute(input_data, context):
        return {
            "status": "ok",
            "data": {
                "execution_id": "agent-needs-authority",
                "status": "authority_approval_required",
                "approval_required": True,
                "requires_approval": True,
                "finish_reason": "authority_approval_required",
                "authority": {
                    "status": "authority_approval_required",
                    "approval_required": True,
                    "requires_approval": True,
                    "request_id": "auth_mimo",
                    "approval_request_id": "auth_mimo",
                    "permission_id": "model.invoke",
                    "principal_id": "profile:defaultspack.mimo_coding_company",
                    "message": "MiMo provider needs authority approval",
                },
                "result": {"status": "authority_approval_required"},
            },
        }

    monkeypatch.setattr("blocks.agent.execute.run", fake_execute)

    result = run_subagent_block(
        {
            "task": "Reply with exactly OK_MIMO_CONTEXT.",
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "profile_id": "defaultspack.mimo_coding_company",
        },
        {},
    )

    assert result["status"] == "ok"
    data = result["data"]
    assert data["status"] == "authority_approval_required"
    assert data["code"] == "authority_approval_required"
    assert data["approval_required"] is True
    assert data["result"]["request_id"] == "auth_mimo"
    assert data["result"]["permission_id"] == "model.invoke"
    assert data["result"]["principal_id"] == "profile:defaultspack.mimo_coding_company"
    assert "DELEGATE_RUN_FAILED" not in json.dumps(data, ensure_ascii=False)


def test_agent_delegate_forwards_profile_authority_context(monkeypatch):
    from domain.input.actions.agent_delegate import handle
    from domain.input.envelope import RumiInputEnvelope

    seen: dict[str, object] = {}

    def fake_execute(input_data, context):
        seen["input_data"] = input_data
        seen["context"] = context
        return {
            "status": "ok",
            "data": {
                "execution_id": "agent_1",
                "status": "ok",
                "result": {"assistant_text": "done"},
            },
        }

    monkeypatch.setattr("blocks.agent.execute.run", fake_execute)

    result = handle(
        RumiInputEnvelope(
            role="user",
            input="MiMo monitor smoke",
            chat={},
            source={"type": "compatibility", "provider": "subagent"},
            target={"conversation_id": "conv_1"},
            delivery={"action_id": "agent.delegate"},
            attachments=[],
            metadata={"profile_id": "defaultspack.mimo_coding_company"},
            params={
                "task": "MiMo monitor smoke",
                "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
                "profile_id": "defaultspack.mimo_coding_company",
            },
            tools=[],
        ),
        {
            "profile_id": "defaultspack.mimo_coding_company",
            "authority_principal_id": "profile:defaultspack.mimo_coding_company",
        },
    )

    assert result["status"] == "ok"
    assert seen["input_data"]["model"] == "xiaomi-token-plan-sgp/mimo-v2-omni"
    assert seen["context"]["profile_id"] == "defaultspack.mimo_coding_company"
    assert seen["context"]["principal_id"] == "profile:defaultspack.mimo_coding_company"
    assert seen["context"]["conversation_id"] == "conv_1"


def test_agent_delegate_does_not_trust_payload_principal(monkeypatch):
    from domain.input.actions.agent_delegate import handle
    from domain.input.envelope import RumiInputEnvelope

    seen: dict[str, object] = {}

    def fake_execute(input_data, context):
        seen["input_data"] = input_data
        seen["context"] = context
        return {
            "status": "ok",
            "data": {
                "execution_id": "agent_1",
                "status": "ok",
                "result": {"assistant_text": "done"},
            },
        }

    monkeypatch.setattr("blocks.agent.execute.run", fake_execute)

    result = handle(
        RumiInputEnvelope(
            role="user",
            input="MiMo monitor smoke",
            chat={},
            source={"type": "compatibility", "provider": "subagent"},
            target={"conversation_id": "conv_1"},
            delivery={"action_id": "agent.delegate"},
            attachments=[],
            metadata={"profile_id": "profile-from-metadata", "principal_id": "profile:metadata-spoof"},
            params={
                "task": "MiMo monitor smoke",
                "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
                "profile_id": "profile-from-payload",
                "principal_id": "profile:payload-spoof",
                "authority_principal_id": "profile:payload-spoof",
            },
            tools=[],
        ),
        {},
    )

    assert result["status"] == "ok"
    assert seen["input_data"]["model"] == "xiaomi-token-plan-sgp/mimo-v2-omni"
    assert "profile_id" not in seen["context"]
    assert "principal_id" not in seen["context"]
    assert "authority_principal_id" not in seen["context"]
    assert seen["context"]["conversation_id"] == "conv_1"


def test_agent_engine_preserves_authority_approval_model_result(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    from domain.agent.engine import AgentEngine

    def fake_ai(self, messages, model, context, tools=None):
        return {
            "status": "error",
            "error": {
                "code": "AUTHORITY_APPROVAL_REQUIRED",
                "message": "MiMo provider needs authority approval",
                "details": {
                    "status": "authority_approval_required",
                    "approval_required": True,
                    "requires_approval": True,
                    "finish_reason": "authority_approval_required",
                    "request_id": "auth_mimo",
                    "approval_request_id": "auth_mimo",
                    "permission_id": "model.invoke",
                    "principal_id": "profile:defaultspack.mimo_coding_company",
                    "message": "MiMo provider needs authority approval",
                },
            },
        }

    monkeypatch.setattr("domain.agent.engine.AgentEngine._ai_complete", fake_ai)

    result = AgentEngine().execute(
        "Reply with exactly OK_MIMO_CONTEXT.",
        [],
        "xiaomi-token-plan-sgp/mimo-v2-omni",
        None,
        {
            "profile_id": "defaultspack.mimo_coding_company",
            "authority_principal_id": "profile:defaultspack.mimo_coding_company",
        },
    )

    assert result["status"] == "authority_approval_required"
    assert result["approval_required"] is True
    assert result["authority"]["request_id"] == "auth_mimo"
    assert result["result"]["status"] == "authority_approval_required"
    assert result["result"]["steps"][-1]["step_type"] == "authority_approval_required"


def test_agent_engine_preserves_top_level_authority_approval_model_result(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    from domain.agent.engine import AgentEngine

    def fake_ai(self, messages, model, context, tools=None):
        return {
            "status": "authority_approval_required",
            "code": "authority_approval_required",
            "error": "MiMo provider needs authority approval",
            "message": "MiMo provider needs authority approval",
            "approval_required": True,
            "requires_approval": True,
            "finish_reason": "authority_approval_required",
            "authority": {
                "status": "authority_approval_required",
                "request_id": "auth_mimo",
                "permission_id": "model.invoke",
                "principal_id": "profile:defaultspack.mimo_coding_company",
            },
        }

    monkeypatch.setattr("domain.agent.engine.AgentEngine._ai_complete", fake_ai)

    result = AgentEngine().execute(
        "Reply with exactly OK_MIMO_CONTEXT.",
        [],
        "xiaomi-token-plan-sgp/mimo-v2-omni",
        None,
        {
            "profile_id": "defaultspack.mimo_coding_company",
            "authority_principal_id": "profile:defaultspack.mimo_coding_company",
        },
    )

    assert result["status"] == "authority_approval_required"
    assert result["approval_required"] is True
    assert result["authority"]["request_id"] == "auth_mimo"
    assert result["result"]["steps"][-1]["step_type"] == "authority_approval_required"


def test_agent_run_subagent_utility_forwards_authority_context(monkeypatch):
    seen: dict[str, object] = {}

    def fake_call_model(input_data, context, *, call_handler=None):
        seen["input_data"] = input_data
        seen["context"] = context
        seen["call_handler"] = call_handler
        return {"status": "ok", "output": {"recommended_tools": [{"tool_id": "search_docs"}]}}

    monkeypatch.setattr("domain.agent.subagent_orchestrator.call_model", fake_call_model)

    result = run_subagent_block(
        {
            "role_id": "tool_selector",
            "model": "xiaomi-token-plan-sgp/mimo-v2-omni",
            "payload": {"candidate_tools": [{"tool_id": "search_docs"}]},
        },
        {
            "profile_id": "defaultspack.mimo_coding_company",
            "authority_principal_id": "profile:defaultspack.mimo_coding_company",
        },
    )

    assert result["status"] == "ok"
    assert seen["context"]["profile_id"] == "defaultspack.mimo_coding_company"
    assert seen["context"]["principal_id"] == "profile:defaultspack.mimo_coding_company"
    assert seen["context"]["_model_call_depth"] == 0


def test_agent_run_subagent_nested_payload_keeps_http_fallback_timeout_for_execute(monkeypatch):
    seen: dict[str, object] = {}

    def fake_execute(input_data, context):
        seen["timeout_seconds"] = input_data.get("timeout_seconds")
        seen["task"] = input_data.get("task")
        return {"status": "ok", "data": {"execution_id": "run_1", "status": "queued"}}

    monkeypatch.setattr("blocks.agent.execute.run", fake_execute)

    result = run_subagent_block(
        {
            "role_id": "delegate",
            "timeout_seconds": 300,
            "payload": {"task": "delegate this"},
        },
        {},
    )

    assert result["status"] == "ok"
    assert seen == {"timeout_seconds": 300, "task": "delegate this"}


def test_agent_run_subagent_nested_timeout_wins_over_http_fallback_timeout(monkeypatch):
    seen: dict[str, object] = {}

    def fake_execute(input_data, context):
        seen["timeout_seconds"] = input_data.get("timeout_seconds")
        return {"status": "ok", "data": {"execution_id": "run_1", "status": "queued"}}

    monkeypatch.setattr("blocks.agent.execute.run", fake_execute)

    result = run_subagent_block(
        {
            "role_id": "delegate",
            "timeout_seconds": 300,
            "payload": {"task": "delegate this", "timeout_seconds": 123},
        },
        {},
    )

    assert result["status"] == "ok"
    assert seen["timeout_seconds"] == 123


def test_agent_run_subagent_delegate_extracts_nested_assistant_text(monkeypatch):
    def fake_dispatch(envelope, context):
        return {
            "status": "ok",
            "assistant_text": "",
            "delegate": {"execution_id": "run_1"},
            "result": {
                "status": "ok",
                "result": {
                    "result": {
                        "things": [
                            "Inspect repo files",
                            "Run focused tests",
                            "Report findings",
                        ]
                    }
                },
            },
        }

    monkeypatch.setattr("domain.input.dispatcher.dispatch_input", fake_dispatch)

    result = run_subagent_block({"payload": {"task": "Simple test: List 3 things you can do as a subagent."}}, {})

    assert result["status"] == "ok"
    assert "Inspect repo files" in result["data"]["assistant_text"]
    assert result["data"]["route_kind"] == "agent.delegate"


def test_agent_subagent_http_route_gets_long_running_timeout():
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer

    assert (
        DefaultsHttpServer._fallback_function_timeout_seconds(
            "blocks.agent.run_subagent",
            {},
        )
        == 300.0
    )


def test_agent_run_subagent_defaultspack_function_manifest_keeps_long_timeout():
    from domain.function_runtime.manifest_factory import FUNCTION_SPECS_BY_ID, manifest_for

    generated = manifest_for(FUNCTION_SPECS_BY_ID["agent_run_subagent"])
    committed = json.loads(
        (ROOT / "ecosystem" / "defaultspack" / "functions" / "agent_run_subagent" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert generated["grant_config"]["timeout"] == 300
    assert committed["grant_config"]["timeout"] == 300


def test_agent_run_subagent_direct_function_call_uses_manifest_timeout(monkeypatch):
    from tempfile import TemporaryDirectory

    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert_retired_module_absent("core_runtime.capability_executor")
    assert_retired_module_absent("core_runtime.function_registry")
    assert_profile_resolver_requires_authority_snapshot()
    with TemporaryDirectory() as root:
        assert_payload_mutations_denied(harness(Path(root)))


def test_agent_run_subagent_delegate_provider_error_surfaces_safe_text(monkeypatch):
    secret = "sk-subagent-secret"

    def fake_execute(input_data, context):
        return {
            "status": "ok",
            "data": {
                "execution_id": "agent-provider-fail",
                "status": "error",
                "result": {
                    "execution_id": "agent-provider-fail",
                    "status": "error",
                    "error": "provider error: API key " + secret,
                },
            },
        }

    monkeypatch.setattr("blocks.agent.execute.run", fake_execute)

    result = run_subagent_block({"payload": {"task": "delegate this"}}, {})

    assert result["status"] == "ok"
    data = result["data"]
    assert data["status"] == "error"
    assert data["code"] == "DELEGATE_PROVIDER_ERROR"
    assert data["assistant_text"]
    assert data["error"] == data["assistant_text"]
    assert data["delegate"]["status"] == "error"
    assert data["delegate"]["execution_id"] == "agent-provider-fail"
    serialized = json.dumps(data, ensure_ascii=False)
    assert secret not in serialized
    assert "API key" not in serialized


def test_agent_run_subagent_delegate_timeout_surfaces_failed_result(monkeypatch):
    def fake_execute(input_data, context):
        return {
            "status": "ok",
            "data": {
                "execution_id": "agent-timeout",
                "status": "ok",
                "result": {
                    "execution_id": "agent-timeout",
                    "status": "timeout",
                    "error": "handler execution timed out",
                },
            },
        }

    monkeypatch.setattr("blocks.agent.execute.run", fake_execute)

    result = run_subagent_block({"payload": {"task": "delegate this"}}, {})

    assert result["status"] == "ok"
    data = result["data"]
    assert data["status"] == "error"
    assert data["code"] == "DELEGATE_RUN_FAILED"
    assert data["assistant_text"] == SUBAGENT_FAILED_TEXT
    assert data["result"]["status"] == "timeout"
    assert data["result"]["error_redacted"] is True


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


def test_subagent_rumi_function_executes_local_controller(
    monkeypatch, defaultspack_capability_plan_context
):
    from domain.tool.registry import ToolRegistry
    from domain.tool_policy.internal_context import mark_tool_server_approval_context

    ToolRegistry._instance = None
    tool_def = ToolRegistry().get("subagent")
    assert tool_def is not None
    seen: dict[str, object] = {}

    class FakeCapabilityExecutor:
        _initialized = True

        def execute(self, principal_id, request):
            raise AssertionError("subagent must not use capability subprocess dispatch")

    def fake_run(self, arguments, context):
        seen["arguments"] = dict(arguments)
        seen["conversation_id"] = context.get("conversation_id")
        return {
            "summary": "subagent answered",
            "child_conversation_id": "child-1",
            "parent_conversation_id": context.get("conversation_id"),
        }

    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.subagent.SubagentController.run",
        fake_run,
    )

    tool_context = mark_tool_server_approval_context(
        {
            **defaultspack_capability_plan_context("subagent"),
            "profile_policy": {"yolo_mode": True},
            "conversation_id": "parent-1",
            "principal_id": "rumi_default_tools_pack",
            "capability_executor": FakeCapabilityExecutor(),
        }
    )
    result = ToolExecutor(
        subagent_factory=SubagentController,
    ).execute(
        "subagent",
        {"task": "hello from child"},
        tool_context,
    )

    assert result["is_error"] is False
    assert result["result"] == "subagent answered"
    assert result["widget"]["type"] == "subagent"
    assert result["widget"]["child_conversation_id"] == "child-1"
    assert seen == {
        "arguments": {"task": "hello from child"},
        "conversation_id": "parent-1",
    }


def test_tool_subagent_defaultspack_function_manifest_keeps_long_timeout():
    from domain.function_runtime.manifest_factory import FUNCTION_SPECS_BY_ID, manifest_for

    generated = manifest_for(FUNCTION_SPECS_BY_ID["tool_subagent"])
    committed = json.loads(
        (ROOT / "ecosystem" / "defaultspack" / "functions" / "tool_subagent" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert generated["grant_config"]["timeout"] == 240
    assert committed["grant_config"]["timeout"] == 240


def test_tool_subagent_direct_function_call_uses_manifest_timeout(monkeypatch):
    from tempfile import TemporaryDirectory

    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert_retired_module_absent("core_runtime.capability_executor")
    assert_retired_module_absent("core_runtime.function_registry")
    assert_profile_resolver_requires_authority_snapshot()
    with TemporaryDirectory() as root:
        assert_payload_mutations_denied(harness(Path(root)))


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


def test_non_stream_subagent_child_creates_durable_assistant_draft_before_model(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    store = ChatStore()
    parent = store.create_conversation(model="stub/default")
    child = store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="subagent",
        metadata={
            "parent_conversation_id": parent["id"],
            "subagent": {"task": "probe", "source": "subagent_tool"},
        },
    )

    from domain.chat.stream_engine import ChatRunEngine

    events = ChatRunEngine().stream(
        {
            "conversation_id": child["id"],
            "message": {"role": "user", "content": "do the child work"},
            "params": {},
            "tools": [],
        },
        {"chat_history_mode": "current_turn", "subagent_child_durable_draft": True},
        stream_mode=False,
    )
    try:
        seen_types = []
        for _ in range(5):
            event = next(events)
            seen_types.append(event.get("type"))
            if event.get("type") == "assistant_message_started":
                break
        assert "assistant_message_started" in seen_types
        child_after = ChatStore().get_conversation(child["id"])
        assert [message["role"] for message in child_after["messages"]] == ["user", "assistant"]
        draft = child_after["messages"][-1]
        assert draft["finish_reason"] == "streaming"
        assert draft["raw_text"] == SUBAGENT_PENDING_TEXT
        assert draft["metadata"][SUBAGENT_DURABLE_DRAFT_FLAG] is True
        assert draft["metadata"]["status"] == "running"
    finally:
        events.close()


def test_mimo_monitor_ignores_active_subagent_child_running_durable_draft(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    store = ChatStore()
    parent = store.create_conversation(model="stub/default")
    child = store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="subagent",
        metadata={
            "parent_conversation_id": parent["id"],
            "subagent": {"task": "active child", "source": "subagent_tool"},
        },
    )
    store.add_message(
        child["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "please finish"}],
        },
    )
    store.add_message(
        child["id"],
        {
            "role": "assistant",
            "content": [{"type": "text", "text": SUBAGENT_PENDING_TEXT}],
            "raw_text": SUBAGENT_PENDING_TEXT,
            "finish_reason": "streaming",
            "metadata": subagent_durable_draft_metadata("stub/default", {}),
        },
    )

    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    runtime = MimoCodingCompanyRuntime()
    monkeypatch.setattr(runtime, "_conversation_age_seconds", lambda conversation: 1.0)

    result = runtime._subagent_reply_gaps({"conversation_id": parent["id"]})

    assert result["checked_ids"] == [child["id"]]
    assert result["unanswered"] == []
    assert result["failed"] == []
    assert result["repaired"] == []
    child_after = ChatStore().get_conversation(child["id"])
    assert [message["role"] for message in child_after["messages"]] == ["user", "assistant"]
    assert child_after["messages"][-1]["finish_reason"] == "streaming"
    assert child_after["messages"][-1]["metadata"]["status"] == "running"
    assert "error_code" not in child_after["messages"][-1]["metadata"]


def test_non_stream_subagent_child_starts_draft_before_tool_selection_events(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    store = ChatStore()
    parent = store.create_conversation(model="stub/default")
    child = store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="subagent",
        metadata={
            "parent_conversation_id": parent["id"],
            "subagent": {"task": "probe", "source": "subagent_tool"},
        },
    )
    user = store.add_message(
        child["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "do the child work"}],
        },
    )

    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine

    def fake_prepare(input_data, context):
        del input_data, context
        conversation = ChatStore().get_conversation(child["id"])
        return PreparedChatRun(
            conversation_id=child["id"],
            conversation=conversation,
            input_data={},
            request_id="req-subagent-draft-order",
            content=[{"type": "text", "text": "do the child work"}],
            metadata={},
            user_message=user,
            model="stub/default",
            params={},
            request_context={
                "tool_selection": {
                    "selection_id": "select-1",
                    "mode": "connected",
                    "strategy": "deterministic",
                    "selected_tool_ids": [],
                    "selected_services": [],
                }
            },
            tool_context={},
            standard_messages=[],
            user_text="do the child work",
            system_prompt="",
            enrich_info={},
            raw_tools=[],
            provider_tools=[],
            tools_called=[],
            connected_tool_names=set(),
            call_handler=None,
            model_routing={},
        )

    def fake_execute(self, prepared, draft):
        del self, prepared, draft
        if False:
            yield {}
        return {
            "content": [{"type": "text", "text": "child complete"}],
            "finish_reason": "stop",
            "usage": {},
            "metadata": {},
        }

    monkeypatch.setattr("domain.chat.stream_engine.prepare_chat_run", fake_prepare)
    monkeypatch.setattr("domain.chat.stream_engine.ChatRunEngine._execute", fake_execute)

    events = list(
        ChatRunEngine().stream(
            {
                "conversation_id": child["id"],
                "message": {"role": "user", "content": "do the child work"},
                "params": {},
                "tools": [],
            },
            {"chat_history_mode": "current_turn", "subagent_child_durable_draft": True},
            stream_mode=False,
        )
    )

    event_types = [event.get("type") for event in events]
    assert event_types.index("assistant_message_started") < event_types.index("tool_selection_started")
    child_after = ChatStore().get_conversation(child["id"])
    assert [message["role"] for message in child_after["messages"]] == ["user", "assistant"]
    assert child_after["messages"][-1]["raw_text"] == "child complete"


def test_non_stream_subagent_child_success_finalizes_without_draft_metadata(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    store = ChatStore()
    parent = store.create_conversation(model="stub/default")
    child = store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="subagent",
        metadata={
            "parent_conversation_id": parent["id"],
            "subagent": {"task": "probe", "source": "subagent_tool"},
        },
    )

    def call_handler(name, payload):
        assert name == "defaults.ai.complete"
        return {
            "status": "ok",
            "data": {
                "content": [{"type": "text", "text": "child complete"}],
                "finish_reason": "stop",
                "metadata": {
                    SUBAGENT_DURABLE_DRAFT_FLAG: True,
                    "streaming": True,
                    "draft": True,
                    "status": "running",
                    "thinking": {"state": "running"},
                },
            },
        }

    from domain.chat.stream_engine import ChatRunEngine

    events = list(
        ChatRunEngine().stream(
            {
                "conversation_id": child["id"],
                "message": {"role": "user", "content": "do the child work"},
                "params": {},
                "tools": [],
            },
            {
                "call_handler": call_handler,
                "chat_history_mode": "current_turn",
                "subagent_child_durable_draft": True,
            },
            stream_mode=False,
        )
    )

    assert any(event.get("type") == "assistant_message_started" for event in events)
    assert any(event.get("type") == "assistant_message_completed" for event in events)
    child_after = ChatStore().get_conversation(child["id"])
    assert [message["role"] for message in child_after["messages"]] == ["user", "assistant"]
    assistant = child_after["messages"][-1]
    assert assistant["raw_text"] == "child complete"
    assert assistant["finish_reason"] == "stop"
    metadata = assistant["metadata"]
    assert "streaming" not in metadata
    assert "draft" not in metadata
    assert SUBAGENT_DURABLE_DRAFT_FLAG not in metadata
    assert metadata.get("status") != "running"
    assert metadata["thinking"]["state"] == "completed"


def test_non_stream_subagent_child_prepare_failure_adds_failed_assistant_marker(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    store = ChatStore()
    parent = store.create_conversation(model="stub/default")
    child = store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="subagent",
        metadata={
            "parent_conversation_id": parent["id"],
            "subagent": {"task": "probe", "source": "subagent_tool"},
        },
    )

    def fake_prepare(input_data, context):
        del context
        ChatStore().add_message(
            input_data["conversation_id"],
            {
                "role": "user",
                "content": [{"type": "text", "text": "do the child work"}],
            },
        )
        raise TimeoutError("handler execution timed out")

    from domain.chat.stream_engine import ChatRunEngine

    monkeypatch.setattr("domain.chat.stream_engine.prepare_chat_run", fake_prepare)

    with pytest.raises(TimeoutError):
        list(
            ChatRunEngine().stream(
                {
                    "conversation_id": child["id"],
                    "message": {"role": "user", "content": "do the child work"},
                    "params": {},
                    "tools": [],
                },
                {"chat_history_mode": "current_turn", "subagent_child_durable_draft": True},
                stream_mode=False,
            )
        )

    child_after = ChatStore().get_conversation(child["id"])
    assert [message["role"] for message in child_after["messages"]] == ["user", "assistant"]
    marker = child_after["messages"][-1]
    assert marker["finish_reason"] == "error"
    assert marker["raw_text"] == SUBAGENT_FAILED_TEXT
    assert marker["metadata"]["status"] == "error"
    assert marker["metadata"]["error_code"] == "SUBAGENT_PREPARE_FAILED"
    assert child_after["metadata"]["subagent"]["status"] == "error"
    assert child_after["metadata"]["subagent"]["error_code"] == "SUBAGENT_PREPARE_FAILED"


def test_tool_subagent_adds_safe_marker_when_dispatch_returns_without_assistant(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    parent = ChatStore().create_conversation(model="stub/default")

    def fake_dispatch(envelope, context):
        ChatStore().add_message(
            envelope.target["conversation_id"],
            {
                "role": "user",
                "content": [{"type": "text", "text": envelope.input}],
                "metadata": envelope.metadata,
            },
        )
        return {"status": "ok", "assistant_text": ""}

    monkeypatch.setattr("ecosystem.rumi_default_tools_pack.domain.tool.subagent.dispatch_input", fake_dispatch)

    result = SubagentController().run({"task": "silent child"}, {"conversation_id": parent["id"]})

    child = ChatStore().get_conversation(result["child_conversation_id"])
    assert [message["role"] for message in child["messages"]] == ["user", "assistant"]
    marker = child["messages"][-1]
    assert marker["finish_reason"] == "error"
    assert "completed without producing a visible response" in marker["raw_text"]
    assert marker["metadata"]["error_code"] == "SUBAGENT_EMPTY_RESPONSE"
    assert SUBAGENT_DURABLE_DRAFT_FLAG not in marker["metadata"]


def test_tool_subagent_persists_nested_delegate_assistant_text(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    parent = ChatStore().create_conversation(model="stub/default")

    def fake_dispatch(envelope, context):
        ChatStore().add_message(
            envelope.target["conversation_id"],
            {
                "role": "user",
                "content": [{"type": "text", "text": envelope.input}],
                "metadata": envelope.metadata,
            },
        )
        return {
            "status": "ok",
            "assistant_text": "",
            "result": {
                "status": "ok",
                "result": {
                    "result": {
                        "things": [
                            "Inspect repo files",
                            "Run focused tests",
                            "Report findings",
                        ]
                    }
                },
            },
        }

    monkeypatch.setattr("ecosystem.rumi_default_tools_pack.domain.tool.subagent.dispatch_input", fake_dispatch)

    result = SubagentController().run(
        {"task": "Simple test: List 3 things you can do as a subagent."},
        {"conversation_id": parent["id"]},
    )

    assert "Inspect repo files" in result["summary"]
    child = ChatStore().get_conversation(result["child_conversation_id"])
    assert [message["role"] for message in child["messages"]] == ["user", "assistant"]
    assistant = child["messages"][-1]
    assert assistant["finish_reason"] == "stop"
    assert "Inspect repo files" in assistant["raw_text"]
    assert assistant["metadata"]["error_code"] == "SUBAGENT_RESPONSE_REPAIRED"


def test_tool_subagent_returns_error_and_marks_child_failed_when_dispatch_times_out(
    monkeypatch, tmp_path, defaultspack_capability_plan_context
):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    parent = ChatStore().create_conversation(
        model="stub/default",
        system_prompt_id="mimo_coding_company",
        group_id="company:mimo-coding-company",
        metadata={"profile_id": "defaultspack.mimo_coding_company", "company_id": "mimo-coding-company"},
    )

    def fake_dispatch(envelope, context):
        ChatStore().add_message(
            envelope.target["conversation_id"],
            {
                "role": "user",
                "content": [{"type": "text", "text": envelope.input}],
                "metadata": envelope.metadata,
            },
        )
        raise TimeoutError("handler execution timed out")

    monkeypatch.setattr("ecosystem.rumi_default_tools_pack.domain.tool.subagent.dispatch_input", fake_dispatch)

    result = run_defaultspack_function(
        "tool_subagent",
        {"task": "simple json probe"},
        {
            **defaultspack_capability_plan_context("subagent"),
            "conversation_id": parent["id"],
        },
        subagent_factory=SubagentController,
    )

    assert result["status"] == "ok"
    assert result["data"]["is_error"] is True
    assert result["data"]["widget"]["type"] == "subagent"
    assert result["data"]["widget"]["child_conversation_id"]
    parent_after = ChatStore().get_conversation(parent["id"])
    child_id = parent_after["child_conversation_ids"][0]
    assert result["data"]["widget"]["child_conversation_id"] == child_id
    child = ChatStore().get_conversation(child_id)
    assert child["metadata"]["subagent"]["status"] == "error"
    assert child["metadata"]["subagent"]["error_code"] == "SUBAGENT_DISPATCH_TIMEOUT"
    assert result["data"]["widget"]["error_type"] == "timeout"
    assert [message["role"] for message in child["messages"]] == ["user", "assistant"]
    assert child["messages"][-1]["finish_reason"] == "error"
    assert "could not complete" in child["messages"][-1]["raw_text"]
    assert "timed out" not in child["messages"][-1]["raw_text"]


def test_mimo_monitor_repairs_stale_running_durable_subagent_draft(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    store = ChatStore()
    parent = store.create_conversation(model="stub/default")
    child = store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="subagent",
        metadata={
            "parent_conversation_id": parent["id"],
            "subagent": {"task": "stale child", "source": "subagent_tool"},
        },
    )
    store.add_message(
        child["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "please finish"}],
        },
    )
    store.add_message(
        child["id"],
        {
            "role": "assistant",
            "content": [{"type": "text", "text": SUBAGENT_PENDING_TEXT}],
            "raw_text": SUBAGENT_PENDING_TEXT,
            "finish_reason": "streaming",
            "metadata": subagent_durable_draft_metadata("stub/default", {}),
        },
    )

    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    runtime = MimoCodingCompanyRuntime()
    monkeypatch.setattr(runtime, "_conversation_age_seconds", lambda conversation: 999.0)

    result = runtime._subagent_reply_gaps({"conversation_id": parent["id"]})

    assert result["unanswered"][0]["child_conversation_id"] == child["id"]
    assert result["unanswered"][0]["failed"] is True
    assert result["failed"][0]["child_conversation_id"] == child["id"]
    assert result["repaired"] == []
    child_after = ChatStore().get_conversation(child["id"])
    assert [message["role"] for message in child_after["messages"]] == ["user", "assistant"]
    assert child_after["metadata"]["subagent"]["status"] == "error"
    assert child_after["metadata"]["subagent"]["error_code"] == "SUBAGENT_DISPATCH_INTERRUPTED"
    assert child_after["messages"][-1]["finish_reason"] == "error"
    assert "could not complete" in child_after["messages"][-1]["raw_text"]
    metadata = child_after["messages"][-1]["metadata"]
    assert metadata["status"] == "error"
    assert metadata["error_code"] == "SUBAGENT_DISPATCH_INTERRUPTED"
    assert metadata["final"] is True
    assert "streaming" not in metadata
    assert "draft" not in metadata
    assert SUBAGENT_DURABLE_DRAFT_FLAG not in metadata


def test_mimo_monitor_repairs_stale_subagent_child_missing_from_parent_index(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    store = ChatStore()
    parent = store.create_conversation(model="stub/default")
    child = store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="subagent",
        metadata={
            "parent_conversation_id": parent["id"],
            "subagent": {"task": "orphaned child", "source": "subagent_tool"},
        },
    )
    store.update_conversation(parent["id"], {"child_conversation_ids": []})
    store.add_message(
        child["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "Simple test JSON probe"}],
        },
    )

    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    runtime = MimoCodingCompanyRuntime()
    monkeypatch.setattr(runtime, "_conversation_age_seconds", lambda conversation: 999.0)

    result = runtime._subagent_reply_gaps({"conversation_id": parent["id"]})

    assert result["checked_ids"] == [child["id"]]
    assert result["unanswered"][0]["child_conversation_id"] == child["id"]
    assert result["unanswered"][0]["failed"] is True
    assert result["failed"][0]["child_conversation_id"] == child["id"]
    assert result["repaired"] == []
    child_after = ChatStore().get_conversation(child["id"])
    assert [message["role"] for message in child_after["messages"]] == ["user", "assistant"]
    assert child_after["metadata"]["subagent"]["status"] == "error"
    assert child_after["metadata"]["subagent"]["error_code"] == "SUBAGENT_DISPATCH_INTERRUPTED"


def test_mimo_monitor_repairs_stale_subagent_child_under_company_loop(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    store = ChatStore()
    parent = store.create_conversation(model="stub/default")
    loop = store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="mimo_coding_company_loop",
        metadata={
            "company_id": "mimo-coding-company",
            "loop_key": "qa_loop",
            "parent_conversation_id": parent["id"],
        },
    )
    child = store.create_conversation(
        model="stub/default",
        parent_conversation_id=loop["id"],
        conversation_kind="subagent",
        metadata={
            "parent_conversation_id": loop["id"],
            "subagent": {"task": "loop child", "source": "subagent_tool"},
        },
    )
    store.update_conversation(parent["id"], {"child_conversation_ids": [loop["id"]]})
    store.add_message(
        child["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "Simple test JSON probe under loop"}],
        },
    )

    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    runtime = MimoCodingCompanyRuntime()
    monkeypatch.setattr(runtime, "_conversation_age_seconds", lambda conversation: 999.0)

    result = runtime._subagent_reply_gaps(
        {
            "conversation_id": parent["id"],
            "loop_conversation_ids": {"qa_loop": loop["id"]},
        }
    )

    assert result["checked_ids"] == [child["id"]]
    assert result["unanswered"][0]["child_conversation_id"] == child["id"]
    assert result["unanswered"][0]["failed"] is True
    assert result["failed"][0]["child_conversation_id"] == child["id"]
    child_after = ChatStore().get_conversation(child["id"])
    assert [message["role"] for message in child_after["messages"]] == ["user", "assistant"]
    assert child_after["metadata"]["subagent"]["status"] == "error"
    assert child_after["metadata"]["subagent"]["error_code"] == "SUBAGENT_DISPATCH_INTERRUPTED"


def test_subagent_completed_assistant_text_counts_success_json_stop():
    assert has_completed_assistant_text(
        [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": json.dumps({"status": "ok", "answer": "done"})}],
                "raw_text": json.dumps({"status": "ok", "answer": "done"}),
                "finish_reason": "stop",
                "metadata": {"status": "completed"},
            }
        ]
    )


def test_mimo_monitor_keeps_failed_subagent_child_unanswered(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    ChatStore._instance = None
    store = ChatStore()
    parent = store.create_conversation(model="stub/default")
    child = store.create_conversation(
        model="stub/default",
        parent_conversation_id=parent["id"],
        conversation_kind="subagent",
        metadata={
            "parent_conversation_id": parent["id"],
            "subagent": {"task": "failed child", "source": "subagent_tool"},
        },
    )
    store.add_message(
        child["id"],
        {
            "role": "user",
            "content": [{"type": "text", "text": "please finish"}],
        },
    )
    store.add_message(
        child["id"],
        {
            "role": "assistant",
            "content": [{"type": "text", "text": SUBAGENT_FAILED_TEXT}],
            "raw_text": SUBAGENT_FAILED_TEXT,
            "finish_reason": "error",
            "metadata": {
                "source": "subagent_tool",
                "status": "error",
                "error_code": "SUBAGENT_DISPATCH_TIMEOUT",
                "final": True,
            },
        },
    )

    assert not has_completed_assistant_text(store.get_conversation(child["id"])["messages"])
    assert not has_completed_assistant_text(
        [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "The delegated agent could not complete."}],
                "raw_text": "The delegated agent could not complete.",
                "finish_reason": "stop",
                "metadata": {"status": "error"},
            }
        ]
    )

    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime

    runtime = MimoCodingCompanyRuntime()
    monkeypatch.setattr(runtime, "_conversation_age_seconds", lambda conversation: 999.0)

    result = runtime._subagent_reply_gaps({"conversation_id": parent["id"]})

    assert result["repaired"] == []
    assert result["unanswered"][0]["child_conversation_id"] == child["id"]
    assert result["unanswered"][0]["failed"] is True
    assert result["unanswered"][0]["failure_code"] == "SUBAGENT_DISPATCH_TIMEOUT"
    assert result["failed"][0]["child_conversation_id"] == child["id"]


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


def test_subagent_alias_does_not_bypass_tool_policy_or_approval(
    defaultspack_capability_plan_context,
):
    result = ToolExecutor().execute(
        "subagent",
        {"task": "hello"},
        {
            **defaultspack_capability_plan_context("subagent"),
            "profile_policy": {"disabled_tools": ["subagent"]},
        },
    )

    assert result["is_error"] is True
    assert result["rejected_by_policy"] is True
