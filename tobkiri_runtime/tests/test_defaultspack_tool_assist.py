from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_v4_tool_dispatch")


def test_tool_recommender_prefers_related_tools():
    from domain.chat.tool_recommender import recommend_tool_ids

    tools = [
        {
            "tool_id": "web_search",
            "name": "Web Search",
            "summary": "Search the web for current weather and news.",
            "tags": ["search", "web"],
        },
        {
            "tool_id": "coding_file_write",
            "name": "Write File",
            "summary": "Write files in the workspace.",
            "tags": ["coding", "write"],
        },
    ]

    assert recommend_tool_ids("webで今日のweatherを検索して", tools, limit=2) == ["web_search"]


def test_tool_recommender_uses_mcp_metadata_and_input_schema():
    from domain.chat.tool_recommender import recommend_tool_ids

    tools = [
        {
            "tool_id": "mcp__filesystem__read_file",
            "name": "mcp_fs_read_file",
            "summary": "",
            "tags": ["mcp"],
            "schema": {
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"description": "workspace file path to read"},
                    },
                }
            },
            "metadata": {
                "source": "mcp",
                "server_id": "filesystem",
                "server_name": "filesystem",
                "mcp_tool_name": "read_file",
                "description": "Read files from the workspace through MCP.",
            },
        },
        {"tool_id": "calculator", "summary": "Compute arithmetic.", "tags": ["math"]},
    ]

    assert recommend_tool_ids("MCP filesystemでworkspaceのfileをreadして", tools, limit=2) == [
        "mcp__filesystem__read_file"
    ]


def test_tool_recommender_uses_skill_metadata():
    from domain.chat.tool_recommender import recommend_tool_ids

    tools = [
        {
            "tool_id": "hatch_pet_tool",
            "name": "Pet Builder",
            "summary": "Create animated pets.",
            "skills": ["hatch-pet"],
            "metadata": {"skills": ["spritesheet", "pet animation"]},
            "ui": {"keywords": "hatch pet sprite atlas"},
        },
        {"tool_id": "web_search", "summary": "Search web pages.", "tags": ["web"]},
    ]

    assert recommend_tool_ids("hatch-pet skillでsprite atlasを作って", tools, limit=2) == [
        "hatch_pet_tool"
    ]


def test_tool_recommender_expands_japanese_coding_file_terms():
    from domain.chat.tool_recommender import recommend_tool_ids

    tools = [
        {
            "tool_id": "coding_file_patch",
            "name": "Patch File",
            "summary": "Patch and edit a workspace source file.",
            "tags": ["coding", "file", "patch"],
        },
        {"tool_id": "web_search", "summary": "Search web pages.", "tags": ["web"]},
    ]

    assert (
        recommend_tool_ids("コードを編集してファイルを修正", tools, limit=2)[0]
        == "coding_file_patch"
    )


def test_tool_search_returns_overview_and_schema_from_docs(tmp_path):
    from domain.chat.tool_recommender import search_tools

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    (tmp_path / "SKILL.md").write_text(
        "Use this tool for parquet schema extraction and dataset docs.", encoding="utf-8"
    )
    tools = [
        {
            "tool_id": "dataset_schema",
            "name": "Dataset Schema",
            "summary": "",
            "tags": [],
            "schema": {
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"description": "dataset path"}},
                }
            },
            "metadata": {"manifest_path": str(manifest)},
        }
    ]

    overview = search_tools("parquet docs", tools, include_schema=False)
    schema = search_tools("parquet docs", tools, include_schema=True)

    assert overview[0]["tool_id"] == "dataset_schema"
    assert overview[0]["usage"]["phase"] == "overview"
    assert "schema" not in overview[0]
    assert schema[0]["schema"]["parameters"]["properties"]["path"]["description"] == "dataset path"


def test_effective_tool_assist_defaults_to_auto_and_migrates_legacy_all():
    from domain.chat.tool_recommender import effective_tool_assist_mode

    assert effective_tool_assist_mode({}) == "auto"
    assert effective_tool_assist_mode({"tools": {"tool_assist_mode": "all"}}) == "auto"
    assert effective_tool_assist_mode({"tools": {"tool_assist_mode": "auto"}}) == "auto"
    assert effective_tool_assist_mode({"tools": {"tool_assist_mode": "vector"}}) == "vector"
    assert (
        effective_tool_assist_mode({"tools": {"tool_assist_mode": "all_schemas"}}) == "all_schemas"
    )


def test_tool_loading_defaults_to_vector_and_only_always_is_eager():
    from domain.tool.loading import normalize_tool_loading_mode, split_tools_by_loading

    assert normalize_tool_loading_mode(None) == "vector"
    assert normalize_tool_loading_mode("all") == "vector"
    assert normalize_tool_loading_mode("vector") == "vector"
    assert normalize_tool_loading_mode("always") == "always"

    always, vector = split_tools_by_loading(
        [
            {"tool_id": "tool_names", "loading": "always"},
            {"tool_id": "web_search"},
            {"tool_id": "calculator", "metadata": {"loading": "all"}},
        ]
    )

    assert [tool["tool_id"] for tool in always] == ["tool_names"]
    assert [tool["tool_id"] for tool in vector] == ["web_search", "calculator"]


def test_tool_search_is_eager_discovery_fallback():
    from domain.tool.loading import tool_loading_mode
    from domain.tool.registry import ToolRegistry

    tool = ToolRegistry().get("tool_search")

    assert tool is not None
    assert tool_loading_mode(tool) == "always"


def test_tool_discovery_fallback_prompt_points_to_names_and_search():
    from domain.chat import run_request

    prompt = run_request._tool_discovery_fallback_prompt([
        {"tool_id": "tool_names"},
        {"tool_id": "tool_search"},
    ])

    assert "tool_names" in prompt
    assert "tool_search" in prompt
    assert '"query":"coding"' in prompt


def test_select_relevant_keeps_always_tools_when_vector_matches_are_empty(monkeypatch):
    from blocks.tool import select_relevant

    fake_tools = [
        {"tool_id": "tool_names", "summary": "List tool names.", "loading": "always"},
        {"tool_id": "calculator", "summary": "Compute arithmetic.", "tags": ["math"]},
    ]
    captured = {}

    class FakeRegistry:
        def list_tools(self):
            return list(fake_tools)

    def fake_search_tools(query, tools, limit=8, threshold=0.0):
        del query, limit, threshold
        captured["candidate_ids"] = [tool["tool_id"] for tool in tools]
        return []

    monkeypatch.setattr(select_relevant, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(select_relevant, "search_tools", fake_search_tools)

    result = select_relevant.run({"query": "summarize this"}, {})

    assert result["status"] == "ok"
    assert [tool["tool_id"] for tool in result["data"]["tools"]] == ["tool_names"]
    assert result["data"]["always_tools"] == ["tool_names"]
    assert captured["candidate_ids"] == ["calculator"]


def test_run_request_all_schemas_tool_assist_exposes_every_tool_when_explicitly_configured(
    monkeypatch,
):
    from domain.chat import run_request

    fake_tools = [
        {"tool_id": "web_search", "summary": "Search web pages."},
        {"tool_id": "calculator", "summary": "Compute arithmetic."},
    ]

    class FakeRegistry:
        def list_tools(self):
            return list(fake_tools)

        def get(self, tool_id):
            return next((tool for tool in fake_tools if tool["tool_id"] == tool_id), None)

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "effective_tool_assist_mode", lambda **_kwargs: "all_schemas")

    resolved, unknown = run_request._resolve_selected_tools(None, user_text="anything", context={})

    assert unknown == []
    assert [tool["tool_id"] for tool in resolved] == ["web_search", "calculator"]


def test_run_request_vector_tool_assist_recommends_when_tools_are_not_selected(monkeypatch):
    from domain.chat import run_request

    fake_tools = [
        {
            "tool_id": "web_search",
            "name": "Web Search",
            "summary": "Search web pages and recent weather.",
            "tags": ["web", "search"],
        },
        {
            "tool_id": "calculator",
            "name": "Calculator",
            "summary": "Compute arithmetic.",
            "tags": ["math"],
        },
    ]

    class FakeRegistry:
        def list_tools(self):
            return list(fake_tools)

        def get(self, tool_id):
            return next((tool for tool in fake_tools if tool["tool_id"] == tool_id), None)

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "effective_tool_assist_mode", lambda **_kwargs: "vector")
    monkeypatch.setattr(run_request, "tool_assist_limit", lambda **_kwargs: 4)

    context = {}
    resolved, unknown = run_request._resolve_selected_tools(
        None,
        user_text="今日のweatherをwebで検索して",
        context=context,
    )

    assert unknown == []
    assert [tool["tool_id"] for tool in resolved] == ["web_search"]
    assert context["tool_assist"]["mode"] == "vector"


def test_run_request_mimo_profile_filters_runtime_tools_before_auto_recommendation(monkeypatch):
    from domain.chat import run_request

    fake_tools = [
        {
            "tool_id": "coding_file_read",
            "name": "Read File",
            "summary": "Read workspace source files.",
            "tags": ["coding", "file", "read"],
        },
        {
            "tool_id": "coding_file_search",
            "name": "Search File",
            "summary": "Search workspace files.",
            "tags": ["coding", "file", "search"],
        },
        {
            "tool_id": "sandbox_exec",
            "name": "Sandbox Exec",
            "summary": "Execute shell commands in a sandbox.",
            "tags": ["shell", "execute"],
        },
    ]

    class FakeRegistry:
        def list_tools(self):
            return list(fake_tools)

        def get(self, tool_id):
            return next((tool for tool in fake_tools if tool["tool_id"] == tool_id), None)

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "effective_tool_assist_mode", lambda **_kwargs: "auto")
    monkeypatch.setattr(run_request, "tool_assist_limit", lambda **_kwargs: 4)

    context = {"profile_id": "defaultspack.mimo_coding_company"}
    resolved, unknown = run_request._resolve_selected_tools(
        None,
        user_text="ファイル名つきで短く返して。必要ならtoolで確認して。",
        context=context,
    )

    assert unknown == []
    assert [tool["tool_id"] for tool in resolved] == ["coding_file_read", "coding_file_search"]
    assert context["tool_assist"]["mode"] == "auto"


def test_run_request_auto_tool_assist_keeps_always_tools_and_vector_matches(monkeypatch):
    from domain.chat import run_request

    fake_tools = [
        {"tool_id": "tool_names", "summary": "List tool names.", "loading": "always"},
        {
            "tool_id": "web_search",
            "name": "Web Search",
            "summary": "Search web pages and recent weather.",
            "tags": ["web", "search"],
        },
        {"tool_id": "calculator", "summary": "Compute arithmetic.", "tags": ["math"]},
    ]

    class FakeRegistry:
        def list_tools(self):
            return list(fake_tools)

        def get(self, tool_id):
            return next((tool for tool in fake_tools if tool["tool_id"] == tool_id), None)

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "effective_tool_assist_mode", lambda **_kwargs: "auto")
    monkeypatch.setattr(run_request, "tool_assist_limit", lambda **_kwargs: 4)

    context = {}
    resolved, unknown = run_request._resolve_selected_tools(
        None,
        user_text="今日のweatherをwebで検索して",
        context=context,
    )

    assert unknown == []
    assert [tool["tool_id"] for tool in resolved] == ["tool_names", "web_search"]
    assert context["tool_assist"]["mode"] == "auto"
    assert context["tool_assist"]["always_tools"] == ["tool_names"]


def test_run_request_tool_assist_off_keeps_unselected_tools_empty(monkeypatch):
    from domain.chat import run_request

    class FakeRegistry:
        def list_tools(self):
            return [{"tool_id": "web_search", "summary": "Search the web"}]

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "effective_tool_assist_mode", lambda **_kwargs: "off")

    resolved, unknown = run_request._resolve_selected_tools(None, user_text="search", context={})

    assert resolved == []
    assert unknown == []


def test_run_request_explicit_empty_selected_tools_blocks_inferred_computer_tools():
    from domain.chat import run_request

    updated = run_request._with_inferred_tools(
        {
            "tools": [],
            "params": {"tool_policy": {"selected_tools": []}},
            "message": {"metadata": {"selected_tools": []}},
        },
        ["computer_use", "browser_computer"],
    )

    assert updated["tools"] == []


def test_run_request_metadata_selected_tools_disables_auto_recommendation(monkeypatch):
    from domain.chat import run_request

    class FakeRegistry:
        def list_tools(self):
            return [{"tool_id": "web_search", "summary": "Search the web"}]

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "resolve_runtime_profile_context", lambda context: context or {})
    monkeypatch.setattr(run_request, "filter_tool_definitions_for_runtime_profile", lambda tools, *_args, **_kwargs: tools)
    monkeypatch.setattr(run_request, "adapt_tool_definitions", lambda tools: tools)

    raw_tools, _provider_tools, tool_context = run_request._available_tools(
        {},
        {"message": {"metadata": {"selected_tools": []}}},
        user_text="search the web",
    )

    assert raw_tools == []
    assert tool_context["tool_selection"]["mode"] == "none"


def test_run_request_tool_selection_auto_preserves_settings_driven_selection(monkeypatch):
    from domain.chat import run_request

    class FakeRegistry:
        def list_tools(self):
            return [{"tool_id": "web_search", "name": "Web Search", "summary": "Search the web"}]

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "_read_frontend_settings", lambda: {"tools": {"selection_strategy": "lexical"}})
    monkeypatch.setattr(run_request, "resolve_runtime_profile_context", lambda context: context or {})
    monkeypatch.setattr(run_request, "filter_tool_definitions_for_runtime_profile", lambda tools, *_args, **_kwargs: tools)
    monkeypatch.setattr(run_request, "adapt_tool_definitions", lambda tools: tools)

    raw_tools, _provider_tools, tool_context = run_request._available_tools(
        {},
        {"params": {"tool_selection": {"mode": "auto"}}},
        user_text="search the web",
    )

    assert [tool["tool_id"] for tool in raw_tools] == ["web_search"]
    assert tool_context["tool_selection"]["mode"] == "auto"


def test_run_request_tool_selection_auto_merges_inferred_tools(monkeypatch):
    from domain.chat import run_request

    updated = run_request._with_inferred_tools(
        {"params": {"tool_selection": {"mode": "auto"}}},
        ["computer_use", "browser_computer"],
    )

    assert updated["tools"] == ["computer_use", "browser_computer"]

    class FakeRegistry:
        def list_tools(self):
            return [
                {"tool_id": "computer_use", "summary": "Computer control"},
                {"tool_id": "browser_computer", "summary": "Browser computer control"},
            ]

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "_read_frontend_settings", lambda: {"tools": {"selection_strategy": "all_schemas"}})
    monkeypatch.setattr(run_request, "resolve_runtime_profile_context", lambda context: context or {})
    monkeypatch.setattr(run_request, "filter_tool_definitions_for_runtime_profile", lambda tools, *_args, **_kwargs: tools)
    monkeypatch.setattr(run_request, "adapt_tool_definitions", lambda tools: tools)

    raw_tools, _provider_tools, _tool_context = run_request._available_tools(
        {"principal_capabilities": ["developer"]},
        updated,
        user_text="open Chrome",
    )

    assert [tool["tool_id"] for tool in raw_tools] == ["computer_use", "browser_computer"]


def test_run_request_tool_selection_none_blocks_auto_and_inferred_tools(monkeypatch):
    from domain.chat import run_request

    updated = run_request._with_inferred_tools(
        {"params": {"tool_selection": {"mode": "none"}}},
        ["computer_use", "browser_computer"],
    )

    assert "tools" not in updated

    class FakeRegistry:
        def list_tools(self):
            return [{"tool_id": "computer_use", "summary": "Computer control"}]

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "resolve_runtime_profile_context", lambda context: context or {})
    monkeypatch.setattr(run_request, "filter_tool_definitions_for_runtime_profile", lambda tools, *_args, **_kwargs: tools)
    monkeypatch.setattr(run_request, "adapt_tool_definitions", lambda tools: tools)

    raw_tools, _provider_tools, tool_context = run_request._available_tools(
        {},
        {"params": {"tool_selection": {"mode": "none"}}},
        user_text="open Chrome",
    )

    assert raw_tools == []
    assert tool_context["tool_selection"]["mode"] == "none"


def test_run_request_tool_selection_manual_does_not_require_tool_choice(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "use the calculator if needed"},
            "params": {"tool_selection": {"mode": "manual", "include": ["calculator"]}},
        },
        {},
    )

    assert prepared.params.get("tool_choice") != "required"
    ChatStore._instance = None


def test_run_request_tool_selection_must_use_requires_tool_choice(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "must use calculator"},
            "params": {
                "tool_selection": {"mode": "manual", "include": ["calculator"], "must_use": True}
            },
        },
        {"principal_capabilities": ["developer"]},
    )

    assert prepared.params["tool_choice"] == {
        "type": "function",
        "function": {"name": "calculator"},
    }
    ChatStore._instance = None


def test_run_request_tool_selection_must_use_rejects_unknown_only_tools(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    try:
        prepare_chat_run(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "must use the selected tool"},
                "params": {
                    "tool_selection": {
                        "mode": "manual",
                        "include": ["definitely_missing_tool"],
                        "must_use": True,
                    }
                },
            },
            {},
        )
        raise AssertionError("must_use should fail when no eligible tool remains")
    except ValueError as exc:
        assert "must_use requires at least one eligible tool" in str(exc)
    finally:
        ChatStore._instance = None


def test_run_request_rejects_unimplemented_or_conflicting_tool_selection():
    from domain.chat.run_request import validate_chat_run_input

    def payload(tool_selection):
        return {
            "conversation_id": "conv-1",
            "message": {"role": "user", "content": "hello"},
            "params": {"tool_selection": tool_selection},
        }

    assert validate_chat_run_input(payload({"mode": "review"})) is None
    assert validate_chat_run_input(payload({"mode": "auto", "scope": "conversation"})) is None
    assert validate_chat_run_input(payload({"mode": "auto", "review": True})) is None
    assert "mode=none" in validate_chat_run_input(payload({"mode": "none", "must_use": True}))
    assert "mode=none" in validate_chat_run_input(
        payload({"mode": "manual", "include": [], "must_use": True})
    )
    assert "cannot include tools" in validate_chat_run_input(
        payload({"mode": "none", "include": ["web_search"]})
    )
    assert "mode must be" in validate_chat_run_input(payload({"mode": "sometimes"}))


def test_run_request_tool_selection_manual_empty_normalizes_to_none(monkeypatch):
    from domain.chat import run_request

    class FakeRegistry:
        def list_tools(self):
            return [{"tool_id": "web_search", "summary": "Search the web"}]

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "resolve_runtime_profile_context", lambda context: context or {})
    monkeypatch.setattr(run_request, "filter_tool_definitions_for_runtime_profile", lambda tools, *_args, **_kwargs: tools)
    monkeypatch.setattr(run_request, "adapt_tool_definitions", lambda tools: tools)

    _raw_tools, _provider_tools, tool_context = run_request._available_tools(
        {},
        {"params": {"tool_selection": {"mode": "manual", "include": []}}},
        user_text="search",
    )

    assert _raw_tools == []
    assert tool_context["tool_selection"]["mode"] == "none"


def test_run_request_tool_selection_exclude_wins_over_include(monkeypatch):
    from domain.chat import run_request

    class FakeRegistry:
        def list_tools(self):
            return [
                {"tool_id": "web_search", "summary": "Search the web"},
                {"tool_id": "calculator", "summary": "Compute arithmetic"},
            ]

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "resolve_runtime_profile_context", lambda context: context or {})
    monkeypatch.setattr(run_request, "filter_tool_definitions_for_runtime_profile", lambda tools, *_args, **_kwargs: tools)
    monkeypatch.setattr(run_request, "adapt_tool_definitions", lambda tools: tools)

    raw_tools, _provider_tools, tool_context = run_request._available_tools(
        {"principal_capabilities": ["developer"]},
        {
            "params": {
                "tool_selection": {
                    "mode": "manual",
                    "include": ["web_search", "calculator"],
                    "exclude": ["web_search"],
                }
            }
        },
        user_text="search",
    )

    assert [tool["tool_id"] for tool in raw_tools] == ["calculator"]
    assert tool_context["tool_selection"]["exclude"] == [{"kind": "tool", "id": "web_search"}]


def test_run_request_tool_selection_takes_priority_over_legacy_tools(monkeypatch):
    from domain.chat import run_request

    class FakeRegistry:
        def list_tools(self):
            return [
                {"tool_id": "web_search", "summary": "Search the web"},
                {"tool_id": "calculator", "summary": "Compute arithmetic"},
            ]

    monkeypatch.setattr(run_request, "ToolRegistry", lambda: FakeRegistry())
    monkeypatch.setattr(run_request, "resolve_runtime_profile_context", lambda context: context or {})
    monkeypatch.setattr(run_request, "filter_tool_definitions_for_runtime_profile", lambda tools, *_args, **_kwargs: tools)
    monkeypatch.setattr(run_request, "adapt_tool_definitions", lambda tools: tools)

    raw_tools, _provider_tools, _tool_context = run_request._available_tools(
        {"principal_capabilities": ["developer"]},
        {
            "tools": ["web_search"],
            "params": {
                "tool_selection": {"mode": "manual", "include": ["calculator"]},
                "tool_policy": {"selected_tools": ["web_search"]},
            },
        },
        user_text="search",
    )

    assert [tool["tool_id"] for tool in raw_tools] == ["calculator"]


def test_run_request_selected_shell_tool_respects_profile_policy_yolo():
    from domain.chat import run_request

    raw_tools, provider_tools, _tool_context = run_request._available_tools(
        {
            "principal_capabilities": ["developer"],
            "profile_policy": {
                "yolo_mode": True,
                "allow_shell": True,
                "allow_file_write": True,
                "write_actions_require_approval": False,
            }
        },
        {
            "tools": ["coding_terminal_exec"],
            "params": {
                "tool_policy": {
                    "selected_tools": ["coding_terminal_exec"],
                    "yolo_mode": True,
                    "allow_shell": True,
                    "allow_file_write": True,
                    "write_actions_require_approval": False,
                }
            },
        },
        user_text="run coding_terminal_exec",
    )

    assert [tool["tool_id"] for tool in raw_tools] == ["coding_terminal_exec"]
    assert provider_tools[0]["function"]["name"] == "coding_terminal_exec"


def test_prepare_chat_run_promotes_tool_policy_tool_choice(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "use the calculator"},
            "tools": [],
            "params": {"tool_policy": {"selected_tools": [], "tool_choice": "required"}},
        },
        {},
    )

    assert prepared.params["tool_choice"] == "required"
    ChatStore._instance = None


def test_assistant_tool_history_preserves_reasoning_content():
    from blocks.chat.send import _append_assistant_tool_use_message

    messages = []

    _append_assistant_tool_use_message(
        messages,
        [
            {
                "type": "tool_use",
                "id": "call_exec",
                "name": "coding_terminal_exec",
                "input": {"command": "echo ok"},
            }
        ],
        reasoning_content="I need the terminal result.",
    )

    assert messages == [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "I need the terminal result.",
            "tool_calls": [
                {
                    "id": "call_exec",
                    "type": "function",
                    "function": {
                        "name": "coding_terminal_exec",
                        "arguments": '{"command": "echo ok"}',
                    },
                }
            ],
        }
    ]


def test_coding_tools_get_larger_default_tool_limit():
    from domain.chat.stream_engine import _default_tool_limit_for_connected_tools

    assert _default_tool_limit_for_connected_tools(4, {"coding_terminal_exec"}) == 12
    assert _default_tool_limit_for_connected_tools(4, {"coding_file_write"}) == 12
    assert _default_tool_limit_for_connected_tools(4, {"calculator"}) == 4
    assert _default_tool_limit_for_connected_tools(2, {"coding_terminal_exec"}) == 2


def test_stream_preserves_omitted_tools_for_auto_selection():
    from blocks.chat import stream

    original = {
        "conversation_id": "conv-1",
        "message": {"role": "user", "content": "hello"},
        "params": {"tool_policy": {}},
    }

    updated = stream._input_with_default_empty_tools(original)

    assert updated is original
    assert "tools" not in original


def test_stream_preserves_explicit_selected_tools_without_tools_field():
    from blocks.chat import stream

    original = {
        "conversation_id": "conv-1",
        "message": {"role": "user", "content": "hello"},
        "params": {"tool_policy": {"selected_tools": ["web_search"]}},
    }

    assert stream._input_with_default_empty_tools(original) is original
