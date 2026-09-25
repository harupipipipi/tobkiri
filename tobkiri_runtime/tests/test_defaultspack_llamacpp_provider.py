from __future__ import annotations

import json
from pathlib import Path
import sys
import urllib.error

import pytest

DEFAULTSPACK = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
if str(DEFAULTSPACK) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK))

from domain.ai_client.providers.llamacpp_provider import LlamaCppProvider


class _Response:
    def __init__(self, payload: object):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _isolate_cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        LlamaCppProvider,
        "_cache_path",
        lambda self: tmp_path / f"{self._scope()}.json",
    )


def test_llamacpp_single_model_falls_back_and_merges_props_without_mutation(
    tmp_path, monkeypatch
):
    _isolate_cache(monkeypatch, tmp_path)
    calls: list[str] = []

    def urlopen(request, **_kwargs):
        calls.append(request.full_url)
        if request.full_url.endswith("/models") and not request.full_url.endswith(
            "/v1/models"
        ):
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        if request.full_url.endswith("/v1/models"):
            return _Response(
                {
                    "data": [
                        {
                            "id": r"C:\models\Qwen.GGUF",
                            "meta": {"n_ctx_train": 8192, "n_params": 7},
                        }
                    ]
                }
            )
        return _Response(
            {
                "default_generation_settings": {"n_ctx": 4096},
                "chat_template": "template",
                "chat_template_caps": {
                    "supports_tools": True,
                    "supports_json_schema": True,
                },
                "modalities": {"vision": True},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    model = LlamaCppProvider(base_url="localhost:8080").refresh_models()[0]

    assert model["model_id"] == r"C:\models\Qwen.GGUF"
    assert model["display_name"] == "Qwen.GGUF"
    assert model["context_window"] == 4096
    assert model["type"] == "chat"
    assert model["capabilities"]["tool_calling"] is True
    assert model["capabilities"]["json_schema"] is True
    assert model["capabilities"]["image_input"] is True
    assert calls == [
        "http://localhost:8080/models",
        "http://localhost:8080/v1/models",
        "http://localhost:8080/props",
    ]
    assert all("reload" not in url and "/load" not in url for url in calls)


def test_llamacpp_router_inventory_preserves_state_path_and_modalities(
    tmp_path, monkeypatch
):
    _isolate_cache(monkeypatch, tmp_path)
    calls: list[str] = []
    payload = {
        "data": [
            {
                "id": "org/model:Q4",
                "path": r"D:\models\model.gguf",
                "status": {"value": "sleeping"},
                "architecture": {
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text"],
                },
            },
            {"id": "loading/model", "status": {"value": "loading"}},
            {"id": "failed/model", "status": {"value": "failed", "failed": True}},
        ]
    }

    def urlopen(request, **_kwargs):
        calls.append(request.full_url)
        return _Response(payload)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    models = LlamaCppProvider().refresh_models()

    assert [model["model_id"] for model in models] == [
        "org/model:Q4",
        "loading/model",
        "failed/model",
    ]
    assert models[0]["metadata"]["server_mode"] == "router"
    assert models[0]["metadata"]["load_state"] == "sleeping"
    assert models[0]["metadata"]["model_path"] == r"D:\models\model.gguf"
    assert models[0]["capabilities"]["image_input"] is True
    assert models[1]["metadata"]["load_state"] == "loading"
    assert models[2]["metadata"]["load_failed"] is True
    assert calls == ["http://127.0.0.1:8080/models"]


def test_llamacpp_router_props_explicitly_disable_autoload(tmp_path, monkeypatch):
    _isolate_cache(monkeypatch, tmp_path)
    requests: list[str] = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, **_kwargs: requests.append(request.full_url)
        or _Response({"is_sleeping": True}),
    )

    assert LlamaCppProvider().model_props("org/model:Q4")["is_sleeping"] is True
    assert requests == [
        "http://127.0.0.1:8080/props?model=org%2Fmodel%3AQ4&autoload=false"
    ]


def test_llamacpp_last_known_good_is_connection_scoped(tmp_path, monkeypatch):
    _isolate_cache(monkeypatch, tmp_path)
    provider = LlamaCppProvider(api_key="secret")
    monkeypatch.setattr(
        provider,
        "_fetch_models",
        lambda: provider._normalize_models([{"id": "one"}], router=True),
    )
    assert provider.refresh_models()[0]["model_id"] == "one"
    monkeypatch.setattr(
        provider,
        "_fetch_models",
        lambda: (_ for _ in ()).throw(OSError("offline")),
    )

    assert provider.refresh_models()[0]["metadata"]["catalog_cache_state"] == "stale"
    assert "secret" not in next(tmp_path.iterdir()).read_text(encoding="utf-8")
    assert LlamaCppProvider(api_key="other")._load_cache() is None


def test_llamacpp_health_loading_is_connected(tmp_path, monkeypatch):
    _isolate_cache(monkeypatch, tmp_path)

    def loading(*_args, **_kwargs):
        raise urllib.error.HTTPError("health", 503, "loading", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", loading)
    assert LlamaCppProvider().probe() == {
        "connected": True,
        "status": "loading",
        "status_code": 503,
    }


def test_llamacpp_component_is_canonical_and_has_no_placeholder(monkeypatch):
    from ecosystem.defaultspack.domain.ai_client.providers import (
        _instantiate_manifest_provider,
        _provider_manifest_map,
        get_provider_catalog_map,
    )
    from ecosystem.defaultspack.domain.extensions.runtime import get_extension_registry

    get_extension_registry(force_reload=True)
    manifests = _provider_manifest_map()
    provider = _instantiate_manifest_provider(manifests["llamacpp"])
    catalog = get_provider_catalog_map()

    assert isinstance(provider, LlamaCppProvider)
    assert "llama_cpp" not in manifests
    assert "llama_cpp" not in catalog
    assert catalog["llamacpp"]["default_model"] == ""
    assert "local-gguf" not in json.dumps(catalog["llamacpp"])


def test_llamacpp_saved_provider_alias_resolves_to_canonical_runtime():
    from ecosystem.defaultspack.domain.ai_client.client import AIClient

    stub = object()
    provider = object()
    client = AIClient.__new__(AIClient)
    client._providers = {"stub": stub, "llamacpp": provider}
    client._profiles = {}

    resolved, model_id = client.resolve_provider("llama_cpp/org/model:Q4")

    assert resolved is provider
    assert model_id == "org/model:Q4"


def test_llamacpp_rejects_remote_plaintext_credentials():
    with pytest.raises(ValueError, match="require HTTPS"):
        LlamaCppProvider(api_key="secret", base_url="http://models.example/v1")

    assert LlamaCppProvider(api_key="secret", base_url="http://127.0.0.1:8080/v1")
