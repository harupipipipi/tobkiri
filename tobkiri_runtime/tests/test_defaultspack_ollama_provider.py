from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.ai_client.client import AIClient  # noqa: E402
from domain.ai_client.providers import (  # noqa: E402
    _instantiate_manifest_provider,
    _provider_manifest_map,
    get_provider_catalog_map,
)
from domain.ai_client.providers.ollama_provider import (  # noqa: E402
    OllamaAPIError,
    OllamaProvider,
)


TAGS = [
    {
        "name": "registry.example/team/gemma4:latest",
        "model": "registry.example/team/gemma4:latest",
        "modified_at": "2026-07-01T00:00:00Z",
        "size": 9_608_350_245,
        "digest": "digest-gemma4",
        "details": {
            "format": "gguf",
            "family": "gemma4",
            "families": ["gemma4"],
            "parameter_size": "8.0B",
            "quantization_level": "Q4_K_M",
        },
    },
    {
        "name": "nomic-embed-text:latest",
        "model": "nomic-embed-text:latest",
        "modified_at": "2026-07-02T00:00:00Z",
        "size": 274_000_000,
        "digest": "digest-nomic",
        "details": {
            "format": "gguf",
            "family": "nomic-bert",
            "families": ["nomic-bert"],
            "parameter_size": "137M",
            "quantization_level": "F16",
        },
    },
]

SHOW = {
    "registry.example/team/gemma4:latest": {
        "parameters": "temperature 0.7\nnum_ctx 4096",
        "license": "test license",
        "template": "{{ .Prompt }}",
        "capabilities": ["completion", "vision", "tools", "thinking"],
        "details": {"family": "gemma4", "parameter_size": "8.0B"},
        "model_info": {
            "general.architecture": "gemma4",
            "gemma4.context_length": 131072,
        },
    },
    "nomic-embed-text:latest": {
        "capabilities": ["embedding"],
        "details": {"family": "nomic-bert", "parameter_size": "137M"},
        "model_info": {
            "general.architecture": "nomic-bert",
            "nomic-bert.context_length": 8192,
        },
    },
}

RUNNING = [
    {
        "name": "registry.example/team/gemma4:latest",
        "model": "registry.example/team/gemma4:latest",
        "digest": "digest-gemma4",
        "size": 6_591_830_464,
        "size_vram": 5_333_539_264,
        "expires_at": "2026-07-10T22:00:00Z",
        "context_length": 4096,
    }
]


def _provider(**kwargs):
    return OllamaProvider(
        base_url="http://127.0.0.1:11434/v1",
        server_base_url="http://127.0.0.1:11434",
        **kwargs,
    )


def _fake_native(path, *, body=None, timeout=None):
    del timeout
    if path == "/api/tags":
        return {"models": TAGS}
    if path == "/api/ps":
        return {"models": RUNNING}
    if path == "/api/show":
        return SHOW[str((body or {}).get("model"))]
    raise AssertionError(path)


def test_native_inventory_preserves_exact_ids_details_and_verified_capabilities():
    provider = _provider(detail_workers=2)
    with patch.object(provider, "_native_request_json", side_effect=_fake_native):
        models = provider._fetch_native_inventory([])

    assert [model["model_id"] for model in models] == [
        "registry.example/team/gemma4:latest",
        "nomic-embed-text:latest",
    ]
    assert [model["id"] for model in models] == [
        "ollama/registry.example/team/gemma4:latest",
        "ollama/nomic-embed-text:latest",
    ]

    gemma = models[0]
    assert gemma["type"] == "chat"
    assert gemma["context_window"] == 131072
    assert gemma["capabilities"]["image_input"] is True
    assert gemma["capabilities"]["tool_calling"] is True
    assert gemma["capabilities"]["thinking"] is True
    assert gemma["capabilities"]["json_schema"] is None
    assert gemma["thinking"]["levels"] == []
    assert gemma["thinking"]["levels_verified"] is True
    assert gemma["metadata"]["digest"] == "digest-gemma4"
    assert gemma["metadata"]["running"] is True
    assert gemma["metadata"]["installed"] is True
    assert gemma["metadata"]["available_for_invocation"] is None
    assert gemma["metadata"]["size_vram"] == 5_333_539_264
    assert gemma["metadata"]["active_context_length"] == 4096
    assert gemma["metadata"]["default_context_length"] == 4096
    assert gemma["metadata"]["quantization_level"] == "Q4_K_M"

    embedding = models[1]
    assert embedding["type"] == "embedding"
    assert embedding["context_window"] == 8192
    assert embedding["capabilities"]["text_input"] is True
    assert embedding["capabilities"]["text_output"] is False
    assert embedding["capabilities"]["streaming"] is False
    assert embedding["metadata"]["running"] is False
    assert embedding["metadata"]["load_state"] == "installed_not_running"


def test_digest_cache_skips_unchanged_show_details_but_refreshes_running_state():
    provider = _provider(detail_workers=2)
    with patch.object(provider, "_native_request_json", side_effect=_fake_native):
        previous = provider._fetch_native_inventory([])

    stopped = {**RUNNING[0], "model": "unrelated:latest", "name": "unrelated:latest"}

    def refresh_native(path, *, body=None, timeout=None):
        if path == "/api/ps":
            return {"models": [stopped]}
        return _fake_native(path, body=body, timeout=timeout)

    with (
        patch.object(provider, "_native_request_json", side_effect=refresh_native),
        patch.object(
            provider,
            "_show_model",
        ) as show_model,
    ):
        refreshed = provider._fetch_native_inventory(previous)

    show_model.assert_not_called()
    assert refreshed[0]["metadata"]["running"] is False
    assert refreshed[0]["metadata"]["load_state"] == "installed_not_running"
    assert refreshed[0]["metadata"]["catalog_cache_state"] == "fresh"


def test_detail_and_running_failures_remain_unknown_without_name_guessing():
    provider = _provider(detail_workers=1)
    unknown_tag = {
        "name": "looks-like-an-embed-model:latest",
        "model": "looks-like-an-embed-model:latest",
        "digest": "digest-unknown",
        "details": {"format": "gguf"},
    }

    def partial_failure(path, *, body=None, timeout=None):
        del body, timeout
        if path == "/api/tags":
            return {"models": [unknown_tag]}
        raise OllamaAPIError("offline", kind="network_error")

    with patch.object(provider, "_native_request_json", side_effect=partial_failure):
        models = provider._fetch_native_inventory([])

    assert len(models) == 1
    model = models[0]
    assert model["model_id"] == "looks-like-an-embed-model:latest"
    assert model["type"] == "unknown"
    assert all(value is None for value in model["capabilities"].values())
    assert model["metadata"]["capability_confidence"] == "unknown"
    assert model["metadata"]["detail_state"] == "unavailable"
    assert model["metadata"]["running"] is None
    assert model["metadata"]["load_state"] == "unknown"
    assert model["metadata"]["running_state_source"] == "unavailable"


def test_url_normalization_no_auth_and_optional_proxy_auth(monkeypatch):
    for name in ("OLLAMA_BASE_URL", "OLLAMA_HOST", "OLLAMA_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    provider = OllamaProvider(base_url="localhost:11434")
    assert provider.BASE_URL == "http://localhost:11434/v1"
    assert provider._server_base_url() == "http://localhost:11434"
    assert "Authorization" not in provider._headers(content_type="")

    monkeypatch.setenv("OLLAMA_HOST", "https://proxy.example.test/team/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "proxy-token")
    authenticated = OllamaProvider()
    assert authenticated.BASE_URL == "https://proxy.example.test/team/v1"
    assert authenticated._server_base_url() == "https://proxy.example.test/team"
    assert authenticated._headers(content_type="")["Authorization"] == ("Bearer proxy-token")


def test_cache_is_connection_scoped_and_keeps_stale_inventory_on_failure(tmp_path):
    provider = _provider(cache_ttl_seconds=60)
    cache_path = tmp_path / "ollama.models.json"

    with (
        patch.object(provider, "_remote_model_cache_path", return_value=cache_path),
        patch.object(
            provider,
            "_native_request_json",
            side_effect=_fake_native,
        ),
    ):
        fresh = provider.refresh_models()

    assert len(fresh) == 2
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["inventory_scope"] == provider._inventory_scope_hash()
    assert "proxy-token" not in cache_path.read_text(encoding="utf-8")

    with (
        patch.object(provider, "_remote_model_cache_path", return_value=cache_path),
        patch.object(
            provider,
            "_fetch_native_inventory",
            side_effect=OllamaAPIError("offline", kind="network_error"),
        ),
    ):
        stale = provider.refresh_models()

    assert len(stale) == 2
    assert all(model["metadata"]["catalog_cache_state"] == "stale" for model in stale)
    assert stale[0]["metadata"]["running_state_confidence"] == "stale"

    other_endpoint = OllamaProvider(base_url="http://127.0.0.1:22468/v1")
    assert other_endpoint._inventory_scope_hash() != provider._inventory_scope_hash()


def test_discovery_is_read_only_and_unload_requires_explicit_keep_alive_zero():
    provider = _provider()
    calls = []

    def fake_request(path, *, body=None, timeout=None):
        calls.append((path, body, timeout))
        if path == "/api/tags":
            return {"models": []}
        if path == "/api/ps":
            return {"models": []}
        return {"done": True}

    with patch.object(provider, "_native_request_json", side_effect=fake_request):
        assert provider._fetch_native_inventory([]) == []
        provider.unload_model("ollama/registry.example/team/gemma4:latest")

    assert [path for path, _, _ in calls[:-1]] == ["/api/tags", "/api/ps"]
    assert calls[-1] == (
        "/api/generate",
        {
            "model": "registry.example/team/gemma4:latest",
            "keep_alive": 0,
            "stream": False,
        },
        None,
    )
    assert not any(path in {"/api/pull", "/api/create", "/api/delete"} for path, _, _ in calls)


def test_catalog_pack_owns_native_provider_without_static_inventory():
    manifest_path = (
        ROOT
        / "ecosystem"
        / "rumi_model_catalog_pack"
        / "catalog"
        / "providers"
        / "ollama"
        / "manifest.json"
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    provider_manifest = payload["provider_manifest"]

    assert provider_manifest["adapter"] == "python_entrypoint"
    assert provider_manifest["entrypoint"].endswith("ollama_provider:OllamaProvider")
    assert provider_manifest["default_base_url"] == "local://ollama"
    assert provider_manifest["config"]["model_list_path"] == "/api/tags"
    assert provider_manifest["config"]["model_detail_path"] == "/api/show"
    assert provider_manifest["config"]["running_models_path"] == "/api/ps"
    assert provider_manifest.get("default_model", "") == ""

    legacy_dir = (
        ROOT
        / "ecosystem"
        / "rumi_model_catalog_pack"
        / "extensions"
        / "llm"
        / "providers"
        / "ollama"
    )
    assert not (legacy_dir / "manifest.json").exists()
    assert not (legacy_dir / "models" / "llama3.1-8b.json").exists()

    manifest = _provider_manifest_map()["ollama"]
    assert manifest["adapter"] == "python_entrypoint"
    assert isinstance(_instantiate_manifest_provider(manifest), OllamaProvider)
    entry = get_provider_catalog_map()["ollama"]
    assert entry["default_model"] == ""
    assert entry["availability"]["supports_invoke"] is True


def test_ai_client_surfaces_native_inventory_without_invented_thinking_levels(
    monkeypatch,
):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_ENABLE_LOCAL_PROVIDERS", "1")
    AIClient._instance = None
    native = _provider()._normalize_model(
        TAGS[0],
        SHOW[TAGS[0]["model"]],
        RUNNING[0],
        running_known=True,
    )
    assert native is not None

    with patch.object(OllamaProvider, "list_models", return_value=[native]):
        models = AIClient().list_models(provider="ollama")

    assert [model["qualified_model_id"] for model in models] == [
        "ollama/registry.example/team/gemma4:latest"
    ]
    assert models[0]["supports_thinking"] is True
    assert models[0]["thinking_levels"] == []
    assert models[0]["metadata"]["source"] == "ollama_native_api"
