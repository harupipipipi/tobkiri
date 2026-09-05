from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.contract


def _tools():
    return [
        {
            "tool_id": "web_search",
            "name": "Web Search",
            "summary": "Search the web.",
            "tags": ["web", "search"],
            "action_class": "search",
        },
        {
            "tool_id": "github_issue_search",
            "name": "GitHub Issues",
            "summary": "Search GitHub issues and pull requests.",
            "tags": ["github", "issue"],
            "action_class": "search",
            "metadata": {"service_id": "github"},
            "schema": {
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                }
            },
        },
        {
            "tool_id": "coding_file_read",
            "name": "Read File",
            "summary": "Read a workspace file.",
            "tags": ["coding", "file"],
            "action_class": "read",
        },
    ]


def test_raw_tool_target_requires_developer_mode_even_when_profile_connected():
    from domain.chat.tool_selection_schema import ToolSelectionRequest
    from domain.chat.tool_selection_service import ToolSelectionService

    with pytest.raises(PermissionError, match="developer capability"):
        ToolSelectionService(settings={}).select(
            "read the file",
            _tools(),
            selection=ToolSelectionRequest(
                mode="manual",
                include=[{"kind": "tool", "id": "coding_file_read"}],
            ),
            context={"profile_authorized_tool_targets": ["coding_file_read"]},
        )


def test_verified_text_mention_allows_only_the_exact_tool_target():
    from domain.chat.tool_selection_schema import ToolSelectionRequest
    from domain.chat.tool_selection_service import ToolSelectionService

    decision = ToolSelectionService(settings={}).select(
        "@Read File read the file",
        _tools(),
        selection=ToolSelectionRequest(
            mode="manual",
            include=[{"kind": "tool", "id": "coding_file_read"}],
        ),
        context={"verified_explicit_tool_ids": ["coding_file_read"]},
    )

    assert [tool["tool_id"] for tool in decision.selected_tools] == [
        "coding_file_read"
    ]


def test_all_schemas_exposes_every_schema_without_recommendations():
    from domain.chat.tool_selection_schema import ToolSelectionRequest
    from domain.chat.tool_selection_service import ToolSelectionService

    decision = ToolSelectionService(settings={"tools": {"selection_strategy": "all_schemas"}}).select(
        "show me the project state",
        _tools(),
        selection=ToolSelectionRequest(mode="auto", strategy="all_schemas"),
        context={"developer_mode": True},
    )

    assert [tool["tool_id"] for tool in decision.selected_tools] == [
        "web_search",
        "github_issue_search",
        "coding_file_read",
    ]
    assert decision.provider_schema_count == 3
    assert decision.recommendations == []


def test_all_with_hints_exposes_every_schema_and_keeps_recommendations(monkeypatch):
    from domain.chat import tool_selection_orchestrator
    from domain.chat.tool_selection_schema import ToolSelectionRequest
    from domain.chat.tool_selection_service import ToolSelectionService

    captured = {}

    def fake_call_model(input_data, context, *, call_handler=None):
        del context, call_handler
        captured["question"] = input_data["question"]
        return {
            "status": "ok",
            "output": {
                "selected_tools": [
                    {"tool_id": "github_issue_search", "confidence": 0.91, "reason": "GitHub context"}
                ]
            },
        }

    class FakeEmbeddingIndex:
        def search(self, user_text, tools, *, limit, backend="auto", model=""):
            del user_text, tools, limit, backend, model
            return {
                "tool_ids": ["github_issue_search"],
                "results": [],
                "stage": "semantic",
                "cache_hit": False,
                "catalog_hash": "fake",
                "duration_ms": 1,
            }

    monkeypatch.setattr(tool_selection_orchestrator, "call_model", fake_call_model)
    monkeypatch.setattr("domain.chat.tool_selection_service.ToolEmbeddingIndex", lambda: FakeEmbeddingIndex())

    decision = ToolSelectionService(settings={"tools": {"selection_strategy": "all_with_hints"}}).select(
        "check GitHub issues",
        _tools(),
        selection=ToolSelectionRequest(mode="auto", strategy="all_with_hints"),
        context={"developer_mode": True},
    )

    assert [tool["tool_id"] for tool in decision.selected_tools] == [
        "web_search",
        "github_issue_search",
        "coding_file_read",
    ]
    assert decision.provider_schema_count == 3
    assert [item.tool_id for item in decision.recommendations] == ["github_issue_search"]
    assert decision.metrics["recommended_tools"][0]["reason"] == "GitHub context"
    assert "web_search" in captured["question"]
    assert "github_issue_search" in captured["question"]
    assert "coding_file_read" in captured["question"]


def test_catalog_ai_direct_sends_every_compact_candidate_to_selector(monkeypatch):
    from domain.chat import tool_selection_orchestrator
    from domain.chat.tool_selection_schema import ToolSelectionRequest
    from domain.chat.tool_selection_service import ToolSelectionService

    captured = {}

    def fake_call_model(input_data, context, *, call_handler=None):
        del context, call_handler
        captured["question"] = input_data["question"]
        return {
            "status": "ok",
            "output": {
                "selected_tools": [
                    {"tool_id": "web_search", "confidence": 0.8, "reason": "web search"}
                ]
            },
        }

    class FakeEmbeddingIndex:
        def search(self, user_text, tools, *, limit, backend="auto", model=""):
            del user_text, tools, limit, backend, model
            return {
                "tool_ids": ["web_search"],
                "results": [],
                "stage": "semantic",
                "cache_hit": False,
                "catalog_hash": "fake",
                "duration_ms": 1,
            }

    monkeypatch.setattr(tool_selection_orchestrator, "call_model", fake_call_model)
    monkeypatch.setattr("domain.chat.tool_selection_service.ToolEmbeddingIndex", lambda: FakeEmbeddingIndex())

    decision = ToolSelectionService(settings={"tools": {"selection_strategy": "catalog_ai", "catalog_ai_direct_limit": 20}}).select(
        "search the web and GitHub",
        _tools(),
        selection=ToolSelectionRequest(mode="auto", strategy="catalog_ai"),
        context={"developer_mode": True},
    )

    assert decision.stage == "catalog_ai_direct"
    assert decision.candidate_count == 3
    question = captured["question"]
    assert "web_search" in question
    assert "github_issue_search" in question
    assert "coding_file_read" in question
    assert "Candidate tools:" in question
    assert "properties" not in question


def test_catalog_ai_uses_full_catalog_even_above_direct_limit(monkeypatch):
    from domain.chat import tool_selection_orchestrator
    from domain.chat.tool_selection_schema import ToolSelectionRequest
    from domain.chat.tool_selection_service import ToolSelectionService

    captured = {}

    def fake_call_model(input_data, context, *, call_handler=None):
        del context, call_handler
        captured["question"] = input_data["question"]
        return {
            "status": "ok",
            "output": {"selected_tools": [{"tool_id": "coding_file_read"}]},
        }

    class FakeEmbeddingIndex:
        def search(self, user_text, tools, *, limit, backend="auto", model=""):
            del user_text, tools, limit, backend, model
            return {
                "tool_ids": ["web_search"],
                "results": [],
                "stage": "semantic",
                "cache_hit": False,
                "catalog_hash": "fake",
                "duration_ms": 1,
            }

    monkeypatch.setattr(tool_selection_orchestrator, "call_model", fake_call_model)
    monkeypatch.setattr("domain.chat.tool_selection_service.ToolEmbeddingIndex", lambda: FakeEmbeddingIndex())

    decision = ToolSelectionService(
        settings={"tools": {"selection_strategy": "catalog_ai", "catalog_ai_direct_limit": 1}}
    ).select(
        "read project files",
        _tools(),
        selection=ToolSelectionRequest(mode="auto", strategy="catalog_ai"),
        context={"developer_mode": True},
    )

    assert decision.candidate_count == 3
    assert [tool["tool_id"] for tool in decision.selected_tools] == ["coding_file_read"]
    assert "web_search" in captured["question"]
    assert "github_issue_search" in captured["question"]
    assert "coding_file_read" in captured["question"]


def test_explicit_tool_helper_model_does_not_force_fast_route(monkeypatch):
    from domain.chat import tool_selection_orchestrator
    from domain.chat.tool_selection_schema import ToolSelectionRequest
    from domain.chat.tool_selection_service import ToolSelectionService

    captured = {}

    def fake_call_model(input_data, context, *, call_handler=None):
        del context, call_handler
        captured["model_hint"] = input_data["model_hint"]
        captured["required_capabilities"] = input_data["required_capabilities"]
        return {
            "status": "ok",
            "model": "custom/slow-helper",
            "output": {
                "selected_tools": [
                    {"tool_id": "web_search", "confidence": 0.8, "reason": "web search"}
                ]
            },
        }

    class FakeEmbeddingIndex:
        def search(self, user_text, tools, *, limit, backend="auto", model=""):
            del user_text, tools, limit, backend, model
            return {
                "tool_ids": ["web_search"],
                "results": [],
                "stage": "semantic",
                "cache_hit": False,
                "catalog_hash": "fake",
                "duration_ms": 1,
            }

    monkeypatch.setattr(tool_selection_orchestrator, "call_model", fake_call_model)
    monkeypatch.setattr("domain.chat.tool_selection_service.ToolEmbeddingIndex", lambda: FakeEmbeddingIndex())

    decision = ToolSelectionService(
        settings={
            "models": {"utility_models": {"tool_selector": "custom/slow-helper"}},
            "tools": {"selection_strategy": "catalog_ai", "catalog_ai_direct_limit": 20},
        }
    ).select(
        "search the web",
        _tools(),
        selection=ToolSelectionRequest(mode="auto", strategy="catalog_ai"),
        context={"developer_mode": True},
    )

    assert captured["model_hint"] == "custom/slow-helper"
    assert captured["required_capabilities"] == []
    assert decision.metrics["selector_model"] == "custom/slow-helper"


def test_all_with_hints_prompt_includes_selector_recommendations():
    from domain.chat import run_request

    prompt = run_request._tool_selection_hints_prompt(
        {
            "tool_selection": {
                "strategy": "all_with_hints",
                "metrics": {
                    "recommended_tools": [
                        {"tool_id": "github_issue_search", "confidence": 0.91, "reason": "GitHub context"}
                    ]
                },
            }
        }
    )

    assert "Tool selection hints for all_with_hints strategy" in prompt
    assert "github_issue_search" in prompt
    assert "GitHub context" in prompt


def test_semantic_auto_resolves_configured_embedding_model(monkeypatch):
    from domain.chat import tool_selection_service as service_module
    from domain.chat.tool_selection_schema import ToolSelectionRequest

    captured = {}

    class FakeEmbeddingIndex:
        def search(self, user_text, tools, *, limit, backend="auto", model=""):
            del user_text, tools, limit, backend
            captured["model"] = model
            return {
                "tool_ids": ["web_search"],
                "results": [],
                "stage": "semantic",
                "cache_hit": False,
                "catalog_hash": "fake",
                "duration_ms": 1,
            }

    monkeypatch.setattr(service_module, "ToolEmbeddingIndex", lambda: FakeEmbeddingIndex())
    monkeypatch.setattr(
        service_module,
        "search_models",
        lambda filters: {
            "models": [
                {
                    "profile_id": "google/text-embedding-004",
                    "qualified_model_id": "google/text-embedding-004",
                    "type": "embedding",
                    "configured": True,
                }
            ],
            "filters_applied": filters,
        },
    )

    decision = service_module.ToolSelectionService(
        settings={
            "tools": {
                "selection_strategy": "semantic",
                "embedding_model": "",
                "auto_discover_embedding_model": True,
            }
        }
    ).select(
        "search the web",
        _tools(),
        selection=ToolSelectionRequest(mode="auto", strategy="semantic"),
    )

    assert captured["model"] == "google/text-embedding-004"
    assert [tool["tool_id"] for tool in decision.selected_tools] == ["web_search"]


def test_semantic_default_does_not_scan_provider_catalog(monkeypatch):
    from domain.chat import tool_selection_service as service_module

    def unexpected_search(_filters):
        raise AssertionError("provider catalog must not be scanned in the chat hot path")

    monkeypatch.setattr(service_module, "search_models", unexpected_search)

    service = service_module.ToolSelectionService(
        settings={"tools": {"selection_strategy": "semantic", "embedding_model": ""}}
    )

    assert service._embedding_model() == ""


def test_embedding_index_calls_ai_client_embed_with_selected_model(tmp_path, monkeypatch):
    from domain.chat import tool_embedding_index

    calls = []

    class FakeAIClient:
        def embed(self, model, texts):
            calls.append((model, list(texts)))
            if len(texts) == 2:
                return {"embeddings": [[1.0, 0.0], [0.0, 1.0]]}
            return {"embeddings": [[1.0, 0.0]]}

    monkeypatch.setattr(tool_embedding_index, "AIClient", lambda: FakeAIClient())

    result = tool_embedding_index.ToolEmbeddingIndex(pack_root=tmp_path).search(
        "search the web",
        _tools()[:2],
        limit=1,
        model="google/text-embedding-004",
    )

    assert result["stage"] == "semantic"
    assert result["tool_ids"] == ["web_search"]
    assert calls[0][0] == "google/text-embedding-004"
    assert calls[1][0] == "google/text-embedding-004"


def test_conversation_tool_preferences_mode_overrides_default_turn_selection():
    from domain.chat.tool_selection_schema import ToolSelectionRequest
    from domain.chat.tool_selection_service import ToolSelectionService

    decision = ToolSelectionService(settings={"tools": {"selection_strategy": "all_schemas"}}).select(
        "search the web",
        _tools(),
        selection=ToolSelectionRequest(mode="auto", scope="turn", source="tool_selection"),
        context={
            "conversation_tool_preferences": {
                "mode": "none",
                "include": [{"kind": "service", "id": "github"}],
            },
            "developer_mode": True,
        },
    )

    assert decision.mode == "none"
    assert decision.selected_tools == []
    assert decision.provider_schema_count == 0


def test_settings_permissions_auto_confirm_block_and_service_overrides():
    from domain.tool.permission_resolver import ToolPermissionResolver

    resolver = ToolPermissionResolver(
        {
            "tools": {
                "standard_permissions": {
                    "create": "auto",
                    "update": "confirm",
                    "delete": "auto",
                },
                "service_permission_overrides": {
                    "github": {"update": "auto"},
                },
            }
        }
    )

    assert resolver.resolve({"tool_id": "doc_create", "action_class": "create"})["permission"] == "auto"
    assert resolver.resolve({"tool_id": "doc_update", "action_class": "update"})["permission"] == "confirm"
    assert resolver.resolve({"tool_id": "github_update_issue", "action_class": "update", "metadata": {"service_id": "github"}})["permission"] == "auto"
    assert resolver.resolve({"tool_id": "file_delete", "action_class": "delete"})["permission"] == "confirm"
    assert resolver.resolve({"tool_id": "external_send", "action_class": "send", "requires_approval": True})["permission"] == "confirm"


def test_browser_computer_is_computer_service_for_overrides():
    from domain.tool.permission_resolver import ToolPermissionResolver
    from domain.tool.service_catalog import ToolServiceCatalog, infer_service_id

    tool = {
        "tool_id": "browser_computer",
        "name": "browser_computer",
        "summary": "Control the browser and computer screen",
        "action_class": "computer",
    }
    resolver = ToolPermissionResolver(
        {
            "tools": {
                "standard_permissions": {"computer": "confirm"},
                "service_permission_overrides": {"computer": {"computer": "block"}},
            }
        }
    )

    assert infer_service_id(tool) == "computer"
    assert ToolServiceCatalog.compact_record(tool)["service_id"] == "computer"
    assert resolver.resolve(tool)["service_id"] == "computer"
    assert resolver.resolve(tool)["permission"] == "block"


def test_profile_write_and_high_risk_flags_do_not_escalate_read_tools():
    from domain.tool.permission_resolver import ToolPermissionResolver

    resolver = ToolPermissionResolver(
        {
            "tools": {
                "standard_permissions": {
                    "read": "auto",
                    "search": "auto",
                    "update": "auto",
                }
            }
        }
    )
    policy = {
        "write_actions_require_approval": True,
        "high_risk_tools_require_approval": True,
    }

    assert resolver.resolve({"tool_id": "coding_file_read", "action_class": "read"}, context={"profile_policy": policy})["permission"] == "auto"
    assert resolver.resolve({"tool_id": "web_search", "action_class": "search"}, context={"profile_policy": policy})["permission"] == "auto"
    assert resolver.resolve({"tool_id": "coding_file_write", "action_class": "update"}, context={"profile_policy": policy})["permission"] == "confirm"
    assert resolver.resolve({"tool_id": "secret_scan", "action_class": "read", "risk": "critical"}, context={"profile_policy": policy})["permission"] == "confirm"


def test_frontend_settings_block_wins_over_server_approval_full_access_and_safe_memo(monkeypatch):
    from domain.tool import executor as executor_mod

    class Resolver:
        def resolve(self, tool, *, context=None):
            return {
                "tool_id": "memo_note_upsert",
                "service_id": "memory",
                "action_class": "update",
                "permission": "block",
                "minimum_permission": "auto",
                "sources": [{"source": "tool:memo_note_upsert", "value": "block"}],
            }

    monkeypatch.setattr(executor_mod, "ToolPermissionResolver", lambda: Resolver())
    monkeypatch.setattr(executor_mod, "_context_has_tool_server_approval", lambda context: True)
    monkeypatch.setattr(executor_mod, "is_safe_first_party_memo_tool", lambda tool: True)

    _, response = executor_mod._preflight_frontend_tool_permission(
        "memo_note_upsert",
        {"tool_id": "memo_note_upsert", "name": "memo_note_upsert", "action_class": "update"},
        {"note": "x"},
        {},
        {"full_access": True},
    )

    assert response["is_error"] is True
    assert response["rejected_by_tool_permission_policy"] is True
    assert response["tool_permission_policy_decision"]["status"] == "denied"


def test_frontend_settings_confirm_can_be_satisfied_by_server_approval(monkeypatch):
    from domain.tool import executor as executor_mod

    class Resolver:
        def resolve(self, tool, *, context=None):
            return {
                "tool_id": "coding_file_write",
                "service_id": "coding",
                "action_class": "update",
                "permission": "confirm",
                "minimum_permission": "confirm",
                "sources": [{"source": "tool:coding_file_write", "value": "confirm"}],
            }

    monkeypatch.setattr(executor_mod, "ToolPermissionResolver", lambda: Resolver())
    monkeypatch.setattr(executor_mod, "_context_has_tool_server_approval", lambda context: True)

    _, response = executor_mod._preflight_frontend_tool_permission(
        "coding_file_write",
        {"tool_id": "coding_file_write", "name": "coding_file_write", "action_class": "update"},
        {"path": "app.py", "content": "x"},
        {},
        {},
    )

    assert response is None


def test_frontend_settings_resolver_failure_fails_closed_for_write_tools(monkeypatch):
    from domain.tool import executor as executor_mod
    from domain.safety import approval

    approval.reset_approval_state_for_tests()

    class Resolver:
        def resolve(self, tool, *, context=None):
            raise RuntimeError("settings unavailable")

    monkeypatch.setattr(executor_mod, "ToolPermissionResolver", lambda: Resolver())

    _, write_response = executor_mod._preflight_frontend_tool_permission(
        "coding_file_write",
        {"tool_id": "coding_file_write", "name": "coding_file_write", "action_class": "update"},
        {"path": "app.py", "content": "x"},
        {"conversation_id": "conv-settings-fail-closed"},
        {},
    )
    _, read_response = executor_mod._preflight_frontend_tool_permission(
        "coding_file_read",
        {"tool_id": "coding_file_read", "name": "coding_file_read", "action_class": "read"},
        {"path": "app.py"},
        {},
        {},
    )

    assert write_response["widget"]["type"] == "approval_request"
    assert write_response["widget"]["approval_required"] is True
    assert read_response is None


def test_full_tool_selection_trace_creates_hidden_child_conversation(
    tmp_path, monkeypatch, defaultspack_conversation_owner
):
    conversation_path = tmp_path / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(conversation_path))

    from domain.chat.store import ChatStore

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    assert conversation["id"]
    assert conversation["model"] == "stub/default"
    assert defaultspack_conversation_owner.get(conversation["id"]) is not None
    assert not conversation_path.exists()
    assert not (tmp_path / "traces").exists()


def test_summary_tool_selection_trace_does_not_persist_json(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_TOOL_SELECTION_TRACE_DIR", str(tmp_path / "traces"))

    from domain.chat import run_request
    from domain.chat.tool_selection_schema import ToolSelectionDecision

    context = {
        "conversation_id": "conv-summary",
        "_authenticated_principal": {"profile_id": "profile-alice"},
        "tool_selection": {"selection_id": "sel-summary", "strategy": "semantic"},
    }
    run_request._persist_tool_selection_trace(
        context,
        {"tools": {"selector_trace": "summary"}},
        ToolSelectionDecision(
            selection_id="sel-summary",
            mode="auto",
            strategy="semantic",
            stage="semantic",
            selected_tools=[{"tool_id": "web_search"}],
        ),
        user_text="search the web",
        trace={"selection_id": "sel-summary", "input": "technical trace"},
    )

    trace_dir = tmp_path / "traces"
    assert context["tool_selection"]["trace_mode"] == "summary"
    assert "trace_conversation_id" not in context["tool_selection"]
    assert not trace_dir.exists() or list(trace_dir.glob("*.json")) == []


def test_tool_selection_summary_trace_requires_owner_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_TOOL_SELECTION_TRACE_DIR", str(tmp_path / "traces"))

    from blocks.tool import selection_trace
    from domain.chat.tool_selection_trace import ToolSelectionTraceStore

    store = ToolSelectionTraceStore()
    store.save(
        {
            "selection_id": "trace-authorized",
            "owner_profile_id": "profile-alice",
            "conversation_id": "conv-a",
            "selected_tool_ids": ["web_search"],
            "expires_at_epoch": 9_999_999_999,
        }
    )

    ok_result = selection_trace.run(
        {"trace_id": "trace-authorized"},
        {"_authenticated_principal": {"profile_id": "profile-alice"}, "conversation_id": "conv-a"},
    )
    assert ok_result["status"] == "ok"
    assert ok_result["data"]["selected_tool_ids"] == ["web_search"]

    wrong_profile = selection_trace.run(
        {"trace_id": "trace-authorized"},
        {"_authenticated_principal": {"profile_id": "profile-bob"}, "conversation_id": "conv-a"},
    )
    assert wrong_profile["status"] == "error"
    assert wrong_profile["error"]["code"] == "FORBIDDEN"

    wrong_conversation = selection_trace.run(
        {"trace_id": "trace-authorized"},
        {"_authenticated_principal": {"profile_id": "profile-alice"}, "conversation_id": "conv-b"},
    )
    assert wrong_conversation["status"] == "error"
    assert wrong_conversation["error"]["code"] == "FORBIDDEN"

    store.save(
        {
            "selection_id": "trace-expired",
            "owner_profile_id": "profile-alice",
            "conversation_id": "conv-a",
            "expires_at_epoch": 1,
        }
    )
    expired = selection_trace.run(
        {"trace_id": "trace-expired"},
        {"_authenticated_principal": {"profile_id": "profile-alice"}, "conversation_id": "conv-a"},
    )
    assert expired["status"] == "error"
    assert expired["error"]["code"] == "EXPIRED"


def test_tool_preferences_are_profile_scoped_and_schema_checked(
    tmp_path, monkeypatch, defaultspack_conversation_owner
):
    conversation_path = tmp_path / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(conversation_path))

    from domain.chat.store import ChatStore

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(
        model="stub/default",
        metadata={"owner_profile_id": "profile-alice"},
    )

    assert conversation["metadata"]["owner_profile_id"] == "profile-alice"
    assert defaultspack_conversation_owner.get(conversation["id"]) is not None
    assert not conversation_path.exists()


def test_tool_preferences_claim_owner_for_unowned_conversation(
    tmp_path, monkeypatch, defaultspack_conversation_owner
):
    conversation_path = tmp_path / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(conversation_path))

    from domain.chat.store import ChatStore

    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    assert conversation["id"]
    assert defaultspack_conversation_owner.get(conversation["id"]) is not None
    assert not conversation_path.exists()


def test_tool_selection_preview_snapshot_overrides_tampered_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_TOOL_SELECTION_PREVIEW_DIR", str(tmp_path / "previews"))

    from domain.chat import run_request
    from domain.chat.tool_selection_preview import ToolSelectionPreviewStore

    ToolSelectionPreviewStore().save(
        {
            "preview_id": "preview-a",
            "owner_profile_id": "profile-alice",
            "conversation_id": "conv-a",
            "expires_at_epoch": 9_999_999_999,
            "selection": {
                "mode": "review",
                "strategy": "catalog_ai",
                "scope": "turn",
                "include": [{"kind": "tool", "id": "web_search"}],
                "exclude": [],
                "must_use": True,
                "review": True,
            },
        }
    )
    selection = run_request.NormalizedToolSelection(
        mode="review",
        strategy="semantic",
        include=[{"kind": "tool", "id": "coding_file_read"}],
        preview_id="preview-a",
    )

    hydrated = run_request._apply_tool_selection_preview_snapshot(
        selection,
        {"_authenticated_principal": {"profile_id": "profile-alice"}},
        conversation_id="conv-a",
    )

    assert hydrated.mode == "review"
    assert hydrated.strategy == "catalog_ai"
    assert hydrated.include == [{"kind": "tool", "id": "web_search"}]
    assert hydrated.must_use is True
    assert hydrated.review is True
    assert hydrated.source == "tool_selection_preview"

    with pytest.raises(ValueError, match="NOT_FOUND"):
        run_request._apply_tool_selection_preview_snapshot(
            selection,
            {"_authenticated_principal": {"profile_id": "profile-alice"}},
            conversation_id="conv-a",
        )

    ToolSelectionPreviewStore().save(
        {
            "preview_id": "preview-b",
            "owner_profile_id": "profile-alice",
            "conversation_id": "conv-a",
            "expires_at_epoch": 9_999_999_999,
            "selection": {
                "mode": "review",
                "strategy": "catalog_ai",
                "scope": "turn",
                "include": [{"kind": "tool", "id": "web_search"}],
                "exclude": [],
                "must_use": True,
                "review": True,
            },
        }
    )
    with pytest.raises(ValueError, match="FORBIDDEN"):
        run_request._apply_tool_selection_preview_snapshot(
            run_request.NormalizedToolSelection(mode="review", preview_id="preview-b"),
            {"_authenticated_principal": {"profile_id": "profile-bob"}},
            conversation_id="conv-a",
        )


def test_tool_selection_preview_snapshot_rejects_payload_swap(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_TOOL_SELECTION_PREVIEW_DIR", str(tmp_path / "previews"))

    from domain.chat import run_request
    from domain.chat.tool_selection_preview import ToolSelectionPreviewStore, preview_payload_bindings

    original_input = {"message": {"content": "search cats"}, "params": {"model": "stub/default"}}
    ToolSelectionPreviewStore().save(
        {
            "preview_id": "preview-bound",
            "owner_profile_id": "profile-alice",
            "conversation_id": "conv-a",
            "expires_at_epoch": 9_999_999_999,
            "bindings": preview_payload_bindings(
                original_input,
                {"_authenticated_principal": {"profile_id": "profile-alice"}},
                user_text="search cats",
                model="stub/default",
            ),
            "selection": {
                "mode": "review",
                "strategy": "semantic",
                "scope": "turn",
                "include": [{"kind": "tool", "id": "web_search"}],
                "exclude": [],
                "must_use": False,
                "review": True,
            },
        }
    )

    with pytest.raises(ValueError, match="PAYLOAD_MISMATCH"):
        run_request._apply_tool_selection_preview_snapshot(
            run_request.NormalizedToolSelection(mode="review", preview_id="preview-bound"),
            {"_authenticated_principal": {"profile_id": "profile-alice"}},
            conversation_id="conv-a",
            input_data={"message": {"content": "search dogs"}, "params": {"model": "stub/default"}},
            user_text="search dogs",
            model="stub/default",
        )


def test_tool_selection_preview_api_persists_authorized_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_TOOL_SELECTION_PREVIEW_DIR", str(tmp_path / "previews"))

    from blocks.tool import selection_preview
    from domain.chat.tool_selection_preview import ToolSelectionPreviewStore
    from domain.chat.tool_selection_schema import ToolSelectionDecision

    class FakeRegistry:
        def list_tools(self):
            return _tools()

    class FakeSelectionService:
        def __init__(self, *args, **kwargs):
            pass

        def select(self, *args, **kwargs):
            return ToolSelectionDecision(
                selection_id="preview-from-api",
                mode="review",
                strategy="semantic",
                stage="semantic",
                selected_tools=[{"tool_id": "web_search"}],
            )

    monkeypatch.setattr(selection_preview, "ToolRegistry", FakeRegistry)
    monkeypatch.setattr(selection_preview, "ToolSelectionService", FakeSelectionService)

    result = selection_preview.run(
        {
            "conversation_id": "conv-a",
            "user_text": "search",
            "tool_selection": {"mode": "review", "strategy": "semantic"},
        },
        {"_authenticated_principal": {"profile_id": "profile-alice"}},
    )

    assert result["status"] == "ok"
    snapshot = ToolSelectionPreviewStore().get_authorized(
        "preview-from-api",
        {"_authenticated_principal": {"profile_id": "profile-alice"}, "conversation_id": "conv-a"},
    )
    assert snapshot["selection"]["include"] == [{"kind": "tool", "id": "web_search"}]
    assert snapshot["owner_profile_id"] == "profile-alice"


def test_available_tools_falls_back_when_selector_service_fails(monkeypatch):
    from domain.chat import run_request
    from domain.chat.tool_selection_schema import ToolSelectionDecision

    class FakeRegistry:
        def list_tools(self):
            return _tools()

    def fake_filter(tools, runtime_profile, **kwargs):
        del runtime_profile, kwargs
        return tools

    def fake_select(self, user_text, tools, *, selection, context=None):
        del self, user_text, context
        if getattr(selection, "strategy", "") != "lexical":
            raise RuntimeError("selector exploded")
        return ToolSelectionDecision(
            selection_id="fallback-selection",
            mode=getattr(selection, "mode", "auto"),
            strategy="lexical",
            stage="lexical",
            selected_tools=[tool for tool in tools if tool["tool_id"] == "web_search"],
            candidate_count=len(tools),
            selected_count=1,
        )

    monkeypatch.setattr(run_request, "ToolRegistry", FakeRegistry)
    monkeypatch.setattr(run_request, "filter_tool_definitions_for_runtime_profile", fake_filter)
    monkeypatch.setattr(run_request, "_read_frontend_settings", lambda: {"tools": {"selection_strategy": "catalog_ai"}})
    monkeypatch.setattr(run_request.ToolSelectionService, "select", fake_select)

    raw, provider, context = run_request._available_tools(
        {"runtime_profile": {"connected_tools": ["web_search", "github_issue_search"]}},
        {"params": {"tool_selection": {"mode": "auto", "strategy": "catalog_ai"}}},
        user_text="search the web",
    )

    assert [tool["tool_id"] for tool in raw] == ["web_search"]
    assert [tool["function"]["name"] for tool in provider] == ["web_search"]
    assert context["tool_selection"]["stage"] == "selection_failed_lexical_fallback"
    assert context["tool_selection"]["fallbacks"][0]["reason"] == "selector exploded"
