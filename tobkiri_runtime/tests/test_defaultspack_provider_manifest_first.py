from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _write_openai_compatible_extension(root: Path) -> Path:
    provider_dir = root / "llm" / "providers" / "acme"
    models_dir = provider_dir / "models"
    models_dir.mkdir(parents=True)
    (provider_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "acme",
                "category": "llm_provider",
                "version": "1",
                "display_name": "Acme AI",
                "enabled": True,
                "priority": 5,
                "adapter": "openai_compatible",
                "api_key_env": "ACME_API_KEY",
                "base_url_env": "ACME_BASE_URL",
                "default_base_url": "https://acme.example/v1",
                "credential_required": True,
                "catalog_only": False,
                "default_model": "acme-chat",
                "default_model_for": {"fast": "acme-mini"},
                "capabilities": {"streaming": True, "native_tool_calling": True},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    for model_id, defaults in (
        ("acme-chat", {"chat": True}),
        ("acme-mini", {"chat": True, "fast": True}),
    ):
        (models_dir / f"{model_id}.json").write_text(
            json.dumps(
                {
                    "id": f"acme/{model_id}",
                    "category": "llm_model",
                    "version": "1",
                    "provider_id": "acme",
                    "model_id": model_id,
                    "display_name": model_id,
                    "type": "chat",
                    "enabled": True,
                    "defaults": defaults,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return root


def test_openai_compatible_provider_can_be_added_by_manifest_without_a_static_model_snapshot(
    monkeypatch,
    tmp_path,
    configured_cloud_provider,
):
    extension_root = _write_openai_compatible_extension(tmp_path / "extensions")

    from domain.extensions import runtime as extension_runtime
    from domain.extensions.runtime import get_extension_registry
    from domain.ai_client.api_key_store import set_provider_api_key
    from domain.ai_client.providers import (
        detect_available_providers,
        get_all_known_models,
        get_provider_catalog_map,
    )

    monkeypatch.setattr(
        extension_runtime,
        "get_extensions_roots",
        lambda: extension_runtime.build_extensions_roots(
            DEFAULTSPACK_ROOT,
            extra_roots=[extension_root],
        ),
    )
    result = set_provider_api_key(
        "acme",
        "test-key",
        api_id="default",
        name="Default",
    )
    assert result["success"] is True
    get_extension_registry(force_reload=True)
    catalog = get_provider_catalog_map()
    models = {item["id"]: item for item in get_all_known_models("acme")}
    available = detect_available_providers()

    assert catalog["acme"]["metadata"]["catalog_source"] == "extension_manifest"
    assert catalog["acme"]["metadata"]["adapter"] == "openai_compatible"
    assert catalog["acme"]["availability"]["supports_invoke"] is True
    assert catalog["acme"]["availability"]["configuration_source"] == "defaultspack_secret"
    # The extension can declare its protocol, but account-visible models are
    # loaded from the endpoint at runtime rather than from models/*.json.
    assert models == {}
    assert "acme" in available
    assert getattr(available["acme"], "provider_id", "") == "acme"


def test_provider_catalog_reads_do_not_force_extension_registry_reload(monkeypatch):
    from domain.ai_client import providers

    calls: list[bool] = []

    class FakeLlmRegistry:
        def providers(self, enabled_only=True):
            return [
                {
                    "id": "stub",
                    "provider_id": "stub",
                    "default_model": "default",
                    "default_model_for": {"chat": "default"},
                }
            ]

        def models(self, provider_id="", enabled_only=True):
            return [
                {
                    "id": "stub/default",
                    "provider_id": "stub",
                    "model_id": "default",
                    "type": "chat",
                }
            ]

        def best_model(self, name, use_case="chat"):
            return {"model_id": "default"} if name == "stub" else None

    class FakeRegistry:
        def llm(self):
            return FakeLlmRegistry()

        def get(self, category, name):
            return {"default_model": "default"} if category == "llm_provider" and name == "stub" else None

    def fake_get_extension_registry(*, force_reload=False):
        calls.append(force_reload)
        return FakeRegistry()

    monkeypatch.setattr(providers, "get_extension_registry", fake_get_extension_registry)

    assert providers._list_provider_manifests()[0]["id"] == "stub"
    assert providers._load_model_manifests("stub")[0]["id"] == "stub/default"
    assert providers.get_best_model_for_provider("stub") == "default"
    assert providers.validate_provider_catalog_coverage() == []
    assert calls
    assert all(force_reload is False for force_reload in calls)


def test_ai_blocks_guard_prevents_new_direct_aiclient_imports():
    allowed = {
        "blocks/ai/embed.py",
        "blocks/ai/image_analyze.py",
        "blocks/ai/image_gen.py",
        "blocks/ai/providers.py",
        "blocks/ai/routing/analyze.py",
        "blocks/ai/routing/log.py",
        "blocks/ai/routing/profiles.py",
        "blocks/ai/routing/route.py",
        "blocks/ai/routing/rules.py",
        "blocks/ai/stream.py",
        "blocks/ai/transcribe.py",
        "blocks/ai/tts.py",
        "blocks/chat/stream.py",
    }
    offenders: list[str] = []
    for base in (DEFAULTSPACK_ROOT / "blocks" / "ai", DEFAULTSPACK_ROOT / "blocks" / "chat"):
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            has_direct_import = (
                "from domain.ai_client.client import AIClient" in text
                or "from ecosystem.defaultspack.domain.ai_client.client import AIClient" in text
            )
            rel = path.relative_to(DEFAULTSPACK_ROOT).as_posix()
            if has_direct_import and rel not in allowed:
                offenders.append(rel)

    assert offenders == []
