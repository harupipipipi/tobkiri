from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULTSPACK_ROOT = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.chat.run_request import (  # noqa: E402
    _resolve_selected_tools,
    _runtime_profile_with_policy_connected_tools,
    prepare_chat_run,
)
from domain.chat.store import ChatStore  # noqa: E402


class _Decision:
    def __init__(self, model: str) -> None:
        self.selected_model = model
        self.original_model = model
        self.selected_group = "default"
        self.reason_codes = ["test"]
        self.warnings = []
        self.bridge_required = False
        self.bridge_plan = {}

    def to_dict(self) -> dict:
        return {"selected_model": self.selected_model}


class _FakeToolRegistry:
    def list_tools(self):
        return [
            {"tool_id": "web_search", "name": "web_search", "schema": {"type": "object"}},
            {"tool_id": "computer_use", "name": "computer_use", "schema": {"type": "object"}},
        ]

    def get(self, tool_id):
        return next(
            (tool for tool in self.list_tools() if tool["tool_id"] == tool_id),
            None,
        )


def test_resolve_selected_tools_accepts_normalized_tool_target_dict(monkeypatch) -> None:
    monkeypatch.setattr("domain.chat.run_request.ToolRegistry", _FakeToolRegistry)

    resolved, unknown = _resolve_selected_tools(
        [{"kind": "tool", "id": "computer_use"}],
    )

    assert unknown == []
    assert [tool["tool_id"] for tool in resolved] == ["computer_use"]


def _fake_prompt(input_data):
    prompt_id = str(input_data.get("system_prompt_id") or "default_chat")
    return {
        "prompt_id": prompt_id,
        "source": f"test.{prompt_id}",
        "source_type": "profile_override",
        "content": f"Runtime prompt for {prompt_id}",
        "final_content": f"Runtime prompt for {prompt_id}",
        "source_chain": [],
    }


def _fake_enrich_messages(standard_messages, system_prompt, conversation_id, user_text, manager):
    return {
        "knowledge_text": "--- Related Knowledge ---\n[1] Runtime knowledge",
        "memory_text": "--- Related Memory ---\n[1] Runtime memory",
        "knowledge_results": [{"id": "knowledge-1", "content": "Runtime knowledge"}],
        "memory_results": [{"id": "memory-1", "content": "Runtime memory"}],
        "enriched_prompt": system_prompt,
    }


def test_ai_input_trace_is_applied_to_chat_runtime_context(
    monkeypatch,
    tmp_path: Path,
    defaultspack_conversation_owner,
) -> None:
    user_data_root = tmp_path / "user_data"
    chat_store_path = tmp_path / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data_root))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(chat_store_path))
    ChatStore._instance = None

    # A legacy active marker must not override the verified v4 activation.
    active_marker = user_data_root / "profiles" / "active_profile.json"
    active_marker.parent.mkdir(parents=True, exist_ok=True)
    active_marker.write_text(
        json.dumps({"version": 1, "active_profile_id": "research-profile"}) + "\n",
        encoding="utf-8",
    )

    conversation = ChatStore().create_conversation(model="stub/default")
    monkeypatch.setattr("domain.chat.run_request.enrich_messages", _fake_enrich_messages)
    monkeypatch.setattr("core_runtime.ai_input_segments.ToolRegistry", _FakeToolRegistry)
    monkeypatch.setattr("core_runtime.ai_input_segments.resolve_effective_prompt", _fake_prompt)
    monkeypatch.setattr("domain.chat.run_request.route_model_request", lambda request: _Decision("stub/default"))
    monkeypatch.setattr(
        "domain.chat.run_request.get_model_capabilities",
        lambda model: {
            "supports_image_input": False,
            "supports_vision": False,
            "supports_tool_calling": True,
            "supports_thinking": True,
        },
    )
    monkeypatch.setattr(
        "domain.chat.run_request._resolve_selected_tools",
        lambda raw_tools, **kwargs: (
            [
                {"tool_id": "web_search", "name": "web_search", "schema": {"parameters": {"type": "object"}}},
                {"tool_id": "computer_use", "name": "computer_use", "schema": {"parameters": {"type": "object"}}},
            ],
            [],
        ),
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "search the web"},
        },
        {},
    )

    assert prepared.request_context["ai_input_trace"]["profile_id"] == "defaults"
    assert "effective_tool_allowlist" not in prepared.request_context
    assert prepared.request_context["resolved_profile"]["profile_id"] == "defaults"
    trace_dir = user_data_root / "workspaces" / "defaults" / "runtime_traces"
    trace = json.loads((trace_dir / "latest_ai_input.json").read_text(encoding="utf-8"))
    assert trace["blocked"] == []
    assert trace["provider_payload_summary"]["context_segment_count"] == 2
    context_segment_ids = [segment["id"] for segment in trace["effective_input"]["context_segments"]]
    assert context_segment_ids == ["retrieval:knowledge.results", "memory:conversation.recalled_memory"]
    ChatStore._instance = None


def test_explicit_computer_tool_selection_marks_user_requested_computer_use(
    monkeypatch,
    tmp_path: Path,
    defaultspack_conversation_owner,
) -> None:
    user_data_root = tmp_path / "user_data"
    chat_store_path = tmp_path / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data_root))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(chat_store_path))
    ChatStore._instance = None

    conversation = ChatStore().create_conversation(model="stub/default")
    monkeypatch.setattr("domain.chat.run_request.enrich_messages", _fake_enrich_messages)
    monkeypatch.setattr("domain.chat.run_request.route_model_request", lambda request: _Decision("stub/default"))
    monkeypatch.setattr(
        "domain.chat.run_request.get_model_capabilities",
        lambda model: {
            "supports_image_input": False,
            "supports_vision": False,
            "supports_tool_calling": True,
            "supports_thinking": True,
        },
    )
    computer_tool = {
        "tool_id": "computer_use",
        "name": "computer_use",
        "schema": {"parameters": {"type": "object"}},
        "requires_runtime_capabilities": ["runtime.user_requested_computer_use"],
    }
    from domain.chat import run_request as run_request_module

    monkeypatch.setattr(
        run_request_module.ToolRegistry,
        "list_tools",
        lambda self: [computer_tool],
    )
    monkeypatch.setattr(
        run_request_module.ToolRegistry,
        "get",
        lambda self, tool_name: computer_tool if tool_name == "computer_use" else None,
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {
                "role": "user",
                "content": "Use the selected desktop tool.",
                "metadata": {
                    "profile_id": "defaultspack.operations_company",
                    "selected_tools": ["computer_use"],
                },
            },
            "tools": ["computer_use"],
            "params": {
                "tool_policy": {"selected_tools": ["computer_use"]},
                "tool_selection": {
                    "mode": "manual",
                    "include": ["computer_use"],
                    "scope": "turn",
                    "must_use": True,
                },
            },
        },
        {"profile_id": "default-profile", "developer_mode": True},
    )

    assert prepared.request_context["profile_id"] == "defaults"
    assert prepared.request_context["ignored_requested_profile_id"] == "defaultspack.operations_company"
    assert prepared.request_context["user_text"] == "Use the selected desktop tool."
    assert "Use the selected desktop tool." in prepared.request_context["conversation_user_text"]
    assert prepared.request_context["user_requested_computer_use"] is True
    assert prepared.tools_called == ["computer_use"]
    assert "runtime.user_requested_computer_use" in prepared.request_context[
        "runtime_capability_snapshot"
    ]["runtime_capabilities"]
    ChatStore._instance = None


def test_requested_legacy_profile_does_not_expand_connected_tools(
    monkeypatch,
) -> None:
    from domain.capability import catalog as capability_catalog
    from domain.chat import run_request as run_request_module

    monkeypatch.setattr(
        capability_catalog,
        "effective_pack_ids",
        lambda: frozenset({"defaultspack", "rumi_operations_company_pack"}),
    )
    run_request_module._profile_snapshot.cache_clear()
    stale_runtime_profile = {
        "profile_id": "default-profile",
        "defaultspack": {
            "agents": {
                "client_manager": {
                    "node_instance_id": "client_manager",
                    "node_id": "defaultspack.agent",
                    "tools": ["web_search"],
                }
            }
        },
    }

    runtime_profile, agent_id = _runtime_profile_with_policy_connected_tools(
        stale_runtime_profile,
        profile_id="defaultspack.operations_company",
        agent_id="client_manager",
    )

    assert runtime_profile["profile_id"] == "defaultspack.operations_company"
    assert agent_id == "client_manager"
    assert runtime_profile["defaultspack"]["agents"]["client_manager"]["tools"] == [
        "web_search"
    ]
    run_request_module._profile_snapshot.cache_clear()
