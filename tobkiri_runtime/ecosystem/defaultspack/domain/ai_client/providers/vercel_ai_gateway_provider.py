from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from ..metadata_json import MetadataJsonError, load_strict_metadata_json
from ..model_metadata_schema import ModelMetadataSchemaError, validate_model_catalog_source
from ..provider_routing_settings import vercel_gateway_options
from .openai_compatible_provider import OpenAICompatibleProvider


class VercelAIGatewayProvider(OpenAICompatibleProvider):
    """Vercel AI Gateway with dynamic model discovery and provider routing."""

    provider_name = "vercel-ai-gateway"
    display_name = "Vercel AI Gateway"
    DEFAULT_BASE_URL = "https://ai-gateway.vercel.sh/v1"

    @classmethod
    def from_manifest(
        cls,
        manifest: Dict[str, Any],
        *,
        api_key: str = "",
        model_manifests: List[Dict[str, Any]] | None = None,
        allow_declared_models: bool = True,
    ) -> "VercelAIGatewayProvider":
        """Build the dedicated adapter while preserving manifest model overlays."""
        del manifest
        del allow_declared_models
        return cls(api_key=api_key, known_models=model_manifests)

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        known_models: List[Dict[str, Any]] | None = None,
    ) -> None:
        resolved_base_url = str(
            base_url
            or os.environ.get("AI_GATEWAY_BASE_URL", "")
            or os.environ.get("VERCEL_AI_GATEWAY_BASE_URL", "")
            or self.DEFAULT_BASE_URL
        ).strip()
        models = self._catalog_models() if known_models is None else known_models
        super().__init__(
            api_key=api_key,
            base_url=resolved_base_url,
            provider_id=self.provider_name,
            display_name=self.display_name,
            api_key_env=("AI_GATEWAY_API_KEY", "VERCEL_AI_GATEWAY_API_KEY"),
            base_url_env="AI_GATEWAY_BASE_URL",
            default_base_url=self.DEFAULT_BASE_URL,
            credential_required=True,
            known_models=models,
            remote_model_discovery=True,
            remote_model_discovery_requires_auth=False,
            remote_model_list_path="/models",
            remote_model_cache_ttl_seconds=3600,
        )

    @classmethod
    def _provider_model_id(cls, model: str) -> str:
        model_ref = str(model or "").strip()
        prefix = f"{cls.provider_name}/"
        if model_ref.startswith(prefix):
            return model_ref[len(prefix) :]
        return model_ref

    @classmethod
    def _catalog_models(cls) -> List[Dict[str, Any]]:
        # Keep this compatibility catalog read anchored to the repository's
        # canonical model-catalog pack. Runtime invocation still receives an
        # explicit empty inventory and discovers the gateway's live /models
        # response; this path is only for callers that request the public
        # Vercel catalog directly.
        path = (
            Path(__file__).resolve().parents[4]
            / "rumi_model_catalog_pack"
            / "catalog"
            / "providers"
            / cls.provider_name
            / "models.json"
        )
        try:
            payload = load_strict_metadata_json(path)
            validate_model_catalog_source(payload, path=path)
        except (MetadataJsonError, ModelMetadataSchemaError):
            return []
        raw_models = payload.get("models") if isinstance(payload, dict) else []
        return (
            [dict(model) for model in raw_models if isinstance(model, dict)]
            if isinstance(raw_models, list)
            else []
        )

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

        model_type = (
            str(
                raw.get("type")
                or raw.get("model_type")
                or raw.get("modelType")
                or normalized.get("type")
                or "chat"
            )
            .strip()
            .lower()
        )
        type_aliases = {
            "language": "chat",
            "text": "chat",
            "llm": "chat",
            "embedding": "embedding",
            "embeddings": "embedding",
            "rerank": "rerank",
            "reranking": "rerank",
            "image": "image_gen",
            "image_generation": "image_gen",
            "video": "video_gen",
            "video_generation": "video_gen",
            "speech": "tts",
            "audio": "tts",
        }
        normalized["type"] = type_aliases.get(model_type, model_type)

        raw_capabilities = (
            raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else {}
        )
        supported_parameters = self._string_list(
            raw.get("supported_parameters") or raw.get("supportedParameters")
        )
        parameter_tokens = {item.lower() for item in supported_parameters}
        input_modalities = self._string_list(
            raw.get("input_modalities")
            or raw.get("inputModalities")
            or raw_capabilities.get("input_modalities")
        )
        output_modalities = self._string_list(
            raw.get("output_modalities")
            or raw.get("outputModalities")
            or raw_capabilities.get("output_modalities")
        )
        input_tokens = {item.lower() for item in input_modalities}
        output_tokens = {item.lower() for item in output_modalities}
        is_chat = normalized["type"] == "chat"
        supports_tools = bool(
            raw_capabilities.get("tools")
            or raw_capabilities.get("tool_calling")
            or parameter_tokens.intersection({"tools", "tool_choice", "parallel_tool_calls"})
        )
        supports_reasoning = bool(
            raw_capabilities.get("reasoning")
            or raw_capabilities.get("thinking")
            or parameter_tokens.intersection({"reasoning", "reasoning_effort"})
        )
        supports_structured = bool(
            raw_capabilities.get("structured_output")
            or parameter_tokens.intersection(
                {"response_format", "json_schema", "structured_outputs"}
            )
        )

        capabilities = dict(normalized.get("capabilities") or {})
        capabilities.update(
            {
                "chat": is_chat,
                "text_input": is_chat or "text" in input_tokens,
                "text_output": is_chat or "text" in output_tokens,
                "streaming": is_chat,
                "image_input": "image" in input_tokens or bool(raw_capabilities.get("vision")),
                "vision": "image" in input_tokens or bool(raw_capabilities.get("vision")),
                "audio_input": "audio" in input_tokens,
                "tool_calling": supports_tools,
                "tool_calls": supports_tools,
                "parallel_tool_calls": "parallel_tool_calls" in parameter_tokens,
                "thinking": supports_reasoning,
                "reasoning": supports_reasoning,
                "structured_output": supports_structured,
                "json_schema": bool(
                    parameter_tokens.intersection({"json_schema", "structured_outputs"})
                ),
                "embeddings": normalized["type"] == "embedding",
                "rerank": normalized["type"] == "rerank",
                "image_generation": normalized["type"] == "image_gen",
                "video_generation": normalized["type"] == "video_gen",
                "tts": normalized["type"] == "tts",
            }
        )
        normalized["capabilities"] = capabilities

        context_window = self._positive_int(
            raw.get("context_window") or raw.get("contextWindow") or raw.get("context_length")
        )
        if context_window:
            normalized["context_window"] = context_window
            normalized["max_context"] = context_window
            normalized["max_context_tokens"] = context_window

        pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
        providers = raw.get("providers") if isinstance(raw.get("providers"), list) else []
        metadata = dict(normalized.get("metadata") or {})
        metadata.update(
            {
                "source": "vercel_ai_gateway_models_api",
                "source_endpoint": "/models",
                "visibility_scope": "public_gateway_catalog",
                "capability_source": "vercel_ai_gateway_models_api",
                "capability_confidence": "provider_reported",
                "description": raw.get("description"),
                "creator": raw.get("creator") or raw.get("owned_by"),
                "pricing": dict(pricing),
                "supported_parameters": supported_parameters,
                "input_modalities": input_modalities,
                "output_modalities": output_modalities,
                "providers": [dict(item) for item in providers if isinstance(item, dict)],
                "endpoint_metadata_path": f"/models/{str(normalized.get('model_id') or '').strip()}/endpoints",
            }
        )
        normalized["metadata"] = metadata
        if pricing:
            normalized["pricing"] = dict(pricing)

        request_features = {
            "tool_choice": "tool_choice" in parameter_tokens,
            "parallel_tool_calls": "parallel_tool_calls" in parameter_tokens,
            "response_format": "response_format" in parameter_tokens,
            "structured_outputs": bool(
                parameter_tokens.intersection({"structured_outputs", "json_schema"})
            ),
            "reasoning": "reasoning" in parameter_tokens,
            "reasoning_effort": "reasoning_effort" in parameter_tokens,
        }
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
        return normalized

    def _with_gateway_routing(self, params: Dict[str, Any] | None) -> Dict[str, Any]:
        routed = dict(params or {})
        extra_body = (
            dict(routed.get("extra_body") or {})
            if isinstance(routed.get("extra_body"), dict)
            else {}
        )
        provider_options = routed.get("providerOptions")
        if not isinstance(provider_options, dict):
            provider_options = extra_body.get("providerOptions")
        if not isinstance(provider_options, dict):
            provider_options = {}
        provider_options = dict(provider_options)
        if "gateway" not in provider_options:
            options = vercel_gateway_options()
            if options:
                provider_options["gateway"] = options
        if provider_options:
            routed["providerOptions"] = provider_options
        return routed

    def complete(self, model, messages, tools, params):
        return super().complete(
            self._provider_model_id(model),
            messages,
            tools,
            self._with_gateway_routing(params),
        )

    def stream(self, model, messages, tools, params):
        return super().stream(
            self._provider_model_id(model),
            messages,
            tools,
            self._with_gateway_routing(params),
        )
