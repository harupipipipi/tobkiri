"""AssemblyAI LLM Gateway live catalog adapter."""

from __future__ import annotations

from typing import Any, Dict, List

from .openai_compatible_provider import OpenAICompatibleProvider


class AssemblyAIProvider(OpenAICompatibleProvider):
    """Use AssemblyAI's account-aware OpenAI-compatible Models API.

    The LLM Gateway accepts the AssemblyAI API key directly in the
    ``Authorization`` header, not as a Bearer token.  Its `/models` response
    includes the currently routable providers, parameter support and limits.
    """

    provider_name = "assemblyai"
    display_name = "AssemblyAI"
    DEFAULT_BASE_URL = "https://llm-gateway.assemblyai.com/v1"

    @classmethod
    def from_manifest(
        cls,
        manifest: Dict[str, Any],
        *,
        api_key: str = "",
        model_manifests: List[Dict[str, Any]] | None = None,
        allow_declared_models: bool = True,
    ) -> "AssemblyAIProvider":
        del model_manifests
        del allow_declared_models
        return cls(
            api_key=api_key,
            api_key_env=manifest.get("api_key_env") or "ASSEMBLYAI_API_KEY",
            base_url_env=manifest.get("base_url_env") or "ASSEMBLYAI_LLM_GATEWAY_BASE_URL",
            default_base_url=str(manifest.get("default_base_url") or cls.DEFAULT_BASE_URL),
        )

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        *,
        api_key_env: Any = "ASSEMBLYAI_API_KEY",
        base_url_env: Any = "ASSEMBLYAI_LLM_GATEWAY_BASE_URL",
        default_base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            provider_id=self.provider_name,
            display_name=self.display_name,
            api_key_env=api_key_env,
            base_url_env=str(
                base_url_env[0]
                if isinstance(base_url_env, (list, tuple)) and base_url_env
                else base_url_env
            ),
            default_base_url=default_base_url,
            credential_required=True,
            known_models=[],
            remote_model_discovery=True,
            remote_model_discovery_requires_auth=True,
            remote_model_list_path="/models",
            remote_model_cache_ttl_seconds=3600,
        )

    def _headers(self, content_type: str = "application/json") -> Dict[str, str]:
        headers = super()._headers(content_type)
        if self._api_key:
            headers["Authorization"] = self._api_key
        return headers

    def _normalize_remote_model(self, raw: Any) -> Dict[str, Any] | None:
        model = super()._normalize_remote_model(raw)
        if model is None or not isinstance(raw, dict):
            return model
        parameters = raw.get("supported_parameters")
        parameter_set = (
            {str(value or "").strip().lower() for value in parameters if str(value or "").strip()}
            if isinstance(parameters, list)
            else set()
        )
        provider = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
        capabilities = dict(model.get("capabilities") or {})
        supports_tools = bool({"tools", "tool_choice"} & parameter_set)
        capabilities.update(
            {
                "tool_calling": supports_tools,
                "tool_calls": supports_tools,
                "streaming": "stream" in parameter_set,
                "structured_output": "response_format" in parameter_set,
                "json_schema": "response_format" in parameter_set,
            }
        )
        model["capabilities"] = capabilities
        for field in ("context_length", "max_context_tokens"):
            try:
                value = int(raw.get(field) or provider.get(field) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                model["context_window"] = value
                model["max_context"] = value
                model["max_context_tokens"] = value
                break
        metadata = dict(model.get("metadata") or {})
        metadata.update(
            {
                "source": "assemblyai_llm_gateway_models_api",
                "capability_source": "assemblyai_llm_gateway_models_api",
                "capability_confidence": "provider_reported",
                "supported_parameters": sorted(parameter_set),
                "top_provider": provider,
                "description": raw.get("description"),
                "pricing": raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {},
            }
        )
        model["metadata"] = metadata
        return model
