from domain.ai_client.providers.openai_compatible_provider import OpenAICompatibleProvider
from domain.ai_client.providers.assemblyai_provider import AssemblyAIProvider
from domain.ai_client.providers.ai21_provider import AI21Provider
from domain.ai_client.providers.portkey_ai_gateway_provider import PortkeyAIGatewayProvider
from domain.ai_client.providers.vercel_ai_gateway_provider import VercelAIGatewayProvider


_OPENAI_COMPATIBLE_PROVIDERS = [
    {
        # OpenAI's authenticated /v1/models inventory is authoritative.  No
        # product-model snapshot is needed to make new account models appear.
        "provider_name": "openai",
        "display_name": "OpenAI",
        "env_vars": ("OPENAI_API_KEY",),
        "base_url_env_vars": ("OPENAI_BASE_URL",),
        "default_base_url": "https://api.openai.com/v1",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # DashScope exposes its account-visible Qwen inventory through the
        # same OpenAI-compatible endpoint used for inference.  Regional
        # endpoints remain configurable through DASHSCOPE_BASE_URL.
        "provider_name": "alibaba-dashscope",
        "display_name": "Alibaba DashScope / Qwen",
        "env_vars": ("DASHSCOPE_API_KEY",),
        "base_url_env_vars": ("DASHSCOPE_BASE_URL",),
        "default_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Z.AI documents this as an OpenAI-compatible REST surface.  The
        # account-visible /models response, not a checked-in GLM snapshot, is
        # the source of truth.
        "provider_name": "glm",
        "display_name": "Zhipu GLM / Z.AI",
        "env_vars": ("GLM_API_KEY",),
        "base_url_env_vars": ("GLM_BASE_URL",),
        "default_base_url": "https://api.z.ai/api/paas/v4",
        "headers": {"Accept-Language": "en-US,en"},
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # SiliconFlow's authenticated OpenAI-compatible /models catalog is
        # authoritative for the models enabled for this account.
        "provider_name": "siliconflow",
        "display_name": "SiliconFlow",
        "env_vars": ("SILICONFLOW_API_KEY",),
        "base_url_env_vars": ("SILICONFLOW_BASE_URL",),
        "default_base_url": "https://api.siliconflow.cn/v1",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # GitHub's catalog is a bare JSON array at /catalog/models, whereas
        # inference uses its OpenAI-compatible /inference endpoint.  Both are
        # account-scoped and authenticated with the same models-scope token.
        "provider_name": "github-models",
        "display_name": "GitHub Models",
        "env_vars": ("GITHUB_TOKEN", "GH_TOKEN"),
        "base_url_env_vars": ("GITHUB_MODELS_BASE_URL",),
        "default_base_url": "https://models.github.ai/inference",
        "remote_model_base_url": "https://models.github.ai/catalog",
        "headers": {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Hugging Face documents this endpoint as OpenAI-compatible and
        # exposes its complete, current chat inventory through GET /v1/models.
        # Do not turn that inventory into a checked-in model JSON snapshot.
        "provider_name": "huggingface-inference",
        "display_name": "Hugging Face Inference Providers",
        "env_vars": ("HF_TOKEN", "HUGGINGFACE_API_KEY"),
        "base_url_env_vars": ("HUGGINGFACE_INFERENCE_BASE_URL",),
        "default_base_url": "https://router.huggingface.co/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Jina serves its account-visible model list from /v1/models and its
        # embedding/chat APIs use the same OpenAI-compatible contract.  Keep
        # this live: a Jina model release must appear without an app update.
        "provider_name": "jina-ai",
        "display_name": "Jina AI",
        "env_vars": ("JINA_API_KEY",),
        "base_url_env_vars": ("JINA_BASE_URL",),
        "default_base_url": "https://api.jina.ai/v1",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Qianfan exposes the models enabled for the authenticated account at
        # GET /v2/models. Keep this live inventory separate from a product
        # snapshot: it includes both platform and account custom models.
        "provider_name": "baidu-qianfan",
        "display_name": "Baidu Qianfan / ERNIE",
        "env_vars": ("QIANFAN_API_KEY",),
        "base_url_env_vars": ("QIANFAN_BASE_URL",),
        "default_base_url": "https://qianfan.baidubce.com/v2",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # LiteLLM Proxy returns the complete, deployment-specific inventory
        # from its OpenAI-compatible /models endpoint. A single proxy can
        # surface models from every upstream provider without copying them
        # into this application.
        "provider_name": "litellm-proxy",
        "display_name": "LiteLLM Proxy",
        "env_vars": ("LITELLM_API_KEY",),
        "base_url_env_vars": ("LITELLM_BASE_URL",),
        "default_base_url": "http://127.0.0.1:4000/v1",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # xAI publishes every model visible to the API key at /v1/models,
        # including language, image, and video models.  Never freeze Grok
        # releases in the launcher.
        "provider_name": "xai",
        "display_name": "xAI",
        "env_vars": ("XAI_API_KEY",),
        "base_url_env_vars": ("XAI_BASE_URL",),
        "default_base_url": "https://api.x.ai/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        "provider_name": "groq",
        "display_name": "Groq",
        "env_vars": ("GROQ_API_KEY",),
        "base_url_env_vars": ("GROQ_BASE_URL",),
        "default_base_url": "https://api.groq.com/openai/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Together's OpenAI-compatible /v1/models inventory is authoritative
        # for both hosted and account-enabled models.
        "provider_name": "together",
        "display_name": "Together",
        "env_vars": ("TOGETHER_API_KEY",),
        "base_url_env_vars": ("TOGETHER_BASE_URL",),
        "default_base_url": "https://api.together.xyz/v1",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # DeepSeek's documented GET /models endpoint returns the current API
        # model inventory, so no release-name snapshot is needed.
        "provider_name": "deepseek",
        "display_name": "DeepSeek",
        "env_vars": ("DEEPSEEK_API_KEY",),
        "base_url_env_vars": ("DEEPSEEK_BASE_URL",),
        "default_base_url": "https://api.deepseek.com/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Fireworks exposes the models available to a key through its
        # OpenAI-compatible /v1/models endpoint.
        "provider_name": "fireworks",
        "display_name": "Fireworks",
        "env_vars": ("FIREWORKS_API_KEY",),
        "base_url_env_vars": ("FIREWORKS_BASE_URL",),
        "default_base_url": "https://api.fireworks.ai/inference/v1",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        "provider_name": "cerebras",
        "display_name": "Cerebras",
        "env_vars": ("CEREBRAS_API_KEY",),
        "base_url_env_vars": ("CEREBRAS_BASE_URL",),
        "default_base_url": "https://api.cerebras.ai/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # SambaNova's /models endpoint is explicitly scoped to the active
        # environment, including self-hosted SambaStack deployments.
        "provider_name": "sambanova",
        "display_name": "SambaNova",
        "env_vars": ("SAMBANOVA_API_KEY",),
        "base_url_env_vars": ("SAMBANOVA_BASE_URL",),
        "default_base_url": "https://api.sambanova.ai/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Perplexity publishes its complete current Agent API inventory at
        # /v1/models in the OpenAI list format.
        "provider_name": "perplexity",
        "display_name": "Perplexity",
        "env_vars": ("PERPLEXITY_API_KEY",),
        "base_url_env_vars": ("PERPLEXITY_BASE_URL",),
        "default_base_url": "https://api.perplexity.ai/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Kimi documents GET /v1/models as the authoritative list of models
        # currently available to the API key.
        "provider_name": "moonshotai",
        "display_name": "Moonshot AI",
        "env_vars": ("MOONSHOT_API_KEY",),
        "base_url_env_vars": ("MOONSHOT_BASE_URL",),
        "default_base_url": "https://api.moonshot.ai/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Mistral's authenticated GET /v1/models endpoint lists every model
        # available to the user, including fine-tuned models.
        "provider_name": "mistral",
        "display_name": "Mistral",
        "env_vars": ("MISTRAL_API_KEY",),
        "base_url_env_vars": ("MISTRAL_BASE_URL",),
        "default_base_url": "https://api.mistral.ai/v1",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # NVIDIA Build/NIM exposes the account-visible hosted catalog via
        # GET /v1/models; its contents change independently of this app.
        "provider_name": "nvidia",
        "display_name": "Nvidia",
        "env_vars": ("NVIDIA_API_KEY", "NGC_API_KEY"),
        "base_url_env_vars": ("NVIDIA_BASE_URL",),
        "default_base_url": "https://integrate.api.nvidia.com/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Novita documents its OpenAI-compatible account model inventory at
        # /openai/v1/models.  The old /v3/openai base was not that API.
        "provider_name": "novita",
        "display_name": "Novita",
        "env_vars": ("NOVITA_API_KEY",),
        "base_url_env_vars": ("NOVITA_BASE_URL",),
        "default_base_url": "https://api.novita.ai/openai/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Nebius Studio's OpenAI-compatible /v1/models endpoint reports the
        # models currently supported by the Studio account.
        "provider_name": "nebius",
        "display_name": "Nebius",
        "env_vars": ("NEBIUS_API_KEY",),
        "base_url_env_vars": ("NEBIUS_BASE_URL",),
        "default_base_url": "https://api.studio.nebius.ai/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # DeepInfra's native model catalog covers all modalities and is kept
        # live at /models/list, separately from its OpenAI-compatible
        # inference base URL.
        "provider_name": "deepinfra",
        "display_name": "DeepInfra",
        "env_vars": ("DEEPINFRA_API_KEY",),
        "base_url_env_vars": ("DEEPINFRA_BASE_URL",),
        "default_base_url": "https://api.deepinfra.com/v1/openai",
        "remote_model_base_url": "https://api.deepinfra.com",
        "remote_model_list_path": "/models/list",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Friendli exposes the currently served Serverless models from the
        # same OpenAI-compatible /models endpoint used for inference.
        "provider_name": "friendli",
        "display_name": "Friendli",
        "env_vars": ("FRIENDLI_API_KEY",),
        "base_url_env_vars": ("FRIENDLI_BASE_URL",),
        "default_base_url": "https://api.friendli.ai/serverless/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Hyperbolic's OpenAI-compatible GET /v1/models lists the active
        # inference catalog, including newly enabled account models.
        "provider_name": "hyperbolic",
        "display_name": "Hyperbolic",
        "env_vars": ("HYPERBOLIC_API_KEY",),
        "base_url_env_vars": ("HYPERBOLIC_BASE_URL",),
        "default_base_url": "https://api.hyperbolic.xyz/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Inference.net supplies its complete provider catalog at /v1/models.
        "provider_name": "inference-net",
        "display_name": "InferenceNet",
        "env_vars": ("INFERENCE_NET_API_KEY", "INFERENCENET_API_KEY"),
        "base_url_env_vars": ("INFERENCE_NET_BASE_URL", "INFERENCENET_BASE_URL"),
        "default_base_url": "https://api.inference.net/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Avian follows the OpenAI /v1/models contract.  Read that live
        # catalog rather than pinning a subset of routed model names.
        "provider_name": "avian",
        "display_name": "Avian",
        "env_vars": ("AVIAN_API_KEY",),
        "base_url_env_vars": ("AVIAN_BASE_URL",),
        "default_base_url": "https://api.avian.io/v1",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Upstage's authenticated OpenAI-compatible /v1/models endpoint is
        # the source of truth for the models available to this key.
        "provider_name": "upstage",
        "display_name": "Upstage",
        "env_vars": ("UPSTAGE_API_KEY",),
        "base_url_env_vars": ("UPSTAGE_BASE_URL",),
        "default_base_url": "https://api.upstage.ai/v1",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # LongCat exposes inference through /openai/v1 but keeps its live
        # account model inventory at the root /v1/models endpoint.
        "provider_name": "longcat",
        "display_name": "LongCat",
        "env_vars": ("LONGCAT_API_KEY",),
        "base_url_env_vars": ("LONGCAT_BASE_URL",),
        "default_base_url": "https://api.longcat.chat/openai/v1",
        "remote_model_base_url": "https://api.longcat.chat",
        "remote_model_list_path": "/v1/models",
        "supports_embeddings": False,
        "remote_model_discovery": True,
        "curated_models": [],
    },
    {
        # Tencent Hunyuan's authenticated OpenAI-compatible Models API
        # returns the exact models and statuses available to the key.
        "provider_name": "tencent-hunyuan",
        "display_name": "Tencent Hunyuan",
        "env_vars": ("HUNYUAN_API_KEY", "TENCENT_HUNYUAN_API_KEY"),
        "base_url_env_vars": ("HUNYUAN_BASE_URL", "TENCENT_HUNYUAN_BASE_URL"),
        "default_base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "supports_embeddings": True,
        "remote_model_discovery": True,
        "curated_models": [],
    },
]


def _build_provider_class(spec):
    attrs = {
        "provider_name": spec["provider_name"],
        "display_name": spec["display_name"],
        "env_vars": tuple(spec.get("env_vars", ())),
        "base_url_env_vars": tuple(spec.get("base_url_env_vars", ())),
        "default_base_url": spec.get("default_base_url", ""),
        "supports_embeddings": spec.get("supports_embeddings", False),
        "curated_models": list(spec.get("curated_models", [])),
        "KNOWN_MODELS": list(spec.get("curated_models", [])),
        "remote_model_discovery": bool(spec.get("remote_model_discovery", False)),
        "remote_model_list_path": str(spec.get("remote_model_list_path", "/models") or "/models"),
        "remote_model_cache_ttl_seconds": int(
            spec.get("remote_model_cache_ttl_seconds", 21600) or 21600
        ),
        "__doc__": "{} API provider via OpenAI-compatible adapter.".format(spec["display_name"]),
    }
    class_name = "{}Provider".format(
        spec["provider_name"].replace("-", " ").title().replace(" ", "")
    )
    return class_name, type(class_name, (OpenAICompatibleProvider,), attrs)


OPENAI_COMPATIBLE_PROVIDER_SPECS = {}
OPENAI_COMPATIBLE_PROVIDER_CLASSES = {}

for _spec in _OPENAI_COMPATIBLE_PROVIDERS:
    _class_name, _provider_cls = _build_provider_class(_spec)
    OPENAI_COMPATIBLE_PROVIDER_SPECS[_spec["provider_name"]] = dict(_spec)
    OPENAI_COMPATIBLE_PROVIDER_CLASSES[_spec["provider_name"]] = _provider_cls
    globals()[_class_name] = _provider_cls

# Vercel has public inventory and gateway-specific request semantics, so it
# must not silently fall back to the generic OpenAI-compatible adapter.
OPENAI_COMPATIBLE_PROVIDER_CLASSES["vercel-ai-gateway"] = VercelAIGatewayProvider
# Portkey's Models API and inference surface are OpenAI-compatible, but it
# authenticates with x-portkey-api-key rather than Authorization: Bearer.
OPENAI_COMPATIBLE_PROVIDER_CLASSES["portkey-ai-gateway"] = PortkeyAIGatewayProvider
# AssemblyAI is OpenAI-compatible but its API key is sent as the raw
# Authorization header value, rather than Bearer <token>.
OPENAI_COMPATIBLE_PROVIDER_CLASSES["assemblyai"] = AssemblyAIProvider
OPENAI_COMPATIBLE_PROVIDER_CLASSES["ai21"] = AI21Provider
