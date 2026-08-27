"""Canonical provider program records, intentionally without model inventories.

The provider program owns provider identity and inventory strategy.  It does
not contain a per-provider model list: account/server visible model ids belong
to the live inventory adapter, or to a dated official snapshot when an API
cannot enumerate them.
"""

from __future__ import annotations

from typing import Any, Dict


# Derived from the provider-completion program matrix (2026-07-11).  Existing
# component manifests take precedence; these records make every remaining
# required connection visible and give it one canonical inventory strategy.
PROVIDER_PROGRAM_RECORDS = (
    ("ai21", "AI21 Labs", "hosted_expansion", "official_models_api_or_snapshot"),
    (
        "alibaba-dashscope",
        "Alibaba DashScope / Qwen",
        "hosted_expansion",
        "regional_models_api_or_snapshot",
    ),
    ("anthropic", "Anthropic", "direct_hosted", "native_api"),
    ("assemblyai", "AssemblyAI", "task_specific", "generated_official_snapshot"),
    ("avian", "Avian", "direct_hosted", "official_models_api_or_snapshot"),
    ("aws-bedrock", "Amazon Bedrock", "enterprise_control_plane", "regional_control_plane"),
    (
        "azure-ai-foundry",
        "Azure AI Foundry",
        "enterprise_control_plane",
        "project_deployment_control_plane",
    ),
    ("azure-openai", "Azure OpenAI", "enterprise_control_plane", "deployment_control_plane"),
    (
        "baidu-qianfan",
        "Baidu Qianfan / ERNIE",
        "hosted_expansion",
        "regional_models_api_or_snapshot",
    ),
    ("black-forest-labs", "Black Forest Labs", "task_specific", "generated_official_snapshot"),
    ("cerebras", "Cerebras", "direct_hosted", "official_models_api"),
    (
        "cloudflare-ai-gateway",
        "Cloudflare AI Gateway",
        "gateway_expansion",
        "gateway_config_and_logs_api",
    ),
    ("cloudflare-workers-ai", "Cloudflare Workers AI", "hosted_expansion", "account_catalog"),
    ("cohere", "Cohere", "hosted_expansion", "official_models_api_or_snapshot"),
    (
        "databricks-model-serving",
        "Databricks Model Serving",
        "enterprise_control_plane",
        "workspace_endpoint_control_plane",
    ),
    ("deepgram", "Deepgram", "task_specific", "generated_official_snapshot"),
    ("deepinfra", "DeepInfra", "direct_hosted", "official_models_api"),
    ("deepseek", "DeepSeek", "direct_hosted", "official_models_api_or_snapshot"),
    ("elevenlabs", "ElevenLabs", "task_specific", "generated_official_snapshot"),
    ("fal-ai", "fal.ai", "task_specific", "model_registry_api"),
    ("fireworks", "Fireworks AI", "direct_hosted", "official_models_api"),
    ("friendli", "FriendliAI", "direct_hosted", "official_models_api_or_snapshot"),
    ("genspark", "Genspark", "direct_hosted", "openai_compatible_or_snapshot"),
    ("github-models", "GitHub Models", "hosted_expansion", "account_catalog"),
    (
        "gitlawb-opengateway",
        "Gitlawb OpenGateway",
        "gateway_or_regional",
        "contract_allowlist_or_gateway_api",
    ),
    ("glm", "Zhipu GLM", "direct_hosted", "official_models_api_or_snapshot"),
    ("google", "Google Gemini", "direct_hosted", "native_api"),
    (
        "google-vertex-ai",
        "Google Vertex AI / Model Garden",
        "enterprise_control_plane",
        "project_region_control_plane",
    ),
    ("groq", "Groq", "direct_hosted", "official_models_api"),
    ("helicone-gateway", "Helicone Gateway", "gateway_expansion", "gateway_config_api"),
    (
        "huggingface-inference",
        "Hugging Face Inference Providers / Endpoints",
        "hosted_expansion",
        "account_scoped_catalog",
    ),
    ("huggingface-tgi", "Hugging Face TGI", "selfhosted_expansion", "served_models_api_or_manual"),
    ("hyperbolic", "Hyperbolic", "direct_hosted", "official_models_api_or_snapshot"),
    ("ibm-watsonx", "IBM watsonx.ai", "enterprise_control_plane", "project_control_plane"),
    ("inference-net", "Inference.net", "direct_hosted", "official_models_api_or_snapshot"),
    ("jan", "Jan", "selfhosted_expansion", "local_server_api"),
    ("jina-ai", "Jina AI", "hosted_expansion", "generated_official_snapshot"),
    ("litellm-proxy", "LiteLLM Proxy", "gateway_expansion", "proxy_models_api"),
    ("llamacpp", "llama.cpp", "local_or_custom", "native_or_router_api"),
    ("llamafile", "llamafile", "selfhosted_expansion", "served_model_api"),
    ("lmstudio", "LM Studio", "local_or_custom", "native_server_api"),
    ("localai", "LocalAI", "selfhosted_expansion", "native_and_openai_compatible_api"),
    ("longcat", "LongCat", "direct_hosted", "generated_official_snapshot"),
    ("mistral", "Mistral", "direct_hosted", "official_models_api"),
    ("mlc-llm-server", "MLC LLM server", "selfhosted_expansion", "served_model_api"),
    ("mlx-lm-server", "MLX-LM server", "selfhosted_expansion", "served_model_api"),
    ("moonshotai", "Moonshot AI / Kimi", "direct_hosted", "official_models_api_or_snapshot"),
    ("nebius", "Nebius AI Studio", "direct_hosted", "official_models_api_or_snapshot"),
    ("novita", "Novita AI", "direct_hosted", "official_models_api_or_snapshot"),
    ("nvidia", "NVIDIA NIM", "direct_hosted", "official_models_api_or_catalog"),
    ("ollama", "Ollama", "local_or_custom", "native_server_api"),
    ("openai", "OpenAI", "direct_hosted", "native_api"),
    (
        "openai_compatible",
        "Generic OpenAI-compatible connection",
        "local_or_custom",
        "configurable_api_or_manual",
    ),
    ("opencode-go", "OpenCode Go", "gateway_or_regional", "account_scoped_gateway_api"),
    ("opencode-zen", "OpenCode Zen", "gateway_or_regional", "account_scoped_gateway_api"),
    ("openrouter", "OpenRouter", "gateway_or_regional", "account_scoped_gateway_api"),
    (
        "oracle-oci-generative-ai",
        "Oracle OCI Generative AI",
        "enterprise_control_plane",
        "regional_control_plane",
    ),
    ("perplexity", "Perplexity", "direct_hosted", "generated_official_snapshot"),
    (
        "portkey-ai-gateway",
        "Portkey AI Gateway",
        "gateway_expansion",
        "gateway_catalog_and_config_api",
    ),
    ("replicate", "Replicate", "hosted_expansion", "model_registry_api"),
    ("sambanova", "SambaNova", "direct_hosted", "official_models_api_or_snapshot"),
    ("sglang", "SGLang", "selfhosted_expansion", "served_models_api"),
    ("siliconflow", "SiliconFlow", "hosted_expansion", "official_models_api"),
    ("snowflake-cortex", "Snowflake Cortex", "enterprise_control_plane", "account_region_catalog"),
    ("stability-ai", "Stability AI", "task_specific", "generated_official_snapshot"),
    ("tencent-hunyuan", "Tencent Hunyuan", "hosted_expansion", "regional_models_api_or_snapshot"),
    (
        "text-generation-webui",
        "text-generation-webui",
        "selfhosted_expansion",
        "local_server_api_or_manual",
    ),
    ("together", "Together AI", "direct_hosted", "official_models_api"),
    ("upstage", "Upstage", "direct_hosted", "official_models_api_or_snapshot"),
    (
        "vercel-ai-gateway",
        "Vercel AI Gateway",
        "gateway_or_regional",
        "public_and_account_gateway_api",
    ),
    ("vllm", "vLLM", "local_or_custom", "served_models_api"),
    ("voyage-ai", "Voyage AI", "hosted_expansion", "generated_official_snapshot"),
    ("xai", "xAI", "direct_hosted", "official_models_api"),
    ("xiaomi-mimo", "Xiaomi MiMo", "gateway_or_regional", "regional_api_or_snapshot"),
    ("xiaomi-mimo-cn", "Xiaomi MiMo CN", "gateway_or_regional", "regional_api_or_snapshot"),
    ("xiaomi-mimo-global", "Xiaomi MiMo Global", "gateway_or_regional", "regional_api_or_snapshot"),
    (
        "xiaomi-token-plan-ams",
        "Xiaomi Token Plan AMS",
        "gateway_or_regional",
        "plan_scoped_inventory",
    ),
    (
        "xiaomi-token-plan-cn",
        "Xiaomi Token Plan CN",
        "gateway_or_regional",
        "plan_scoped_inventory",
    ),
    (
        "xiaomi-token-plan-sgp",
        "Xiaomi Token Plan SGP",
        "gateway_or_regional",
        "plan_scoped_inventory",
    ),
)


# These runtimes expose an OpenAI-compatible served-model endpoint.  There is
# deliberately no model list here: the server is authoritative for its loaded
# inventory.  A user can override every endpoint through the listed env var.
LOCAL_OPENAI_RUNTIME_CONNECTIONS = {
    "vllm": ("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
    "llamacpp": ("LLAMACPP_BASE_URL", "http://127.0.0.1:8080/v1"),
    "localai": ("LOCALAI_BASE_URL", "http://127.0.0.1:8080/v1"),
    "huggingface-tgi": ("TGI_BASE_URL", "http://127.0.0.1:8080/v1"),
    "jan": ("JAN_BASE_URL", "http://127.0.0.1:1337/v1"),
    "llamafile": ("LLAMAFILE_BASE_URL", "http://127.0.0.1:8080/v1"),
    "mlc-llm-server": ("MLC_LLM_BASE_URL", "http://127.0.0.1:8000/v1"),
    "mlx-lm-server": ("MLX_LM_BASE_URL", "http://127.0.0.1:8080/v1"),
    "sglang": ("SGLANG_BASE_URL", "http://127.0.0.1:30000/v1"),
    "text-generation-webui": ("TEXT_GENERATION_WEBUI_BASE_URL", "http://127.0.0.1:5000/v1"),
}


def provider_program_manifests() -> Dict[str, Dict[str, Any]]:
    manifests: Dict[str, Dict[str, Any]] = {}
    for provider_id, display_name, family, inventory_strategy in PROVIDER_PROGRAM_RECORDS:
        kind = (
            "local"
            if family in {"local_or_custom", "selfhosted_expansion"}
            else (
                "enterprise"
                if family == "enterprise_control_plane"
                else ("gateway" if "gateway" in family else "cloud")
            )
        )
        manifests[provider_id] = {
            "id": provider_id,
            "display_name": display_name,
            "description": "Provider program connection. Configure its account or server before use.",
            "kind": kind,
            "adapter": "connection_required",
            "credential_required": True,
            "catalog_only": True,
            "supports_invoke": False,
            "models": [],
            "config": {
                "provider_program": True,
                "family": family,
                "inventory_strategy": inventory_strategy,
                "connection_required": True,
            },
        }
    return manifests


def local_openai_runtime_manifests() -> Dict[str, Dict[str, Any]]:
    program = provider_program_manifests()
    manifests: Dict[str, Dict[str, Any]] = {}
    for provider_id, (base_url_env, default_base_url) in LOCAL_OPENAI_RUNTIME_CONNECTIONS.items():
        record = program[provider_id]
        manifests[provider_id] = {
            **record,
            "adapter": "openai_compatible",
            "credential_required": False,
            "catalog_only": False,
            "supports_invoke": True,
            "base_url_env": base_url_env,
            "default_base_url": default_base_url,
            "config": {
                **dict(record["config"]),
                "model_sync": "remote_merge",
                "model_list_path": "/models",
                "model_list_requires_auth": False,
            },
        }
    return manifests


def missing_program_provider_ids(manifests: Dict[str, Dict[str, Any]]) -> list[str]:
    """Return required external provider ids absent from the canonical registry."""
    return sorted(set(provider_program_manifests()) - set(manifests))
