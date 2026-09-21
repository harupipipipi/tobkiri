from __future__ import annotations

from typing import Any, Dict, List

from .openai_compatible_provider import OpenAICompatibleProvider


class PortkeyAIGatewayProvider(OpenAICompatibleProvider):
    """Portkey's OpenAI-compatible gateway with account-scoped inventory.

    Portkey deliberately uses ``x-portkey-api-key`` rather than an HTTP
    ``Authorization`` bearer token.  Its authenticated Models API is the
    authoritative list because an organization can expose only a subset of
    integrations and models to a workspace.
    """

    provider_name = "portkey-ai-gateway"
    display_name = "Portkey AI Gateway"
    DEFAULT_BASE_URL = "https://api.portkey.ai/v1"

    @classmethod
    def from_manifest(
        cls,
        manifest: Dict[str, Any],
        *,
        api_key: str = "",
        model_manifests: List[Dict[str, Any]] | None = None,
        allow_declared_models: bool = True,
    ) -> "PortkeyAIGatewayProvider":
        del model_manifests
        del allow_declared_models
        return cls(
            api_key=api_key,
            api_key_env=manifest.get("api_key_env") or "PORTKEY_API_KEY",
            base_url_env=manifest.get("base_url_env") or "PORTKEY_BASE_URL",
            default_base_url=str(manifest.get("default_base_url") or cls.DEFAULT_BASE_URL),
        )

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        *,
        api_key_env: Any = "PORTKEY_API_KEY",
        base_url_env: Any = "PORTKEY_BASE_URL",
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
        # Portkey rejects a bearer token as its credential.  Do not emit both
        # auth schemes: a customer's gateway policy may validate headers
        # strictly.
        headers.pop("Authorization", None)
        if self._api_key:
            headers["x-portkey-api-key"] = self._api_key
        return headers
