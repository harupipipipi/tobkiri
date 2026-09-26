from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ...extensions.loading import import_entrypoint
from ...extensions.runtime import get_extension_registry
from ..api_key_store import (
    list_custom_providers,
    load_provider_api_keys_into_env,
    provider_has_api_key,
    provider_named_api_keys,
    provider_secret_keys,
    read_provider_api_key,
)
from ..provider_program import (
    local_openai_runtime_manifests,
    missing_program_provider_ids,
    provider_program_manifests,
)
from ..model_metadata_schema import (
    context_window_value,
    normalize_capability_map,
    normalize_request_features,
    normalize_routing_defaults,
)
from ..metadata_json import load_strict_metadata_json
from ..oauth_store import provider_has_oauth_connection, provider_oauth_status
from .component_metadata import (
    model_manifests_from_provider_components,
    provider_component_metadata_map,
    provider_manifests_from_components,
)
from .openai_compatible_provider import OpenAICompatibleProvider
from .provider_catalog import (
    OPENAI_COMPATIBLE_PROVIDER_CLASSES,
    OPENAI_COMPATIBLE_PROVIDER_SPECS,
)
from . import google_provider as google_provider

"""
providers package - provider discovery and catalog helpers.

The extension registry is the primary runtime source of truth. Curated metadata
below exists only to preserve the richer master-side catalog/API surface when a
manifest does not spell out every compatibility field.
"""


_LEGACY_PROVIDER_REGISTRY = [
    (
        ("OPENAI_API_KEY",),
        "openai",
        "ecosystem.defaultspack.domain.ai_client.providers.openai_provider",
        "OpenAIProvider",
    ),
    (
        ("ANTHROPIC_API_KEY",),
        "anthropic",
        "ecosystem.defaultspack.domain.ai_client.providers.anthropic_provider",
        "AnthropicProvider",
    ),
    (
        ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        "google",
        "ecosystem.defaultspack.domain.ai_client.providers.google_provider",
        "GoogleProvider",
    ),
    (
        ("GENSPARK_API_KEY",),
        "genspark",
        "ecosystem.defaultspack.domain.ai_client.providers.genspark_provider",
        "GensparkProvider",
    ),
]

# Legacy compatibility fallback: new provider metadata should live in
# domain/providers/<provider_id>/manifest.json and models.json. Keep these
# hardcoded curated tables only to preserve existing catalog behavior, and
# retire entries gradually as provider components reach full coverage.
_CURATED_PROVIDER_METADATA: Dict[str, Dict[str, Any]] = {
    "stub": {
        "display_name": "Stub",
        "kind": "builtin",
        "description": "Built-in test provider for deterministic responses.",
        "env_vars": [],
        "base_url_envs": [],
        "catalog_only": False,
        "supports_invoke": True,
        "default_model": "default",
        "capabilities": ["chat", "embedding", "image", "transcription", "tts"],
    },
    "openai": {
        "display_name": "OpenAI",
        "kind": "cloud",
        "description": "OpenAI hosted models and multimodal APIs.",
        "env_vars": ["OPENAI_API_KEY"],
        "base_url_envs": ["OPENAI_BASE_URL"],
        "catalog_only": False,
        "supports_invoke": True,
        "default_model": "gpt-5.5",
        "capabilities": [
            "chat",
            "tool_calls",
            "vision",
            "embedding",
            "image",
            "transcription",
            "tts",
        ],
    },
    "anthropic": {
        "display_name": "Anthropic",
        "kind": "cloud",
        "description": "Anthropic Claude models.",
        "env_vars": ["ANTHROPIC_API_KEY"],
        "base_url_envs": ["ANTHROPIC_BASE_URL"],
        "catalog_only": False,
        "supports_invoke": True,
        "default_model": "claude-sonnet-4-0",
        "capabilities": ["chat", "tool_calls", "vision", "reasoning"],
    },
    "google": {
        "display_name": "Google",
        "kind": "cloud",
        "description": "Google Gemini and multimodal APIs.",
        "env_vars": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
        "base_url_envs": ["GOOGLE_BASE_URL"],
        "catalog_only": False,
        "supports_invoke": True,
        "default_model": "gemini-2.5-pro",
        "capabilities": ["chat", "tool_calls", "vision", "embedding"],
    },
    "genspark": {
        "display_name": "Genspark",
        "kind": "cloud",
        "description": "Genspark OpenAI-compatible hosted models.",
        "env_vars": ["GENSPARK_API_KEY", "OPENAI_API_KEY"],
        "base_url_envs": ["GENSPARK_LLM_BASE_URL", "OPENAI_BASE_URL"],
        "catalog_only": False,
        "supports_invoke": True,
        "default_model": "gpt-5-mini",
        "capabilities": ["chat", "tool_calls", "vision"],
    },
    "groq": {
        "display_name": "Groq",
        "kind": "cloud",
        "description": "Fast hosted inference for open-weight models.",
        "env_vars": ["GROQ_API_KEY"],
        "base_url_envs": ["GROQ_BASE_URL"],
        "catalog_only": False,
        "supports_invoke": True,
        "default_base_url": "https://api.groq.com/openai/v1",
        "default_model": "openai/gpt-oss-120b",
        "default_model_for": {
            "chat": "openai/gpt-oss-120b",
            "reasoning": "openai/gpt-oss-120b",
            "fast": "openai/gpt-oss-20b",
            "general": "llama-3.3-70b-versatile",
            "cheap": "llama-3.1-8b-instant",
            "vision": "meta-llama/llama-4-scout-17b-16e-instruct",
        },
        "capabilities": [
            "chat",
            "streaming",
            "tool_calls",
            "reasoning",
            "vision",
            "openai_compatible",
        ],
    },
    "mistral": {
        "display_name": "Mistral",
        "kind": "cloud",
        "description": "Mistral hosted models.",
        "env_vars": ["MISTRAL_API_KEY"],
        "base_url_envs": ["MISTRAL_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        "default_model": "mistral-large-latest",
        "capabilities": ["chat", "embedding"],
    },
    "xai": {
        "display_name": "xAI",
        "kind": "cloud",
        "description": "xAI Grok models.",
        "env_vars": ["XAI_API_KEY"],
        "base_url_envs": ["XAI_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        "default_model": "grok-2-latest",
        "capabilities": ["chat", "vision"],
    },
    "openrouter": {
        "display_name": "OpenRouter",
        "kind": "aggregator",
        "description": "OpenRouter gateway backed by the connected account's live model inventory.",
        "env_vars": ["OPENROUTER_API_KEY"],
        "base_url_envs": ["OPENROUTER_BASE_URL"],
        "catalog_only": False,
        "supports_invoke": True,
        "default_base_url": "https://openrouter.ai/api/v1",
        "default_model": "",
        "default_model_for": {},
        "capabilities": [
            "chat",
            "streaming",
            "tool_calls",
            "reasoning",
            "vision",
            "openai_compatible",
        ],
    },
    "gitlawb-opengateway": {
        "display_name": "Gitlawb OpenGateway",
        "kind": "aggregator",
        "description": "Gitlawb OpenGateway allowlist for MiMo. API keys are required for all OpenGateway models.",
        "env_vars": ["GITLAWB_OPENGATEWAY_API_KEY"],
        "base_url_envs": ["GITLAWB_OPENGATEWAY_BASE_URL"],
        "catalog_only": False,
        "supports_invoke": True,
        "default_base_url": "https://opengateway.gitlawb.com/v1",
        "default_model": "mimo-v2.5-pro",
        "default_model_for": {
            "chat": "mimo-v2.5-pro",
            "reasoning": "mimo-v2.5-pro",
            "fast": "mimo-v2-flash",
            "vision": "mimo-v2-omni",
        },
        "capabilities": ["chat", "streaming", "openai_compatible", "reasoning", "vision"],
    },
    "deepseek": {
        "display_name": "DeepSeek",
        "kind": "cloud",
        "description": "DeepSeek chat and reasoning models.",
        "env_vars": ["DEEPSEEK_API_KEY"],
        "base_url_envs": ["DEEPSEEK_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        "default_model": "deepseek-chat",
        "capabilities": ["chat", "reasoning"],
    },
    "perplexity": {
        "display_name": "Perplexity",
        "kind": "aggregator",
        "description": "Perplexity online and sonar models.",
        "env_vars": ["PERPLEXITY_API_KEY"],
        "base_url_envs": ["PERPLEXITY_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        "default_model": "sonar-pro",
        "capabilities": ["chat", "search"],
    },
    "together": {
        "display_name": "Together",
        "kind": "aggregator",
        "description": "Hosted inference for open models.",
        "env_vars": ["TOGETHER_API_KEY"],
        "base_url_envs": ["TOGETHER_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        "default_model": "llama-3.1-70b-instruct-turbo",
        "capabilities": ["chat", "tool_calls"],
    },
    "fireworks": {
        "display_name": "Fireworks",
        "kind": "aggregator",
        "description": "Hosted inference and image APIs for open models.",
        "env_vars": ["FIREWORKS_API_KEY"],
        "base_url_envs": ["FIREWORKS_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        "default_model": "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "capabilities": ["chat", "tool_calls", "vision", "image", "embedding"],
    },
    "glm": {
        "display_name": "GLM",
        "kind": "cloud",
        "description": "GLM hosted models via an OpenAI-compatible surface.",
        "env_vars": ["GLM_API_KEY"],
        "base_url_envs": ["GLM_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        "default_model": "glm-4.5",
        "capabilities": ["chat", "tool_calls", "streaming"],
    },
    "longcat": {
        "display_name": "Longcat",
        "kind": "cloud",
        "description": "Longcat hosted models via an OpenAI-compatible surface.",
        "env_vars": ["LONGCAT_API_KEY"],
        "base_url_envs": ["LONGCAT_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        "default_model": "LongCat-Flash-Chat",
        "default_base_url": "https://api.longcat.chat/openai/v1",
        "capabilities": ["chat", "streaming"],
    },
    "ollama": {
        "display_name": "Ollama",
        "kind": "local",
        "description": "Local Ollama runtime with native installed-model discovery.",
        "env_vars": ["OLLAMA_API_KEY"],
        "base_url_envs": ["OLLAMA_BASE_URL", "OLLAMA_HOST"],
        "catalog_only": False,
        "supports_invoke": True,
        # The local server is authoritative. Never publish a guessed default.
        "default_model": "",
        "default_base_url": "local://ollama",
        "capabilities": [
            "chat",
            "embedding",
            "local",
            "openai_compatible",
            "runtime_model_discovery",
        ],
    },
    "lmstudio": {
        "display_name": "LM Studio",
        "kind": "local",
        "description": "Local OpenAI-compatible endpoint from LM Studio.",
        "env_vars": [],
        "base_url_envs": ["LMSTUDIO_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        # LM Studio reports the installed model inventory through its native
        # management API.  A checked-in default would be a stale placeholder.
        "default_model": "",
        "default_base_url": "http://127.0.0.1:1234/v1",
        "capabilities": ["chat", "embedding", "local", "openai_compatible"],
    },
    "vllm": {
        "display_name": "vLLM",
        "kind": "local",
        "description": "Self-hosted OpenAI-compatible inference server.",
        "env_vars": [],
        "base_url_envs": ["VLLM_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        "default_model": "deepseek-r1",
        "default_base_url": "http://127.0.0.1:8000/v1",
        "capabilities": ["chat", "embedding", "local", "openai_compatible"],
    },
    "llamacpp": {
        "display_name": "llama.cpp",
        "kind": "local",
        "description": "Local OpenAI-compatible inference server.",
        "env_vars": [],
        "base_url_envs": ["LLAMACPP_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        "default_model": "local-gguf",
        "default_base_url": "http://127.0.0.1:8080/v1",
        "capabilities": ["chat", "embedding", "local", "openai_compatible"],
    },
    "openai_compatible": {
        "display_name": "OpenAI Compatible",
        "kind": "custom",
        "description": "Generic OpenAI-compatible endpoint.",
        "env_vars": ["OPENAI_COMPATIBLE_API_KEY"],
        "base_url_envs": ["OPENAI_COMPATIBLE_BASE_URL"],
        "catalog_only": True,
        "supports_invoke": False,
        "default_model": "custom-model",
        "capabilities": ["chat", "tool_calls", "embedding", "openai_compatible"],
    },
    "rumi": {
        "display_name": "Rumi",
        "kind": "meta",
        "description": "Meta-provider for routing and orchestration.",
        "env_vars": [],
        "base_url_envs": [],
        "catalog_only": False,
        "supports_invoke": True,
        "default_model": "rumi",
        "capabilities": ["chat", "routing", "meta", "review_chain", "tool_calls", "thinking"],
    },
}

_CURATED_PROVIDER_MODELS: Dict[str, List[Dict[str, Any]]] = {
    "stub": [
        {"model_id": "default", "name": "Stub Default Model", "type": "chat"},
        {"model_id": "fast", "name": "Stub Fast Model", "type": "chat"},
        {"model_id": "large", "name": "Stub Large Model", "type": "chat"},
    ],
    "rumi": [
        {
            "model_id": "rumi",
            "name": "Rumi",
            "display_name": "Rumi",
            "type": "chat",
            "supports_thinking": True,
            "thinking_levels": ["low", "medium", "high", "xhigh"],
            "default_thinking_level": "medium",
            "capabilities": ["chat", "routing", "review_chain", "tool_calls", "thinking"],
            "metadata": {
                "process_model": True,
                "model_pack_ref": "modelpack/rumi",
                "base_model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
                "intended_base_model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
                "resolved_base_model": "runtime-selected",
                "fallback_reason": "rumi/auto uses active provider fallback when the intended base model is unavailable",
                "fallback_policy": "active_provider_fallback",
                "notes": "Rumi process model built on MiMo V2.5 Pro with explicit reasoning brief, review chain, freshness, trace, watchdog, and escalation policy.",
            },
        },
        {
            "model_id": "auto",
            "name": "Rumi Auto",
            "display_name": "Rumi Auto",
            "type": "chat",
            "supports_thinking": True,
            "thinking_levels": ["low", "medium", "high", "xhigh"],
            "default_thinking_level": "medium",
            "capabilities": ["chat", "routing", "review_chain", "tool_calls", "thinking"],
            "metadata": {
                "process_model": True,
                "model_pack_ref": "modelpack/rumi",
                "intended_base_model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
                "resolved_base_model": "runtime-selected",
                "fallback_policy": "active_provider_fallback",
            },
        },
        {
            "model_id": "mimo",
            "name": "Rumi MiMo V2.5 Pro",
            "display_name": "Rumi MiMo",
            "type": "chat",
            "supports_thinking": True,
            "thinking_levels": ["low", "medium", "high", "xhigh"],
            "default_thinking_level": "medium",
            "capabilities": ["chat", "routing", "review_chain", "tool_calls", "thinking"],
            "metadata": {
                "process_model": True,
                "model_pack_ref": "modelpack/rumi",
                "intended_base_model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
                "resolved_base_model": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
                "fallback_policy": "requires_intended_base_model",
            },
        },
    ],
    "groq": [
        {"model_id": "openai/gpt-oss-120b", "name": "GPT OSS 120B via Groq", "type": "reasoning"},
        {"model_id": "openai/gpt-oss-20b", "name": "GPT OSS 20B via Groq", "type": "reasoning"},
        {"model_id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B Versatile", "type": "chat"},
        {"model_id": "llama-3.1-8b-instant", "name": "Llama 3.1 8B Instant", "type": "chat"},
        {
            "model_id": "meta-llama/llama-4-scout-17b-16e-instruct",
            "name": "Llama 4 Scout 17B 16E Instruct via Groq",
            "type": "vision",
            "metadata": {"preview": True, "do_not_use_as_default": True},
        },
    ],
    "mistral": [
        {"model_id": "mistral-large-latest", "name": "Mistral Large", "type": "chat"},
        {"model_id": "ministral-8b-latest", "name": "Ministral 8B", "type": "chat"},
        {"model_id": "codestral-latest", "name": "Codestral", "type": "chat"},
    ],
    "xai": [
        {"model_id": "grok-2-latest", "name": "Grok 2", "type": "chat"},
        {"model_id": "grok-vision-beta", "name": "Grok Vision", "type": "vision"},
    ],
    "openrouter": [
        {
            "model_id": "cohere/north-mini-code:free",
            "name": "Cohere North Mini Code (free)",
            "type": "chat",
        },
        {
            "model_id": "anthropic/claude-sonnet-5",
            "name": "Claude Sonnet 5",
            "type": "vision",
            "capabilities": ["chat", "streaming", "tool_calls", "reasoning", "vision"],
        },
        {
            "model_id": "openai/o3-pro",
            "name": "OpenAI o3 Pro",
            "type": "vision",
            "capabilities": ["chat", "streaming", "tool_calls", "reasoning", "vision"],
        },
        {
            "model_id": "google/gemini-2.5-pro",
            "name": "Gemini 2.5 Pro",
            "type": "vision",
            "capabilities": ["chat", "streaming", "tool_calls", "reasoning", "vision"],
        },
        {
            "model_id": "z-ai/glm-5.2",
            "name": "GLM 5.2",
            "type": "reasoning",
            "capabilities": ["chat", "streaming", "tool_calls", "reasoning"],
        },
        {
            "model_id": "moonshotai/kimi-k2.7-code",
            "name": "Kimi K2.7 Code",
            "type": "vision",
            "capabilities": ["chat", "streaming", "tool_calls", "reasoning", "vision"],
        },
        {
            "model_id": "deepseek/deepseek-r1-0528",
            "name": "DeepSeek R1 0528",
            "type": "reasoning",
            "capabilities": ["chat", "streaming", "tool_calls", "reasoning"],
        },
        {
            "model_id": "qwen/qwen3-coder-next",
            "name": "Qwen3 Coder Next",
            "type": "chat",
            "capabilities": ["chat", "streaming", "tool_calls"],
        },
        {
            "model_id": "nvidia/nemotron-3-ultra-550b-a55b:free",
            "name": "NVIDIA Nemotron 3 Ultra (free)",
            "type": "reasoning",
            "capabilities": ["chat", "streaming", "tool_calls", "reasoning"],
        },
        {
            "model_id": "tencent/hy3-preview:free",
            "name": "Tencent Hy3 preview (free)",
            "type": "chat",
            "defaults": {"legacy": True},
        },
    ],
    "gitlawb-opengateway": [
        {
            "model_id": "mimo-v2.5-pro",
            "name": "MiMo V2.5 Pro via Gitlawb OpenGateway",
            "type": "reasoning",
        },
        {
            "model_id": "mimo-v2-flash",
            "name": "MiMo V2 Flash via Gitlawb OpenGateway",
            "type": "chat",
        },
        {
            "model_id": "mimo-v2-omni",
            "name": "MiMo V2 Omni via Gitlawb OpenGateway",
            "type": "chat",
            "capabilities": ["chat", "streaming", "vision"],
        },
        {
            "model_id": "mimo-v2-pro",
            "name": "MiMo V2 Pro via Gitlawb OpenGateway",
            "type": "reasoning",
            "capabilities": ["chat", "reasoning", "streaming"],
            "supports_thinking": True,
            "thinking_levels": ["low", "medium", "high", "xhigh"],
            "default_thinking_level": "medium",
        },
        {
            "model_id": "mimo-v2.5",
            "name": "MiMo V2.5 via Gitlawb OpenGateway",
            "type": "reasoning",
            "capabilities": ["chat", "reasoning", "streaming"],
            "supports_thinking": True,
            "thinking_levels": ["low", "medium", "high", "xhigh"],
            "default_thinking_level": "medium",
        },
    ],
    "deepseek": [
        {"model_id": "deepseek-chat", "name": "DeepSeek Chat", "type": "chat"},
        {"model_id": "deepseek-r1", "name": "DeepSeek R1", "type": "reasoning"},
    ],
    "perplexity": [
        {"model_id": "sonar-pro", "name": "Sonar Pro", "type": "chat"},
        {"model_id": "sonar-reasoning-pro", "name": "Sonar Reasoning Pro", "type": "reasoning"},
    ],
    "together": [
        {
            "model_id": "llama-3.1-70b-instruct-turbo",
            "name": "Llama 3.1 70B Instruct Turbo",
            "type": "chat",
        },
        {
            "model_id": "qwen2.5-coder-32b-instruct",
            "name": "Qwen 2.5 Coder 32B Instruct",
            "type": "chat",
        },
        {"model_id": "deepseek-r1", "name": "DeepSeek R1", "type": "reasoning"},
    ],
    "fireworks": [
        {
            "model_id": "accounts/fireworks/models/llama-v3p1-70b-instruct",
            "name": "Llama 3.1 70B Instruct",
            "type": "chat",
        }
    ],
    "glm": [{"model_id": "glm-4.5", "name": "GLM 4.5", "type": "chat"}],
    "longcat": [{"model_id": "LongCat-Flash-Chat", "name": "LongCat Flash Chat", "type": "chat"}],
    # Ollama inventory is supplied only by the configured native server.
    "ollama": [],
    "lmstudio": [
        {"model_id": "deepseek-r1", "name": "DeepSeek R1", "type": "reasoning"},
        {"model_id": "llama3.1:8b", "name": "Llama 3.1 8B", "type": "chat"},
        {"model_id": "gpt-oss-20b", "name": "GPT OSS 20B", "type": "chat"},
    ],
    "vllm": [
        {"model_id": "deepseek-r1", "name": "DeepSeek R1", "type": "reasoning"},
        {"model_id": "qwen2.5-coder:32b", "name": "Qwen 2.5 Coder 32B", "type": "chat"},
        {"model_id": "gpt-oss-20b", "name": "GPT OSS 20B", "type": "chat"},
    ],
    "llamacpp": [{"model_id": "local-gguf", "name": "Local GGUF Model", "type": "chat"}],
    "openai_compatible": [
        {"model_id": "custom-model", "name": "Custom Model", "type": "chat"},
    ],
}

_BEST_MODEL_BY_PROVIDER = {
    "stub": "default",
    "openai": "gpt-5.5",
    "anthropic": "claude-sonnet-4-0",
    "google": "gemini-2.5-pro",
    "genspark": "gpt-5-mini",
    "groq": "openai/gpt-oss-120b",
    "cerebras": "gpt-oss-120b",
    "nvidia": "nvidia/llama-3.3-nemotron-super-49b-v1",
    "moonshotai": "kimi-k2-0711-preview",
    "mistral": "mistral-large-latest",
    "xai": "grok-2-latest",
    "openrouter": "cohere/north-mini-code:free",
    "gitlawb-opengateway": "mimo-v2.5-pro",
    "opencode-go": "kimi-k2.6",
    "opencode-zen": "minimax-m3-free",
    "deepseek": "deepseek-chat",
    "perplexity": "sonar-pro",
    "together": "llama-3.1-70b-instruct-turbo",
    "fireworks": "accounts/fireworks/models/llama-v3p1-70b-instruct",
    "glm": "glm-4.5",
    "longcat": "LongCat-Flash-Chat",
    "lmstudio": "deepseek-r1",
    "vllm": "deepseek-r1",
    "llamacpp": "local-gguf",
    "openai_compatible": "custom-model",
    "rumi": "rumi",
}


def _list_provider_manifests() -> List[Dict[str, Any]]:
    try:
        registry = get_extension_registry(force_reload=False)
        return registry.llm().providers(enabled_only=True)
    except Exception:
        return []


def _load_model_manifests(provider_id: str = "") -> List[Dict[str, Any]]:
    try:
        registry = get_extension_registry(force_reload=False)
        return registry.llm().models(provider_id=provider_id, enabled_only=True)
    except Exception:
        return []


def _model_ref_matches(provider_id: str, model_ref: str, model: Dict[str, Any]) -> bool:
    ref = str(model_ref or "").strip()
    if not ref:
        return False
    model_id = str(model.get("model_id") or "").strip()
    full_id = str(model.get("id") or "").strip()
    qualified = "{}/{}".format(provider_id, ref)
    return ref in {model_id, full_id} or qualified == full_id


def validate_provider_catalog_coverage(registry: Any = None) -> List[Dict[str, Any]]:
    """Validate provider/model manifest coverage for extension-backed catalogs."""
    try:
        active_registry = registry or get_extension_registry(force_reload=False)
        llm_registry = active_registry.llm()
        providers = llm_registry.providers(enabled_only=True)
        models = llm_registry.models(enabled_only=True)
    except Exception as exc:
        return [{"type": "registry_unavailable", "error": str(exc)}]

    provider_ids = {
        str(provider.get("id") or provider.get("provider_id") or "").strip()
        for provider in providers
        if str(provider.get("id") or provider.get("provider_id") or "").strip()
    }
    models_by_provider: Dict[str, List[Dict[str, Any]]] = {
        provider_id: [] for provider_id in provider_ids
    }
    issues: List[Dict[str, Any]] = []

    for model in models:
        provider_id = str(model.get("provider_id") or "").strip()
        model_id = str(model.get("model_id") or "").strip()
        full_id = str(model.get("id") or "").strip()
        if not provider_id or provider_id not in provider_ids:
            issues.append(
                {
                    "type": "model_provider_missing",
                    "provider_id": provider_id,
                    "model_id": model_id or full_id,
                }
            )
            continue
        if not model_id or not full_id:
            issues.append(
                {
                    "type": "model_identity_missing",
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "id": full_id,
                }
            )
            continue
        models_by_provider.setdefault(provider_id, []).append(model)

    for provider in providers:
        provider_id = str(provider.get("id") or provider.get("provider_id") or "").strip()
        if not provider_id:
            issues.append({"type": "provider_identity_missing"})
            continue
        provider_models = models_by_provider.get(provider_id, [])
        if not provider_models:
            issues.append({"type": "provider_models_missing", "provider_id": provider_id})
            continue

        default_model = str(provider.get("default_model") or "").strip()
        if default_model and not any(
            _model_ref_matches(provider_id, default_model, model) for model in provider_models
        ):
            issues.append(
                {
                    "type": "provider_default_model_missing",
                    "provider_id": provider_id,
                    "model_id": default_model,
                }
            )

        default_model_for = provider.get("default_model_for") or {}
        if isinstance(default_model_for, dict):
            for use_case, model_ref in default_model_for.items():
                ref = str(model_ref or "").strip()
                if ref and not any(
                    _model_ref_matches(provider_id, ref, model) for model in provider_models
                ):
                    issues.append(
                        {
                            "type": "provider_default_model_for_missing",
                            "provider_id": provider_id,
                            "use_case": str(use_case),
                            "model_id": ref,
                        }
                    )

    return issues


def validate_provider_program_coverage() -> List[str]:
    """Hard-fail coverage gate for every required external provider identity."""
    return missing_program_provider_ids(_provider_manifest_map())


def _bundled_model_catalog_provider_manifests() -> Dict[str, Dict[str, Any]]:
    """Load executable manifests from the fixed, bundled model-catalog pack.

    Pack architecture moved provider metadata out of ``defaultspack``.  The
    generic component registry intentionally rejects executable manifests from
    arbitrary sibling packs, so this loader has a deliberately narrower trust
    boundary: one repository-owned pack root, fixed ``manifest.json`` names,
    and provider entrypoints restricted to this package.
    """
    ecosystem_root = Path(__file__).resolve().parents[4]
    roots = (
        ecosystem_root / "rumi_model_catalog_pack" / "catalog" / "providers",
        ecosystem_root / "rumi_model_catalog_pack" / "extensions" / "llm" / "providers",
    )
    manifests: Dict[str, Dict[str, Any]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/manifest.json")):
            try:
                raw = load_strict_metadata_json(path)
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            candidate = raw.get("provider_manifest")
            if not isinstance(candidate, dict):
                candidate = raw
            provider_id = str(candidate.get("id") or raw.get("provider_id") or "").strip()
            if (
                not provider_id
                or not provider_id.isprintable()
                or any(character.isspace() for character in provider_id)
            ):
                continue
            entrypoint = str(candidate.get("entrypoint") or "").strip()
            if entrypoint and not entrypoint.startswith("domain.ai_client.providers."):
                continue
            manifest = dict(candidate)
            manifest["id"] = provider_id
            manifest["models"] = []
            manifest["source_pack_id"] = "rumi_model_catalog_pack"
            manifest["source_path"] = str(path)
            manifests[provider_id] = manifest
    return manifests


def _provider_manifest_map() -> Dict[str, Dict[str, Any]]:
    manifests: Dict[str, Dict[str, Any]] = {}
    for manifest in _list_provider_manifests():
        provider_id = str(manifest.get("id", "")).strip()
        if provider_id:
            manifests[provider_id] = dict(manifest)
    for provider_id, manifest in provider_manifests_from_components().items():
        manifests.setdefault(provider_id, dict(manifest))
    # The catalog pack is a fixed bundled trust root.  Its executable provider
    # definitions replace the old defaultspack/domain/providers ownership.
    for provider_id, manifest in _bundled_model_catalog_provider_manifests().items():
        manifests[provider_id] = dict(manifest)
    # The compatibility registry is an executable provider definition, not just
    # a documentation table.  It supersedes legacy extension manifests that
    # still carry default-model or fixed-allowlist snapshots, so an API key
    # always enables the connected endpoint's complete /models inventory.
    for raw_provider_id, raw_spec in OPENAI_COMPATIBLE_PROVIDER_SPECS.items():
        provider_id = str(raw_provider_id).strip()
        if not provider_id or not isinstance(raw_spec, dict):
            continue
        manifests[provider_id] = _openai_compatible_spec_manifest(dict(raw_spec))
    for provider_id, manifest in local_openai_runtime_manifests().items():
        # Local runtime endpoints report the exact models currently loaded by
        # that server.  Do not let an older extension manifest replace this
        # keyless live-discovery contract with a static catalog.
        manifests[provider_id] = manifest
    # Native providers whose invocation protocol is not OpenAI-compatible can
    # still expose their complete account inventory from an official Models
    # endpoint.  Register the executable adapter before the program's honest
    # connection placeholder is applied.
    # Native runtime definitions must take precedence over extension manifests
    # that predate live inventory support and can still carry fixed defaults.
    manifests.__setitem__(
        "anthropic",
        {
            "id": "anthropic",
            "display_name": "Anthropic",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.anthropic_provider:AnthropicProvider",
            "api_key_env": ["ANTHROPIC_API_KEY"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {"model_sync": "remote_merge", "model_list_path": "/v1/models"},
        },
    )
    manifests.__setitem__(
        "google",
        {
            "id": "google",
            "display_name": "Google Gemini",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.google_provider:GoogleProvider",
            "api_key_env": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {"model_sync": "remote_merge", "model_list_path": "/v1beta/models"},
        },
    )
    manifests.__setitem__(
        "cohere",
        {
            "id": "cohere",
            "display_name": "Cohere",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.cohere_provider:CohereProvider",
            "api_key_env": ["COHERE_API_KEY"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {"model_sync": "remote_merge", "model_list_path": "/v1/models"},
        },
    )
    manifests.__setitem__(
        "replicate",
        {
            "id": "replicate",
            "display_name": "Replicate",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.replicate_provider:ReplicateProvider",
            "api_key_env": ["REPLICATE_API_TOKEN"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {"model_sync": "remote_merge", "model_list_path": "/v1/models"},
        },
    )
    manifests.__setitem__(
        "elevenlabs",
        {
            "id": "elevenlabs",
            "display_name": "ElevenLabs",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.elevenlabs_provider:ElevenLabsProvider",
            "api_key_env": ["ELEVENLABS_API_KEY"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {"model_sync": "remote_merge", "model_list_path": "/v1/models"},
        },
    )
    manifests.__setitem__(
        "cloudflare-workers-ai",
        {
            "id": "cloudflare-workers-ai",
            "display_name": "Cloudflare Workers AI",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.cloudflare_workers_ai_provider:CloudflareWorkersAIProvider",
            "api_key_env": ["CLOUDFLARE_API_TOKEN"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {"model_sync": "remote_merge", "model_list_path": "/models/search"},
        },
    )
    manifests.__setitem__(
        "deepgram",
        {
            "id": "deepgram",
            "display_name": "Deepgram",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.deepgram_provider:DeepgramProvider",
            "api_key_env": ["DEEPGRAM_API_KEY"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {"model_sync": "remote_merge", "model_list_path": "/v1/models"},
        },
    )
    manifests.__setitem__(
        "databricks-model-serving",
        {
            "id": "databricks-model-serving",
            "display_name": "Databricks Model Serving",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.databricks_model_serving_provider:DatabricksModelServingProvider",
            "api_key_env": ["DATABRICKS_TOKEN"],
            "base_url_env": ["DATABRICKS_HOST", "DATABRICKS_BASE_URL"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "model_list_path": "/api/2.0/serving-endpoints",
            },
        },
    )
    manifests.__setitem__(
        "azure-openai",
        {
            "id": "azure-openai",
            "display_name": "Azure OpenAI",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.azure_openai_provider:AzureOpenAIProvider",
            "api_key_env": ["AZURE_OPENAI_API_KEY"],
            "base_url_env": ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_BASE_URL"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {"model_sync": "remote_merge", "model_list_path": "/openai/deployments"},
        },
    )
    manifests.__setitem__(
        "azure-ai-foundry",
        {
            "id": "azure-ai-foundry",
            "display_name": "Azure AI Foundry",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.azure_ai_foundry_provider:AzureAIFoundryProvider",
            "api_key_env": ["AZURE_AI_FOUNDRY_API_KEY"],
            "base_url_env": ["AZURE_AI_FOUNDRY_ENDPOINT"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "model_list_path": "/deployments?api-version=v1",
                "inventory_strategy": "project_deployment_control_plane",
            },
        },
    )
    manifests.__setitem__(
        "aws-bedrock",
        {
            "id": "aws-bedrock",
            "display_name": "Amazon Bedrock",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.aws_bedrock_provider:AwsBedrockProvider",
            "api_key_env": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
            "base_url_env": ["AWS_REGION", "AWS_DEFAULT_REGION"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "model_list_path": "/foundation-models",
                "inventory_strategy": "regional_control_plane",
                "api_family": "bedrock_converse",
            },
        },
    )
    manifests.__setitem__(
        "stability-ai",
        {
            "id": "stability-ai",
            "display_name": "Stability AI",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.stability_ai_provider:StabilityAIProvider",
            "api_key_env": ["STABILITY_API_KEY"],
            "base_url_env": ["STABILITY_API_BASE_URL"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "model_list_path": "/v1/engines/list",
                "inventory_strategy": "account_engines_api",
            },
        },
    )
    manifests.__setitem__(
        "portkey-ai-gateway",
        {
            "id": "portkey-ai-gateway",
            "display_name": "Portkey AI Gateway",
            "adapter": "openai_compatible",
            "api_key_env": ["PORTKEY_API_KEY"],
            "base_url_env": ["PORTKEY_BASE_URL"],
            "default_base_url": "https://api.portkey.ai/v1",
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "model_list_path": "/models",
                "inventory_strategy": "workspace_model_catalog_api",
            },
        },
    )
    manifests.__setitem__(
        "fal-ai",
        {
            "id": "fal-ai",
            "display_name": "fal.ai",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.fal_ai_provider:FalAIProvider",
            "api_key_env": ["FAL_KEY", "FAL_AI_API_KEY"],
            "base_url_env": ["FAL_API_BASE_URL"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "model_list_path": "/v1/models",
                "inventory_strategy": "paginated_models_api_and_queue",
            },
        },
    )
    manifests.__setitem__(
        "assemblyai",
        {
            "id": "assemblyai",
            "display_name": "AssemblyAI",
            "adapter": "openai_compatible",
            "api_key_env": ["ASSEMBLYAI_API_KEY"],
            "base_url_env": ["ASSEMBLYAI_LLM_GATEWAY_BASE_URL"],
            "default_base_url": "https://llm-gateway.assemblyai.com/v1",
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "model_list_path": "/models",
                "inventory_strategy": "llm_gateway_models_api",
            },
        },
    )
    manifests.__setitem__(
        "ibm-watsonx",
        {
            "id": "ibm-watsonx",
            "display_name": "IBM watsonx.ai",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.ibm_watsonx_provider:IBMWatsonxProvider",
            "api_key_env": ["WATSONX_API_KEY", "IBM_WATSONX_API_KEY"],
            "base_url_env": ["WATSONX_BASE_URL"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "model_list_path": "/ml/v1/foundation_model_specs",
                "inventory_strategy": "foundation_model_specs_api",
            },
        },
    )
    manifests.__setitem__(
        "ai21",
        {
            "id": "ai21",
            "display_name": "AI21 Labs",
            "adapter": "openai_compatible",
            "api_key_env": ["AI21_API_KEY"],
            "base_url_env": ["AI21_BASE_URL"],
            "default_base_url": "https://api.ai21.com/studio/v1",
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "inventory_strategy": "official_model_document",
            },
        },
    )
    manifests.__setitem__(
        "black-forest-labs",
        {
            "id": "black-forest-labs",
            "display_name": "Black Forest Labs",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.black_forest_labs_provider:BlackForestLabsProvider",
            "api_key_env": ["BFL_API_KEY"],
            "base_url_env": ["BFL_BASE_URL"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "inventory_strategy": "official_openapi_document_catalog",
            },
        },
    )
    manifests.__setitem__(
        "voyage-ai",
        {
            "id": "voyage-ai",
            "display_name": "Voyage AI",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.voyage_ai_provider:VoyageAIProvider",
            "api_key_env": ["VOYAGE_API_KEY"],
            "base_url_env": ["VOYAGE_BASE_URL"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "inventory_strategy": "official_model_document",
            },
        },
    )
    manifests.__setitem__(
        "genspark",
        {
            "id": "genspark",
            "display_name": "Genspark",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.genspark_provider:GensparkProvider",
            "api_key_env": ["GENSPARK_API_KEY"],
            "base_url_env": ["GENSPARK_LLM_BASE_URL"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "model_list_path": "/models",
                "inventory_strategy": "account_models_endpoint",
            },
        },
    )
    manifests.__setitem__(
        "google-vertex-ai",
        {
            "id": "google-vertex-ai",
            "display_name": "Google Vertex AI",
            "adapter": "native",
            "entrypoint": "domain.ai_client.providers.google_vertex_ai_provider:GoogleVertexAIProvider",
            "api_key_env": ["VERTEX_AI_ACCESS_TOKEN", "GOOGLE_VERTEX_AI_ACCESS_TOKEN"],
            "base_url_env": ["VERTEX_AI_BASE_URL"],
            "credential_required": True,
            "catalog_only": False,
            "supports_invoke": True,
            "models": [],
            "config": {
                "model_sync": "remote_merge",
                "model_list_path": "/endpoints",
                "inventory_strategy": "project_deployment_control_plane",
            },
        },
    )
    # The provider program supplies identity and inventory strategy for every
    # required provider, but never a hand-maintained model list.  Dedicated
    # component manifests above remain authoritative when present.
    for provider_id, manifest in provider_program_manifests().items():
        manifests.setdefault(provider_id, manifest)
    # A user can add any OpenAI-compatible service from Settings.  Treat those
    # saved definitions exactly like extension manifests so they are discoverable
    # by the provider/model catalog and not merely shown as inert API-key rows.
    for provider_id, manifest in _custom_openai_provider_manifests().items():
        # A saved endpoint is an explicit user choice.  It must override both
        # a program placeholder and a built-in OpenAI-compatible default so
        # account/project/proxy-specific model inventories are fetched from
        # the endpoint the user actually configured.
        if manifest.get("default_base_url"):
            manifests[provider_id] = manifest
        else:
            manifests.setdefault(provider_id, manifest)
    return manifests


def _openai_compatible_spec_manifest(spec: Dict[str, Any]) -> Dict[str, Any]:
    provider_id = str(spec.get("provider_name") or "").strip()
    return {
        "id": provider_id,
        "display_name": str(spec.get("display_name") or provider_id),
        "adapter": "openai_compatible",
        "credential_required": True,
        "supports_invoke": True,
        "api_key_env": list(spec.get("env_vars") or []),
        "base_url_env": list(spec.get("base_url_env_vars") or []),
        "default_base_url": str(spec.get("default_base_url") or ""),
        "headers": dict(spec.get("headers") or {}),
        # An OpenAI-compatible connection must expose the inventory returned
        # by its authenticated server, never a hand-maintained provider/model
        # snapshot.  The adapter handles /models (and its account-scoped cache)
        # after the user supplies a connection.
        "models": [],
        "config": {
            "model_sync": "remote_merge",
            "model_list_path": str(spec.get("remote_model_list_path") or "/models"),
            "model_list_base_url": str(spec.get("remote_model_base_url") or ""),
            "model_cache_ttl_seconds": int(
                spec.get("remote_model_cache_ttl_seconds", 3600) or 3600
            ),
        },
    }


def _custom_openai_provider_manifests() -> Dict[str, Dict[str, Any]]:
    definitions = {
        str(item.get("provider_id") or "").strip(): dict(item)
        for item in list_custom_providers()
        if isinstance(item, dict) and str(item.get("provider_id") or "").strip()
    }
    apis_by_provider: Dict[str, List[Dict[str, Any]]] = {}
    for api in provider_named_api_keys():
        if not isinstance(api, dict):
            continue
        provider_id = str(api.get("provider_id") or "").strip()
        if provider_id:
            apis_by_provider.setdefault(provider_id, []).append(dict(api))

    manifests: Dict[str, Dict[str, Any]] = {}
    for provider_id in sorted(set(definitions) | set(apis_by_provider)):
        definition = definitions.get(provider_id, {})
        apis = apis_by_provider.get(provider_id, [])
        # "custom" represents non-LLM integrations in the settings UI.  Only
        # LLM entries can be safely treated as an OpenAI-compatible endpoint.
        llm_apis = [api for api in apis if str(api.get("kind") or "llm").lower() == "llm"]
        if not llm_apis and str(definition.get("kind") or "llm").lower() != "llm":
            continue
        selected_api = next(
            (api for api in llm_apis if api.get("configured")), llm_apis[0] if llm_apis else {}
        )
        base_url = str(selected_api.get("base_url") or "").strip().rstrip("/")
        unauthenticated = str(selected_api.get("credential_mode") or "").strip().lower() == "none"
        manifests[provider_id] = {
            "id": provider_id,
            "display_name": str(definition.get("label") or provider_id),
            "description": "User-configured OpenAI-compatible model provider.",
            "adapter": "openai_compatible",
            "credential_required": not unauthenticated,
            "supports_invoke": True,
            "default_base_url": base_url,
            # Saved model hints are routing preferences, not an inventory.
            # The connected endpoint's live /models response is authoritative.
            "models": [],
            "config": {
                "custom_openai_compatible": True,
                "api_id": str(selected_api.get("api_id") or "").strip(),
                "model_sync": "remote_merge",
                "model_list_path": "/models",
                "model_list_requires_auth": not unauthenticated,
                "model_cache_ttl_seconds": 3600,
            },
        }
    return manifests


def _manifest_env_list(*values: Any) -> List[str]:
    envs: List[str] = []
    for value in values:
        if isinstance(value, str):
            item = value.strip()
            if item and item not in envs:
                envs.append(item)
        elif isinstance(value, (list, tuple, set)):
            for nested in value:
                item = str(nested or "").strip()
                if item and item not in envs:
                    envs.append(item)
    return envs


def _capability_list(manifest: Dict[str, Any], curated: Dict[str, Any]) -> List[str]:
    capabilities: List[str] = []

    def _add(value: str) -> None:
        item = str(value or "").strip()
        if item and item not in capabilities:
            capabilities.append(item)

    for item in curated.get("capabilities", []):
        _add(item)
    for item in curated.get("catalog_features", []):
        _add(item)

    raw_caps = manifest.get("capabilities", manifest.get("catalog_features", {}))
    if isinstance(raw_caps, dict):
        for key, enabled in raw_caps.items():
            if enabled:
                _add(key)
    elif isinstance(raw_caps, (list, tuple, set)):
        for item in raw_caps:
            _add(str(item))
    for item in manifest.get("catalog_features", []):
        _add(item)

    adapter = str(manifest.get("adapter", "")).strip()
    if adapter == "openai_compatible":
        _add("openai_compatible")
    return capabilities


def _infer_kind(provider_id: str, manifest: Dict[str, Any], curated: Dict[str, Any]) -> str:
    if curated.get("kind"):
        return str(curated["kind"])
    if provider_id == "stub":
        return "builtin"
    if provider_id == "rumi":
        return "meta"
    if provider_id in {"openrouter", "together", "fireworks", "perplexity"}:
        return "aggregator"
    if provider_id in {"ollama", "lmstudio", "vllm", "llamacpp", "openai_compatible"}:
        return "local"
    if str(manifest.get("adapter", "")).strip() == "openai_compatible":
        return "cloud"
    return "cloud"


def _provider_catalog_only(
    provider_id: str, manifest: Dict[str, Any], curated: Dict[str, Any]
) -> bool:
    if "catalog_only" in manifest:
        return bool(manifest["catalog_only"])
    if "catalog_only" in curated:
        return bool(curated["catalog_only"])
    adapter = str(manifest.get("adapter", "")).strip()
    return adapter == "openai_compatible" and provider_id not in {"stub", "rumi"}


def _provider_supports_invoke(
    provider_id: str, manifest: Dict[str, Any], curated: Dict[str, Any]
) -> bool:
    if "supports_invoke" in manifest:
        return bool(manifest["supports_invoke"])
    if "supports_invoke" in curated:
        return bool(curated["supports_invoke"])
    adapter = str(manifest.get("adapter", "")).strip()
    entrypoint = str(manifest.get("entrypoint", "")).strip()
    return bool(
        entrypoint
        or adapter in {"python_entrypoint", "openai_compatible"}
        or provider_id in {"stub", "rumi"}
    )


def _catalog_source(provider_id: str, manifest: Dict[str, Any]) -> str:
    if manifest.get("source_path"):
        return "extension_manifest"
    if manifest.get("component_manifest_path"):
        return "component_manifest"
    if manifest:
        return "manifest"
    if provider_id in _CURATED_PROVIDER_METADATA:
        return "curated_fallback"
    return "runtime_active"


def _subscription_plans(manifest: Dict[str, Any], curated: Dict[str, Any]) -> List[Dict[str, Any]]:
    def _list_from(value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        plans: List[Dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            plan_id = str(item.get("id") or item.get("plan_id") or "").strip()
            if not plan_id:
                continue
            plan = dict(item)
            plan["id"] = plan_id
            plans.append(plan)
        return plans

    config = manifest.get("config", {})
    if not isinstance(config, dict):
        config = {}

    for value in (
        manifest.get("subscription_plans"),
        config.get("subscription_plans"),
        curated.get("subscription_plans"),
    ):
        plans = _list_from(value)
        if plans:
            return plans

    token_plan_value = config.get("token_plan") or curated.get("token_plan")
    token_plan = token_plan_value.strip() if isinstance(token_plan_value, str) else ""
    if not token_plan:
        return []

    default_model_for = manifest.get("default_model_for", {})
    applies_to_models: List[str] = []
    if isinstance(default_model_for, dict):
        applies_to_models = [
            str(model_id).strip()
            for model_id in dict.fromkeys(default_model_for.values())
            if str(model_id).strip()
        ]
    return [
        {
            "id": token_plan,
            "type": "token_grant",
            "status": "available_if_confirmed",
            "region": str(config.get("region") or "").strip(),
            "region_scoped": bool(config.get("token_plan_region_scoped", False)),
            "requires_manual_signup": bool(config.get("token_plan_requires_manual_signup", False)),
            "do_not_auto_enable": bool(config.get("token_plan_do_not_auto_enable", False)),
            "applies_to_models": applies_to_models,
        }
    ]


def _merge_provider_entry(
    provider_id: str,
    manifest: Optional[Dict[str, Any]] = None,
    *,
    component_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    manifest = dict(manifest or {})
    manifest_was_present = bool(manifest)
    metadata_map = (
        component_metadata
        if component_metadata is not None
        else provider_component_metadata_map()
    )
    component_metadata_entry = dict(metadata_map.get(provider_id, {}))
    curated = {
        **dict(_CURATED_PROVIDER_METADATA.get(provider_id, {})),
        **component_metadata_entry,
    }
    component_provider_manifest = curated.pop("provider_manifest", {})
    if isinstance(component_provider_manifest, dict):
        manifest = {**component_provider_manifest, **manifest}
    display_name = str(
        manifest.get("display_name")
        or curated.get("display_name")
        or provider_id.replace("_", " ").title()
    )
    env_vars = _manifest_env_list(manifest.get("api_key_env"), curated.get("env_vars", []))
    base_url_envs = _manifest_env_list(
        manifest.get("base_url_env"), curated.get("base_url_envs", [])
    )
    default_model = str(
        manifest.get("default_model")
        or (manifest.get("default_model_for", {}) or {}).get("chat")
        or curated.get("default_model")
        or ""
    )
    default_base_url = str(
        manifest.get("default_base_url") or curated.get("default_base_url") or ""
    ).strip()
    default_model_for = manifest.get("default_model_for", {})
    if not isinstance(default_model_for, dict):
        default_model_for = {}
    default_model_for = {**dict(curated.get("default_model_for", {})), **default_model_for}
    adapter = str(manifest.get("adapter", "")).strip()
    entrypoint = str(manifest.get("entrypoint", "")).strip()
    subscription_plans = _subscription_plans(manifest, curated)
    return {
        "id": provider_id,
        "provider_id": provider_id,
        "display_name": display_name,
        "name": display_name,
        "kind": _infer_kind(provider_id, manifest, curated),
        "description": str(manifest.get("description") or curated.get("description") or ""),
        "env_vars": env_vars,
        "base_url_envs": base_url_envs,
        "default_model": default_model,
        "default_model_for": {str(key): str(value) for key, value in default_model_for.items()},
        "default_base_url": default_base_url,
        "catalog_only": _provider_catalog_only(provider_id, manifest, curated),
        "supports_invoke_base": _provider_supports_invoke(provider_id, manifest, curated),
        "capabilities": _capability_list(manifest, curated),
        "credential_required": bool(manifest.get("credential_required", True)),
        "adapter": adapter,
        "entrypoint": entrypoint,
        "subscription_plans": subscription_plans,
        "priority": int(manifest.get("priority", 100)),
        "manifest": manifest,
        "catalog_source": _catalog_source(provider_id, manifest),
        "curated_fallback_used": bool(
            provider_id in _CURATED_PROVIDER_METADATA and manifest_was_present
        ),
    }


def _provider_is_configured(entry: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    provider_id = str(entry.get("provider_id", "")).strip()
    credential_required = bool(entry.get("credential_required", True))
    default_base_url = str(entry.get("default_base_url", "") or "").strip()
    if provider_id and provider_has_oauth_connection(provider_id):
        return True, "browser_oauth"
    if provider_id and provider_has_api_key(provider_id):
        return True, "defaultspack_secret"
    if not credential_required and default_base_url.startswith("local://"):
        return True, "builtin_local_provider"
    if entry.get("kind") == "local" and default_base_url:
        return True, "default_local_endpoint"
    if not credential_required and default_base_url:
        return True, "no_key_gateway"
    if entry["provider_id"] == "stub":
        return True, "builtin"
    return False, None


def _provider_status(entry: Dict[str, Any], active: bool, configured: bool) -> str:
    if active:
        return "active"
    if entry.get("catalog_only"):
        return "catalog_only"
    if configured:
        return "configured"
    return "unconfigured"


def get_provider_catalog(active_provider_ids=None):
    active_ids = set(active_provider_ids or [])
    manifests = _provider_manifest_map()
    component_metadata = provider_component_metadata_map()
    provider_ids = set(manifests.keys()) | set(_CURATED_PROVIDER_METADATA.keys()) | active_ids
    entries = [
        _merge_provider_entry(
            provider_id,
            manifests.get(provider_id),
            component_metadata=component_metadata,
        )
        for provider_id in provider_ids
    ]
    entries.sort(key=lambda item: (int(item.get("priority", 100)), item["provider_id"]))

    catalog = []
    for entry in entries:
        configured, configuration_source = _provider_is_configured(entry)
        active = entry["provider_id"] in active_ids
        availability = {
            "active": active,
            "available": active,
            "configured": configured,
            "catalog_only": bool(entry.get("catalog_only") and not active),
            "supports_invoke": bool(entry.get("supports_invoke_base") or active),
            "status": _provider_status(entry, active, configured),
            "configuration_source": configuration_source,
            "base_url_hint": entry.get("default_base_url", ""),
        }
        catalog.append(
            {
                "id": entry["provider_id"],
                "provider_id": entry["provider_id"],
                "name": entry["name"],
                "display_name": entry["display_name"],
                "kind": entry["kind"],
                "description": entry["description"],
                "env_vars": list(entry.get("env_vars", [])),
                "base_url_envs": list(entry.get("base_url_envs", [])),
                "default_model": entry.get("default_model", ""),
                "default_model_for": dict(entry.get("default_model_for", {})),
                "capabilities": list(entry.get("capabilities", [])),
                "subscription_plans": list(entry.get("subscription_plans", [])),
                "availability": availability,
                "metadata": {
                    "catalog_only": bool(entry.get("catalog_only", False)),
                    "supports_invoke": bool(entry.get("supports_invoke_base") or active),
                    "default_base_url": entry.get("default_base_url", ""),
                    "default_model_for": dict(entry.get("default_model_for", {})),
                    "subscription_plans": list(entry.get("subscription_plans", [])),
                    "adapter": entry.get("adapter", ""),
                    "entrypoint": entry.get("entrypoint", ""),
                    "config": dict(entry.get("manifest", {}).get("config", {}))
                    if isinstance(entry.get("manifest", {}).get("config"), dict)
                    else {},
                    "catalog_source": entry.get("catalog_source", ""),
                    "curated_fallback_used": bool(entry.get("curated_fallback_used", False)),
                    "manifest_path": entry.get("manifest", {}).get("source_path")
                    or entry.get("manifest", {}).get("component_manifest_path", ""),
                    "oauth": provider_oauth_status(entry["provider_id"]),
                },
            }
        )
    return catalog


def get_provider_catalog_map(active_provider_ids=None):
    return {
        entry["provider_id"]: entry
        for entry in get_provider_catalog(active_provider_ids=active_provider_ids)
    }


def get_provider_availability(provider_id=None, active_provider_ids=None):
    catalog = get_provider_catalog(active_provider_ids=active_provider_ids)
    if provider_id:
        for entry in catalog:
            if entry["provider_id"] == provider_id:
                return dict(entry["availability"])
        return None
    return {entry["provider_id"]: dict(entry["availability"]) for entry in catalog}


def _load_known_models_from_entry(entrypoint: str) -> List[Dict[str, Any]]:
    if not entrypoint:
        return []
    try:
        provider_cls = import_entrypoint(entrypoint)
    except Exception:
        return []
    known_models = getattr(provider_cls, "KNOWN_MODELS", [])
    if not isinstance(known_models, list):
        return []
    return [dict(model) for model in known_models if isinstance(model, dict)]


def _load_models_for_provider(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    provider_id = entry["provider_id"]
    models: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}

    def _append(items: Iterable[Dict[str, Any]]) -> None:
        for raw in items:
            if not isinstance(raw, dict):
                continue
            raw_id = str(raw.get("id", "")).strip()
            model_id = str(raw.get("model_id", "")).strip()
            if not raw_id and not model_id:
                continue
            key = raw_id or "{}/{}".format(provider_id, model_id)
            if key in seen:
                existing = seen[key]
                for field, value in raw.items():
                    if field not in existing or existing.get(field) in (None, "", [], {}):
                        existing[field] = value
                continue
            item = dict(raw)
            seen[key] = item
            models.append(item)

    # ``get_all_known_models`` is the declarative catalog surface.  External
    # provider runtime inventories remain live-only in their provider adapters;
    # this surface may still expose repository-owned metadata for discovery,
    # routing, and capability inspection.
    _append(model_manifests_from_provider_components(provider_id))
    if provider_id in {"stub", "rumi"}:
        _append(_load_model_manifests(provider_id))
        _append(_load_known_models_from_entry(str(entry.get("entrypoint", ""))))
        _append(_CURATED_PROVIDER_MODELS.get(provider_id, []))
    return models


def _capability_fields(raw_capabilities: Any) -> tuple[List[str], Dict[str, Any]]:
    capability_map = normalize_capability_map(raw_capabilities)
    public_map = dict(capability_map)
    legacy_aliases = {
        "chat": bool(capability_map.get("text_input") or capability_map.get("text_output")),
        "vision": bool(capability_map.get("image_input")),
        "reasoning": bool(capability_map.get("thinking")),
        "tool_calls": bool(capability_map.get("tool_calling")),
    }
    for key, value in legacy_aliases.items():
        public_map.setdefault(key, value)
    return [str(key) for key, value in public_map.items() if bool(value)], public_map


def _normalize_model_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _annotate_model_collisions(models):
    counts: Dict[str, int] = {}
    for item in models:
        key = _normalize_model_token(item.get("model_id"))
        counts[key] = counts.get(key, 0) + 1

    for item in models:
        key = _normalize_model_token(item.get("model_id"))
        collision_count = counts.get(key, 0)
        has_collision = collision_count > 1
        disambiguated_name = item.get("display_name") or item.get("name") or item.get("model_id")
        if has_collision:
            disambiguated_name = "{} ({})".format(
                item.get("display_name") or item.get("name") or item.get("model_id"),
                item.get("provider_display_name") or item.get("provider_id"),
            )
        item["name_collision"] = has_collision
        item["provider_count_for_model_name"] = collision_count
        item["ambiguity_key"] = key
        item["disambiguated_name"] = disambiguated_name
        metadata = dict(item.get("metadata", {}))
        metadata.update(
            {
                "name_collision": has_collision,
                "provider_count_for_model_name": collision_count,
                "ambiguity_key": key,
                "provider_model_key": item.get("qualified_model_id"),
                "disambiguated_name": disambiguated_name,
            }
        )
        item["metadata"] = metadata
    return models


def get_all_known_models(provider_id=None, active_provider_ids=None):
    catalog_map = get_provider_catalog_map(active_provider_ids=active_provider_ids)
    if provider_id:
        provider_ids = [provider_id]
    elif active_provider_ids is not None:
        active_ids = {
            str(item).strip()
            for item in active_provider_ids
            if str(item or "").strip()
        }
        provider_ids = [
            current_provider_id
            for current_provider_id in catalog_map
            if current_provider_id in active_ids
        ]
    else:
        provider_ids = list(catalog_map.keys())
    models = []

    for current_provider_id in provider_ids:
        provider_entry = catalog_map.get(current_provider_id)
        if provider_entry is None:
            continue
        for raw in _load_models_for_provider(
            provider_entry["metadata"] | {"provider_id": current_provider_id}
        ):
            model_provider_id = raw.get("provider") or raw.get("provider_id") or current_provider_id
            qualified_model_id = str(raw.get("id", "")).strip()
            model_id = str(raw.get("model_id", "")).strip()
            if qualified_model_id and "/" in qualified_model_id and not model_id:
                _, model_id = qualified_model_id.split("/", 1)
            if not model_id:
                model_id = str(raw.get("model_name") or raw.get("name") or "").strip()
            if not model_id:
                continue
            if not qualified_model_id:
                qualified_model_id = "{}/{}".format(model_provider_id, model_id)
            display_name = str(raw.get("display_name") or raw.get("name") or model_id)
            defaults = normalize_routing_defaults(raw)
            routing = dict(raw.get("routing", {})) if isinstance(raw.get("routing"), dict) else {}
            metadata = dict(raw.get("metadata", {}))
            pricing = dict(raw.get("pricing", {})) if isinstance(raw.get("pricing"), dict) else {}
            capabilities, capability_map = _capability_fields(raw.get("capabilities", []))
            request_features = normalize_request_features(raw.get("request_features", {}))
            thinking = (
                dict(raw.get("thinking", {})) if isinstance(raw.get("thinking"), dict) else {}
            )
            if capability_map and "capabilities" not in metadata:
                metadata["capabilities"] = capability_map
            if request_features and "request_features" not in metadata:
                metadata["request_features"] = request_features
            if thinking and "thinking" not in metadata:
                metadata["thinking"] = thinking
            metadata.update(
                {
                    "provider_model_key": qualified_model_id,
                    "provider_display_name": provider_entry["display_name"],
                    "provider_kind": provider_entry["kind"],
                    "availability_status": provider_entry["availability"].get("status"),
                    "defaults": defaults,
                    "pricing": pricing,
                    "routing": routing,
                }
            )
            context_window = context_window_value(raw, default=0)
            item = {
                "id": qualified_model_id,
                "qualified_model_id": qualified_model_id,
                "provider": model_provider_id,
                "provider_id": model_provider_id,
                "provider_display_name": provider_entry["display_name"],
                "model_id": model_id,
                "model_name": model_id,
                "name": display_name,
                "display_name": display_name,
                "type": str(raw.get("type", "chat")),
                "context_window": context_window,
                "capabilities": capabilities,
                "request_features": request_features,
                "routing": routing,
                "thinking": thinking,
                "availability": dict(provider_entry["availability"]),
                "supports_invoke": bool(
                    provider_entry["availability"].get("supports_invoke", False)
                ),
                "defaults": defaults,
                "pricing": pricing,
                "metadata": metadata,
            }
            if "supports_thinking" in raw:
                item["supports_thinking"] = bool(raw.get("supports_thinking"))
            if isinstance(raw.get("thinking_levels"), list):
                item["thinking_levels"] = list(raw.get("thinking_levels", []))
            if "default_thinking_level" in raw:
                item["default_thinking_level"] = raw.get("default_thinking_level")
            if thinking:
                item["supports_thinking"] = bool(
                    thinking.get("supported", item.get("supports_thinking", False))
                )
                if isinstance(thinking.get("levels"), list):
                    item["thinking_levels"] = list(thinking.get("levels") or [])
                if "default_level" in thinking:
                    item["default_thinking_level"] = thinking.get("default_level")
            models.append(item)

    deduped: Dict[str, Dict[str, Any]] = {}
    for model in models:
        deduped.setdefault(model["qualified_model_id"], model)
    return _annotate_model_collisions(list(deduped.values()))


def _find_model_entry(models, model_ref="", provider_id="", model_id=""):
    if model_ref:
        for entry in models:
            if entry["qualified_model_id"] == model_ref or entry["id"] == model_ref:
                return entry
    if provider_id and model_id:
        qualified = "{}/{}".format(provider_id, model_id)
        for entry in models:
            if entry["qualified_model_id"] == qualified:
                return entry
    return None


def build_profile_catalog(active_provider_ids=None, custom_profiles=None):
    models = get_all_known_models(active_provider_ids=active_provider_ids)
    profiles = []
    for model in models:
        metadata = dict(model.get("metadata", {}))
        metadata.update(
            {
                "profile_source": "catalog",
                "resolved_model_key": model["qualified_model_id"],
            }
        )
        profile = {
            "id": model["qualified_model_id"],
            "profile_id": model["qualified_model_id"],
            "name": model["display_name"],
            "display_name": model["display_name"],
            "provider": model["provider_id"],
            "provider_id": model["provider_id"],
            "provider_display_name": model["provider_display_name"],
            "model": model["model_id"],
            "model_id": model["model_id"],
            "model_name": model["model_name"],
            "qualified_model_id": model["qualified_model_id"],
            "availability": dict(model["availability"]),
            "name_collision": model["name_collision"],
            "provider_count_for_model_name": model["provider_count_for_model_name"],
            "disambiguated_name": model["disambiguated_name"],
            "type": model.get("type", "chat"),
            "context_window": int(model.get("context_window", 0) or 0),
            "capabilities": list(model.get("capabilities", [])),
            "request_features": dict(model.get("request_features", {}))
            if isinstance(model.get("request_features"), dict)
            else {},
            "routing": dict(model.get("routing", {}))
            if isinstance(model.get("routing"), dict)
            else {},
            "thinking": dict(model.get("thinking", {}))
            if isinstance(model.get("thinking"), dict)
            else {},
            "defaults": dict(model.get("defaults", {}))
            if isinstance(model.get("defaults"), dict)
            else {},
            "pricing": dict(model.get("pricing", {}))
            if isinstance(model.get("pricing"), dict)
            else {},
            "metadata": metadata,
        }
        if "supports_thinking" in model:
            profile["supports_thinking"] = bool(model.get("supports_thinking"))
        if isinstance(model.get("thinking_levels"), list):
            profile["thinking_levels"] = list(model.get("thinking_levels", []))
        if "default_thinking_level" in model:
            profile["default_thinking_level"] = model.get("default_thinking_level")
        profiles.append(profile)

    for profile_name, raw_profile in (custom_profiles or {}).items():
        if not isinstance(raw_profile, dict):
            continue
        provider_id = raw_profile.get("provider") or raw_profile.get("provider_id", "")
        model_id = raw_profile.get("model") or raw_profile.get("model_id", "")
        resolved = _find_model_entry(
            models,
            model_ref=raw_profile.get("qualified_model_id", ""),
            provider_id=provider_id,
            model_id=model_id,
        )
        if resolved is None and model_id:
            matches = [entry for entry in models if entry["model_id"] == model_id]
            if len(matches) == 1:
                resolved = matches[0]
        availability = (
            dict(resolved["availability"]) if resolved else {"active": False, "available": False}
        )
        metadata = dict(raw_profile.get("metadata", {}))
        metadata.update(
            {
                "profile_source": "custom",
                "resolved_model_key": resolved["qualified_model_id"] if resolved else "",
            }
        )
        profiles.append(
            {
                "id": profile_name,
                "profile_id": profile_name,
                "name": raw_profile.get("display_name") or raw_profile.get("name") or profile_name,
                "display_name": raw_profile.get("display_name")
                or raw_profile.get("name")
                or profile_name,
                "provider": provider_id or (resolved["provider_id"] if resolved else ""),
                "provider_id": provider_id or (resolved["provider_id"] if resolved else ""),
                "provider_display_name": resolved["provider_display_name"] if resolved else "",
                "model": model_id or (resolved["model_id"] if resolved else ""),
                "model_id": model_id or (resolved["model_id"] if resolved else ""),
                "model_name": model_id or (resolved["model_id"] if resolved else ""),
                "qualified_model_id": resolved["qualified_model_id"] if resolved else "",
                "availability": availability,
                "name_collision": bool(resolved and resolved["name_collision"]),
                "provider_count_for_model_name": int(
                    resolved["provider_count_for_model_name"] if resolved else 0
                ),
                "disambiguated_name": resolved["disambiguated_name"] if resolved else profile_name,
                "metadata": metadata,
            }
        )
    return profiles


def _load_legacy_providers() -> Dict[str, Any]:
    available = {}
    for _env_vars, provider_id, module_path, class_name in _LEGACY_PROVIDER_REGISTRY:
        if not provider_has_api_key(provider_id):
            continue
        try:
            module = importlib.import_module(module_path)
            provider_cls = getattr(module, class_name)
            available[provider_id] = provider_cls(api_key=_manifest_credential(provider_id))
        except Exception:
            continue
    return available


def _credentials_ready(manifest: Dict[str, Any], provider_id: str) -> bool:
    if provider_id == "stub":
        return True
    if provider_id == "rumi":
        return False
    credential_required = bool(manifest.get("credential_required", True))
    api_envs = _manifest_env_list(
        manifest.get("api_key_env"),
        _CURATED_PROVIDER_METADATA.get(provider_id, {}).get("env_vars", []),
    )
    base_url_envs = _manifest_env_list(
        manifest.get("base_url_env"),
        _CURATED_PROVIDER_METADATA.get(provider_id, {}).get("base_url_envs", []),
    )
    # The unqualified Xiaomi key is an explicit SGP token-plan opt-in.  It
    # must not implicitly enable the global account inventory or another
    # region, whose endpoint and trust record are independently selected.
    if "MIMO_API_KEY" in api_envs and provider_id != "xiaomi-token-plan-sgp":
        # A regional provider may advertise the shared legacy name for
        # compatibility, but only its own credential key may enable it.
        direct_keys = set(provider_secret_keys(provider_id)) - {"MIMO_API_KEY"}
        if not direct_keys:
            return False
    if provider_has_oauth_connection(provider_id):
        return True
    if provider_has_api_key(provider_id):
        return True
    if not credential_required:
        return not base_url_envs or bool(str(manifest.get("default_base_url", "")).strip())
    return False


def _cloud_runtime_enabled() -> bool:
    # Cloud execution is a profile/host decision.  An ambient process flag
    # must never grant authority or make a missing credential appear ready.
    from core_runtime.host_contract import host_contract_value

    return host_contract_value("cloud_providers_enabled").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _instantiate_manifest_provider(
    manifest: Dict[str, Any], *, injected_api_key: str = ""
):
    provider_id = str(manifest.get("id", "")).strip()
    if not provider_id or provider_id == "rumi":
        return None

    adapter = str(manifest.get("adapter", "")).strip()
    entrypoint = str(manifest.get("entrypoint", "")).strip()
    if adapter == "openai_compatible":
        config_value = manifest.get("config")
        config = dict(config_value) if isinstance(config_value, dict) else {}
        if config.get("custom_openai_compatible"):
            api_id = str(config.get("api_id") or "").strip()
            if not api_id:
                return None
            api_key = read_provider_api_key(provider_id, api_id) or ""
            requires_credential = bool(manifest.get("credential_required", True))
            if not api_key and requires_credential:
                return None
            return OpenAICompatibleProvider(
                provider_id=provider_id,
                display_name=str(manifest.get("display_name") or provider_id),
                api_key=api_key,
                base_url=str(manifest.get("default_base_url") or ""),
                known_models=list(manifest.get("models") or []),
                credential_required=requires_credential,
                remote_model_discovery=True,
                remote_model_discovery_requires_auth=bool(
                    config.get("model_list_requires_auth", True)
                ),
                remote_model_list_path=str(config.get("model_list_path") or "/models"),
                remote_model_cache_ttl_seconds=config.get("model_cache_ttl_seconds", 3600),
            )
        provider_cls = OPENAI_COMPATIBLE_PROVIDER_CLASSES.get(
            provider_id,
            OpenAICompatibleProvider,
        )
        program_provider = provider_id in provider_program_manifests()
        return provider_cls.from_manifest(
            manifest,
            api_key=str(injected_api_key or _manifest_credential(provider_id) or ""),
            # The provider program forbids static inventory snapshots: its
            # authenticated /models response is the sole runtime source.
            # Independently installed custom extensions may still explicitly
            # opt into their own declared model manifests.
            model_manifests=[] if program_provider else _load_model_manifests(provider_id),
            allow_declared_models=not program_provider,
        )
    if entrypoint:
        provider_cls = _import_provider_entrypoint(entrypoint)
        if provider_id.startswith("xiaomi-token-plan-"):
            return provider_cls(api_key=str(injected_api_key or "").strip())
        if provider_id == "ollama":
            return provider_cls(
                api_key=str(
                    injected_api_key or _manifest_credential(provider_id) or ""
                ).strip()
            )
        return provider_cls()
    return None


def _manifest_credential(provider_id: str) -> str:
    """Resolve a selected connection without consulting process globals."""

    value = read_provider_api_key(provider_id, "legacy")
    if value:
        return str(value).strip()
    for connection in provider_named_api_keys(provider_id):
        if not connection.get("configured"):
            continue
        api_id = str(connection.get("api_id") or "").strip()
        if api_id:
            value = read_provider_api_key(provider_id, api_id)
            if value:
                return str(value).strip()
    return ""


def _import_provider_entrypoint(entrypoint: str):
    raw = str(entrypoint or "").strip()
    if ":" not in raw:
        return import_entrypoint(raw)
    module_name, attr_name = raw.split(":", 1)
    if module_name.startswith("domain.ai_client.providers."):
        legacy_module = sys.modules.get(module_name)
        if legacy_module is not None and hasattr(legacy_module, attr_name):
            return getattr(legacy_module, attr_name)
    return import_entrypoint(raw)


def detect_available_providers():
    """Detect manifest-driven runtime providers, then fall back to legacy shims."""
    load_provider_api_keys_into_env()
    available = {}
    manifests = _provider_manifest_map()
    for provider_id, manifest in manifests.items():
        if not _credentials_ready(manifest, provider_id):
            continue
        try:
            injected_api_key = ""
            if provider_id.startswith("xiaomi-token-plan-"):
                injected_api_key = read_provider_api_key(provider_id, "legacy") or ""
            provider = _instantiate_manifest_provider(
                manifest,
                injected_api_key=injected_api_key,
            )
        except Exception:
            provider = None
        if provider is not None:
            available[provider_id] = provider

    for provider_id, provider in _load_legacy_providers().items():
        available.setdefault(provider_id, provider)
    return available


def detect_rumi_provider(client):
    """Create the rumi meta-provider when a non-stub provider is active."""
    non_stub = [name for name in client._providers if name != "stub"]
    if not non_stub:
        return None

    manifest = _provider_manifest_map().get("rumi", {})
    entrypoint = str(manifest.get("entrypoint", "")).strip()
    if entrypoint:
        try:
            provider_cls = import_entrypoint(entrypoint)
            return provider_cls(client)
        except Exception:
            return None

    try:
        from .rumi_provider import RumiProvider

        return RumiProvider(client)
    except Exception:
        return None


def get_best_model_for_provider(name, use_case="chat"):
    """Return a preferred model only for internal pseudo-providers.

    External provider inventories are account- and connection-scoped. Their
    checked-in extension manifests may describe routing preferences, but those
    references are not proof that a model is currently visible or invokable.
    """
    if name not in {"stub", "rumi"}:
        return None
    try:
        registry = get_extension_registry(force_reload=False)
        best = registry.llm().best_model(name, use_case=use_case)
        if best is not None:
            return str(best.get("model_id", ""))
        provider_manifest = registry.get("llm_provider", name)
        if provider_manifest:
            defaults = provider_manifest.get("default_model_for", {}) or {}
            if use_case in defaults:
                return str(defaults[use_case])
            if provider_manifest.get("default_model"):
                return str(provider_manifest["default_model"])
    except Exception:
        pass
    return _BEST_MODEL_BY_PROVIDER.get(name)
