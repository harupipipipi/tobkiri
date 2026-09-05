from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

_APPROVAL_MODULE_NAMES = (
    "domain.safety.approval",
    "domain.safety.approval_state_json",
    "domain.safety.approval_store",
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.usefixtures(
        "defaultspack_conversation_owner",
        "defaultspack_v4_tool_dispatch",
    ),
]


class _Manager:
    def get_system_prompt(self):
        return "System prompt"

    def get_prompt(self, prompt_id):
        return None

    def get_prompt_by_name(self, prompt_id):
        return None


def _setup_store(tmp_path, monkeypatch):
    from domain.chat import store as facade
    from domain.chat import run_request
    from domain.chat.store import ChatStore
    from domain.tool.registry import ToolRegistry
    from ecosystem.rumi_conversation_store_pack.runtime.store import (
        ConversationStore,
    )

    configured_user_data = os.environ.get("RUMI_USER_DATA")
    configured_path = Path(configured_user_data) if configured_user_data else None
    if configured_path is None or not (configured_path == tmp_path or tmp_path in configured_path.parents):
        monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "user_data"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"))
    ChatStore._instance = None
    ToolRegistry._instance = None
    run_request._profile_snapshot.cache_clear()
    resolve_runtime_profile_context = run_request.resolve_runtime_profile_context

    def resolve_verified_developer_context(
        context: dict[str, Any],
    ) -> dict[str, Any]:
        resolved = resolve_runtime_profile_context(context)
        resolved.setdefault("principal_capabilities", ["developer"])
        return resolved

    monkeypatch.setattr(
        run_request,
        "resolve_runtime_profile_context",
        resolve_verified_developer_context,
    )
    owner = ConversationStore("defaults", user_data_root=tmp_path / "user_data")

    def invoke(contract_id: str, operation: str, payload: dict[str, Any]) -> Any:
        if contract_id == facade.CONVERSATION:
            if operation == "list":
                return owner.snapshot()
            if operation == "get":
                return owner.get(str(payload.get("conversation_id") or ""))
        if contract_id == facade.MESSAGE and operation == "get":
            conversation = owner.get(str(payload.get("conversation_id") or ""))
            return next(
                (
                    message
                    for message in (conversation or {}).get("messages", [])
                    if message.get("id") == payload.get("message_id")
                ),
                None,
            )
        if contract_id == facade.CONVERSATION_MANAGE:
            if operation == "create":
                return owner.create(
                    payload["conversation"],
                    expected_revision=int(payload["expected_revision"]),
                )
            if operation == "update":
                return owner.update(
                    str(payload["conversation_id"]),
                    payload["patch"],
                    expected_conversation_revision=int(
                        payload["expected_conversation_revision"]
                    ),
                )
            if operation == "delete":
                return owner.delete(
                    str(payload["conversation_id"]),
                    expected_conversation_revision=int(
                        payload["expected_conversation_revision"]
                    ),
                )
        if contract_id == facade.MESSAGE_MANAGE:
            if operation == "append":
                return owner.append_message(
                    str(payload["conversation_id"]),
                    payload["message"],
                    expected_conversation_revision=int(
                        payload["expected_conversation_revision"]
                    ),
                )
            if operation in {"update", "delete"}:
                return owner.mutate_message(
                    str(payload["conversation_id"]),
                    str(payload["message_id"]),
                    expected_conversation_revision=int(
                        payload["expected_conversation_revision"]
                    ),
                    patch=payload.get("patch"),
                    delete=operation == "delete",
                )
        raise AssertionError(f"unexpected contract call: {contract_id}/{operation}")

    monkeypatch.setattr(facade, "_invoke", invoke)
    store = ChatStore()
    monkeypatch.setattr("domain.chat.run_request.get_manager", lambda: _Manager())
    monkeypatch.setattr(
        "domain.chat.run_request.get_model_capabilities",
        lambda _model: {"supports_tool_calling": True},
    )
    monkeypatch.setattr(
        "domain.chat.run_request.enrich_messages",
        lambda messages, system_prompt, conversation_id, user_text, manager: {
            "knowledge_text": "",
            "memory_text": "",
            "knowledge_results": [],
            "memory_results": [],
            "enriched_prompt": system_prompt,
        },
    )
    return store


def _external_provider_tool_names(prepared) -> set[str]:
    return {
        tool["function"]["name"]
        for tool in prepared.provider_tools
        if isinstance(tool, dict)
        and isinstance(tool.get("function"), dict)
        and tool["function"].get("name") != "assistant_progress"
    }


def _reload_approval_modules_for_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load fresh approval modules without leaking split module identities.

    This test intentionally exercises approval state against a temporary
    SQLite database. Restoring both ``sys.modules`` and the parent package
    attributes keeps already-imported production aliases (for example the
    coding contract adapter) bound to the same module objects after teardown.
    """

    safety_package = sys.modules.get("domain.safety")
    for module_name in _APPROVAL_MODULE_NAMES:
        if module_name in sys.modules:
            monkeypatch.setitem(sys.modules, module_name, sys.modules[module_name])
        else:
            monkeypatch.delitem(sys.modules, module_name, raising=False)
        sys.modules.pop(module_name, None)

        if safety_package is None:
            continue
        attribute = module_name.rsplit(".", 1)[-1]
        if hasattr(safety_package, attribute):
            monkeypatch.setattr(
                safety_package,
                attribute,
                getattr(safety_package, attribute),
                raising=False,
            )
        else:
            monkeypatch.delattr(safety_package, attribute, raising=False)


def _provider_tool_action_enum(prepared, tool_name: str) -> list[str]:
    for tool in prepared.provider_tools:
        function_def = tool.get("function") if isinstance(tool, dict) else {}
        if not isinstance(function_def, dict) or function_def.get("name") != tool_name:
            continue
        parameters = function_def.get("parameters")
        properties = parameters.get("properties") if isinstance(parameters, dict) else {}
        action_schema = properties.get("action") if isinstance(properties, dict) else {}
        return list(action_schema.get("enum") or []) if isinstance(action_schema, dict) else []
    return []


def test_prepare_chat_run_creates_message_chain_ir_and_context(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.ai_client.model_search import get_model_capabilities
    from domain.chat.store import ChatStore

    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(model="stub/default")
    store.add_message(conv["id"], {"role": "user", "content": [{"type": "text", "text": "old"}]})

    prepared = prepare_chat_run({"conversation_id": conv["id"], "message": {"content": "new"}}, {})

    assert prepared.user_message["content"] == [{"type": "text", "text": "new"}]
    assert prepared.standard_messages[0]["role"] == "system"
    assert str(prepared.standard_messages[0]["content"]).startswith("System prompt")
    assert prepared.standard_messages[1]["role"] == "system"
    assert "Current date/time:" in prepared.standard_messages[1]["content"]
    assert prepared.standard_messages[-1] == {"role": "user", "content": "new"}
    assert prepared.chat_ir.schema_version == "rumi.chat.ir.v2"
    assert prepared.provider_planning["model"] == "cerebras/gpt-oss-120b"
    selected_capabilities = get_model_capabilities(prepared.model)
    assert selected_capabilities is not None
    assert selected_capabilities["availability"]["status"] == "unconfigured"
    assert prepared.request_context["current_date"]
    assert prepared.request_context["conversation_workspace_dir"]
    assert prepared.tool_context["history_json_path"].endswith("history.json")
    assert prepared.request_context["chat_references"]["conversation_id"] == conv["id"]
    ChatStore._instance = None


def test_prepare_chat_run_persists_semantic_mention_metadata(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conversation = store.create_conversation(model="stub/default")
    mentions = [
        {
            "id": "web_search",
            "kind": "tool",
            "label": "Web Search",
            "syntax": "@Web Search",
        }
    ]

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {
                "content": "Use @Web Search",
                "metadata": {"mentions": mentions},
            },
        },
        {},
    )

    assert prepared.user_message["metadata"]["mentions"] == mentions
    reloaded = store.get_conversation(conversation["id"])
    assert reloaded is not None
    assert reloaded["messages"][-1]["metadata"]["mentions"] == mentions
    ChatStore._instance = None


def test_tool_selection_rejects_raw_tool_definition_dict():
    from domain.chat.run_request import validate_chat_run_input

    error = validate_chat_run_input(
        {
            "conversation_id": "conv",
            "message": {"content": "hello"},
            "params": {
                "tool_selection": {
                    "mode": "manual",
                    "include": [
                        {
                            "type": "function",
                            "function": {
                                "name": "attacker_tool",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                }
            },
        }
    )

    assert error == "params.tool_selection.include must contain only string IDs or {kind, id} targets"


def test_approval_followup_tool_is_explicit_only_after_signed_server_verification(
    monkeypatch,
):
    from domain.chat.run_request import _verified_approval_followup_tool_ids
    from domain.safety import approval

    monkeypatch.setattr(
        approval,
        "get_approval_request",
        lambda request_id: {
            "request_id": request_id,
            "status": "approved",
            "operation": "tool.repository_context_prepare",
            "args_hash": "args-hash",
            "details": {"tool_name": "repository_context_prepare"},
        },
    )
    monkeypatch.setattr(
        approval,
        "verify_execution_token",
        lambda token, operation, args_hash, consume=False: SimpleNamespace(
            valid=(
                token == "signed-token"
                and operation == "tool.repository_context_prepare"
                and args_hash == "args-hash"
                and consume is False
            ),
            request_id="apr-followup",
        ),
    )
    request = {
        "message": {
            "metadata": {
                "approval_followup": {
                    "approval_token": "signed-token",
                    "request_id": "apr-followup",
                    "tool_name": "repository_context_prepare",
                }
            }
        }
    }

    assert _verified_approval_followup_tool_ids(request) == [
        "repository_context_prepare"
    ]
    request["message"]["metadata"]["approval_followup"]["tool_name"] = (
        "coding_terminal_exec"
    )
    assert _verified_approval_followup_tool_ids(request) == []


def test_top_level_tools_raw_definition_does_not_bypass_verified_catalog(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(model="stub/default")

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {"content": "use the attacker tool"},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "attacker_tool",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        },
        {},
    )

    assert "attacker_tool" not in _external_provider_tool_names(prepared)
    assert prepared.tool_context["tool_selection"]["provider_compat_tool_ids"] == ["attacker_tool"]
    assert "attacker_tool" not in prepared.connected_tool_names
    ChatStore._instance = None


def test_prepare_chat_run_persists_sanitizes_and_inlines_attachments(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(model="stub/default")
    data_url = "data:image/png;base64," + base64.b64encode(b"abc").decode()

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "files",
                "attachments": [
                    {"id": "t", "name": "a.txt", "type": "text/plain", "content": "file text"},
                    {"id": "i", "name": "i.png", "type": "image/png", "size": 3, "dataUrl": data_url},
                ],
            },
        },
        {},
    )

    assert prepared.metadata["attachments"][1] == {"id": "i", "name": "i.png", "size": 3, "type": "image/png"}
    assert len(prepared.metadata["workspace_attachments"]) == 2
    assert any("file text" in block.get("text", "") for block in prepared.content if isinstance(block, dict))
    assert any(block.get("type") == "image_url" for block in prepared.content if isinstance(block, dict))
    assert any(block.type == "image_url" for message in prepared.chat_ir.messages for block in message.content)
    ChatStore._instance = None


def test_prepare_chat_run_current_turn_history_only_still_works(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(model="stub/default")
    store.add_message(conv["id"], {"role": "user", "content": [{"type": "text", "text": "old"}]})

    prepared = prepare_chat_run({"conversation_id": conv["id"], "message": {"content": "only"}}, {"chat_history_mode": "current_turn"})

    user_messages = [message for message in prepared.standard_messages if message.get("role") == "user"]
    assert user_messages == [{"role": "user", "content": "only"}]
    assert len(prepared.chat_ir.messages) == 1
    ChatStore._instance = None

def test_prepare_chat_run_maps_approval_followup_tokens_for_action_operation_and_computer_aliases(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(model="stub/default")
    store.add_message(conv["id"], {"role": "user", "content": "Operate ChatGPT Atlas"})
    runtime_resume_text = (
        "The user approved the pending browser/computer operation. "
        "Continue with the exact pending tool once."
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "continue",
            },
            "metadata": {
                "approval_followup": {
                    "tool_name": "computer_use",
                    "action": "apps",
                    "operation": "computer.apps",
                    "approval_token": "tok_followup",
                    "request_id": "apr_followup",
                },
                "runtime_content": runtime_resume_text,
            },
        },
        {},
    )

    assert prepared.request_context["user_text"] == "Operate ChatGPT Atlas"
    assert "continue" not in prepared.request_context["conversation_user_text"]
    user_messages = [message for message in prepared.standard_messages if message.get("role") == "user"]
    assert user_messages[-1]["content"] == "Operate ChatGPT Atlas"
    assert all(message.get("content") != "continue" for message in user_messages)
    assert all(message.get("content") != runtime_resume_text for message in user_messages)
    assert prepared.request_context["tool_approval_tokens"] == {
        "computer_use": "tok_followup",
        "browser_use": "tok_followup",
        "browser_computer": "tok_followup",
        "apps": "tok_followup",
        "computer.apps": "tok_followup",
        "apr_followup": "tok_followup",
    }


def test_prepare_chat_run_authority_resume_forces_job_resume_without_progress_tool(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(model="stub/default")
    store.add_message(conv["id"], {"role": "user", "content": "Open Google in Atlas"})
    runtime_resume_text = (
        "Runtime authority resume: model/API access is approved. "
        "Continue without mentioning approval."
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "Internal authority resume.",
                "metadata": {
                    "authority_followup": {
                        "request_id": "auth_1",
                        "permission_id": "model.invoke",
                        "hidden": True,
                    },
                    "chat_display": {
                        "hidden": True,
                        "reason": "authority_followup",
                    },
                    "runtime_content": runtime_resume_text,
                },
            },
            "tools": ["job_resume"],
        },
        {},
    )

    assert [tool["function"]["name"] for tool in prepared.provider_tools] == ["job_resume"]
    assert prepared.params["tool_choice"] == "required"
    assert prepared.request_context["authority_resume_followup"] is True
    assert prepared.request_context["user_text"] == "Open Google in Atlas"
    assert "Internal authority resume." not in prepared.request_context["conversation_user_text"]
    user_messages = [message for message in prepared.standard_messages if message.get("role") == "user"]
    assert user_messages[-1]["content"] == "Open Google in Atlas"
    assert all(message.get("content") != "Internal authority resume." for message in user_messages)
    assert all(message.get("content") != runtime_resume_text for message in user_messages)
    assert "assistant_progress_enabled" not in prepared.tool_context
    ChatStore._instance = None


def test_computer_use_preferences_target_chatgpt_atlas_as_app():
    from domain.chat.run_request import _computer_use_preferences_from_text

    preferences = _computer_use_preferences_from_text("ChatGPT Atlas を操作して YouTube を開いて")

    assert preferences["computer_use_target_app"] == "ChatGPT Atlas"
    assert preferences["computer_use_foreground_preferred"] is True
    assert "computer_use_target_title" not in preferences


def test_computer_use_runtime_prompt_prefers_open_url_for_explicit_browser_url():
    from domain.chat.run_request import _computer_use_runtime_prompt

    prompt = _computer_use_runtime_prompt(
        {
            "user_requested_computer_use": True,
            "computer_use_target_app": "ChatGPT Atlas",
            "user_text": "ChatGPT Atlas で https://www.google.com を開いて youtube を検索して",
        },
        [{"name": "browser_computer"}],
    )

    assert "browser.open_url" in prompt
    assert "first external action" in prompt
    assert "computer.context" in prompt
    assert "visible foreground computer-use run" in prompt
    assert "fallback=foreground" in prompt


def test_computer_use_runtime_prompt_requires_exact_complete_input_without_autocomplete_reliance():
    from domain.chat.run_request import _computer_use_runtime_prompt

    prompt = _computer_use_runtime_prompt(
        {
            "user_requested_computer_use": True,
            "user_text": "Search for youtube",
        },
        [{"name": "browser_computer"}],
    )

    assert "exactly and completely, character-for-character" in prompt
    assert "clear or replace existing content" in prompt
    assert "type the full literal" in prompt
    assert "rely on autocomplete/search suggestions" in prompt


def test_computer_use_runtime_prompt_requires_post_action_recovery_and_final_state_verification():
    from domain.chat.run_request import _computer_use_runtime_prompt

    prompt = _computer_use_runtime_prompt(
        {
            "user_requested_computer_use": True,
            "user_text": "Open a video and play it",
        },
        [{"name": "browser_computer"}],
    )

    assert "after every consequential action" in prompt
    assert "typing, Enter/submission, navigation" in prompt
    assert "last visually verified state" in prompt
    assert "requested final state is visually verified" in prompt
    assert "verify actual playback state" in prompt


def test_prepare_chat_run_shapes_browser_computer_schema_for_ordered_url_navigation(
    tmp_path, monkeypatch
):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(model="openai/gpt-4o-mini")

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "atlas browserで。google.com→input youtube→open youtube.com→いい動画を選んで再生",
                "metadata": {"selected_tools": ["browser_computer"]},
            },
            "tools": ["browser_computer"],
        },
        {},
    )

    actions = _provider_tool_action_enum(prepared, "browser_computer")
    assert actions
    assert actions[0] == "browser.open_url"
    assert "browser.session" not in actions
    assert "computer.context" not in actions
    assert "computer.apps" not in actions
    assert "computer.windows" not in actions
    assert "computer.doctor" not in actions
    assert "computer.type" in actions
    assert "computer.click" in actions
    assert prepared.request_context["user_requested_computer_use"] is True
    [restriction] = prepared.tool_context["provider_tool_schema_restrictions"]
    assert restriction["reason"] == "explicit_browser_navigation"
    assert "computer.context" in restriction["removed_actions"]
    ChatStore._instance = None


def test_prepare_chat_run_keeps_requested_browser_computer_when_profile_has_no_connected_tools(
    tmp_path, monkeypatch
):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(model="openai/gpt-4o-mini")
    runtime_profile = {
        "profile_id": "defaultspack.default",
        "defaultspack": {"agents": {"agent": {"tools": []}}},
    }

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "atlas browserで。google.com→input youtube→open youtube.com→いい動画を選んで再生",
                "metadata": {"selected_tools": ["browser_computer"]},
            },
            "tools": ["browser_computer"],
        },
        {"runtime_profile": runtime_profile, "agent_id": "agent"},
    )

    tool_names = _external_provider_tool_names(prepared)
    assert "browser_computer" in tool_names
    assert prepared.request_context["user_requested_computer_use"] is True
    assert prepared.tool_context.get("unselected_requested_tools") in (None, [])
    assert "browser_computer" in prepared.connected_tool_names
    ChatStore._instance = None


def test_prepare_chat_run_propagates_conversation_workspace_to_tool_context(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore
    from domain.coding.workspace_store import WorkspaceStore

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH",
        str(tmp_path / "coding_workspaces.json"),
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    workspace_root = tmp_path / "rumiai-root"
    workspace_root.mkdir()
    WorkspaceStore().create(workspace_root, workspace_id="rumiai-root")

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(
        model="stub/default",
        metadata={
            "workspace_id": "rumiai-root",
            "workspace_root": str(workspace_root),
        },
    )

    prepared = prepare_chat_run(
        {"conversation_id": conv["id"], "message": {"content": "git status"}},
        {},
    )

    assert prepared.request_context.get("workspace_id") == "rumiai-root"
    assert prepared.request_context.get("workspace_root") == str(workspace_root)
    assert prepared.tool_context.get("workspace_id") == "rumiai-root"
    assert prepared.tool_context.get("workspace_root") == str(workspace_root)
    ChatStore._instance = None


def test_prepare_chat_run_ignores_conversation_legacy_profile_policy(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        metadata={"profile_id": "defaultspack.mimo_coding_company"},
    )

    prepared = prepare_chat_run(
        {"conversation_id": conv["id"], "message": {"content": "look at stop path"}},
        {},
    )

    assert prepared.request_context.get("profile_id") == "defaults"
    assert prepared.request_context.get("ignored_requested_profile_id") == "defaultspack.mimo_coding_company"
    assert "profile_policy" not in prepared.request_context
    ChatStore._instance = None


def test_prepare_chat_run_does_not_trust_client_tool_policy_approval_bypass(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_APPROVAL_DB_PATH",
        str(tmp_path / "approval.sqlite3"),
    )
    _reload_approval_modules_for_probe(monkeypatch)

    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore
    import domain.safety.approval as approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(
        model="stub/default",
        metadata={"workspace_root": str(workspace_root)},
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "write a probe",
                "metadata": {
                    "workspace_root": str(workspace_root),
                    "selected_tools": ["coding_file_write"],
                },
            },
            "tools": ["coding_file_write"],
            "params": {
                "tool_policy": {
                    "selected_tools": ["coding_file_write"],
                    "action_approval_mode": "full",
                    "yolo_mode": True,
                    "allow_client_supplied_approved": True,
                    "direct_tool_execution": True,
                    "full_access": True,
                    "allow_shell": True,
                    "allow_file_write": True,
                    "write_actions_require_approval": False,
                    "tool_permission_policy": {
                        "tools": {"coding_file_write": "allow"},
                    },
                },
            },
        },
        {},
    )

    policy = prepared.request_context.get("profile_policy", {})
    assert policy["selected_tools"] == ["coding_file_write"]
    for key in (
        "action_approval_mode",
        "allow_client_supplied_approved",
        "allow_file_write",
        "allow_shell",
        "direct_tool_execution",
        "full_access",
        "tool_permission_policy",
        "yolo_mode",
        "write_actions_require_approval",
    ):
        assert key not in policy
    assert set(prepared.request_context["ignored_client_tool_policy_keys"]) == {
        "action_approval_mode",
        "allow_client_supplied_approved",
        "allow_file_write",
        "allow_shell",
        "direct_tool_execution",
        "full_access",
        "tool_permission_policy",
        "write_actions_require_approval",
        "yolo_mode",
    }

    result = ToolExecutor().execute(
        "coding_file_write",
        {
            "path": "api-bypass-probe.txt",
            "content": "should not write",
            "workspace_root": str(workspace_root),
        },
        prepared.tool_context,
    )

    assert result["is_error"] is False
    assert result["widget"]["approval_required"] is True
    assert not (workspace_root / "api-bypass-probe.txt").exists()
    ChatStore._instance = None


def test_approval_probe_restores_canonical_module_aliases() -> None:
    """A fresh approval probe must not split aliases used by coding blocks."""

    from domain.coding import contract_adapter
    import domain.safety.approval as approval

    assert contract_adapter.approval is approval


def test_prepare_chat_run_does_not_merge_legacy_workspace_profile(tmp_path, monkeypatch):
    from domain.chat.run_request import _profile_snapshot, prepare_chat_run
    from domain.chat.store import ChatStore

    user_data_root = tmp_path / "user_data"
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data_root))
    profile_dir = user_data_root / "profiles" / "defaultspack.mimo_coding_company"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.yaml").write_text(
        "profile_id: defaultspack.mimo_coding_company\nversion: 1\n",
        encoding="utf-8",
    )
    _profile_snapshot.cache_clear()

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        metadata={"profile_id": "defaultspack.mimo_coding_company"},
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {"content": "review stop path"},
            "tools": ["artifact_export", "coding_file_read"],
        },
        {},
    )

    assert prepared.request_context.get("profile_id") == "defaults"
    assert "profile_policy" not in prepared.request_context
    tool_names = _external_provider_tool_names(prepared)
    assert "coding_file_read" in tool_names
    assert "artifact_export" in tool_names
    _profile_snapshot.cache_clear()
    ChatStore._instance = None


def test_prepare_chat_run_marks_selected_terminal_unattached_when_profile_excludes_it(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(model="xiaomi-token-plan-sgp/mimo-v2.5-pro")
    runtime_profile = {
        "defaultspack": {
            "agents": {
                "client_manager": {
                    "tools": ["coding_file_read"],
                },
            },
        },
        "policy": {
            "tool_allowlist": ["coding_file_read"],
        },
    }

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "read files and run pwd",
                "metadata": {
                    "selected_tools": ["coding_file_read", "coding_terminal_exec"],
                },
            },
            "tools": ["coding_file_read", "coding_terminal_exec"],
        },
        {"runtime_profile": runtime_profile, "agent_id": "client_manager"},
    )

    assert prepared.tool_context["requested_tool_ids"] == ["coding_file_read", "coding_terminal_exec"]
    unselected = prepared.tool_context["unselected_requested_tools"]
    assert unselected[0]["tool_name"] == "coding_terminal_exec"
    assert unselected[0]["reason_code"] == "not_connected_to_profile"
    assert prepared.request_context.get("profile_policy", {}).get("allow_shell") is not True
    assert prepared.request_context.get("user_requested_shell_tool") is True
    filter_entries = {
        entry["tool_name"]: entry
        for entry in prepared.metadata["tool_filter_result"]
    }
    assert filter_entries["coding_terminal_exec"]["status"] == "blocked"
    assert filter_entries["coding_terminal_exec"]["reason_code"] == "not_connected_to_profile"
    tool_names = _external_provider_tool_names(prepared)
    assert "coding_file_read" in tool_names
    assert "coding_terminal_exec" not in tool_names
    ChatStore._instance = None


def test_prepare_chat_run_does_not_trust_runtime_yaml_as_profile_authority(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        metadata={"profile_id": "defaultspack.mimo_coding_company"},
    )
    runtime_profile = {
        "profile_id": "defaultspack.mimo_coding_company",
        "policy": {
            "allow_shell": True,
            "allow_file_write": True,
            "tool_allowlist": [
                "coding_file_read",
                "coding_terminal_exec",
                "coding_file_write",
            ],
        },
        "metadata": {
            "client_manager_agent_id": "client_manager",
        },
    }

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "smoke terminal attach",
                "metadata": {
                    "selected_tools": [
                        "coding_file_read",
                        "coding_terminal_exec",
                        "coding_file_write",
                    ],
                },
            },
            "tools": [
                "coding_file_read",
                "coding_terminal_exec",
                "coding_file_write",
            ],
        },
        {"runtime_profile": runtime_profile},
    )

    assert prepared.tool_context["agent_id"] == "agent"
    assert prepared.tool_context["runtime_profile"]["profile_id"] == "defaults"
    assert "unselected_requested_tools" not in prepared.tool_context
    assert _external_provider_tool_names(prepared) == {
        "coding_file_read",
        "coding_terminal_exec",
        "coding_file_write",
    }
    filter_entries = {
        entry["tool_name"]: entry
        for entry in prepared.metadata["tool_filter_result"]
    }
    assert filter_entries["coding_terminal_exec"]["status"] == "approval_required"
    ChatStore._instance = None


def test_prepare_chat_run_infers_raw_tool_mentions_as_turn_tools(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        metadata={"profile_id": "defaultspack.mimo_coding_company"},
    )
    runtime_profile = {
        "profile_id": "defaultspack.mimo_coding_company",
        "policy": {
            "allow_shell": True,
            "tool_allowlist": ["coding_terminal_exec"],
        },
        "metadata": {
            "client_manager_agent_id": "client_manager",
        },
    }

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "接続確認だけです。@coding_terminal_exec で pwd を実行して",
            },
        },
        {"runtime_profile": runtime_profile},
    )

    assert prepared.input_data["tools"] == ["coding_terminal_exec"]
    assert prepared.tool_context["requested_tool_ids"] == ["coding_terminal_exec"]
    assert prepared.tool_context["agent_id"] == "agent"
    assert "unselected_requested_tools" not in prepared.tool_context
    assert not prepared.request_context.get("user_requested_computer_use")
    assert prepared.request_context.get("user_requested_shell_tool") is True
    assert _external_provider_tool_names(prepared) == {"coding_terminal_exec"}
    assert "coding_terminal_exec" in prepared.connected_tool_names
    [filter_entry] = prepared.metadata["tool_filter_result"]
    assert filter_entry["tool_name"] == "coding_terminal_exec"
    assert filter_entry["status"] == "approval_required"
    ChatStore._instance = None


def test_prepare_chat_run_allows_explicit_shell_tool_request_to_attach(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(model="xiaomi-token-plan-sgp/mimo-v2.5-pro")

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "接続確認です。@coding_terminal_exec で pwd だけ実行して",
            },
        },
        {},
    )

    assert prepared.input_data["tools"] == ["coding_terminal_exec"]
    assert prepared.request_context.get("profile_policy", {}).get("allow_shell") is not True
    assert prepared.request_context.get("user_requested_shell_tool") is True
    assert "unselected_requested_tools" not in prepared.tool_context
    tool_names = _external_provider_tool_names(prepared)
    assert "coding_terminal_exec" in tool_names
    assert "coding_terminal_exec" in prepared.connected_tool_names
    [filter_entry] = prepared.metadata["tool_filter_result"]
    assert filter_entry["tool_name"] == "coding_terminal_exec"
    assert filter_entry["status"] == "approval_required"
    ChatStore._instance = None


def test_prepare_chat_run_infers_coding_pr_tools_from_broad_request(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        metadata={
            "mode": "coding",
            "workspace_root": str(tmp_path),
            "workspace_label": "repo",
        },
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "github.com/harupipipipi/rumiai/ にいい感じのPR出して。",
            },
        },
        {},
    )

    expected_tools = {
        "coding_file_list",
        "coding_file_search",
        "coding_file_read",
        "coding_file_write",
        "coding_terminal_exec",
        "coding_git_status",
        "coding_git_diff",
        "coding_git_commit",
        "coding_git_push",
    }
    assert expected_tools.issubset(set(prepared.input_data["tools"]))
    assert prepared.request_context.get("profile_policy", {}).get("allow_shell") is not True
    assert prepared.request_context.get("user_requested_shell_tool") is True
    unselected_names = {
        entry["tool_name"]
        for entry in prepared.tool_context.get("unselected_requested_tools", [])
    }
    assert "coding_terminal_exec" not in unselected_names
    tool_names = _external_provider_tool_names(prepared)
    assert expected_tools.issubset(tool_names)
    assert "coding_terminal_exec" in prepared.connected_tool_names
    filter_entries = {
        entry["tool_name"]: entry
        for entry in prepared.metadata["tool_filter_result"]
    }
    assert filter_entries["coding_terminal_exec"]["status"] == "approval_required"
    ChatStore._instance = None


def test_prepare_chat_run_keeps_inferred_pr_tools_with_auto_tool_selection(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        metadata={
            "mode": "agent",
            "workspace_root": str(tmp_path),
            "workspace_label": "repo",
        },
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "github.com/harupipipipi/rumiai/ にいい感じのPR出して。",
            },
            "params": {
                "tool_selection": {
                    "mode": "auto",
                    "include": [],
                    "exclude": [],
                    "scope": "turn",
                },
            },
        },
        {},
    )

    expected_tools = {
        "coding_file_list",
        "coding_file_search",
        "coding_file_read",
        "coding_file_write",
        "coding_terminal_exec",
        "coding_git_status",
        "coding_git_diff",
        "coding_git_commit",
        "coding_git_push",
    }
    assert prepared.tool_context["tool_selection"]["mode"] == "manual"
    assert expected_tools.issubset(set(prepared.tool_context["requested_tool_ids"]))
    unselected_names = {
        entry["tool_name"]
        for entry in prepared.tool_context.get("unselected_requested_tools", [])
    }
    assert "coding_terminal_exec" not in unselected_names
    tool_names = _external_provider_tool_names(prepared)
    assert "coding_file_read" in tool_names
    assert "coding_terminal_exec" in tool_names
    assert "coding_terminal_exec" in prepared.connected_tool_names
    filter_entries = {
        entry["tool_name"]: entry
        for entry in prepared.metadata["tool_filter_result"]
    }
    assert filter_entries["coding_terminal_exec"]["status"] == "approval_required"
    ChatStore._instance = None


def test_prepare_chat_run_authority_off_does_not_bypass_write_approval(
    tmp_path, monkeypatch
):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore
    from domain.tool.schema_adapter import max_tool_calls

    monkeypatch.setenv("RUMI_AUTHORITY_MODE", "off")
    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
        metadata={
            "mode": "agent",
            "workspace_root": str(tmp_path),
            "workspace_label": "repo",
        },
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "github.com/harupipipipi/rumiai/ にいい感じのPR出して。",
            },
            "params": {
                "tool_selection": {
                    "mode": "auto",
                    "include": [],
                    "exclude": [],
                    "scope": "turn",
                },
            },
        },
        {},
    )

    filter_status = {
        entry["tool_name"]: entry["status"]
        for entry in prepared.tool_context.get("tool_filter_result", [])
    }
    assert filter_status["coding_file_write"] == "approval_required"
    assert filter_status["coding_git_commit"] == "approval_required"
    assert filter_status["coding_git_push"] == "approval_required"
    assert max_tool_calls(prepared.tool_context) is None
    assert prepared.request_context.get("profile_policy", {}).get("allow_shell") is not True
    assert prepared.request_context.get("user_requested_shell_tool") is True
    ChatStore._instance = None


def test_prepare_chat_run_adds_requested_tool_to_existing_agent_profile(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(
        model="xiaomi-token-plan-sgp/mimo-v2.5-pro",
    )
    runtime_profile = {
        "defaultspack": {
            "agents": {
                "client_manager": {
                    "tools": ["coding_file_read"],
                },
            },
        },
    }

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "接続確認です。@coding_terminal_exec で pwd だけ実行して",
            },
            "params": {
                "tool_selection": {
                    "mode": "auto",
                    "include": [{"kind": "tool", "id": "coding_terminal_exec"}],
                    "scope": "turn",
                },
            },
        },
        {"runtime_profile": runtime_profile, "agent_id": "client_manager"},
    )

    connected_agent_tools = prepared.tool_context["runtime_profile"]["defaultspack"]["agents"][
        "client_manager"
    ]["tools"]
    assert connected_agent_tools == ["coding_file_read"]
    assert prepared.tool_context["unselected_requested_tools"][0]["tool_name"] == "coding_terminal_exec"
    assert prepared.tool_context["unselected_requested_tools"][0]["reason_code"] == "not_connected_to_profile"
    assert not prepared.request_context.get("user_requested_computer_use")
    tool_names = _external_provider_tool_names(prepared)
    assert "coding_terminal_exec" not in tool_names
    assert "coding_terminal_exec" not in prepared.connected_tool_names
    ChatStore._instance = None


def test_explicit_tool_selection_does_not_expand_agent_tool_scope():
    from domain.chat.run_request import (
        _runtime_profile_with_policy_connected_tools,
    )
    runtime_profile = {
        "packs": ["rumi_repository_context_pack"],
        "defaultspack": {
            "agents": {
                "client_manager": {
                    "tools": ["coding_file_read"],
                },
            },
        },
    }

    patched, agent_id = _runtime_profile_with_policy_connected_tools(
        runtime_profile,
        agent_id="client_manager",
    )

    assert agent_id == "client_manager"
    connected_agent_tools = patched["defaultspack"]["agents"][
        "client_manager"
    ]["tools"]
    assert connected_agent_tools == ["coding_file_read"]


def test_profile_snapshot_hydration_does_not_expand_agent_tool_scope(
    monkeypatch,
):
    import domain.chat.run_request as run_request
    monkeypatch.setattr(
        run_request,
        "_profile_snapshot",
        lambda profile_id: {
            "profile_id": profile_id,
            "packs": ["rumi_repository_context_pack"],
            "policy": {"capabilities": ["repository.context.prepare"]},
        },
    )

    patched, agent_id = run_request._runtime_profile_with_policy_connected_tools(
        {
            "profile_id": "default-profile",
            "defaultspack": {
                "agents": {
                    "client_manager": {
                        "tools": ["coding_file_read"],
                    },
                },
            },
        },
        profile_id="default-profile",
        agent_id="client_manager",
    )

    assert agent_id == "client_manager"
    assert patched["packs"] == ["rumi_repository_context_pack"]
    assert patched["defaultspack"]["agents"]["client_manager"]["tools"] == [
        "coding_file_read"
    ]


def test_prepare_chat_run_falls_back_to_selected_workspace_when_metadata_missing(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore
    from domain.coding.workspace_store import WorkspaceStore

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH",
        str(tmp_path / "coding_workspaces.json"),
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    workspace_root = tmp_path / "rumiai-root"
    workspace_root.mkdir()

    workspace_store = WorkspaceStore()
    workspace_store.create(workspace_root, workspace_id="rumiai-root")
    workspace_store.select("rumiai-root")

    store = _setup_store(tmp_path, monkeypatch)
    conv = store.create_conversation(
        model="stub/default",
        metadata={"profile_id": "defaultspack.mimo_coding_company"},
    )

    prepared = prepare_chat_run(
        {"conversation_id": conv["id"], "message": {"content": "git status"}},
        {},
    )

    assert prepared.request_context.get("workspace_id") == "rumiai-root"
    assert prepared.request_context.get("workspace_root") == str(workspace_root)
    assert prepared.tool_context.get("workspace_id") == "rumiai-root"
    assert prepared.tool_context.get("workspace_root") == str(workspace_root)
    ChatStore._instance = None
