from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecosystem.defaultspack.domain.ai_client.providers import detect_available_providers
from ecosystem.defaultspack.domain.ai_client.providers.openai_compatible_provider import (
    OpenAICompatibleProvider,
)
from ecosystem.defaultspack.domain.ai_client.providers.openrouter_provider import (
    OpenRouterProvider,
)
from ecosystem.defaultspack.domain.extensions.discovery import discover_extensions
from ecosystem.defaultspack.domain.extensions.loading import import_entrypoint
from ecosystem.defaultspack.domain.extensions.manifest import (
    ManifestValidationError,
    validate_manifest,
)
from ecosystem.defaultspack.domain.extensions.registry import ExtensionRegistry
from ecosystem.defaultspack.domain.prompt.manager import PromptManager
from ecosystem.defaultspack.domain.tool.broker import ToolBroker
from ecosystem.defaultspack.domain.tool.registry import ToolRegistry
import ecosystem.defaultspack.domain.ai_client.providers as providers_module
import ecosystem.defaultspack.domain.prompt.manager as prompt_manager_module
import ecosystem.defaultspack.domain.tool.registry as tool_registry_module


class _DummyProvider:
    KNOWN_MODELS = [{"id": "dummy/m1", "name": "dummy-m1", "provider": "dummy", "type": "chat"}]


class _FakeLLMRegistry:
    def __init__(self, provider_manifests, model_manifests=None):
        self._provider_manifests = provider_manifests
        self._model_manifests = list(model_manifests or [])

    def providers(self, *, enabled_only: bool = True):
        if enabled_only:
            return [m for m in self._provider_manifests if bool(m.get("enabled", True))]
        return list(self._provider_manifests)

    def models(self, *, provider_id: str = "", enabled_only: bool = True):
        models = list(self._model_manifests)
        if enabled_only:
            models = [m for m in models if bool(m.get("enabled", True))]
        if provider_id:
            models = [m for m in models if m.get("provider_id") == provider_id]
        return models


class _FakeExtensionRegistry:
    def __init__(self, provider_manifests, model_manifests=None):
        self._llm = _FakeLLMRegistry(provider_manifests, model_manifests=model_manifests)

    def llm(self):
        return self._llm


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


OPENROUTER_CURATED_ALLOWLIST = [
    {
        "id": "openrouter/tencent/hy3-preview:free",
        "model_id": "tencent/hy3-preview:free",
        "name": "Tencent Hy3 preview (free)",
        "display_name": "Tencent Hy3 preview (free)",
        "provider": "openrouter",
        "provider_id": "openrouter",
        "type": "chat",
        "defaults": {"chat": True, "fast": True},
    },
    {
        "id": "openrouter/cohere/north-mini-code:free",
        "model_id": "cohere/north-mini-code:free",
        "name": "Cohere North Mini Code (free)",
        "display_name": "Cohere North Mini Code (free)",
        "provider": "openrouter",
        "provider_id": "openrouter",
        "type": "chat",
        "defaults": {"chat": True, "coding": True, "fast": True},
    },
]


def _openrouter_catalog_models():
    return [dict(model) for model in OPENROUTER_CURATED_ALLOWLIST]


def _make_extension_pack(ecosystem_root: Path, pack_id: str) -> Path:
    pack_root = ecosystem_root / pack_id
    _write_json(pack_root / "pack.v4.json", {"pack": {"id": pack_id}})
    (pack_root / "extensions").mkdir(parents=True, exist_ok=True)
    return pack_root


def test_manifest_validation_requires_provider_adapter_or_entrypoint():
    with pytest.raises(ManifestValidationError):
        validate_manifest(
            {
                "id": "x",
                "category": "llm_provider",
                "version": "1",
            },
            expected_category="llm_provider",
        )


def test_manifest_validation_preserves_marketplace_and_signing_metadata():
    manifest = validate_manifest(
        {
            "id": "x",
            "category": "llm_provider",
            "version": "1",
            "adapter": "openai_compatible",
            "marketplace": {
                "registry": "bundled",
                "publisher": "rumi-ai",
                "status": "verified",
            },
            "signing": {
                "mode": "repository_reviewed",
                "verified": True,
            },
        },
        expected_category="llm_provider",
    )

    assert manifest["marketplace"]["status"] == "verified"
    assert manifest["marketplace"]["publisher"] == "rumi-ai"
    assert manifest["signing"]["mode"] == "repository_reviewed"
    assert manifest["signing"]["verified"] is True


def test_manifest_validation_rejects_blacklisted_marketplace_status():
    with pytest.raises(ManifestValidationError, match="blacklisted"):
        validate_manifest(
            {
                "id": "x",
                "category": "llm_provider",
                "version": "1",
                "adapter": "openai_compatible",
                "marketplace": {"status": "blacklisted", "registry": "test"},
            },
            expected_category="llm_provider",
        )


def test_manifest_validation_rejects_required_signing_without_signature():
    with pytest.raises(ManifestValidationError):
        validate_manifest(
            {
                "id": "x",
                "category": "llm_provider",
                "version": "1",
                "adapter": "openai_compatible",
                "signing": {"mode": "ed25519", "required": True},
            },
            expected_category="llm_provider",
        )


def test_discovery_scans_manifest_categories(tmp_path: Path):
    root = tmp_path / "extensions"
    _write_json(
        root / "llm/providers/openai/manifest.json",
        {
            "id": "openai",
            "category": "llm_provider",
            "version": "1",
            "adapter": "openai_compatible",
            "enabled": True,
        },
    )
    _write_json(
        root / "llm/providers/openai/models/gpt-5.5.json",
        {
            "id": "openai/gpt-5.5",
            "category": "llm_model",
            "version": "1",
            "provider_id": "openai",
            "model_id": "gpt-5.5",
            "priority": 10,
            "defaults": {"chat": True},
        },
    )
    _write_json(
        root / "prompts/base_assistant/manifest.json",
        {
            "id": "base_assistant",
            "category": "prompt",
            "version": "1",
        },
    )
    _write_json(
        root / "skills/hatch_pet/manifest.json",
        {
            "id": "hatch-pet",
            "category": "skill",
            "version": "1",
            "triggers": ["sprite", "pet"],
            "applies_to_tools": ["image_gen"],
        },
    )

    result = discover_extensions(root)
    assert len(result.issues) == 0
    assert {(item.category, item.extension_id) for item in result.extensions} >= {
        ("llm_provider", "openai"),
        ("llm_model", "openai/gpt-5.5"),
        ("prompt", "base_assistant"),
        ("skill", "hatch-pet"),
    }
    registry = ExtensionRegistry(root)
    assert registry.skills().list()[0]["triggers"] == ["sprite", "pet"]


def test_extension_registry_excludes_llm_models_without_provider(tmp_path: Path):
    root = tmp_path / "extensions"
    _write_json(
        root / "llm/providers/missing-provider/models/orphan.json",
        {
            "id": "missing-provider/model",
            "category": "llm_model",
            "version": "1",
            "provider_id": "missing-provider",
            "model_id": "model",
        },
    )

    registry = ExtensionRegistry(root)

    assert registry.llm().models() == []
    assert any("provider_id is not registered" in issue.message for issue in registry.issues)

def test_extension_registry_llm_best_model(tmp_path: Path):
    root = tmp_path / "extensions"
    _write_json(
        root / "llm/providers/openai/manifest.json",
        {
            "id": "openai",
            "category": "llm_provider",
            "version": "1",
            "adapter": "openai_compatible",
            "enabled": True,
        },
    )
    _write_json(
        root / "llm/providers/openai/models/gpt-5-mini.json",
        {
            "id": "openai/gpt-5-mini",
            "category": "llm_model",
            "version": "1",
            "provider_id": "openai",
            "model_id": "gpt-5-mini",
            "priority": 50,
            "defaults": {"chat": True},
        },
    )
    _write_json(
        root / "llm/providers/openai/models/gpt-5.5.json",
        {
            "id": "openai/gpt-5.5",
            "category": "llm_model",
            "version": "1",
            "provider_id": "openai",
            "model_id": "gpt-5.5",
            "priority": 10,
            "defaults": {"chat": True},
        },
    )

    registry = ExtensionRegistry(root)
    best = registry.llm().best_model("openai", use_case="chat")
    assert best is not None
    assert best["model_id"] == "gpt-5.5"


def test_extension_registry_synthesizes_provider_default_models(tmp_path: Path):
    root = tmp_path / "extensions"
    _write_json(
        root / "llm/providers/openrouter/manifest.json",
        {
            "id": "openrouter",
            "category": "llm_provider",
            "version": "1",
            "entrypoint": f"{__name__}:_DummyProvider",
            "default_model": "auto",
            "default_model_for": {"chat": "auto", "fast": "openai/gpt-5.5-mini"},
            "enabled": True,
        },
    )

    registry = ExtensionRegistry(root)
    chat_default = registry.llm().best_model("openrouter", use_case="chat")
    fast_default = registry.llm().best_model("openrouter", use_case="fast")
    assert chat_default is not None
    assert chat_default["model_id"] == "auto"
    assert fast_default is not None
    assert fast_default["model_id"] == "openai/gpt-5.5-mini"


def test_extension_registry_preserves_google_api_key_env_list():
    root = (
        Path(__file__).resolve().parent.parent
        / "ecosystem"
        / "rumi_model_catalog_pack"
        / "extensions"
    )

    registry = ExtensionRegistry(root)
    google = next(
        provider
        for provider in registry.llm().providers(enabled_only=True)
        if provider["id"] == "google"
    )

    assert google["api_key_env"] == ["GOOGLE_API_KEY", "GEMINI_API_KEY"]


def test_extension_registry_lists_rumi_bundle_ui_surface():
    root = (
        Path(__file__).resolve().parent.parent
        / "ecosystem"
        / "defaultspack"
        / "extensions"
    )

    registry = ExtensionRegistry(root)
    surfaces = {item["id"]: item for item in registry.ui_surfaces().list(enabled_only=True)}
    assert "rumi_bundle" in surfaces
    assert surfaces["rumi_bundle"]["config"]["module_id"] == "rumi_bundle"
    assert surfaces["rumi_bundle"]["config"]["launch_mode"] == "desktop_app"
    assert surfaces["rumi_bundle"]["config"]["port_source"]["default"] == 8766


def test_build_extensions_roots_ignores_unselected_packs(tmp_path: Path, monkeypatch):
    import ecosystem.defaultspack.domain.extensions.runtime as runtime
    rumi_root = tmp_path / "tobkiri_runtime"
    ecosystem_root = rumi_root / "ecosystem"
    defaultspack = _make_extension_pack(ecosystem_root, "defaultspack")
    pack_a = _make_extension_pack(ecosystem_root, "pack_a")
    pack_b = _make_extension_pack(ecosystem_root, "pack_b")

    monkeypatch.setattr(runtime, "selected_extension_pack_ids", lambda _root: set())
    roots = {path.resolve() for path in runtime.build_extensions_roots(defaultspack)}

    assert (defaultspack / "extensions").resolve() in roots
    assert (pack_a / "extensions").resolve() not in roots
    assert (pack_b / "extensions").resolve() not in roots


def test_build_extensions_roots_ignores_ambient_app_catalog(
    monkeypatch, tmp_path: Path
):
    import ecosystem.defaultspack.domain.extensions.runtime as runtime

    managed_defaultspack = (
        tmp_path / "user_data" / "packs" / "defaultspack" / "versions" / "2.0.0"
    )
    _write_json(managed_defaultspack / "rumi-pack.json", {"pack_id": "defaultspack"})
    _write_json(managed_defaultspack / "ecosystem.json", {"pack_id": "defaultspack"})
    (managed_defaultspack / "extensions").mkdir(parents=True, exist_ok=True)
    app_dir = tmp_path / "app"
    app_ecosystem_root = app_dir / "ecosystem"
    app_defaultspack = _make_extension_pack(app_ecosystem_root, "defaultspack")
    model_catalog_pack = _make_extension_pack(app_ecosystem_root, "rumi_model_catalog_pack")

    monkeypatch.setenv("RUMI_APP_DIR", str(app_dir))
    monkeypatch.setattr(runtime, "selected_extension_pack_ids", lambda pack_root: set())

    roots = {path.resolve() for path in runtime.build_extensions_roots(managed_defaultspack)}

    assert (managed_defaultspack / "extensions").resolve() in roots
    assert (model_catalog_pack / "extensions").resolve() not in roots
    assert (app_defaultspack / "extensions").resolve() not in roots


def test_build_extensions_roots_filters_to_v4_effective_set(tmp_path: Path, monkeypatch):
    import ecosystem.defaultspack.domain.extensions.runtime as runtime
    rumi_root = tmp_path / "tobkiri_runtime"
    ecosystem_root = rumi_root / "ecosystem"
    defaultspack = _make_extension_pack(ecosystem_root, "defaultspack")
    pack_a = _make_extension_pack(ecosystem_root, "pack_a")
    pack_b = _make_extension_pack(ecosystem_root, "pack_b")
    extra_root = tmp_path / "loose_extensions"
    monkeypatch.setattr(runtime, "selected_extension_pack_ids", lambda _root: {"pack_a"})

    roots = {path.resolve() for path in runtime.build_extensions_roots(defaultspack, extra_roots=[extra_root])}

    assert (defaultspack / "extensions").resolve() in roots
    assert (pack_a / "extensions").resolve() in roots
    assert (pack_b / "extensions").resolve() not in roots
    assert extra_root.resolve() in roots


def test_get_extensions_roots_ignores_ambient_extension_root(tmp_path: Path, monkeypatch):
    import ecosystem.defaultspack.domain.extensions.runtime as runtime

    ambient_root = tmp_path / "ambient_extensions"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_EXTENSION_ROOTS", str(ambient_root))

    roots = {path.resolve() for path in runtime.get_extensions_roots()}

    assert ambient_root.resolve() not in roots


def test_build_extensions_roots_fails_closed_on_invalid_setup_selection(tmp_path: Path, monkeypatch):
    import ecosystem.defaultspack.domain.extensions.runtime as runtime
    rumi_root = tmp_path / "tobkiri_runtime"
    ecosystem_root = rumi_root / "ecosystem"
    defaultspack = _make_extension_pack(ecosystem_root, "defaultspack")
    pack_a = _make_extension_pack(ecosystem_root, "pack_a")
    selection_path = rumi_root / "user_data" / "settings" / "setup_pack_selection.json"
    selection_path.parent.mkdir(parents=True, exist_ok=True)
    selection_path.write_text("{broken", encoding="utf-8")

    monkeypatch.setattr(runtime, "selected_extension_pack_ids", lambda _root: set())
    roots = {path.resolve() for path in runtime.build_extensions_roots(defaultspack)}

    assert (defaultspack / "extensions").resolve() in roots
    assert (pack_a / "extensions").resolve() not in roots


def test_get_extension_registry_force_reload_preserves_registry_identity(monkeypatch, tmp_path: Path):
    import ecosystem.defaultspack.domain.extensions.runtime as runtime

    first_root = tmp_path / "first" / "extensions"
    second_root = tmp_path / "second" / "extensions"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    registry = ExtensionRegistry(first_root)

    monkeypatch.setattr(runtime, "_REGISTRY", registry)
    monkeypatch.setattr(runtime, "get_extensions_roots", lambda: [second_root])

    reloaded = runtime.get_extension_registry(force_reload=True)

    assert reloaded is registry
    assert reloaded.root == second_root


def test_openrouter_provider_uses_live_inventory_without_catalog_overlay(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-token")
    monkeypatch.setattr(
        OpenRouterProvider,
        "_remote_discovered_models",
        lambda self: [{
            "id": "openrouter/openai/live-model",
            "model_id": "openai/live-model",
            "provider_id": "openrouter",
            "provider": "openrouter",
            "name": "Live model",
            "display_name": "Live model",
            "type": "chat",
        }],
    )

    provider = OpenRouterProvider()
    models = provider.list_models()
    model_ids = {model["id"] for model in models}
    assert model_ids == {"openrouter/openai/live-model"}
    assert all(model["provider_id"] == "openrouter" for model in models)


def test_openrouter_provider_does_not_load_a_bundled_catalog(monkeypatch):
    monkeypatch.setattr(OpenRouterProvider, "_remote_discovered_models", lambda self: [])
    provider = OpenRouterProvider(known_models=[])
    model_ids = {model["model_id"] for model in provider.list_models()}

    assert model_ids == set()
    assert OpenRouterProvider.KNOWN_MODELS == []


def test_openrouter_provider_rejects_non_allowlisted_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-token")
    monkeypatch.setattr(OpenRouterProvider, "_load_remote_model_cache", lambda self: None)
    monkeypatch.setattr(OpenRouterProvider, "_remote_discovered_models", lambda self: [])

    provider = OpenRouterProvider()
    with pytest.raises(RuntimeError, match="live or last-known-good catalog"):
        provider.complete("openai/gpt-4o-mini", [{"role": "user", "content": "hi"}], [], {})


def test_detect_available_providers_uses_manifest_registry(monkeypatch):
    entrypoint = f"{__name__}:_DummyProvider"
    provider_manifests = [
        {
            "id": "generic_openai_like",
            "category": "llm_provider",
            "version": "1",
            "adapter": "openai_compatible",
            "enabled": True,
            "credential_required": False,
            "default_base_url": "https://example.test/v1",
        },
        {
            "id": "dummy",
            "category": "llm_provider",
            "version": "1",
            "entrypoint": entrypoint,
            "enabled": True,
            "credential_required": False,
        },
    ]
    fake_registry = _FakeExtensionRegistry(provider_manifests)
    monkeypatch.setattr(
        providers_module,
        "get_extension_registry",
        lambda force_reload=True: fake_registry,
    )
    monkeypatch.setattr(providers_module, "_load_legacy_providers", lambda: {})

    available = detect_available_providers()
    assert isinstance(available["generic_openai_like"], OpenAICompatibleProvider)
    assert isinstance(available["dummy"], _DummyProvider)


def test_openai_compatible_provider_uses_model_manifests():
    manifest = {
        "id": "generic_openai_like",
        "category": "llm_provider",
        "version": "1",
        "adapter": "openai_compatible",
        "default_base_url": "https://example.test/v1",
        "credential_required": False,
    }
    model_manifests = [
        {
            "provider_id": "generic_openai_like",
            "model_id": "latest-model",
            "display_name": "Latest Model",
            "type": "chat",
            "defaults": {"chat": True},
        }
    ]
    provider = OpenAICompatibleProvider.from_manifest(
        manifest,
        model_manifests=model_manifests,
    )
    models = provider.list_models()
    assert len(models) == 1
    assert models[0]["id"] == "generic_openai_like/latest-model"
    assert models[0]["model_id"] == "latest-model"
    assert models[0]["provider"] == "generic_openai_like"
    assert models[0]["provider_id"] == "generic_openai_like"
    assert models[0]["name"] == "Latest Model"
    assert models[0]["defaults"] == {"chat": True}


def test_prompt_manager_lists_extension_prompts(monkeypatch, tmp_path: Path):
    extensions_root = tmp_path / "extensions"
    _write_json(
        extensions_root / "prompts/default_chat/manifest.json",
        {
            "id": "default_chat",
            "category": "prompt",
            "version": "1",
            "enabled": True,
            "source_pack_id": "defaultspack",
            "config": {"template_file": "prompt.md"},
        },
    )
    (extensions_root / "prompts/default_chat/prompt.md").write_text(
        "hello {{name}}\n",
        encoding="utf-8",
    )

    registry = ExtensionRegistry(extensions_root)
    monkeypatch.setattr(prompt_manager_module, "get_extension_registry", lambda force_reload=True: registry)
    monkeypatch.setattr(prompt_manager_module, "get_extensions_root", lambda: extensions_root)
    monkeypatch.setattr(
        prompt_manager_module,
        "prompt_pack_source_is_trusted",
        lambda pack_id, source_path: str(pack_id) == "defaultspack" and bool(source_path),
    )

    manager = PromptManager()
    prompt = manager.get_prompt_by_name("default_chat")
    assert prompt is not None
    assert prompt["metadata"]["source"] == "extension"
    assert "hello {{name}}" in prompt["body"]


def test_prompt_manager_rejects_spoofed_builtin_extension_prompt(
    monkeypatch,
    tmp_path: Path,
    defaultspack_component_catalog_selected,
):
    extensions_root = tmp_path / "extensions"
    _write_json(
        extensions_root / "prompts/default_chat/manifest.json",
        {
            "id": "default_chat",
            "category": "prompt",
            "version": "1",
            "enabled": True,
            "source_pack_id": "defaultspack",
            "config": {"template_file": "prompt.md"},
        },
    )
    (extensions_root / "prompts/default_chat/prompt.md").write_text(
        "spoofed prompt injection\n",
        encoding="utf-8",
    )

    registry = ExtensionRegistry(extensions_root)
    monkeypatch.setattr(prompt_manager_module, "get_extension_registry", lambda force_reload=True: registry)
    monkeypatch.setattr(prompt_manager_module, "get_extensions_root", lambda: extensions_root)

    manager = PromptManager()
    prompt = manager.get_prompt_by_name("default_chat")
    assert prompt is not None
    assert prompt["metadata"]["source"] != "extension"
    assert "spoofed prompt injection" not in prompt["body"]


def test_tool_registry_loads_extension_tools(monkeypatch, tmp_path: Path):
    extensions_root = tmp_path / "extensions"
    _write_json(
        extensions_root / "tools/calculator/manifest.json",
        {
                "id": "custom_calc",
            "category": "tool",
            "version": "1",
            "enabled": True,
            "config": {
                "name": "custom_calc",
                "summary": "計算",
                "tags": ["math"],
                "schema": {
                    "parameters": {
                        "type": "object",
                        "properties": {"expression": {"type": "string"}},
                        "required": ["expression"],
                    }
                },
                "execution": {"type": "local"},
            },
        },
    )

    registry = ExtensionRegistry(extensions_root)
    monkeypatch.setattr(tool_registry_module, "get_extension_registry", lambda force_reload=True: registry)
    ToolRegistry._instance = None
    tool_registry = ToolRegistry()
    tool = tool_registry.get("custom_calc")
    assert tool is not None
    assert tool["metadata"]["source"] == "extension"
    assert tool["summary"] == "計算"


def test_tool_broker_prefers_native_strategy_when_capability_present():
    broker = ToolBroker()
    strategy = broker.select_strategy(
        {"capabilities": {"native_tool_calling": True}},
        [{"name": "calculator"}],
    )
    assert strategy == "native"
    prepared = broker.prepare_provider_tools(
        {"capabilities": {"native_tool_calling": False}},
        [{"name": "calculator"}],
    )
    assert prepared["strategy"] == "prompt_fallback"
    assert prepared["tool_names"] == ["calculator"]


def test_import_entrypoint_normalizes_legacy_module_names():
    loaded = import_entrypoint("domain.ai_client.providers.openai_compatible_provider:OpenAICompatibleProvider")
    assert loaded is OpenAICompatibleProvider


def test_chat_http_route_requires_captured_conversation_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/v1/chat/completions",
        "conversation.turn.v1",
        "complete",
    )


def test_legacy_http_route_allowlist_is_physically_absent(monkeypatch):
    del monkeypatch
    from ecosystem.defaultspack.transport.registry import (
        _legacy_http_routes_path,
        load_legacy_http_route_allowlist,
    )

    assert not _legacy_http_routes_path().exists()
    assert load_legacy_http_route_allowlist() == {}
