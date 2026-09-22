from __future__ import annotations

import os
from typing import Any, Dict, List

from ..provider_routing_settings import openrouter_provider_options
from .openai_compatible_provider import OpenAICompatibleProvider


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter provider backed exclusively by its live account inventory."""

    OPENROUTER_PARAM_KEYS = {
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "response_format",
        "structured_outputs",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning",
        "reasoning_effort",
        "include_reasoning",
        "provider",
        "models",
        "web_search_options",
    }
    # Kept empty deliberately: availability must come from /models for the
    # configured OpenRouter account, never from a bundled model snapshot.
    KNOWN_MODELS: List[Dict[str, Any]] = []

    def __init__(self, known_models: List[Dict[str, Any]] | None = None) -> None:
        models = self._catalog_models() if known_models is None else known_models
        super().__init__(
            provider_id="openrouter",
            display_name="OpenRouter",
            api_key_env="OPENROUTER_API_KEY",
            base_url_env="OPENROUTER_BASE_URL",
            default_base_url="https://openrouter.ai/api/v1",
            credential_required=True,
            known_models=models,
            extra_headers={
                "HTTP-Referer": os.environ.get(
                    "OPENROUTER_HTTP_REFERER",
                    "https://github.com/harupipipipi/rumiai",
                ),
                "X-Title": os.environ.get(
                    "OPENROUTER_X_TITLE",
                    os.environ.get("OPENROUTER_X_OPENROUTER_TITLE", "rumiai-defaultspack"),
                ),
            },
            remote_model_discovery=True,
            remote_model_list_path="/models",
            remote_model_cache_ttl_seconds=3600,
        )

    @classmethod
    def _catalog_models(cls) -> List[Dict[str, Any]]:
        # Compatibility hook for injected test/dev inventories. Production
        # discovery always starts empty and requests the provider's /models API.
        return []

    @classmethod
    def _provider_model_id(cls, model: str) -> str:
        model_ref = str(model or "").strip()
        prefix = "openrouter/"
        if model_ref.startswith(prefix):
            return model_ref[len(prefix) :]
        return model_ref

    @staticmethod
    def _string_list(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item or "").strip()]

    @staticmethod
    def _positive_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def _normalize_remote_model(self, raw: Any) -> Dict[str, Any] | None:
        normalized = super()._normalize_remote_model(raw)
        if normalized is None or not isinstance(raw, dict):
            return normalized

        architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
        top_provider = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
        pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
        supported_parameters = self._string_list(raw.get("supported_parameters"))
        input_modalities = self._string_list(
            architecture.get("input_modalities") or raw.get("input_modalities")
        )
        output_modalities = self._string_list(
            architecture.get("output_modalities") or raw.get("output_modalities")
        )
        modality = str(architecture.get("modality") or raw.get("modality") or "").strip()
        modality_tokens = {
            token.strip().lower()
            for token in modality.replace("->", ",").replace("+", ",").split(",")
            if token.strip()
        }
        input_tokens = {item.lower() for item in input_modalities} | modality_tokens
        output_tokens = {item.lower() for item in output_modalities} | modality_tokens
        parameter_tokens = {item.lower() for item in supported_parameters}

        model_type = str(normalized.get("type") or "chat")
        if "embedding" in output_tokens or "embeddings" in output_tokens:
            model_type = "embedding"
        elif "image" in output_tokens and "text" not in output_tokens:
            model_type = "image_gen"
        elif "video" in output_tokens and "text" not in output_tokens:
            model_type = "video_gen"
        elif "audio" in output_tokens and "text" not in output_tokens:
            model_type = "tts"
        normalized["type"] = model_type

        is_chat = model_type == "chat"
        supports_tools = bool(
            parameter_tokens.intersection({"tools", "tool_choice", "parallel_tool_calls"})
        )
        supports_reasoning = bool(
            parameter_tokens.intersection({"reasoning", "reasoning_effort", "include_reasoning"})
        )
        supports_structured = bool(
            parameter_tokens.intersection(
                {"response_format", "structured_outputs", "json_schema", "json_mode"}
            )
        )
        capabilities = dict(normalized.get("capabilities") or {})
        capabilities.update(
            {
                "chat": is_chat,
                "text_input": is_chat or "text" in input_tokens,
                "text_output": is_chat or "text" in output_tokens,
                "streaming": is_chat,
                "image_input": "image" in input_tokens,
                "vision": "image" in input_tokens,
                "audio_input": "audio" in input_tokens,
                "tool_calling": supports_tools,
                "tool_calls": supports_tools,
                "parallel_tool_calls": "parallel_tool_calls" in parameter_tokens,
                "thinking": supports_reasoning,
                "reasoning": supports_reasoning,
                "structured_output": supports_structured,
                "json_schema": bool(
                    parameter_tokens.intersection({"structured_outputs", "json_schema"})
                ),
                "embeddings": model_type == "embedding",
                "rerank": model_type == "rerank",
                "image_generation": model_type == "image_gen",
                "video_generation": model_type == "video_gen",
                "tts": model_type == "tts",
            }
        )
        normalized["capabilities"] = capabilities

        context_window = self._positive_int(
            raw.get("context_length")
            or top_provider.get("context_length")
            or top_provider.get("max_context_length")
        )
        if context_window:
            normalized["context_window"] = context_window
            normalized["max_context"] = context_window
            normalized["max_context_tokens"] = context_window

        request_features = dict(normalized.get("request_features") or {})
        request_features.update(
            {
                "tool_choice": "tool_choice" in parameter_tokens,
                "parallel_tool_calls": "parallel_tool_calls" in parameter_tokens,
                "response_format": "response_format" in parameter_tokens,
                "structured_outputs": "structured_outputs" in parameter_tokens,
                "reasoning": "reasoning" in parameter_tokens,
                "reasoning_effort": "reasoning_effort" in parameter_tokens,
                "web_search_options": "web_search_options" in parameter_tokens,
            }
        )
        normalized["request_features"] = {
            key: value for key, value in request_features.items() if value
        }

        if supports_reasoning:
            normalized["thinking"] = {
                "supported": True,
                "levels": ["low", "medium", "high"],
                "default_level": "medium",
                "provider_mapping": {
                    "none": {},
                    "low": {"reasoning_effort": "low"},
                    "medium": {"reasoning_effort": "medium"},
                    "high": {"reasoning_effort": "high"},
                    "xhigh": {"reasoning_effort": "high"},
                },
            }
            normalized["supports_thinking"] = True
            normalized["thinking_levels"] = ["low", "medium", "high"]
            normalized["default_thinking_level"] = "medium"

        metadata = dict(normalized.get("metadata") or {})
        metadata.update(
            {
                "source": "openrouter_models_api",
                "source_endpoint": "/models",
                "visibility_scope": "account",
                "capability_source": "openrouter_models_api",
                "capability_confidence": "provider_reported",
                "canonical_slug": raw.get("canonical_slug"),
                "hugging_face_id": raw.get("hugging_face_id"),
                "description": raw.get("description"),
                "architecture": dict(architecture),
                "top_provider": dict(top_provider),
                "pricing": dict(pricing),
                "per_request_limits": dict(raw.get("per_request_limits"))
                if isinstance(raw.get("per_request_limits"), dict)
                else {},
                "supported_parameters": supported_parameters,
                "input_modalities": input_modalities,
                "output_modalities": output_modalities,
                "created": raw.get("created"),
            }
        )
        normalized["metadata"] = metadata
        if pricing:
            normalized["pricing"] = dict(pricing)
        return normalized

    @staticmethod
    def _copy_chat_params(body: Dict[str, Any], params: Dict[str, Any]) -> None:
        raw = dict(params or {})
        extra_body = raw.pop("extra_body", None)
        OpenAICompatibleProvider._copy_chat_params(body, raw)
        for key in OpenRouterProvider.OPENROUTER_PARAM_KEYS:
            if key in raw:
                body[key] = raw[key]
        if isinstance(extra_body, dict):
            body.update(extra_body)

    def _with_gateway_routing(self, params: Dict[str, Any] | None) -> Dict[str, Any]:
        routed = dict(params or {})
        extra_body = routed.get("extra_body") if isinstance(routed.get("extra_body"), dict) else {}
        if "provider" in routed or "provider" in extra_body:
            return routed
        options = openrouter_provider_options()
        if options:
            routed["provider"] = options
        return routed

    def list_models(self) -> List[Dict[str, Any]]:
        return self._merge_remote_models([])

    def _assert_supported_model(self, model: str) -> None:
        model_ref = str(model or "").strip()
        provider_model_id = self._provider_model_id(model_ref)
        supported: set[str] = set()
        invocation_models: List[Dict[str, Any]] = []
        # Explicit inventories are used by callers that already performed
        # their own catalog selection. The default provider inventory remains
        # empty and still requires live or last-known-good discovery below.
        invocation_models.extend(self._normalize_remote_models(self.KNOWN_MODELS))
        cache = self._load_remote_model_cache()
        if cache:
            invocation_models.extend(self._normalize_remote_models(cache.get("models")))
        if not invocation_models:
            invocation_models.extend(self._remote_discovered_models())
        for item in invocation_models:
            if not isinstance(item, dict):
                continue
            for key in ("id", "model_id", "qualified_model_id"):
                value = str(item.get(key) or "").strip()
                if value:
                    supported.add(value)
        if model_ref not in supported and provider_model_id not in supported:
            raise RuntimeError(
                "openrouter: model is not present in the live or last-known-good catalog: "
                f"{provider_model_id}"
            )

    def complete(self, model, messages, tools, params):
        self._assert_supported_model(model)
        return super().complete(
            self._provider_model_id(model),
            messages,
            tools,
            self._with_gateway_routing(params),
        )

    def stream(self, model, messages, tools, params):
        self._assert_supported_model(model)
        return super().stream(
            self._provider_model_id(model),
            messages,
            tools,
            self._with_gateway_routing(params),
        )
