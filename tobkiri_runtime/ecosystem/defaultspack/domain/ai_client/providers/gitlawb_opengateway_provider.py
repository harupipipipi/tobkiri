from __future__ import annotations

from typing import Any, Dict, List

from .component_metadata import model_manifests_from_provider_components
from .openai_compatible_provider import OpenAICompatibleProvider


class GitlawbOpengatewayProvider(OpenAICompatibleProvider):
    """Gitlawb OpenGateway provider backed by the account-visible /models API."""

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
    )
    MODEL_IDS = {
        "mimo-v2-flash",
        "mimo-v2-omni",
        "mimo-v2-pro",
        "mimo-v2.5",
        "mimo-v2.5-pro",
    }
    KNOWN_MODELS = [
        {
            "id": "gitlawb-opengateway/mimo-v2.5-pro",
            "model_id": "mimo-v2.5-pro",
            "name": "MiMo V2.5 Pro via Gitlawb OpenGateway",
            "display_name": "MiMo V2.5 Pro via Gitlawb OpenGateway",
            "provider": "gitlawb-opengateway",
            "provider_id": "gitlawb-opengateway",
            "type": "chat",
            "defaults": {"chat": True, "reasoning": True},
            "capabilities": ["chat", "reasoning", "streaming"],
            "supports_thinking": True,
            "thinking_levels": ["low", "medium", "high", "xhigh"],
            "default_thinking_level": "medium",
            "metadata": {
                "source": "gitlawb-opengateway",
                "privacy": "external_api_key_gateway",
                "api_key_required": True,
                "openai_base_url": "https://opengateway.gitlawb.com/v1",
                "openai_model": "mimo-v2.5-pro",
            },
        },
        {
            "id": "gitlawb-opengateway/mimo-v2-flash",
            "model_id": "mimo-v2-flash",
            "name": "MiMo V2 Flash via Gitlawb OpenGateway",
            "display_name": "MiMo V2 Flash via Gitlawb OpenGateway",
            "provider": "gitlawb-opengateway",
            "provider_id": "gitlawb-opengateway",
            "type": "chat",
            "defaults": {"chat": True, "fast": True},
            "capabilities": ["chat", "streaming"],
            "metadata": {
                "source": "gitlawb-opengateway",
                "privacy": "external_api_key_gateway",
                "api_key_required": True,
                "openai_base_url": "https://opengateway.gitlawb.com/v1",
                "openai_model": "mimo-v2-flash",
            },
        },
        {
            "id": "gitlawb-opengateway/mimo-v2-omni",
            "model_id": "mimo-v2-omni",
            "name": "MiMo V2 Omni via Gitlawb OpenGateway",
            "display_name": "MiMo V2 Omni via Gitlawb OpenGateway",
            "provider": "gitlawb-opengateway",
            "provider_id": "gitlawb-opengateway",
            "type": "chat",
            "defaults": {"chat": True, "vision": True},
            "capabilities": ["chat", "streaming", "vision"],
            "metadata": {
                "source": "gitlawb-opengateway",
                "privacy": "external_api_key_gateway",
                "api_key_required": True,
                "vision_verified": True,
                "openai_base_url": "https://opengateway.gitlawb.com/v1",
                "openai_model": "mimo-v2-omni",
            },
        },
        {
            "id": "gitlawb-opengateway/mimo-v2-pro",
            "model_id": "mimo-v2-pro",
            "name": "MiMo V2 Pro via Gitlawb OpenGateway",
            "display_name": "MiMo V2 Pro via Gitlawb OpenGateway",
            "provider": "gitlawb-opengateway",
            "provider_id": "gitlawb-opengateway",
            "type": "reasoning",
            "defaults": {"reasoning": True},
            "capabilities": ["chat", "reasoning", "streaming"],
            "supports_thinking": True,
            "thinking_levels": ["low", "medium", "high", "xhigh"],
            "default_thinking_level": "medium",
            "metadata": {
                "source": "gitlawb-opengateway",
                "privacy": "external_api_key_gateway",
                "api_key_required": True,
                "openai_base_url": "https://opengateway.gitlawb.com/v1",
                "openai_model": "mimo-v2-pro",
            },
        },
        {
            "id": "gitlawb-opengateway/mimo-v2.5",
            "model_id": "mimo-v2.5",
            "name": "MiMo V2.5 via Gitlawb OpenGateway",
            "display_name": "MiMo V2.5 via Gitlawb OpenGateway",
            "provider": "gitlawb-opengateway",
            "provider_id": "gitlawb-opengateway",
            "type": "reasoning",
            "defaults": {"chat": True, "reasoning": True},
            "capabilities": ["chat", "reasoning", "streaming"],
            "supports_thinking": True,
            "thinking_levels": ["low", "medium", "high", "xhigh"],
            "default_thinking_level": "medium",
            "metadata": {
                "source": "gitlawb-opengateway",
                "privacy": "external_api_key_gateway",
                "api_key_required": True,
                "openai_base_url": "https://opengateway.gitlawb.com/v1",
                "openai_model": "mimo-v2.5",
            },
        },
    ]
    # The historical entries above are intentionally inert. Availability is
    # exclusively fetched from the gateway for the configured API key.
    KNOWN_MODELS: List[Dict[str, Any]] = []

    def __init__(self) -> None:
        catalog_models = model_manifests_from_provider_components("gitlawb-opengateway")
        super().__init__(
            provider_id="gitlawb-opengateway",
            display_name="Gitlawb OpenGateway",
            api_key_env="GITLAWB_OPENGATEWAY_API_KEY",
            base_url_env="GITLAWB_OPENGATEWAY_BASE_URL",
            default_base_url="https://opengateway.gitlawb.com/v1",
            credential_required=True,
            known_models=catalog_models,
            extra_headers={
                "User-Agent": self.DEFAULT_USER_AGENT,
            },
            remote_model_discovery=True,
        )

    def _assert_supported_model(self, model: str) -> None:
        model_id = str(model or "").strip()
        if model_id.startswith("gitlawb-opengateway/"):
            model_id = model_id.split("/", 1)[1]
        # The gateway is the model authority.  Selected catalog metadata can
        # describe known routing/capability records, but it must not reject a
        # newly provisioned account model before the gateway sees the request.
        if not model_id:
            raise RuntimeError(
                "unsupported model for gitlawb-opengateway: "
                f"{model}; model id is empty"
            )

    @staticmethod
    def _translate_params(params):
        translated = OpenAICompatibleProvider._translate_params(params)
        if "max_tokens" in translated and "max_completion_tokens" not in translated:
            translated["max_completion_tokens"] = translated.pop("max_tokens")
        return translated

    @staticmethod
    def _copy_chat_params(body, params):
        OpenAICompatibleProvider._copy_chat_params(body, params)
        if "max_completion_tokens" in params:
            body["max_completion_tokens"] = params["max_completion_tokens"]

    def list_models(self) -> List[Dict[str, Any]]:
        return self._merge_remote_models(self.KNOWN_MODELS)

    def complete(self, model, messages, tools, params):
        self._assert_supported_model(model)
        return super().complete(model, messages, tools, params)

    def stream(self, model, messages, tools, params):
        self._assert_supported_model(model)
        return super().stream(model, messages, tools, params)
