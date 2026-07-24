from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_required_provider_program_has_one_canonical_registry_owner():
    from domain.ai_client.provider_program import provider_program_manifests
    from domain.ai_client.providers import validate_provider_program_coverage

    manifests = provider_program_manifests()

    assert len(manifests) == 79
    assert validate_provider_program_coverage() == []
    assert all(manifest["models"] == [] for manifest in manifests.values())


def test_local_openai_runtimes_discover_served_models_without_credentials(monkeypatch):
    from unittest.mock import patch

    from domain.ai_client.client import AIClient
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    monkeypatch.setenv("RUMI_DEFAULTSPACK_ENABLE_LOCAL_PROVIDERS", "1")
    AIClient._instance = None
    with patch.object(
        OpenAICompatibleProvider,
        "_fetch_remote_models",
        return_value=[
            {
                "id": "vllm/served-model",
                "model_id": "served-model",
                "provider_id": "vllm",
                "type": "chat",
                "metadata": {"source": "remote_models_endpoint"},
            }
        ],
    ), patch.object(OpenAICompatibleProvider, "_load_remote_model_cache", return_value=None):
        client = AIClient()
        models = client.list_models(provider="vllm")

    assert [model["qualified_model_id"] for model in models] == ["vllm/served-model"]


def test_ollama_uses_its_live_openai_compatible_models_endpoint_without_credentials(monkeypatch):
    from unittest.mock import patch

    from domain.ai_client.client import AIClient
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    monkeypatch.setenv("RUMI_DEFAULTSPACK_ENABLE_LOCAL_PROVIDERS", "1")
    AIClient._instance = None
    with patch.object(
        OpenAICompatibleProvider,
        "_fetch_remote_models",
        return_value=[
            {
                "id": "ollama/locally-loaded-model",
                "model_id": "locally-loaded-model",
                "provider_id": "ollama",
                "type": "chat",
                "metadata": {"source": "remote_models_endpoint"},
            }
        ],
    ), patch.object(OpenAICompatibleProvider, "_load_remote_model_cache", return_value=None):
        models = AIClient().list_models(provider="ollama")

    assert [model["qualified_model_id"] for model in models] == ["ollama/locally-loaded-model"]


def test_loopback_openai_compatible_connection_discovers_models_without_storing_a_fake_key(tmp_path, monkeypatch):
    from unittest.mock import patch

    from domain.ai_client.api_key_store import provider_named_api_keys, set_provider_api_key
    from domain.ai_client.providers import _custom_openai_provider_manifests, _instantiate_manifest_provider
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    saved = set_provider_api_key(
        "huggingface-tgi",
        "",
        pack_root=tmp_path,
        api_id="local",
        name="Local TGI",
        base_url="http://127.0.0.1:8080/v1",
        credential_mode="none",
    )

    assert saved["success"] is True
    assert saved["configured"] is True
    connections = provider_named_api_keys("huggingface-tgi", pack_root=tmp_path)
    assert connections[0]["credential_mode"] == "none"
    assert connections[0]["configured"] is True
    assert not (tmp_path / "user_data" / "secrets" / f"{saved['key']}.json").exists()

    # Build the executable adapter from the saved endpoint, with no fallback
    # model JSON and no API key requirement.
    monkeypatch.setattr(
        "domain.ai_client.providers.provider_named_api_keys",
        lambda provider_id="": connections if provider_id in {"", "huggingface-tgi"} else [],
    )
    manifest = _custom_openai_provider_manifests()["huggingface-tgi"]
    assert manifest["credential_required"] is False
    assert manifest["config"]["model_list_requires_auth"] is False
    with patch.object(OpenAICompatibleProvider, "_fetch_remote_models", return_value=[]):
        provider = _instantiate_manifest_provider(manifest)
    assert provider is not None
    assert provider._api_key == ""
    assert provider._credential_required is False


def test_program_provider_without_legacy_env_key_saves_a_canonical_default_connection(tmp_path):
    from domain.ai_client.api_key_store import provider_has_api_key, provider_named_api_keys, set_provider_api_key

    saved = set_provider_api_key(
        "azure-ai-foundry",
        "foundry-secret",
        base_url="https://resource.services.ai.azure.com/api/projects/demo",
        pack_root=tmp_path,
    )

    assert saved["success"] is True
    assert saved["key"] == "RUMIAPI_AZURE_AI_FOUNDRY_DEFAULT"
    assert provider_has_api_key("azure-ai-foundry", pack_root=tmp_path) is True
    connections = provider_named_api_keys("azure-ai-foundry", pack_root=tmp_path)
    assert [(item["provider_id"], item["api_id"]) for item in connections] == [
        ("azure-ai-foundry", "default"),
    ]


def test_huggingface_inference_uses_its_live_models_endpoint_without_a_checked_in_model_list(monkeypatch):
    from unittest.mock import patch

    from domain.ai_client.client import AIClient
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider
    from domain.ai_client.providers.provider_catalog import OPENAI_COMPATIBLE_PROVIDER_SPECS

    spec = OPENAI_COMPATIBLE_PROVIDER_SPECS["huggingface-inference"]
    assert spec["default_base_url"] == "https://router.huggingface.co/v1"
    assert spec["curated_models"] == []

    monkeypatch.setenv("HF_TOKEN", "test-hf-token")
    AIClient._instance = None
    with patch.object(
        OpenAICompatibleProvider,
        "_fetch_remote_models",
        return_value=[
            {
                "id": "deepseek-ai/DeepSeek-R1:fastest",
                "model_id": "deepseek-ai/DeepSeek-R1:fastest",
                "provider_id": "huggingface-inference",
                "type": "chat",
                "metadata": {"source": "remote_models_endpoint"},
            }
        ],
    ), patch.object(OpenAICompatibleProvider, "_load_remote_model_cache", return_value=None):
        models = AIClient().list_models(provider="huggingface-inference")

    assert [model["model_id"] for model in models] == ["deepseek-ai/DeepSeek-R1:fastest"]
    assert all(model["provider_id"] == "huggingface-inference" for model in models)


def test_jina_discovers_its_live_models_without_a_checked_in_snapshot(monkeypatch):
    from domain.ai_client.providers import _openai_compatible_spec_manifest
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider
    from domain.ai_client.providers.provider_catalog import OPENAI_COMPATIBLE_PROVIDER_SPECS

    spec = OPENAI_COMPATIBLE_PROVIDER_SPECS["jina-ai"]
    provider = OpenAICompatibleProvider.from_manifest(_openai_compatible_spec_manifest(spec))
    provider._api_key = "jina-key"
    assert spec["curated_models"] == []
    assert provider._base_url == "https://api.jina.ai/v1"
    assert provider._remote_model_list_path == "/models"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (
                b'{"data":[{"id":"jina-embeddings-v4","name":"Jina Embeddings v4",'
                b'"type":"embedding","input_modalities":["text"],'
                b'"output_modalities":["embeddings"],"context_length":32768}]}'
            )

    seen = {}

    def fake_urlopen(request, **_kwargs):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers.get("Authorization")
        return Response()

    monkeypatch.setattr("domain.ai_client.providers.openai_compatible_provider.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(OpenAICompatibleProvider, "_load_remote_model_cache", lambda _self: None)
    models = provider.list_models()

    assert seen == {"url": "https://api.jina.ai/v1/models", "authorization": "Bearer jina-key"}
    assert [model["model_id"] for model in models] == ["jina-embeddings-v4"]
    assert models[0]["type"] == "embedding"
    assert models[0]["capabilities"]["embeddings"] is True
    assert models[0]["metadata"]["source"] == "remote_models_endpoint"


def test_qianfan_uses_its_authenticated_models_api_without_a_snapshot(monkeypatch):
    from domain.ai_client.providers import _openai_compatible_spec_manifest
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider
    from domain.ai_client.providers.provider_catalog import OPENAI_COMPATIBLE_PROVIDER_CLASSES, OPENAI_COMPATIBLE_PROVIDER_SPECS

    spec = OPENAI_COMPATIBLE_PROVIDER_SPECS["baidu-qianfan"]
    provider = OPENAI_COMPATIBLE_PROVIDER_CLASSES["baidu-qianfan"].from_manifest(
        _openai_compatible_spec_manifest(spec)
    )
    provider._api_key = "qianfan-key"
    assert spec["curated_models"] == []
    assert provider.BASE_URL == "https://qianfan.baidubce.com/v2"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"object":"list","data":[{"id":"account-custom-model","owned_by":"me","type":"embeddings","context_length":8192}]}'

    seen = {}

    def fake_urlopen(request, **_kwargs):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers.get("Authorization")
        return Response()

    monkeypatch.setattr("domain.ai_client.providers.openai_compatible_provider.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(OpenAICompatibleProvider, "_load_remote_model_cache", lambda _self: None)
    models = provider.list_models()

    assert seen == {
        "url": "https://qianfan.baidubce.com/v2/models",
        "authorization": "Bearer qianfan-key",
    }
    assert [model["model_id"] for model in models] == ["account-custom-model"]
    assert models[0]["metadata"]["source"] == "remote_models_endpoint"
    assert models[0]["type"] == "embedding"
    assert models[0]["context_window"] == 8192
    assert models[0]["capabilities"]["embeddings"] is True


def test_portkey_uses_workspace_models_api_and_its_required_auth_header(monkeypatch):
    from domain.ai_client.providers import _instantiate_manifest_provider, _provider_manifest_map
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    monkeypatch.setenv("PORTKEY_API_KEY", "portkey-key")
    manifest = _provider_manifest_map()["portkey-ai-gateway"]
    provider = _instantiate_manifest_provider(manifest)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"object":"list","data":[{"id":"@openai-production/gpt-live","slug":"gpt-live"}]}'

    seen = {}

    def fake_urlopen(request, **_kwargs):
        seen["url"] = request.full_url
        seen["portkey_key"] = request.headers.get("X-portkey-api-key")
        seen["authorization"] = request.headers.get("Authorization")
        return Response()

    monkeypatch.setattr("domain.ai_client.providers.openai_compatible_provider.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(OpenAICompatibleProvider, "_load_remote_model_cache", lambda _self: None)
    models = provider.list_models()

    assert manifest["models"] == []
    assert manifest["supports_invoke"] is True
    assert seen == {
        "url": "https://api.portkey.ai/v1/models",
        "portkey_key": "portkey-key",
        "authorization": None,
    }
    assert [model["model_id"] for model in models] == ["@openai-production/gpt-live"]
    assert models[0]["metadata"]["source"] == "remote_models_endpoint"


def test_assemblyai_uses_its_live_gateway_models_without_bearer_rewriting(monkeypatch):
    from domain.ai_client.providers import _instantiate_manifest_provider, _provider_manifest_map
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "assembly-key")
    manifest = _provider_manifest_map()["assemblyai"]
    provider = _instantiate_manifest_provider(manifest)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"id":"claude-live","name":"Claude Live","context_length":200000,"supported_parameters":["tools","tool_choice","response_format","stream"],"top_provider":{"max_completion_tokens":64000}}]}'

    seen = {}

    def fake_urlopen(request, **_kwargs):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers.get("Authorization")
        return Response()

    monkeypatch.setattr("domain.ai_client.providers.openai_compatible_provider.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(OpenAICompatibleProvider, "_load_remote_model_cache", lambda _self: None)
    models = provider.list_models()

    assert manifest["models"] == []
    assert seen == {
        "url": "https://llm-gateway.assemblyai.com/v1/models",
        "authorization": "assembly-key",
    }
    assert [model["model_id"] for model in models] == ["claude-live"]
    assert models[0]["context_window"] == 200000
    assert models[0]["capabilities"]["tool_calling"] is True
    assert models[0]["capabilities"]["structured_output"] is True
    assert models[0]["metadata"]["source"] == "assemblyai_llm_gateway_models_api"


def test_longcat_and_tencent_hunyuan_use_live_openai_compatible_models_apis(monkeypatch):
    from domain.ai_client.providers import _openai_compatible_spec_manifest
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider
    from domain.ai_client.providers.provider_catalog import OPENAI_COMPATIBLE_PROVIDER_SPECS

    expected = {
        "longcat": ("https://api.longcat.chat/v1/models", "LONGCAT_API_KEY"),
        "tencent-hunyuan": ("https://api.hunyuan.cloud.tencent.com/v1/models", "HUNYUAN_API_KEY"),
    }
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"id":"live-model","owned_by":"account"}]}'

    def fake_urlopen(request, **_kwargs):
        seen.append((request.full_url, request.headers.get("Authorization")))
        return Response()

    monkeypatch.setattr("domain.ai_client.providers.openai_compatible_provider.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(OpenAICompatibleProvider, "_load_remote_model_cache", lambda _self: None)
    for provider_id, (url, env_name) in expected.items():
        monkeypatch.setenv(env_name, provider_id + "-key")
        spec = OPENAI_COMPATIBLE_PROVIDER_SPECS[provider_id]
        provider = OpenAICompatibleProvider.from_manifest(_openai_compatible_spec_manifest(spec))
        models = provider.list_models()
        assert spec["curated_models"] == []
        assert [model["model_id"] for model in models] == ["live-model"]
        assert provider._remote_model_list_path in {"/models", "/v1/models"}
    assert seen == [
        ("https://api.longcat.chat/v1/models", "Bearer longcat-key"),
        ("https://api.hunyuan.cloud.tencent.com/v1/models", "Bearer tencent-hunyuan-key"),
    ]


def test_ibm_watsonx_uses_live_foundation_model_specs(monkeypatch):
    import json

    from domain.ai_client.providers.ibm_watsonx_provider import IBMWatsonxProvider

    monkeypatch.setenv("WATSONX_API_KEY", "ibm-key")
    monkeypatch.setenv("WATSONX_ACCESS_TOKEN", "ibm-access-token")
    monkeypatch.setenv("WATSONX_BASE_URL", "https://us-south.ml.cloud.ibm.com")
    IBMWatsonxProvider._CACHE.clear()
    seen = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self):
            return json.dumps({"resources": [
                {"model_id": "ibm/granite-live", "label": "Granite Live", "tasks": ["generation"], "model_limits": {"max_sequence_length": 32768}},
                {"model_id": "ibm/embed-live", "label": "Embed Live", "tasks": ["embeddings"]},
            ]}).encode()

    def fake_urlopen(request, **_kwargs):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers.get("Authorization")
        return Response()

    monkeypatch.setattr("domain.ai_client.providers.ibm_watsonx_provider.urllib.request.urlopen", fake_urlopen)
    models = IBMWatsonxProvider().list_models()

    assert seen == {
        "url": "https://us-south.ml.cloud.ibm.com/ml/v1/foundation_model_specs?version=2024-05-31&tech_preview=true",
        "authorization": "Bearer ibm-access-token",
    }
    assert [model["model_id"] for model in models] == ["ibm/granite-live", "ibm/embed-live"]
    assert models[0]["context_window"] == 32768
    assert models[1]["type"] == "embedding"


def test_ai21_derives_current_models_from_its_official_machine_readable_document(monkeypatch):
    from domain.ai_client.providers.ai21_provider import AI21Provider
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    provider = AI21Provider(api_key="ai21-key")
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self):
            return b"## Model Details\n| API Endpoint |\n| `jamba-live` |\n## API Versioning\n* `jamba-live` currently points to `jamba-live-2026`\n## Model Deprecation\n| `jamba-old` |"
    seen = {}
    def fake_urlopen(request, **_kwargs):
        seen["url"] = request.full_url
        return Response()
    monkeypatch.setattr("domain.ai_client.providers.ai21_provider.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(OpenAICompatibleProvider, "_load_remote_model_cache", lambda _self: None)
    models = provider.list_models()
    assert seen["url"] == AI21Provider.MODEL_DOCUMENT_URL
    assert [model["model_id"] for model in models] == ["jamba-live", "jamba-live-2026"]
    assert models[0]["metadata"]["source"] == "ai21_official_model_document"


def test_bfl_derives_all_model_endpoints_from_its_official_openapi_pages(monkeypatch):
    from domain.ai_client.providers.black_forest_labs_provider import BlackForestLabsProvider

    monkeypatch.setenv("BFL_API_KEY", "bfl-key")
    BlackForestLabsProvider._CACHE.clear()
    pages = {
        BlackForestLabsProvider.DOC_INDEX_URL: b"- [One](https://docs.bfl.ml/api-reference/models/one.md)\n- [Two](https://docs.bfl.ml/api-reference/models/two.md)\n",
        "https://docs.bfl.ml/api-reference/models/one.md": b"# FLUX One\n````yaml https://api.bfl.ai/openapi.json post /v1/flux-one\n",
        "https://docs.bfl.ml/api-reference/models/two.md": b"# FLUX Two\n````yaml https://api.bfl.ai/openapi.json post /v1/flux-two\n",
    }
    class Response:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return self.body
    def fake_urlopen(request, **_kwargs):
        url = request if isinstance(request, str) else request.full_url
        return Response(pages[url])
    monkeypatch.setattr("domain.ai_client.providers.black_forest_labs_provider.urllib.request.urlopen", fake_urlopen)
    models = BlackForestLabsProvider().list_models()
    assert [model["model_id"] for model in models] == ["flux-one", "flux-two"]
    assert all(model["metadata"]["source"] == "bfl_official_openapi_catalog" for model in models)


def test_voyage_derives_embedding_models_from_its_official_current_model_table(monkeypatch):
    from domain.ai_client.providers.voyage_ai_provider import VoyageAIProvider
    monkeypatch.setenv("VOYAGE_API_KEY", "voyage-key")
    VoyageAIProvider._CACHE.clear()
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return b"<h2>Model Choices</h2><code class='rdmd-code'>voyage-live-4</code><code>voyage-code-live</code>Need help deciding"
    monkeypatch.setattr("domain.ai_client.providers.voyage_ai_provider.urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    models = VoyageAIProvider().list_models()
    assert [model["model_id"] for model in models] == ["voyage-live-4", "voyage-code-live"]
    assert all(model["type"] == "embedding" for model in models)


def test_genspark_exposes_its_account_models_endpoint_without_a_static_catalog(monkeypatch):
    from domain.ai_client.providers import _instantiate_manifest_provider, _provider_manifest_map
    from domain.ai_client.providers import genspark_provider
    monkeypatch.setenv("GENSPARK_API_KEY", "genspark-key")
    manifest = _provider_manifest_map()["genspark"]
    provider = _instantiate_manifest_provider(manifest)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return b'{"data":[{"id":"account-live-model"}]}'
    seen = {}
    def fake_urlopen(request, **_kwargs):
        seen["url"] = request.full_url; seen["authorization"] = request.headers.get("Authorization")
        return Response()
    monkeypatch.setattr(genspark_provider.urllib.request, "urlopen", fake_urlopen)
    models = provider.list_models()
    assert manifest["models"] == []
    assert seen == {"url": "https://www.genspark.ai/api/llm_proxy/v1/models", "authorization": "Bearer genspark-key"}
    assert [model["model_id"] for model in models] == ["account-live-model"]


def test_google_vertex_uses_project_deployments_as_the_live_inventory(monkeypatch):
    import json
    from domain.ai_client.providers.google_vertex_ai_provider import GoogleVertexAIProvider
    monkeypatch.setenv("VERTEX_AI_ACCESS_TOKEN", "vertex-token")
    monkeypatch.setenv("VERTEX_AI_BASE_URL", "https://us-central1-aiplatform.googleapis.com/v1/projects/demo/locations/us-central1")
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return json.dumps({"endpoints":[{"name":"projects/demo/locations/us-central1/endpoints/endpoint-1","deployedModels":[{"id":"deployment-1","displayName":"Project Model","model":"projects/demo/locations/us-central1/models/model-1"}]}]}).encode()
    seen={}
    def fake_urlopen(request, **_kwargs):
        seen["url"]=request.full_url; seen["authorization"]=request.headers.get("Authorization"); return Response()
    monkeypatch.setattr("domain.ai_client.providers.google_vertex_ai_provider.urllib.request.urlopen",fake_urlopen)
    models=GoogleVertexAIProvider().list_models()
    assert seen=={"url":"https://us-central1-aiplatform.googleapis.com/v1/projects/demo/locations/us-central1/endpoints","authorization":"Bearer vertex-token"}
    assert [model["model_id"] for model in models]==["endpoint-1/deployment-1"]
    assert models[0]["metadata"]["source"]=="vertex_endpoint_deployments_api"


def test_fal_discovers_every_page_and_uses_the_universal_queue_protocol(monkeypatch):
    import json

    from domain.ai_client.providers.fal_ai_provider import FalAIProvider

    monkeypatch.setenv("FAL_KEY", "fal-key")
    FalAIProvider._INVENTORY_CACHE.clear()
    provider = FalAIProvider()
    requested = []
    responses = {
        "https://api.fal.ai/v1/models": {
            "models": [{"endpoint_id": "fal-ai/flux/live", "metadata": {"display_name": "Live Flux", "category": "text-to-image"}}],
            "next_cursor": "second-page",
        },
        "https://api.fal.ai/v1/models?cursor=second-page": {
            "models": [{"endpoint_id": "fal-ai/voice/live", "metadata": {"category": "text-to-speech"}}],
            "next_cursor": None,
        },
        "https://queue.fal.run/fal-ai/flux/live": {
            "request_id": "request-1",
            "status_url": "https://queue.fal.run/fal-ai/flux/live/requests/request-1/status",
            "response_url": "https://queue.fal.run/fal-ai/flux/live/requests/request-1/response",
        },
        "https://queue.fal.run/fal-ai/flux/live/requests/request-1/status": {"status": "COMPLETED"},
        "https://queue.fal.run/fal-ai/flux/live/requests/request-1/response": {"images": [{"url": "https://fal.media/live.png"}]},
    }

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        requested.append((request.full_url, request.get_method(), request.headers.get("Authorization"), request.data))
        return Response(responses[request.full_url])

    monkeypatch.setattr("domain.ai_client.providers.fal_ai_provider.urllib.request.urlopen", fake_urlopen)
    models = provider.list_models()
    image = provider.image_gen("fal-ai/fal-ai/flux/live", "a live model", {})

    assert [model["model_id"] for model in models] == ["fal-ai/flux/live", "fal-ai/voice/live"]
    assert [model["type"] for model in models] == ["image_gen", "tts"]
    assert image["images"] == ["https://fal.media/live.png"]
    assert requested[0][:3] == ("https://api.fal.ai/v1/models", "GET", "Key fal-key")
    assert requested[1][:3] == ("https://api.fal.ai/v1/models?cursor=second-page", "GET", "Key fal-key")
    assert requested[2][:3] == ("https://queue.fal.run/fal-ai/flux/live", "POST", "Key fal-key")
    assert json.loads(requested[2][3]) == {"prompt": "a live model"}


def test_openai_compatible_inventory_accepts_common_catalog_envelopes_and_same_origin_next_links(monkeypatch):
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        provider_id="gateway",
        api_key="gateway-key",
        base_url="https://gateway.example/v1",
        known_models=[],
        remote_model_discovery=True,
    )
    responses = {
        "https://gateway.example/v1/models": {
            "result": {
                "items": [
                    {"name": "account-chat", "features": ["chat-completions", "function-calling"]},
                    {"slug": "account-image", "task": "image-generation", "capabilities": ["text-to-image"]},
                ]
            },
            "links": {"next": "https://gateway.example/v1/models?page=2"},
        },
        "https://gateway.example/v1/models?page=2": {
            "models": ["account-embed"],
            "next_page_token": "",
        },
    }
    requested = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            import json

            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        requested.append(request.full_url)
        return Response(responses[request.full_url])

    monkeypatch.setattr("domain.ai_client.providers.openai_compatible_provider.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(OpenAICompatibleProvider, "_load_remote_model_cache", lambda _self: None)

    models = provider.list_models()

    assert requested == ["https://gateway.example/v1/models", "https://gateway.example/v1/models?page=2"]
    assert [model["model_id"] for model in models] == ["account-chat", "account-image", "account-embed"]
    assert models[0]["capabilities"]["tool_calling"] is True
    assert models[1]["type"] == "image_gen"
    assert models[1]["capabilities"]["image_generation"] is True
    assert models[2]["type"] == "embedding"


def test_github_models_uses_its_account_catalog_and_openai_compatible_inference_endpoint():
    from domain.ai_client.providers import _openai_compatible_spec_manifest
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider
    from domain.ai_client.providers.provider_catalog import OPENAI_COMPATIBLE_PROVIDER_SPECS

    spec = OPENAI_COMPATIBLE_PROVIDER_SPECS["github-models"]
    manifest = _openai_compatible_spec_manifest(spec)
    provider = OpenAICompatibleProvider.from_manifest(manifest)

    assert spec["curated_models"] == []
    assert provider._base_url == "https://models.github.ai/inference"
    assert provider._remote_model_base_url == "https://models.github.ai/catalog"
    assert provider._headers()["X-GitHub-Api-Version"] == "2026-03-10"
    page, cursor = provider._remote_models_page([{"id": "openai/gpt-4.1", "name": "OpenAI GPT-4.1"}])
    assert page == [{"id": "openai/gpt-4.1", "name": "OpenAI GPT-4.1"}]
    assert cursor == ""


def test_openai_compatible_provider_specs_do_not_freeze_glm_dashscope_or_siliconflow_models():
    from domain.ai_client.providers import _openai_compatible_spec_manifest
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider
    from domain.ai_client.providers.provider_catalog import OPENAI_COMPATIBLE_PROVIDER_SPECS

    expected_endpoints = {
        "alibaba-dashscope": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "glm": "https://api.z.ai/api/paas/v4",
        "siliconflow": "https://api.siliconflow.cn/v1",
    }
    for provider_id, endpoint in expected_endpoints.items():
        spec = OPENAI_COMPATIBLE_PROVIDER_SPECS[provider_id]
        assert spec["curated_models"] == []
        provider = OpenAICompatibleProvider.from_manifest(_openai_compatible_spec_manifest(spec))
        assert provider._base_url == endpoint
        assert provider._remote_model_list_path == "/models"


def test_openai_compatible_provider_specs_use_live_models_endpoints_instead_of_release_lists():
    from domain.ai_client.providers import _openai_compatible_spec_manifest
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider
    from domain.ai_client.providers.provider_catalog import OPENAI_COMPATIBLE_PROVIDER_SPECS

    for provider_id in (
        "xai", "groq", "together", "deepseek", "fireworks", "cerebras", "sambanova", "perplexity", "mistral", "novita", "deepinfra",
        "friendli", "hyperbolic", "inference-net", "upstage",
        "moonshotai", "nvidia", "nebius", "avian",
    ):
        spec = OPENAI_COMPATIBLE_PROVIDER_SPECS[provider_id]
        assert spec["remote_model_discovery"] is True
        assert spec["curated_models"] == []
        provider = OpenAICompatibleProvider.from_manifest(_openai_compatible_spec_manifest(spec))
        assert provider._remote_model_list_path == ("/models/list" if provider_id == "deepinfra" else "/models")

    perplexity = OpenAICompatibleProvider.from_manifest(
        _openai_compatible_spec_manifest(OPENAI_COMPATIBLE_PROVIDER_SPECS["perplexity"])
    )
    assert perplexity._base_url == "https://api.perplexity.ai/v1"
    novita = OpenAICompatibleProvider.from_manifest(
        _openai_compatible_spec_manifest(OPENAI_COMPATIBLE_PROVIDER_SPECS["novita"])
    )
    assert novita._base_url == "https://api.novita.ai/openai/v1"
    deepinfra = OpenAICompatibleProvider.from_manifest(
        _openai_compatible_spec_manifest(OPENAI_COMPATIBLE_PROVIDER_SPECS["deepinfra"])
    )
    assert deepinfra._remote_model_base_url == "https://api.deepinfra.com"
    assert deepinfra._remote_model_list_path == "/models/list"
    deepinfra_model = deepinfra._normalize_remote_model(
        {"model_name": "deepinfra-live-embedding", "type": "embeddings"}
    )
    assert deepinfra_model is not None
    assert deepinfra_model["model_id"] == "deepinfra-live-embedding"
    assert deepinfra_model["type"] == "embedding"
    kimi = OpenAICompatibleProvider.from_manifest(
        _openai_compatible_spec_manifest(OPENAI_COMPATIBLE_PROVIDER_SPECS["moonshotai"])
    )
    kimi_model = kimi._normalize_remote_model(
        {"id": "kimi-live", "supports_image_in": True, "supports_reasoning": True}
    )
    assert kimi_model is not None
    assert kimi_model["capabilities"]["vision"] is True
    assert kimi_model["capabilities"]["reasoning"] is True

    xai_model = OpenAICompatibleProvider._normalize_remote_model(
        OpenAICompatibleProvider.from_manifest(_openai_compatible_spec_manifest(OPENAI_COMPATIBLE_PROVIDER_SPECS["xai"])),
        {"id": "grok-imagine-video", "output_modalities": ["video"]},
    )
    assert xai_model is not None
    assert xai_model["type"] == "video_gen"
    xai_vision_model = OpenAICompatibleProvider._normalize_remote_model(
        OpenAICompatibleProvider.from_manifest(_openai_compatible_spec_manifest(OPENAI_COMPATIBLE_PROVIDER_SPECS["xai"])),
        {"id": "grok-vision", "input_modalities": ["text", "image"], "output_modalities": ["text"]},
    )
    assert xai_vision_model is not None
    assert xai_vision_model["type"] == "chat"
    assert xai_vision_model["capabilities"]["vision"] is True
    assert xai_vision_model["capabilities"]["image_generation"] is False


def test_openai_compatible_manifest_never_exposes_a_checked_in_model_snapshot():
    from domain.ai_client.providers import _openai_compatible_spec_manifest
    from domain.ai_client.providers.provider_catalog import OPENAI_COMPATIBLE_PROVIDER_SPECS

    assert OPENAI_COMPATIBLE_PROVIDER_SPECS
    assert all(
        _openai_compatible_spec_manifest(spec)["models"] == []
        for spec in OPENAI_COMPATIBLE_PROVIDER_SPECS.values()
    )


def test_external_provider_catalog_never_uses_curated_model_fallbacks(monkeypatch):
    from domain.ai_client import providers

    monkeypatch.setattr(providers, "_load_model_manifests", lambda _provider_id: [])
    monkeypatch.setattr(providers, "model_manifests_from_provider_components", lambda _provider_id: [])
    monkeypatch.setattr(providers, "_load_known_models_from_entry", lambda _entrypoint: [])
    monkeypatch.setattr(providers, "get_extension_registry", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))

    assert providers._load_models_for_provider({"provider_id": "openrouter", "entrypoint": ""}) == []
    assert providers.get_best_model_for_provider("openrouter") is None


def test_external_provider_catalog_ignores_pack_model_manifests(monkeypatch):
    from domain.ai_client import providers

    monkeypatch.setattr(providers, "_load_model_manifests", lambda _provider_id: [{"model_id": "stale"}])
    monkeypatch.setattr(
        providers,
        "model_manifests_from_provider_components",
        lambda _provider_id: [{"model_id": "also-stale"}],
    )
    monkeypatch.setattr(
        providers,
        "_load_known_models_from_entry",
        lambda _entrypoint: [{"model_id": "still-stale"}],
    )

    assert providers._load_models_for_provider({"provider_id": "openrouter", "entrypoint": "ignored"}) == []


def test_every_external_provider_starts_without_a_checked_in_model_inventory():
    from domain.ai_client import providers

    catalog = providers.get_provider_catalog()
    stale = {
        entry["provider_id"]: providers._load_models_for_provider(entry)
        for entry in catalog
        if entry["provider_id"] not in {"rumi", "stub"}
    }

    assert {provider_id: models for provider_id, models in stale.items() if models} == {}


def test_named_openai_compatible_connection_activates_a_program_placeholder(monkeypatch):
    from domain.ai_client import providers

    named_connection = {
        "provider_id": "ai21",
        "api_id": "project",
        "name": "project",
        "configured": True,
        "kind": "llm",
        "base_url": "https://gateway.example/v1",
        "credential_mode": "api_key",
        "allowed_models": ["stale-model"],
        "default_model": "stale-default",
    }
    monkeypatch.setattr(providers, "list_custom_providers", lambda: [])
    monkeypatch.setattr(providers, "provider_named_api_keys", lambda *_args, **_kwargs: [named_connection])
    monkeypatch.setattr(providers, "read_provider_api_key", lambda *_args, **_kwargs: "project-token")

    manifest = providers._provider_manifest_map()["ai21"]
    provider = providers._instantiate_manifest_provider(manifest)

    assert manifest["adapter"] == "openai_compatible"
    assert manifest.get("models", []) == []
    assert manifest["config"]["custom_openai_compatible"] is True
    assert provider.provider_id == "ai21"
    assert provider._api_key == "project-token"
    assert provider._base_url == "https://gateway.example/v1"


def test_every_connection_required_program_provider_can_use_a_saved_live_endpoint(monkeypatch):
    from domain.ai_client import providers
    from domain.ai_client.provider_program import provider_program_manifests

    program = provider_program_manifests()
    raw_manifests = providers._provider_manifest_map()
    connection_required_ids = sorted(
        provider_id
        for provider_id in program
        if str(raw_manifests[provider_id].get("adapter") or "") == "connection_required"
    )
    connections = [
        {
            "provider_id": provider_id,
            "api_id": "main",
            "name": "main",
            "configured": True,
            "kind": "llm",
            "base_url": f"https://{provider_id}.example/v1",
            "credential_mode": "api_key",
        }
        for provider_id in connection_required_ids
    ]
    monkeypatch.setattr(providers, "list_custom_providers", lambda: [])
    monkeypatch.setattr(providers, "provider_named_api_keys", lambda *_args, **_kwargs: connections)

    configured = providers._provider_manifest_map()

    assert connection_required_ids
    assert all(configured[provider_id]["adapter"] == "openai_compatible" for provider_id in connection_required_ids)
    assert all(configured[provider_id]["models"] == [] for provider_id in connection_required_ids)
    assert all(configured[provider_id]["config"]["custom_openai_compatible"] for provider_id in connection_required_ids)


def test_every_openai_compatible_program_provider_uses_its_live_models_endpoint(monkeypatch):
    """Every compatible provider must expose what its connected endpoint serves."""
    from domain.ai_client import providers
    from domain.ai_client.provider_program import provider_program_manifests
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    manifests = providers._provider_manifest_map()
    compatible_ids = sorted(
        provider_id
        for provider_id in provider_program_manifests()
        if str(manifests[provider_id].get("adapter") or "") == "openai_compatible"
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"id":"account-visible-model"}],"models":[{"key":"account-visible-model","type":"llm"}]}'

    seen = []

    def fake_urlopen(request, **_kwargs):
        seen.append(request.full_url)
        return Response()

    monkeypatch.setattr("domain.ai_client.providers.openai_compatible_provider.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(OpenAICompatibleProvider, "_load_remote_model_cache", lambda _self: None)
    monkeypatch.setattr(OpenAICompatibleProvider, "_save_remote_model_cache", lambda *_args, **_kwargs: None)

    direct_endpoint_ids = []
    custom_discovery_ids = []
    for provider_id in compatible_ids:
        provider = providers._instantiate_manifest_provider(manifests[provider_id])
        assert provider is not None, provider_id
        if type(provider)._fetch_remote_models is not OpenAICompatibleProvider._fetch_remote_models:
            custom_discovery_ids.append(provider_id)
            continue
        direct_endpoint_ids.append(provider_id)
        provider._api_key = f"{provider_id}-token"
        models = provider.list_models()
        assert [model["model_id"] for model in models] == ["account-visible-model"], provider_id

    # A provider with a vendor-specific live catalog is valid too; it must be
    # explicit rather than silently falling back to a bundled model list.
    assert custom_discovery_ids == ["ai21"]
    assert len(seen) == len(direct_endpoint_ids)


def test_every_python_entrypoint_program_provider_uses_its_live_models_endpoint(monkeypatch):
    """Gateway and local bespoke adapters may not regress to fixed allowlists."""
    from domain.ai_client import providers
    from domain.ai_client.provider_program import provider_program_manifests
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    manifests = providers._provider_manifest_map()
    entrypoint_ids = sorted(
        provider_id
        for provider_id in provider_program_manifests()
        if str(manifests[provider_id].get("adapter") or "") == "python_entrypoint"
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"id":"account-visible-model"}],"models":[{"key":"account-visible-model","type":"llm"}]}'

    seen = []

    def fake_urlopen(request, **_kwargs):
        seen.append(request.full_url)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(OpenAICompatibleProvider, "_load_remote_model_cache", lambda _self: None)
    monkeypatch.setattr(OpenAICompatibleProvider, "_save_remote_model_cache", lambda *_args, **_kwargs: None)

    for provider_id in entrypoint_ids:
        provider = providers._instantiate_manifest_provider(manifests[provider_id])
        assert provider is not None, provider_id
        assert not getattr(provider, "KNOWN_MODELS", []), provider_id
        if hasattr(provider, "_api_key"):
            provider._api_key = f"{provider_id}-token"
        models = provider.list_models()
        assert [model["model_id"] for model in models] == ["account-visible-model"], provider_id

    # Zen has a native list endpoint; the remaining entrypoints share the
    # compatible adapter's connection-scoped cache.  No fixed catalog may
    # satisfy this assertion, regardless of whether a fresh cache was needed.
    assert seen


def test_saved_connection_fetches_the_account_visible_models_for_every_connection_backed_placeholder(monkeypatch):
    """A saved connection, not a checked-in list, is the inventory source.

    These providers do not have a universal public model inventory.  Once a
    user supplies an OpenAI-compatible gateway URL and credential, the saved
    endpoint must be the one queried for both discovery and later invocation.
    """
    from domain.ai_client import providers
    from domain.ai_client.provider_program import provider_program_manifests
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    raw_manifests = providers._provider_manifest_map()
    connection_backed_ids = sorted(
        provider_id
        for provider_id in provider_program_manifests()
        if (
            str(raw_manifests[provider_id].get("adapter") or "") in {"connection_required", "catalog_only"}
            or provider_id == "openai_compatible"
        )
    )
    connections = [
        {
            "provider_id": provider_id,
            "api_id": "main",
            "name": "main",
            "configured": True,
            "kind": "llm",
            "base_url": f"https://{provider_id}.example/v1",
            "credential_mode": "api_key",
        }
        for provider_id in connection_backed_ids
    ]

    class Response:
        def __init__(self, provider_id):
            self._provider_id = provider_id

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return ('{"data":[{"id":"' + self._provider_id + '-visible-model"}]}').encode("utf-8")

    seen = []

    def fake_urlopen(request, **_kwargs):
        provider_id = request.full_url.removeprefix("https://").split(".example/", 1)[0]
        seen.append((request.full_url, request.headers.get("Authorization")))
        return Response(provider_id)

    monkeypatch.setattr(providers, "list_custom_providers", lambda: [])
    monkeypatch.setattr(providers, "provider_named_api_keys", lambda *_args, **_kwargs: connections)
    monkeypatch.setattr(providers, "read_provider_api_key", lambda provider_id, *_args: f"{provider_id}-token")
    monkeypatch.setattr("domain.ai_client.providers.openai_compatible_provider.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(OpenAICompatibleProvider, "_load_remote_model_cache", lambda _self: None)
    monkeypatch.setattr(OpenAICompatibleProvider, "_save_remote_model_cache", lambda *_args, **_kwargs: None)

    configured = providers._provider_manifest_map()
    for provider_id in connection_backed_ids:
        provider = providers._instantiate_manifest_provider(configured[provider_id])
        assert provider is not None
        models = provider.list_models()
        assert [model["model_id"] for model in models] == [f"{provider_id}-visible-model"]

    assert seen == [
        (f"https://{provider_id}.example/v1/models", f"Bearer {provider_id}-token")
        for provider_id in connection_backed_ids
    ]


def test_anthropic_models_endpoint_paginates_and_replaces_its_static_fallback(monkeypatch):
    from domain.ai_client.client import AIClient
    from domain.ai_client.providers.anthropic_provider import AnthropicProvider

    pages = {
        "": {
            "data": [{"id": "claude-live-a", "display_name": "Claude Live A", "capabilities": {"thinking": {"supported": True}}}],
            "has_more": True,
            "last_id": "claude-live-a",
        },
        "claude-live-a": {
            "data": [{"id": "claude-live-b", "display_name": "Claude Live B", "capabilities": {"image_input": {"supported": True}}}],
            "has_more": False,
        },
    }
    monkeypatch.setattr(AnthropicProvider, "_fetch_models_page", lambda self, after_id="": pages[after_id])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-token")
    AnthropicProvider._MODEL_INVENTORY_CACHE.clear()
    AIClient._instance = None

    models = AIClient().list_models(provider="anthropic")

    assert [model["model_id"] for model in models] == ["claude-live-a", "claude-live-b"]
    assert all(model["metadata"]["source"] == "native_models_endpoint" for model in models)
    assert "vision" in models[1]["capabilities"]


def test_openai_spec_uses_live_models_endpoint_without_a_checked_in_model_list():
    from domain.ai_client.providers import _openai_compatible_spec_manifest
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider
    from domain.ai_client.providers.provider_catalog import OPENAI_COMPATIBLE_PROVIDER_SPECS

    spec = OPENAI_COMPATIBLE_PROVIDER_SPECS["openai"]
    assert spec["curated_models"] == []
    provider = OpenAICompatibleProvider.from_manifest(_openai_compatible_spec_manifest(spec))
    assert provider._base_url == "https://api.openai.com/v1"
    assert provider._remote_model_list_path == "/models"


def test_native_openai_models_endpoint_replaces_its_static_fallback(monkeypatch):
    from domain.ai_client.providers.openai_provider import OpenAIProvider

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-token")
    OpenAIProvider._MODEL_INVENTORY_CACHE.clear()
    provider = OpenAIProvider()
    monkeypatch.setattr(
        provider,
        "_fetch_live_models",
        lambda: [
            {"id": "account-chat", "owned_by": "project"},
            {"id": "account-embedding", "owned_by": "project"},
            {"id": "account-image", "owned_by": "project"},
        ],
    )

    models = provider.list_models()

    assert [model["model_id"] for model in models] == [
        "account-chat",
        "account-embedding",
        "account-image",
    ]
    assert [model["type"] for model in models] == ["chat", "embedding", "image_gen"]
    assert all(model["metadata"]["source"] == "native_models_endpoint" for model in models)
    assert OpenAIProvider.KNOWN_MODELS == []


def test_gitlawb_gateway_uses_live_models_without_a_client_side_allowlist(monkeypatch):
    from domain.ai_client.providers.gitlawb_opengateway_provider import GitlawbOpengatewayProvider

    monkeypatch.setenv("GITLAWB_OPENGATEWAY_API_KEY", "test-gateway-token")
    provider = GitlawbOpengatewayProvider()
    monkeypatch.setattr(
        provider,
        "_remote_discovered_models",
        lambda: [{
            "id": "gitlawb-opengateway/account-visible-model",
            "model_id": "account-visible-model",
            "provider_id": "gitlawb-opengateway",
            "provider": "gitlawb-opengateway",
            "type": "chat",
            "metadata": {"source": "remote_models_endpoint"},
        }],
    )

    models = provider.list_models()

    assert [model["model_id"] for model in models] == ["account-visible-model"]
    provider._assert_supported_model("newly-provisioned-model")


def test_google_models_endpoint_paginates_and_replaces_the_curated_fallback(monkeypatch):
    from domain.ai_client.client import AIClient
    from domain.ai_client.providers.google_provider import GoogleProvider

    pages = {
        "": {
            "models": [{"name": "models/gemini-live-a", "displayName": "Gemini Live A", "supportedGenerationMethods": ["generateContent"]}],
            "nextPageToken": "page-two",
        },
        "page-two": {
            "models": [{"name": "models/gemini-live-b", "displayName": "Gemini Live B", "supportedGenerationMethods": ["embedContent"]}],
        },
    }
    monkeypatch.setattr(GoogleProvider, "_fetch_native_models_page", lambda self, token="": pages[token])
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-token")
    GoogleProvider._MODEL_INVENTORY_CACHE.clear()
    AIClient._instance = None

    models = AIClient().list_models(provider="google")

    assert [model["model_id"] for model in models] == ["gemini-live-a", "gemini-live-b"]
    assert all(model["metadata"]["source"] == "native_models_endpoint" for model in models)


def test_cohere_models_endpoint_paginates_and_uses_native_chat_adapter(monkeypatch):
    from domain.ai_client.providers.cohere_provider import CohereProvider

    pages = {
        "": {
            "models": [{"name": "command-live-a", "endpoints": ["chat"], "features": ["chat-completions"], "context_length": 128000}],
            "next_page_token": "page-two",
        },
        "page-two": {
            "models": [{"name": "embed-live-b", "endpoints": ["embed"], "features": ["embeddings"], "context_length": 1024}],
        },
    }
    monkeypatch.setenv("COHERE_API_KEY", "test-cohere-token")
    CohereProvider._MODEL_INVENTORY_CACHE.clear()
    provider = CohereProvider()
    monkeypatch.setattr(provider, "_fetch_models_page", lambda token="": pages[token])

    models = provider.list_models()

    assert [model["model_id"] for model in models] == ["command-live-a", "embed-live-b"]
    assert [model["type"] for model in models] == ["chat", "embedding"]
    assert all(model["metadata"]["source"] == "native_models_endpoint" for model in models)

    captured = {}

    def fake_request(method, path, body=None):
        captured.update({"method": method, "path": path, "body": body})
        return {
            "id": "cohere-response",
            "finish_reason": "COMPLETE",
            "message": {"content": [{"type": "text", "text": "live response"}]},
            "usage": {"tokens": {"input_tokens": 3, "output_tokens": 2}},
        }

    monkeypatch.setattr(provider, "_request_json", fake_request)
    response = provider.complete("command-live-a", [{"role": "user", "content": "hello"}], [], {"max_tokens": 32})

    assert captured == {
        "method": "POST",
        "path": "/v2/chat",
        "body": {"model": "command-live-a", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 32},
    }
    assert response["content"] == [{"type": "text", "text": "live response"}]
    assert response["usage"]["total_tokens"] == 5

    def fake_embed_request(method, path, body=None):
        captured.update({"method": method, "path": path, "body": body})
        return {
            "embeddings": {"float": [[0.1, 0.2]]},
            "usage": {"tokens": {"input_tokens": 2}},
        }

    monkeypatch.setattr(provider, "_request_json", fake_embed_request)
    embedding = provider.embed("embed-live-b", "search text")

    assert captured == {
        "method": "POST",
        "path": "/v2/embed",
        "body": {
            "model": "embed-live-b",
            "inputs": [{"content": [{"type": "text", "text": "search text"}]}],
            "input_type": "search_document",
            "embedding_types": ["float"],
        },
    }
    assert embedding == {"embeddings": [[0.1, 0.2]], "usage": {"input_tokens": 2, "total_tokens": 2}}


def test_native_provider_inventory_is_bound_to_the_saved_api_key_without_model_text_input(monkeypatch):
    from domain.ai_client.model_availability import ModelAvailabilityService

    service = ModelAvailabilityService()
    monkeypatch.setattr(
        service,
        "_catalog_models",
        lambda _provider_id: [{"model_id": "account-visible-model", "metadata": {"source": "native_models_endpoint"}}],
    )

    assert service._live_model_ids("cohere") == ["account-visible-model"]


def test_model_availability_discovers_each_named_connection_with_its_own_credentials(monkeypatch):
    import domain.ai_client.client as client_module
    import domain.ai_client.model_availability as availability_module
    from domain.ai_client.model_availability import ModelAvailabilityService

    class Provider:
        _api_key = "primary-key"
        _base_url = "https://primary.example/v1"
        BASE_URL = _base_url

        def list_models(self):
            return [{
                "model_id": "secondary-only" if self._api_key == "secondary-key" else "primary-only",
                "metadata": {"source": "remote_models_endpoint"},
            }]

    runtime_provider = Provider()

    class Client:
        _providers = {"gateway": runtime_provider}

    monkeypatch.setattr(client_module, "AIClient", lambda: Client())
    monkeypatch.setattr(
        availability_module,
        "provider_api_metadata",
        lambda provider_id, api_id, **_kwargs: {"base_url": "https://secondary.example/v1"},
    )
    monkeypatch.setattr(
        availability_module,
        "read_provider_api_key",
        lambda provider_id, api_id, **_kwargs: "secondary-key",
    )

    models = ModelAvailabilityService()._live_model_ids("gateway", "secondary")

    assert models == ["secondary-only"]
    assert runtime_provider._api_key == "primary-key"
    assert runtime_provider._base_url == "https://primary.example/v1"


def test_elevenlabs_discovers_the_key_visible_audio_models_and_invokes_tts(monkeypatch):
    from domain.ai_client.providers import detect_available_providers
    from domain.ai_client.providers.elevenlabs_provider import ElevenLabsProvider

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-eleven-key")
    ElevenLabsProvider._MODEL_INVENTORY_CACHE.clear()
    provider = ElevenLabsProvider()
    requests = []
    monkeypatch.setattr(
        provider,
        "_request_json",
        lambda method, path, body=None: (
            requests.append((method, path, body))
            or [{"model_id": "tts-live", "name": "TTS live", "can_do_text_to_speech": True}]
        ),
    )

    models = provider.list_models()

    assert requests == [("GET", "/v1/models", None)]
    assert models[0]["model_id"] == "tts-live"
    assert models[0]["type"] == "tts"
    assert models[0]["capabilities"]["tts"] is True

    captured = {}
    monkeypatch.setattr(
        provider,
        "_request_audio",
        lambda path, body: captured.update({"path": path, "body": body}) or b"audio",
    )
    response = provider.tts("elevenlabs/tts-live", "hello", "voice id")

    assert captured == {
        "path": "/v1/text-to-speech/voice%20id",
        "body": {"text": "hello", "model_id": "tts-live"},
    }
    assert response["audio"].startswith("data:audio/mpeg;base64,")
    assert isinstance(detect_available_providers()["elevenlabs"], ElevenLabsProvider)


def test_cloudflare_workers_ai_discovers_account_scoped_models_and_runs_text_generation(monkeypatch):
    from domain.ai_client.providers.cloudflare_workers_ai_provider import CloudflareWorkersAIProvider

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-cloudflare-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account-id")
    CloudflareWorkersAIProvider._MODEL_INVENTORY_CACHE.clear()
    provider = CloudflareWorkersAIProvider()
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return {
                "result": [
                    {"id": "@cf/meta/llama", "name": "Llama", "task": {"name": "text-generation"}},
                    {"id": "@cf/stability/image", "name": "Image", "task": {"name": "text-to-image"}},
                    {"id": "@cf/baai/embed", "name": "Embed", "task": {"name": "text-embedding"}},
                ],
                "result_info": {"total_pages": 1},
            }
        return {"result": {"response": "live answer"}}

    monkeypatch.setattr(provider, "_request_json", fake_request)
    models = provider.list_models()
    response = provider.complete("@cf/meta/llama", [{"role": "user", "content": "hello"}], [], {"max_tokens": 8})

    assert calls[0] == ("GET", "/models/search?format=openrouter&page=1&per_page=100", None)
    assert [model["type"] for model in models] == ["chat", "image_gen", "embedding"]
    assert models[1]["capabilities"]["image_generation"] is True
    assert calls[-1] == (
        "POST",
        "/run/@cf/meta/llama",
        {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 8},
    )
    assert response["content"] == [{"type": "text", "text": "live answer"}]


def test_deepgram_discovers_live_stt_tts_models_and_calls_native_tasks(monkeypatch):
    from domain.ai_client.providers.deepgram_provider import DeepgramProvider

    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-deepgram-key")
    DeepgramProvider._MODEL_INVENTORY_CACHE.clear()
    provider = DeepgramProvider()
    requests = []

    def fake_json(method, path, body=None):
        requests.append((method, path, body))
        if method == "GET":
            return {
                "stt": [{"canonical_name": "nova-live", "languages": ["ja"], "streaming": True}],
                "tts": [{"canonical_name": "aura-live", "languages": ["ja"]}],
            }
        return {"results": {"channels": [{"alternatives": [{"transcript": "live transcript"}]}]}}

    monkeypatch.setattr(provider, "_request_json", fake_json)
    models = provider.list_models()
    transcript = provider.transcribe("deepgram/nova-live", "https://audio.example/input.wav", {})

    assert requests[0] == ("GET", "/v1/models?include_outdated=true", None)
    assert [model["type"] for model in models] == ["transcription", "tts"]
    assert requests[-1] == (
        "POST",
        "/v1/listen?model=nova-live",
        {"url": "https://audio.example/input.wav"},
    )
    assert transcript == {"text": "live transcript"}

    captured = {}
    monkeypatch.setattr(
        provider,
        "_request",
        lambda method, path, body=None, **kwargs: captured.update({"method": method, "path": path, "body": body, **kwargs}) or b"audio",
    )
    response = provider.tts("deepgram/aura-live", "hello", None)

    assert captured == {
        "method": "POST",
        "path": "/v1/speak?model=aura-live",
        "body": {"text": "hello"},
        "accept": "audio/mpeg",
    }
    assert response["audio"].startswith("data:audio/mpeg;base64,")


def test_databricks_discovers_workspace_serving_endpoints_and_invokes_selected_endpoint(monkeypatch):
    import json

    from domain.ai_client.providers import detect_available_providers
    from domain.ai_client.providers.databricks_model_serving_provider import DatabricksModelServingProvider

    monkeypatch.setenv("DATABRICKS_TOKEN", "databricks-token")
    monkeypatch.setenv("DATABRICKS_HOST", "https://workspace.cloud.databricks.com")
    seen = []

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        seen.append((request.method, request.full_url, request.headers.get("Authorization"), request.data))
        if request.method == "GET":
            return Response(
                {
                    "endpoints": [
                        {
                            "name": "chat-endpoint",
                            "state": {"ready": "READY"},
                            "config": {"served_entities": [{"name": "catalog.schema.chat-model"}]},
                        },
                        {
                            "name": "embedding-endpoint",
                            "state": {"ready": "READY"},
                            "config": {"served_entities": [{"name": "bge-embedding-model"}]},
                        },
                    ]
                }
            )
        return Response({"choices": [{"message": {"content": "workspace reply"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(
        "domain.ai_client.providers.databricks_model_serving_provider.urllib.request.urlopen",
        fake_urlopen,
    )
    provider = DatabricksModelServingProvider()
    models = provider.list_models()
    assert [model["model_id"] for model in models] == ["chat-endpoint", "embedding-endpoint"]
    assert models[0]["metadata"]["ready"] is True
    assert models[1]["type"] == "embedding"
    response = provider.complete("databricks-model-serving/chat-endpoint", [{"role": "user", "content": "Hi"}], [], {})
    assert response["content"][0]["text"] == "workspace reply"
    assert seen[0][:3] == (
        "GET",
        "https://workspace.cloud.databricks.com/api/2.0/serving-endpoints",
        "Bearer databricks-token",
    )
    assert seen[1][1].endswith("/serving-endpoints/chat-endpoint/invocations")
    assert isinstance(detect_available_providers()["databricks-model-serving"], DatabricksModelServingProvider)


def test_azure_openai_discovers_live_deployments_and_routes_chat_and_embeddings(monkeypatch):
    import json

    from domain.ai_client.providers.azure_openai_provider import AzureOpenAIProvider

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://resource.openai.azure.com")
    seen = []

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        seen.append((request.method, request.full_url, request.headers.get("Api-key"), request.data))
        if request.method == "GET":
            return Response(
                {
                    "data": [
                        {"id": "chat-deployment", "model": {"name": "gpt-live", "version": "1"}},
                        {"id": "embedding-deployment", "model": {"name": "text-embedding-live", "version": "2"}},
                    ]
                }
            )
        if "/embeddings?" in request.full_url:
            return Response({"data": [{"embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 2, "total_tokens": 2}})
        return Response({"choices": [{"message": {"content": "azure reply"}, "finish_reason": "stop"}]})

    monkeypatch.setattr("domain.ai_client.providers.azure_openai_provider.urllib.request.urlopen", fake_urlopen)
    provider = AzureOpenAIProvider()
    models = provider.list_models()
    assert [model["model_id"] for model in models] == ["chat-deployment", "embedding-deployment"]
    assert models[1]["type"] == "embedding"
    answer = provider.complete("azure-openai/chat-deployment", [{"role": "user", "content": "Hi"}], [], {})
    embeddings = provider.embed("azure-openai/embedding-deployment", "hello")
    assert answer["content"][0]["text"] == "azure reply"
    assert embeddings == {"embeddings": [[0.1, 0.2]], "usage": {"input_tokens": 2, "total_tokens": 2}}
    assert seen[0][:3] == (
        "GET",
        "https://resource.openai.azure.com/openai/deployments?api-version=2024-10-21",
        "azure-key",
    )
    assert "/deployments/chat-deployment/chat/completions?" in seen[1][1]
    assert "/deployments/embedding-deployment/embeddings?" in seen[2][1]


def test_azure_ai_foundry_uses_saved_project_connection_for_live_deployments(monkeypatch):
    from domain.ai_client.providers.azure_ai_foundry_provider import AzureAIFoundryProvider

    AzureAIFoundryProvider._MODEL_INVENTORY_CACHE.clear()
    connection = {
        "provider_id": "azure-ai-foundry",
        "api_id": "team-project",
        "configured": True,
        "base_url": "https://resource.services.ai.azure.com/api/projects/team-project",
    }
    monkeypatch.setattr(
        "domain.ai_client.providers.azure_ai_foundry_provider.provider_named_api_keys",
        lambda *_args, **_kwargs: [connection],
    )
    monkeypatch.setattr(
        "domain.ai_client.providers.azure_ai_foundry_provider.read_provider_api_key",
        lambda *_args, **_kwargs: "foundry-key",
    )
    seen = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            import json

            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        seen.append((request.get_method(), request.full_url, request.headers.get("Api-key")))
        if request.get_method() == "GET":
            return Response({
                "value": [
                    {"name": "chat-prod", "properties": {"model": {"name": "gpt-live", "version": "1"}}},
                    {"name": "embed-prod", "model": {"name": "text-embedding-live"}},
                ]
            })
        if "/embeddings?" in request.full_url:
            return Response({"data": [{"embedding": [0.1]}], "usage": {"prompt_tokens": 1, "total_tokens": 1}})
        return Response({"choices": [{"message": {"content": "foundry reply"}, "finish_reason": "stop"}]})

    monkeypatch.setattr("domain.ai_client.providers.azure_ai_foundry_provider.urllib.request.urlopen", fake_urlopen)
    provider = AzureAIFoundryProvider()
    models = provider.list_models()
    answer = provider.complete("azure-ai-foundry/chat-prod", [{"role": "user", "content": "Hi"}], [], {})
    embeddings = provider.embed("azure-ai-foundry/embed-prod", "hello")

    assert [model["model_id"] for model in models] == ["chat-prod", "embed-prod"]
    assert [model["type"] for model in models] == ["chat", "embedding"]
    assert answer["content"][0]["text"] == "foundry reply"
    assert embeddings == {"embeddings": [[0.1]], "usage": {"input_tokens": 1, "total_tokens": 1}}
    assert seen[0] == (
        "GET",
        "https://resource.services.ai.azure.com/api/projects/team-project/deployments?api-version=v1",
        "foundry-key",
    )
    assert seen[1][1] == "https://resource.services.ai.azure.com/openai/deployments/chat-prod/chat/completions?api-version=2024-10-21"
    assert seen[2][1] == "https://resource.services.ai.azure.com/openai/deployments/embed-prod/embeddings?api-version=2024-10-21"


def test_aws_bedrock_lists_the_live_regional_inventory_and_uses_converse(monkeypatch):
    from domain.ai_client.providers.aws_bedrock_provider import AwsBedrockProvider

    AwsBedrockProvider._MODEL_INVENTORY_CACHE.clear()
    monkeypatch.setattr(
        "domain.ai_client.providers.aws_bedrock_provider.provider_named_api_keys",
        lambda *_args, **_kwargs: [{"provider_id": "aws-bedrock", "api_id": "prod", "configured": True, "base_url": "us-west-2"}],
    )
    monkeypatch.setattr(
        "domain.ai_client.providers.aws_bedrock_provider.read_provider_api_key",
        lambda *_args, **_kwargs: "AKIATEST:secret-test:session-test",
    )
    seen = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            import json

            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        seen.append((request.get_method(), request.full_url, request.headers.get("Authorization"), request.headers.get("X-amz-security-token")))
        if request.get_method() == "GET":
            return Response({"modelSummaries": [
                {"modelId": "amazon.nova-pro-v1:0", "modelName": "Nova Pro", "inputModalities": ["TEXT", "IMAGE"], "outputModalities": ["TEXT"], "responseStreamingSupported": True},
                {"modelId": "amazon.titan-embed-text-v2:0", "modelName": "Titan Embed", "inputModalities": ["TEXT"], "outputModalities": ["EMBEDDING"]},
            ]})
        return Response({"output": {"message": {"content": [{"text": "bedrock reply"}]}}, "stopReason": "end_turn", "usage": {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5}})

    monkeypatch.setattr("domain.ai_client.providers.aws_bedrock_provider.urllib.request.urlopen", fake_urlopen)
    provider = AwsBedrockProvider()
    models = provider.list_models()
    answer = provider.complete("aws-bedrock/amazon.nova-pro-v1:0", [{"role": "user", "content": "Hi"}], [], {"temperature": 0.2})

    assert [model["model_id"] for model in models] == ["amazon.nova-pro-v1:0", "amazon.titan-embed-text-v2:0"]
    assert [model["type"] for model in models] == ["chat", "embedding"]
    assert answer["content"] == [{"type": "text", "text": "bedrock reply"}]
    assert answer["usage"]["total_tokens"] == 5
    assert seen[0][0:2] == ("GET", "https://bedrock.us-west-2.amazonaws.com/foundation-models")
    assert seen[1][0:2] == ("POST", "https://bedrock-runtime.us-west-2.amazonaws.com/model/amazon.nova-pro-v1%3A0/converse")
    assert all(str(item[2]).startswith("AWS4-HMAC-SHA256 Credential=AKIATEST/") for item in seen)
    assert all(item[3] == "session-test" for item in seen)


def test_stability_ai_uses_the_account_engines_api_without_a_model_snapshot(monkeypatch):
    from domain.ai_client.providers.stability_ai_provider import StabilityAIProvider

    StabilityAIProvider._MODEL_INVENTORY_CACHE.clear()
    monkeypatch.setattr(
        "domain.ai_client.providers.stability_ai_provider.provider_named_api_keys",
        lambda *_args, **_kwargs: [{"provider_id": "stability-ai", "api_id": "main", "configured": True}],
    )
    monkeypatch.setattr(
        "domain.ai_client.providers.stability_ai_provider.read_provider_api_key",
        lambda *_args, **_kwargs: "stability-key",
    )
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            import json

            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        calls.append((request.get_method(), request.full_url, request.headers.get("Authorization")))
        if request.get_method() == "GET":
            return Response([{"id": "stable-diffusion-xl-1024-v1-0", "name": "SDXL", "type": "PICTURE"}])
        return Response({"artifacts": [{"base64": "image-bytes"}]})

    monkeypatch.setattr("domain.ai_client.providers.stability_ai_provider.urllib.request.urlopen", fake_urlopen)
    provider = StabilityAIProvider()
    models = provider.list_models()
    generated = provider.image_gen("stability-ai/stable-diffusion-xl-1024-v1-0", "Lighthouse", {})

    assert [model["model_id"] for model in models] == ["stable-diffusion-xl-1024-v1-0"]
    assert models[0]["type"] == "image_gen"
    assert generated["images"] == ["data:image/png;base64,image-bytes"]
    assert calls == [
        ("GET", "https://api.stability.ai/v1/engines/list", "Bearer stability-key"),
        ("POST", "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image", "Bearer stability-key"),
    ]


def test_xiaomi_mimo_global_uses_its_official_openai_endpoint_and_live_models(monkeypatch):
    from domain.ai_client import providers
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider

    monkeypatch.setenv("MIMO_API_KEY", "mimo-key")
    manifest = providers._provider_manifest_map()["xiaomi-mimo-global"]
    provider = providers._instantiate_manifest_provider(manifest)
    monkeypatch.setattr(OpenAICompatibleProvider, "_load_remote_model_cache", lambda _self: None)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (
                b'{"data":[{"id":"mimo-v2.5-pro-ultraspeed",'
                b'"name":"MiMo V2.5 Pro UltraSpeed",'
                b'"input_modalities":["text","image","audio","video"],'
                b'"output_modalities":["text"]}]}'
            )

    seen = {}

    def fake_urlopen(request, **_kwargs):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers.get("Authorization")
        return Response()

    monkeypatch.setattr("domain.ai_client.providers.openai_compatible_provider.urllib.request.urlopen", fake_urlopen)

    models = provider.list_models()

    assert manifest["adapter"] == "openai_compatible"
    assert manifest.get("models", []) == []
    assert manifest["default_base_url"] == "https://api.xiaomimimo.com/v1"
    assert provider._base_url == "https://api.xiaomimimo.com/v1"
    assert [model["model_id"] for model in models] == ["mimo-v2.5-pro-ultraspeed"]
    assert models[0]["metadata"]["source"] == "remote_models_endpoint"
    assert seen == {
        "url": "https://api.xiaomimimo.com/v1/models",
        "authorization": "Bearer mimo-key",
    }


def test_replicate_uses_paginated_live_models_and_runs_the_latest_live_version(monkeypatch):
    from domain.ai_client.providers.replicate_provider import ReplicateProvider

    pages = {
        "models": {
            "results": [{"owner": "owner", "name": "first", "latest_version": {"id": "version-a"}, "default_example": {"input": {"prompt": "old"}}}],
            "next": "https://replicate.example/v1/models?cursor=two",
        },
        "https://replicate.example/v1/models?cursor=two": {
            "results": [{"owner": "owner", "name": "second", "latest_version": {"id": "version-b"}, "default_example": {"input": {"text": "old"}}}],
            "next": None,
        },
    }
    monkeypatch.setenv("REPLICATE_API_TOKEN", "test-replicate-token")
    ReplicateProvider._INVENTORY_CACHE.clear()
    provider = ReplicateProvider()
    calls = []

    def fake_request(method, path, body=None, **_kwargs):
        calls.append((method, path, body))
        if method == "GET":
            return pages[path]
        return {"id": "prediction", "status": "succeeded", "output": "live output"}

    monkeypatch.setattr(provider, "_request_json", fake_request)
    models = provider.list_models()
    response = provider.complete("owner/second", [{"role": "user", "content": "new prompt"}], [], {})

    assert [model["model_id"] for model in models] == ["owner/first", "owner/second"]
    assert all(model["metadata"]["source"] == "native_models_endpoint" for model in models)
    assert calls[-1] == ("POST", "predictions", {"version": "owner/second:version-b", "input": {"text": "new prompt"}})
    assert response["content"] == [{"type": "text", "text": "live output"}]

    image = provider.image_gen("owner/first", "draw this", {})

    assert calls[-1] == ("POST", "predictions", {"version": "owner/first:version-a", "input": {"prompt": "draw this"}})
    assert image["images"] == ["live output"]


def test_litellm_proxy_discovers_every_model_served_by_the_configured_gateway():
    from domain.ai_client.providers import _openai_compatible_spec_manifest
    from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider
    from domain.ai_client.providers.provider_catalog import OPENAI_COMPATIBLE_PROVIDER_SPECS

    spec = OPENAI_COMPATIBLE_PROVIDER_SPECS["litellm-proxy"]
    provider = OpenAICompatibleProvider.from_manifest(_openai_compatible_spec_manifest(spec))

    assert spec["curated_models"] == []
    assert provider._base_url == "http://127.0.0.1:4000/v1"
    assert provider._remote_model_list_path == "/models"


def test_saved_litellm_connection_endpoint_overrides_the_builtin_default(monkeypatch):
    import domain.ai_client.providers as providers

    monkeypatch.setattr(providers, "list_custom_providers", lambda: [])
    monkeypatch.setattr(
        providers,
        "provider_named_api_keys",
        lambda provider_id="": [
            {
                "provider_id": "litellm-proxy",
                "api_id": "team",
                "configured": True,
                "kind": "llm",
                "base_url": "https://gateway.example/v1",
            }
        ] if provider_id in {"", "litellm-proxy"} else [],
    )

    manifest = providers._provider_manifest_map()["litellm-proxy"]

    assert manifest["adapter"] == "openai_compatible"
    assert manifest["default_base_url"] == "https://gateway.example/v1"
    assert manifest["models"] == []


def test_live_inventory_removes_stale_bundled_models_from_the_ui_catalog(monkeypatch):
    import ecosystem.defaultspack.backend.ai_client.provider_catalog as catalog

    class Client:
        def list_providers(self):
            return [{"provider_id": "openrouter"}]

        def list_models(self, provider=None):
            assert provider in {None, "openrouter"}
            return [{
                "id": "openrouter/account-visible-model",
                "qualified_model_id": "openrouter/account-visible-model",
                "provider_id": "openrouter",
                "model_id": "account-visible-model",
                "metadata": {"source": "openrouter_models_api"},
            }]

    monkeypatch.setattr(catalog, "_runtime_client", lambda: Client())
    monkeypatch.setattr(
        catalog,
        "get_all_known_models",
        lambda **_kwargs: [{
            "id": "openrouter/stale-bundled-model",
            "qualified_model_id": "openrouter/stale-bundled-model",
            "provider_id": "openrouter",
            "model_id": "stale-bundled-model",
            "metadata": {"source": "openrouter_curated_overlay"},
        }],
    )

    models = catalog.list_model_catalog("openrouter")

    assert [model["model_id"] for model in models] == ["account-visible-model"]


def test_vercel_live_inventory_replaces_its_static_overlay_and_keeps_media_task_types(monkeypatch):
    import ecosystem.defaultspack.backend.ai_client.provider_catalog as catalog
    from domain.ai_client.providers.vercel_ai_gateway_provider import VercelAIGatewayProvider

    provider = VercelAIGatewayProvider(known_models=[])
    image = provider._normalize_remote_model({"id": "fal/image", "type": "image", "output_modalities": ["image"]})
    video = provider._normalize_remote_model({"id": "fal/video", "type": "video", "output_modalities": ["video"]})

    assert image["type"] == "image_gen"
    assert image["capabilities"]["image_generation"] is True
    assert video["type"] == "video_gen"
    assert video["capabilities"]["video_generation"] is True

    class Client:
        def list_providers(self):
            return [{"provider_id": "vercel-ai-gateway"}]

        def list_models(self, provider=None):
            assert provider in {None, "vercel-ai-gateway"}
            return [{
                "id": "vercel-ai-gateway/account-visible-model",
                "qualified_model_id": "vercel-ai-gateway/account-visible-model",
                "provider_id": "vercel-ai-gateway",
                "model_id": "account-visible-model",
                "metadata": {"source": "vercel_ai_gateway_models_api"},
            }]

    monkeypatch.setattr(catalog, "_runtime_client", lambda: Client())
    monkeypatch.setattr(
        catalog,
        "get_all_known_models",
        lambda **_kwargs: [{
            "id": "vercel-ai-gateway/stale-bundled-model",
            "qualified_model_id": "vercel-ai-gateway/stale-bundled-model",
            "provider_id": "vercel-ai-gateway",
            "model_id": "stale-bundled-model",
        }],
    )

    models = catalog.list_model_catalog("vercel-ai-gateway")

    assert [model["model_id"] for model in models] == ["account-visible-model"]
