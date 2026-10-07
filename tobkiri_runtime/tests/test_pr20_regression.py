from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_component_catalog_selected")


def test_providers_init_imports_importlib():
    import ecosystem.defaultspack.domain.ai_client.providers as provider_module

    assert hasattr(provider_module, "importlib")


def test_detect_available_providers_fails_closed_when_legacy_module_is_absent(monkeypatch):
    import ecosystem.defaultspack.domain.ai_client.providers as provider_module

    absent_module = "domain.ai_client.providers.physically_absent_provider"
    monkeypatch.setattr(
        provider_module,
        "_LEGACY_PROVIDER_REGISTRY",
        [(["ABSENT_API_KEY"], "absent", absent_module, "AbsentProvider")],
    )
    monkeypatch.setattr(provider_module, "provider_has_api_key", lambda _provider_id: True)
    monkeypatch.delitem(sys.modules, absent_module, raising=False)
    with patch.object(provider_module, "_provider_manifest_map", return_value={}):
        providers = provider_module.detect_available_providers()

    assert not (PROJECT_ROOT / "ecosystem/defaultspack/domain/ai_client/providers/physically_absent_provider.py").exists()
    assert providers == {}


def test_selected_v4_catalog_keeps_builtin_tools_without_legacy_fallback():
    import domain.tool.registry as tool_registry_module

    tool_registry_module.ToolRegistry._instance = None

    class _NoToolsRegistry:
        def tools(self):
            return self

        def list(self, *, enabled_only=True):
            return []

    with patch.object(
        tool_registry_module,
        "get_extension_registry",
        return_value=_NoToolsRegistry(),
    ):
        registry = tool_registry_module.ToolRegistry()

    tool_ids = {tool["tool_id"] for tool in registry.list_tools()}
    assert {"web_search", "calculator", "file_reader"} <= tool_ids

    tool_registry_module.ToolRegistry._instance = None


def test_chat_runner_returns_error_when_mode_missing(monkeypatch):
    import ecosystem.defaultspack.domain.chat.runner as chat_runner_module
    from ecosystem.defaultspack.domain.chat.runner import ChatRunner

    fake_registry = MagicMock()
    fake_registry.chat_modes.return_value = MagicMock(get=lambda mode_id: None)
    monkeypatch.setattr(chat_runner_module, "get_extension_registry", lambda: fake_registry)

    runner = ChatRunner()
    result = runner.run("missing", {"message": "hello"})

    assert result["ok"] is False
    assert "missing" in result["error"]


def test_agent_runner_returns_error_when_mode_missing(monkeypatch):
    import ecosystem.defaultspack.domain.agent.runner as agent_runner_module
    from ecosystem.defaultspack.domain.agent.runner import AgentRunner

    fake_registry = MagicMock()
    fake_registry.agent_modes.return_value = MagicMock(get=lambda mode_id: None)
    monkeypatch.setattr(agent_runner_module, "get_extension_registry", lambda: fake_registry)

    runner = AgentRunner()
    result = runner.run("missing", {"task": "hello"})

    assert result["ok"] is False
    assert "missing" in result["error"]


def _fresh_client():
    import ecosystem.defaultspack.domain.ai_client.client as client_module

    client_module.AIClient._instance = None
    with patch("ecosystem.defaultspack.domain.ai_client.client.AIClient._auto_register_providers"):
        with patch("ecosystem.defaultspack.domain.ai_client.client.AIClient._auto_register_rumi"):
            client = client_module.AIClient()
    return client, client_module


def test_ai_client_preserves_profile_and_provider_catalog_api():
    client, client_module = _fresh_client()
    try:
        fake_provider = MagicMock()
        fake_provider.list_models.return_value = [
            {
                "id": "custom/runtime-only",
                "model_id": "runtime-only",
                "name": "Runtime Only",
                "provider": "custom",
                "type": "chat",
            }
        ]
        client.register_provider("custom", fake_provider)
        client.register_profile(
            "fast-chat",
            provider="custom",
            model="runtime-only",
            temperature=0.2,
        )

        profiles = client.list_profiles()
        providers = client.list_providers()
        models = client.list_models()
        resolved_provider, resolved_model = client.resolve_provider("fast-chat")
        matched_provider, matched_model = client.resolve_provider("runtime-only")

        fast_profile = next(profile for profile in profiles if profile["profile_id"] == "fast-chat")
        custom_provider = next(provider for provider in providers if provider["provider_id"] == "custom")
        runtime_model = next(model for model in models if model["qualified_model_id"] == "custom/runtime-only")

        assert fast_profile["provider_id"] == "custom"
        assert fast_profile["model_id"] == "runtime-only"
        assert fast_profile["metadata"]["profile_source"] == "custom"
        assert custom_provider["availability"]["active"] is True
        assert "supports_invoke" in custom_provider["availability"]
        assert runtime_model["provider_id"] == "custom"
        assert runtime_model["name"] == "Runtime Only"
        assert resolved_provider is fake_provider
        assert resolved_model == "runtime-only"
        assert matched_provider is fake_provider
        assert matched_model == "runtime-only"
    finally:
        client_module.AIClient._instance = None


def test_openai_compatible_provider_supports_legacy_constructor():
    from ecosystem.defaultspack.domain.ai_client.providers.openai_compatible_provider import (
        OpenAICompatibleProvider,
    )

    provider = OpenAICompatibleProvider(
        api_key="sk-test",
        base_url="https://legacy.example.test/v1",
        known_models=[{"id": "legacy/provider-model", "name": "Provider Model", "provider": "legacy"}],
        provider_id="legacy",
        display_name="Legacy Compat",
    )

    listed = provider.list_models()
    assert provider.provider_id == "legacy"
    assert provider.BASE_URL == "https://legacy.example.test/v1"
    assert listed[0]["id"] == "legacy/provider-model"


def test_openai_compatible_provider_supports_manifest_constructor():
    from ecosystem.defaultspack.domain.ai_client.providers.openai_compatible_provider import (
        OpenAICompatibleProvider,
    )

    provider = OpenAICompatibleProvider.from_manifest(
        {
            "id": "manifested",
            "display_name": "Manifested",
            "adapter": "openai_compatible",
            "api_key_env": "MANIFESTED_API_KEY",
            "base_url_env": "MANIFESTED_BASE_URL",
            "default_base_url": "https://manifested.example.test/v1",
            "credential_required": False,
            "default_model": "manifest-model",
        },
        model_manifests=[
            {
                "id": "manifested/manifest-model",
                "provider_id": "manifested",
                "model_id": "manifest-model",
                "display_name": "Manifest Model",
                "type": "chat",
                "defaults": {"chat": True},
            }
        ],
    )

    listed = provider.list_models()
    assert provider.provider_id == "manifested"
    assert provider.BASE_URL == "https://manifested.example.test/v1"
    assert listed[0]["id"] == "manifested/manifest-model"
    assert listed[0]["defaults"]["chat"] is True


def test_openai_compatible_provider_supports_manifest_api_key_env_list(
    monkeypatch,
    tmp_path,
):
    from tests.v4_provider_runtime_support import exercise_captured_provider_send

    monkeypatch.setenv("SECONDARY_MANIFESTED_API_KEY", "ambient-attacker")
    sent = exercise_captured_provider_send(
        tmp_path,
        monkeypatch,
        "openai_compatible",
        endpoint="https://manifested.example.test/v1",
    )

    assert sent["credential_bound"] is True
    assert sent["provider_id"] == "openai_compatible"
    assert "ambient-attacker" not in str(sent)
