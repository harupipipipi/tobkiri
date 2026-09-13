from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from ecosystem.defaultspack.backend.ai_client.provider_catalog import (
    list_model_catalog,
    list_profile_catalog,
    list_provider_catalog,
)
from ecosystem.defaultspack.backend.ai_client.provider_registry import ProviderRegistry
from ecosystem.defaultspack.domain.ai_client.api_key_store import PROVIDER_SECRET_KEYS


class _FakeInterfaceRegistry:
    def __init__(self) -> None:
        self.calls = []

    def register(self, key, value, meta=None):
        self.calls.append((key, value, meta))


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch, tmp_path):
    from ecosystem.defaultspack.backend.ai_client import provider_catalog
    from ecosystem.defaultspack.backend.tool import permission_policy as permission_policy_module
    from core_runtime import resolved_profile_scope
    try:
        from backend.tool import permission_policy as top_level_permission_policy_module
    except Exception:
        top_level_permission_policy_module = None

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_TOOL_PERMISSION_POLICY_PATH",
        str(tmp_path / "tool_permission_policy.json"),
    )
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_SECRETS_DIR",
        str(tmp_path / "secrets"),
    )
    # The provider catalog is selected by the resolved profile.  Keep this
    # unit group independent of the repository's persisted startup profile so
    # the fallback is exercised under the explicit model-catalog owner.
    monkeypatch.setattr(
        resolved_profile_scope,
        "effective_pack_ids",
        lambda: frozenset({"rumi_model_catalog_pack"}),
    )
    provider_env_names = {
        env_name
        for provider in list_provider_catalog()
        for env_name in (
            list(provider.get("env_vars") or [])
            + list(provider.get("base_url_envs") or [])
        )
        if str(env_name or "").strip()
    }
    provider_env_names.update(
        env_name
        for env_names in PROVIDER_SECRET_KEYS.values()
        for env_name in env_names
        if str(env_name or "").strip()
    )
    provider_env_names.update(
        {
            "GOOGLE_APPLICATION_CREDENTIALS",
            "OLLAMA_HOST",
            "LMSTUDIO_BASE_URL",
            "VLLM_BASE_URL",
            "LLAMACPP_BASE_URL",
            "OPENAI_COMPATIBLE_BASE_URL",
            "RUMI_DEFAULTSPACK_ENABLE_CLOUD_PROVIDERS",
            "RUMI_DEFAULTSPACK_ENABLE_LOCAL_PROVIDERS",
        }
    )

    for env_name in provider_env_names:
        monkeypatch.delenv(env_name, raising=False)
    permission_policy_module._POLICY_STORE = None
    if top_level_permission_policy_module is not None:
        top_level_permission_policy_module._POLICY_STORE = None
    provider_catalog._clear_runtime_inventory_cache()
    _reset_defaultspack_domain_singletons()
    yield
    permission_policy_module._POLICY_STORE = None
    if top_level_permission_policy_module is not None:
        top_level_permission_policy_module._POLICY_STORE = None
    provider_catalog._clear_runtime_inventory_cache()
    _reset_defaultspack_domain_singletons()


def _reset_defaultspack_domain_singletons() -> None:
    for module_name, class_name in (
        ("ecosystem.defaultspack.domain.ai_client.client", "AIClient"),
        ("domain.ai_client.client", "AIClient"),
        ("ecosystem.defaultspack.domain.tool.runtime_creator", "RuntimeToolCreator"),
        ("domain.tool.runtime_creator", "RuntimeToolCreator"),
        ("ecosystem.defaultspack.domain.tool.mcp_client", "McpClient"),
        ("domain.tool.mcp_client", "McpClient"),
        ("ecosystem.defaultspack.domain.tool.registry", "ToolRegistry"),
        ("domain.tool.registry", "ToolRegistry"),
    ):
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except Exception:
            continue
        cls._instance = None


def test_ai_and_tool_setup_register_new_foundation_routes():
    from ecosystem.defaultspack.blocks.ai import setup as ai_setup
    from ecosystem.defaultspack.blocks.tool import setup as tool_setup

    ai_registry = _FakeInterfaceRegistry()
    tool_registry = _FakeInterfaceRegistry()

    ai_setup.run({"interface_registry": ai_registry})
    tool_setup.run({"interface_registry": tool_registry})

    ai_routes = {(value["method"], value["pattern"]) for key, value, _ in ai_registry.calls if key == "io.http.route"}
    tool_routes = {(value["method"], value["pattern"]) for key, value, _ in tool_registry.calls if key == "io.http.route"}

    assert ("GET", "/api/ai/providers") in ai_routes
    assert ("GET", "/api/ai/models") in ai_routes
    assert ("GET", "/api/ai/profiles") in ai_routes

    assert ("GET", "/api/tools") in tool_routes
    assert ("GET", "/api/tools/names") in tool_routes
    assert ("POST", "/api/tools/invoke") in tool_routes
    assert ("POST", "/api/tools/mcp/connect") in tool_routes
    assert ("GET", "/api/tools/mcp") in tool_routes
    assert ("GET", "/api/tools/permissions") in tool_routes
    assert ("PUT", "/api/tools/permissions") in tool_routes
    assert ("POST", "/api/tools/permissions/check") in tool_routes


def test_tool_permission_routes_require_captured_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/tools/permissions",
        "tobkiri.tool-permission.v1",
        "defaultspack.tool-permission.list",
    )


def test_tool_permissions_run_dispatches_http_method_handlers(tmp_path, monkeypatch):
    from ecosystem.defaultspack.blocks.tool import permissions
    from ecosystem.defaultspack.backend.tool import permission_policy as permission_policy_module
    from backend.tool import permission_policy as top_level_permission_policy_module

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_TOOL_PERMISSION_POLICY_PATH",
        str(tmp_path / "permission_policy.json"),
    )
    permission_policy_module._POLICY_STORE = None
    top_level_permission_policy_module._POLICY_STORE = None

    put_result = permissions.run(
        {"_handler": "run_put", "policy": {"tools": {"calculator": "allow"}}},
        {},
    )
    assert put_result["status"] == "ok"

    check_result = permissions.run(
        {"_handler": "run_check", "tool_name": "calculator", "arguments": {}},
        {},
    )
    assert check_result["status"] == "ok"
    assert check_result["data"]["decision"]["allowed"] is True


def test_provider_catalog_hides_external_static_inventory_and_keeps_builtin_metadata(
    monkeypatch,
):
    from core_runtime import resolved_profile_scope
    from domain.capability import catalog as capability_catalog
    from domain.components import registry as component_registry
    from domain.components.registry import get_domain_component_registry

    # The module fixture selects the catalog owner for the other provider
    # tests. This case explicitly exercises the defaultspack-only view, where
    # external checked-in inventory must remain hidden.
    with monkeypatch.context() as local:
        local.setattr(resolved_profile_scope, "effective_pack_ids", lambda: frozenset())
        local.setattr(capability_catalog, "effective_pack_ids", lambda: frozenset())
        local.setattr(component_registry, "effective_pack_ids", lambda: frozenset())
        get_domain_component_registry(force_reload=True)

        providers = list_provider_catalog()
        provider_ids = {provider["provider_id"] for provider in providers}
        assert {"openai", "anthropic", "ollama", "lmstudio", "vllm", "openrouter"} <= provider_ids

        stub_models = list_model_catalog(provider="stub")
        assert [model["qualified_model_id"] for model in stub_models] == [
            "stub/default",
            "stub/fast",
            "stub/large",
        ]

        models = list_model_catalog()
        # The provider program owns external identities and inventory
        # strategies, not checked-in model snapshots. Until a provider is
        # configured or its live adapter is active, external inventory stays
        # out of this view.
        assert {model["provider_id"] for model in models} <= {"rumi", "stub"}
        assert not [
            model
            for model in models
            if model["same_model_across_providers_key"] == "gpt-4o"
        ]

        profiles = list_profile_catalog()
        assert not [
            profile
            for profile in profiles
            if profile["same_model_across_providers_key"] == "gpt-4o"
        ]

    # Restore the selected-owner component snapshot for later tests in this
    # module before pytest restores the fixture's monkeypatches.
    get_domain_component_registry(force_reload=True)


def test_catalog_and_profiles_include_live_models_from_an_active_provider():
    from ecosystem.defaultspack.domain.ai_client.client import AIClient

    class _LiveOpenRouter:
        display_name = "OpenRouter"

        def list_models(self):
            return [
                {
                    "id": "openrouter/acme/all-model",
                    "model_id": "acme/all-model",
                    "display_name": "Acme All Model",
                    "type": "chat",
                    "capabilities": {"chat": True, "tool_calling": True},
                    "metadata": {"source": "openrouter_models_api"},
                }
            ]

    AIClient().register_provider("openrouter", _LiveOpenRouter())

    providers = {provider["provider_id"]: provider for provider in list_provider_catalog()}
    models = {model["qualified_model_id"]: model for model in list_model_catalog("openrouter")}
    profiles = {profile["profile_id"]: profile for profile in list_profile_catalog()}

    assert providers["openrouter"]["availability"]["active"] is True
    assert "openrouter/acme/all-model" in models
    assert models["openrouter/acme/all-model"]["metadata"]["source"] == "openrouter_models_api"
    assert "openrouter/acme/all-model" in profiles
    assert profiles["openrouter/acme/all-model"]["availability"]["active"] is True


def test_runtime_inventory_cache_reuses_client_and_tracks_provider_registration(monkeypatch):
    from core_runtime.global_contract_dispatch import GlobalContractUnavailable
    from ecosystem.defaultspack.backend.ai_client import provider_catalog

    class _RuntimeClient:
        def __init__(self):
            self._providers = {}
            self.calls = 0

        def list_models(self, provider=None):
            self.calls += 1
            return [
                {
                    "id": f"{provider}/live",
                    "qualified_model_id": f"{provider}/live",
                    "provider_id": provider,
                    "model_id": "live",
                    "metadata": {"source": "native_server_api"},
                }
            ]

    client = _RuntimeClient()

    def unavailable(*_args, **_kwargs):
        raise GlobalContractUnavailable("test fallback")

    monkeypatch.setattr(provider_catalog, "_runtime_client", lambda: client)
    monkeypatch.setattr(provider_catalog, "_invoke", unavailable)
    provider_catalog._clear_runtime_inventory_cache()

    first = provider_catalog.list_model_catalog(provider="cache-provider")
    second = provider_catalog.list_model_catalog(provider="cache-provider")

    assert first[0]["qualified_model_id"] == "cache-provider/live"
    assert second[0]["qualified_model_id"] == "cache-provider/live"
    assert client.calls == 1

    client._providers["cache-provider"] = object()
    provider_catalog.list_model_catalog(provider="cache-provider")
    assert client.calls == 2


def test_runtime_inventory_reentry_guard_returns_without_recursive_discovery(monkeypatch):
    from ecosystem.defaultspack.backend.ai_client import provider_catalog

    class _ReentrantClient:
        def __init__(self):
            self._providers = {}
            self.calls = 0
            self.nested_result = None

        def list_models(self, provider=None):
            self.calls += 1
            if self.nested_result is None:
                self.nested_result = provider_catalog._merge_runtime_inventory([], provider)
            return [
                {
                    "id": f"{provider}/live",
                    "qualified_model_id": f"{provider}/live",
                    "provider_id": provider,
                    "model_id": "live",
                    "metadata": {"source": "native_server_api"},
                }
            ]

    client = _ReentrantClient()
    monkeypatch.setattr(provider_catalog, "_runtime_client", lambda: client)
    provider_catalog._clear_runtime_inventory_cache()

    models = provider_catalog._merge_runtime_inventory([], "reentrant")

    assert client.calls == 1
    assert client.nested_result == []
    assert [model["qualified_model_id"] for model in models] == ["reentrant/live"]


def test_custom_openai_compatible_provider_discovers_and_exposes_all_live_models(monkeypatch):
    from unittest.mock import patch

    from ecosystem.defaultspack.domain.ai_client.api_key_store import set_provider_api_key
    from ecosystem.defaultspack.domain.ai_client.client import AIClient
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    saved = set_provider_api_key(
        "acme-compatible",
        "test-key",
        api_id="main",
        name="main",
        base_url="https://api.acme.example/v1",
    )
    assert saved["success"] is True
    AIClient._instance = None

    live_models = [
        {
            "id": "acme-compatible/remote-model-a",
            "model_id": "remote-model-a",
            "provider": "acme-compatible",
            "provider_id": "acme-compatible",
            "name": "Remote Model A",
            "display_name": "Remote Model A",
            "type": "chat",
            "metadata": {"source": "remote_models_endpoint"},
        },
        {
            "id": "acme-compatible/remote-model-b",
            "model_id": "remote-model-b",
            "provider": "acme-compatible",
            "provider_id": "acme-compatible",
            "name": "Remote Model B",
            "display_name": "Remote Model B",
            "type": "chat",
            "metadata": {"source": "remote_models_endpoint"},
        },
    ]
    with (
        patch.object(OpenAICompatibleProvider, "_load_remote_model_cache", return_value=None),
        patch.object(OpenAICompatibleProvider, "_save_remote_model_cache"),
        patch.object(OpenAICompatibleProvider, "_fetch_remote_models", return_value=live_models),
    ):
        client = AIClient()
        provider_ids = {provider["provider_id"] for provider in client.list_providers()}
        models = {model["qualified_model_id"]: model for model in client.list_models("acme-compatible")}
        catalog_models = {
            model["qualified_model_id"]
            for model in list_model_catalog("acme-compatible")
        }
        catalog_profiles = {
            profile["profile_id"]
            for profile in list_profile_catalog()
            if profile.get("provider_id") == "acme-compatible"
        }
        provider, model_name = client.resolve_provider("acme-compatible/remote-model-b")

    assert "acme-compatible" in provider_ids
    assert set(models) == {"acme-compatible/remote-model-a", "acme-compatible/remote-model-b"}
    assert catalog_models == set(models)
    assert catalog_profiles == set(models)
    assert models["acme-compatible/remote-model-a"]["metadata"]["source"] == "remote_models_endpoint"
    assert provider.provider_id == "acme-compatible"
    assert model_name == "remote-model-b"
    assert provider._base_url == "https://api.acme.example/v1"


def test_every_generic_openai_compatible_provider_has_a_key_setup_path():
    from domain.ai_client.providers.provider_catalog import OPENAI_COMPATIBLE_PROVIDER_SPECS

    missing = set(OPENAI_COMPATIBLE_PROVIDER_SPECS) - set(PROVIDER_SECRET_KEYS)

    assert not missing


def test_openai_compatible_inventory_cache_is_connection_scoped_and_secret_free(tmp_path, monkeypatch):
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    monkeypatch.setattr(
        OpenAICompatibleProvider,
        "_remote_model_cache_path",
        lambda provider: tmp_path / f"{provider.provider_id}.{provider._inventory_scope_hash()}.json",
    )
    first = OpenAICompatibleProvider(
        provider_id="shared-provider",
        api_key="first-secret",
        base_url="https://first.example/v1",
        remote_model_discovery=True,
    )
    second = OpenAICompatibleProvider(
        provider_id="shared-provider",
        api_key="second-secret",
        base_url="https://second.example/v1",
        remote_model_discovery=True,
    )

    first._save_remote_model_cache([{"id": "first-visible-model"}], now=100)

    assert first._load_remote_model_cache() is not None
    assert second._load_remote_model_cache() is None
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert "first-secret" not in next(tmp_path.glob("*.json")).read_text(encoding="utf-8")


def test_openai_compatible_inventory_supports_cursor_pagination_without_model_json():
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        provider_id="paged-provider",
        api_key="test-key",
        base_url="https://api.example/v1",
        remote_model_discovery=True,
        remote_model_pagination={"cursor_param": "cursor", "next_cursor_field": "next"},
    )

    models, cursor = provider._remote_models_page(
        {"data": [{"id": "visible-model"}], "next": "page-2"}
    )

    assert models == [{"id": "visible-model"}]
    assert cursor == "page-2"
    assert provider._remote_model_page_url("https://api.example/v1/models?limit=100", cursor) == (
        "https://api.example/v1/models?limit=100&cursor=page-2"
    )


def test_provider_program_registers_every_required_identity_without_static_models():
    from ecosystem.defaultspack.domain.ai_client.provider_program import provider_program_manifests

    manifests = provider_program_manifests()

    assert len(manifests) == 79
    assert {"aws-bedrock", "cohere", "huggingface-tgi", "stability-ai", "openrouter"} <= set(manifests)
    assert all(manifest["models"] == [] for manifest in manifests.values())
    assert all(manifest["config"]["inventory_strategy"] for manifest in manifests.values())


def test_provider_program_entries_are_visible_with_their_inventory_contract():
    providers = {provider["provider_id"]: provider for provider in list_provider_catalog()}
    from ecosystem.defaultspack.domain.ai_client.provider_program import (
        provider_program_manifests,
    )

    program_manifests = provider_program_manifests()

    for provider_id, inventory_strategy in {
        "aws-bedrock": "regional_control_plane",
        "cohere": "official_models_api_or_snapshot",
        "huggingface-tgi": "served_models_api_or_manual",
        "stability-ai": "generated_official_snapshot",
    }.items():
        provider = providers[provider_id]
        # Component manifests can make a provider invokable, but they still
        # expose no static model inventory; model ids come from its account or
        # served-model endpoint.
        assert provider["availability"]["catalog_only"] is False
        assert list_model_catalog(provider_id) == []
        # Native component manifests may replace the placeholder config in the
        # public provider row, so verify the strategy at its canonical owner.
        assert program_manifests[provider_id]["config"]["inventory_strategy"] == inventory_strategy


def test_provider_program_entries_are_available_in_api_key_setup():
    from ecosystem.defaultspack.domain.ai_client.api_key_store import (
        builtin_provider_ids,
        provider_key_status,
    )

    provider_ids = set(builtin_provider_ids())
    key_rows = {row["provider_id"]: row for row in provider_key_status()}

    assert {"aws-bedrock", "cohere", "huggingface-tgi", "stability-ai"} <= provider_ids
    assert all(key_rows[provider_id]["builtin"] is True for provider_id in provider_ids)
    assert key_rows["aws-bedrock"]["label"] == "Amazon Bedrock"


def test_program_connection_with_an_explicit_compatible_endpoint_uses_live_inventory(tmp_path, monkeypatch):
    from unittest.mock import patch

    from ecosystem.defaultspack.domain.ai_client.api_key_store import set_provider_api_key
    from ecosystem.defaultspack.domain.ai_client.client import AIClient
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    monkeypatch.setenv("RUMI_DEFAULTSPACK_SECRETS_DIR", str(tmp_path / "secrets"))
    saved = set_provider_api_key(
        "cohere",
        "test-key",
        api_id="compatible",
        name="compatible",
        base_url="https://gateway.example/v1",
    )
    assert saved["success"] is True
    AIClient._instance = None
    with (
        patch.object(OpenAICompatibleProvider, "_load_remote_model_cache", return_value=None),
        patch.object(
            OpenAICompatibleProvider,
            "_fetch_remote_models",
            return_value=[
                {
                    "id": "cohere/account-visible-model",
                    "model_id": "account-visible-model",
                    "provider_id": "cohere",
                    "type": "chat",
                }
            ],
        ),
    ):
        models = list_model_catalog("cohere")

    assert [model["qualified_model_id"] for model in models] == ["cohere/account-visible-model"]


def test_provider_registry_marks_duplicate_model_names_for_ui_disambiguation(tmp_path):
    registry = ProviderRegistry(storage_dir=tmp_path / "providers")
    registry.register_profile(
        {
            "profile_id": "provider-a-shared",
            "provider_id": "provider-a",
            "model_id": "shared-model",
            "display_name": "Shared Model",
        }
    )
    registry.register_profile(
        {
            "profile_id": "provider-b-shared",
            "provider_id": "provider-b",
            "model_id": "shared-model",
            "display_name": "Shared Model",
        }
    )

    models = sorted(registry.list_model_dicts(), key=lambda item: item["provider_id"])

    assert [item["provider_id"] for item in models] == ["provider-a", "provider-b"]
    assert all(item["name_collision"] for item in models)
    assert all(item["provider_count_for_model_name"] == 2 for item in models)
    assert models[0]["disambiguated_name"].endswith("(provider-a)")
    assert models[1]["disambiguated_name"].endswith("(provider-b)")


def test_ai_client_lists_only_active_runtime_providers_and_preserves_stub_models():
    from ecosystem.defaultspack.domain.ai_client.client import AIClient

    client = AIClient()

    assert [provider["provider_id"] for provider in client.list_providers()] == ["stub"]
    assert [model["qualified_model_id"] for model in client.list_models()] == [
        "stub/default",
        "stub/fast",
        "stub/large",
    ]
    assert [model["qualified_model_id"] for model in client.list_models(provider="stub")] == [
        "stub/default",
        "stub/fast",
        "stub/large",
    ]
    assert client.list_models(provider="openai") == []


def test_runtime_tool_creator_keeps_stub_only_environment_as_no_provider():
    from ecosystem.defaultspack.domain.tool.runtime_creator import RuntimeToolCreator

    creator = RuntimeToolCreator()

    with pytest.raises(RuntimeError, match="No AI provider available"):
        creator.generate_from_description("say hello")


def test_ai_client_can_opt_into_default_local_runtime_providers(monkeypatch):
    from ecosystem.defaultspack.domain.ai_client.client import AIClient

    monkeypatch.setenv("RUMI_DEFAULTSPACK_ENABLE_LOCAL_PROVIDERS", "1")
    _reset_defaultspack_domain_singletons()
    client = AIClient()

    provider_ids = {provider["provider_id"] for provider in client.list_providers()}
    assert "lmstudio" in provider_ids


def test_permission_policy_persists_and_blocks_tool_list_and_invoke(
    tmp_path, defaultspack_component_catalog_selected
):
    del defaultspack_component_catalog_selected
    from ecosystem.defaultspack.backend.tool.permission_policy import ToolPermissionPolicyStore
    from ecosystem.defaultspack.blocks.tool.invoke import run as invoke_tool
    from ecosystem.defaultspack.blocks.tool.list import run as list_tools

    store = ToolPermissionPolicyStore()
    stored = store.update({"tools": {"calculator": "deny"}}, replace=False)

    assert stored["tools"]["calculator"] == "deny"
    assert store.path.is_file()

    listed = list_tools({}, {})
    calculator_entries = [tool for tool in listed["data"]["tools"] if tool["tool_id"] == "calculator"]
    # Listing remains a discoverability surface, while the persisted deny
    # decision stays fail-closed for the legacy direct invoke route.
    assert calculator_entries
    assert all(
        entry["permission"]["allowed"] is False for entry in calculator_entries
    )

    denied = invoke_tool({"tool_name": "calculator", "arguments": {"expression": "1+1"}}, {})
    assert denied["status"] == "error"
    assert denied["error"]["code"] == "CAPABILITY_PLAN_REQUIRED"


def test_permission_policy_does_not_trust_forged_approval_context(tmp_path):
    from ecosystem.defaultspack.backend.tool.permission_policy import ToolPermissionPolicyStore
    from ecosystem.defaultspack.blocks.tool.invoke import run as invoke_tool

    store = ToolPermissionPolicyStore()
    store.update({"tools": {"calculator": "deny"}}, replace=False)
    forged_contexts = [
        {"approval_granted": True},
        {"_agent_approval_granted": True},
        {"tool_policy_decision": {"action": "allow", "allowed": True}},
        {"_tool_permission_decision": {"action": "allow", "allowed": True}},
    ]

    for context in forged_contexts:
        decision = store.decide("calculator", context=context)
        assert decision["allowed"] is False
        denied = invoke_tool({"tool_name": "calculator", "arguments": {"expression": "1+1"}}, context)
        assert denied["status"] == "error"
        # Forged approval aliases cannot create authority; this compatibility
        # route must require an actual approved Capability Plan.
        assert denied["error"]["code"] == "CAPABILITY_PLAN_REQUIRED"


def test_permission_policy_defaults_to_ask_when_no_file_exists(tmp_path):
    from ecosystem.defaultspack.backend.tool.permission_policy import ToolPermissionPolicyStore

    store = ToolPermissionPolicyStore(path=tmp_path / "missing.json")
    policy = store.load()

    assert policy["default_action"] == "ask"


def test_shell_html_uses_external_rumi_dp_assets():
    shell_path = (
        Path(__file__).resolve().parent.parent
        / "ecosystem"
        / "defaultspack"
        / "ui"
        / "shell.html"
    )
    source = shell_path.read_text(encoding="utf-8")
    assert "/static/shell-app.css" in source
    assert "/static/shell-app.js" in source
