from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK = ROOT / "ecosystem" / "defaultspack"
if str(DEFAULTSPACK) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK))

from domain.ai_client.providers import (  # noqa: E402
    _instantiate_manifest_provider,
    get_provider_catalog_map,
)
from domain.components.registry import get_domain_component_registry  # noqa: E402


DIRECT_HOSTED_IDS = {
    "anthropic", "avian", "cerebras", "deepinfra", "deepseek", "fireworks",
    "friendli", "genspark", "glm", "google", "groq", "hyperbolic",
    "inference-net", "longcat", "mistral", "moonshotai", "nebius", "novita",
    "nvidia", "openai", "perplexity", "sambanova", "together", "upstage", "xai",
}
NEW_IDS = {
    "avian", "deepinfra", "fireworks", "friendli", "hyperbolic",
    "inference-net", "nebius", "novita", "sambanova", "upstage",
}


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"data": [{"id": "Exact/Model:Version", "owned_by": "account"}]}).encode()


def _manifest(provider_id: str):
    directory = "gemini" if provider_id == "google" else provider_id
    payload = json.loads(
        (
            DEFAULTSPACK / "domain" / "providers" / directory / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    return payload["provider_manifest"]


def test_all_direct_hosted_providers_have_one_canonical_executable_component():
    legacy_root = (
        ROOT
        / "ecosystem"
        / "rumi_model_catalog_pack"
        / "extensions"
        / "llm"
        / "providers"
    )
    for provider_id in DIRECT_HOSTED_IDS:
        manifest = _manifest(provider_id)
        assert manifest["id"] == provider_id
        assert manifest["supports_invoke"] is True
        assert manifest["catalog_only"] is False
        assert not (legacy_root / provider_id / "manifest.json").exists()


def test_direct_hosted_matrix_has_one_enabled_owner_and_no_invented_defaults():
    get_domain_component_registry(force_reload=True)
    catalog = get_provider_catalog_map()
    assert DIRECT_HOSTED_IDS <= set(catalog)
    for provider_id in NEW_IDS:
        manifest = _manifest(provider_id)
        assert manifest["adapter"] == "openai_compatible"
        assert "default_model" not in manifest
        assert manifest["config"]["model_sync"] == "remote_merge"
        assert manifest["config"]["inventory_scope"] == "account"
        assert manifest["config"]["source_docs"].startswith("https://")


def test_direct_hosted_live_inventory_preserves_upstream_id(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-key")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: _Response())
    provider = _instantiate_manifest_provider(_manifest("deepinfra"))
    monkeypatch.setattr(provider, "_remote_model_cache_path", lambda: tmp_path / "models.json")
    models = provider.list_models()
    discovered = next(item for item in models if item["model_id"] == "Exact/Model:Version")
    assert discovered["id"] == "deepinfra/Exact/Model:Version"


def test_direct_hosted_base_urls_and_keys_are_provider_scoped():
    manifests = [_manifest(provider_id) for provider_id in NEW_IDS]
    assert len({item["api_key_env"] for item in manifests}) == len(manifests)
    assert all(item["default_base_url"].startswith("https://") for item in manifests)
    assert all(item["config"]["model_list_path"] == "/models" for item in manifests)
