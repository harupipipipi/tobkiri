"""AI21 Studio catalog derived from the vendor's machine-readable model page."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from typing import Any, Dict, List

from .openai_compatible_provider import OpenAICompatibleProvider


class AI21Provider(OpenAICompatibleProvider):
    provider_name = "ai21"
    display_name = "AI21 Labs"
    DEFAULT_BASE_URL = "https://api.ai21.com/studio/v1"
    MODEL_DOCUMENT_URL = "https://docs.ai21.com/docs/jamba-foundation-models.md"

    @classmethod
    def from_manifest(
        cls,
        manifest: Dict[str, Any],
        *,
        api_key: str = "",
        model_manifests: List[Dict[str, Any]] | None = None,
        allow_declared_models: bool = True,
    ) -> "AI21Provider":
        del model_manifests
        del allow_declared_models
        return cls(
            api_key=api_key,
            api_key_env=manifest.get("api_key_env") or "AI21_API_KEY",
            base_url_env=manifest.get("base_url_env") or "AI21_BASE_URL",
            default_base_url=str(manifest.get("default_base_url") or cls.DEFAULT_BASE_URL),
        )

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        *,
        api_key_env: Any = "AI21_API_KEY",
        base_url_env: Any = "AI21_BASE_URL",
        default_base_url: str = DEFAULT_BASE_URL,
    ):
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
            remote_model_discovery_requires_auth=False,
            remote_model_list_path="/models",
            remote_model_cache_ttl_seconds=86400,
        )

    def _fetch_remote_models(self) -> List[Dict[str, Any]]:
        """AI21 publishes current callable endpoint IDs in docs, not /models.

        Fetch the official Markdown on refresh so releases appear without a
        checked-in provider model snapshot. Deprecated table entries are
        intentionally excluded.
        """
        request = urllib.request.Request(
            self.MODEL_DOCUMENT_URL,
            headers={"Accept": "text/markdown", "User-Agent": "RumiAI/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=20) as response:
                document = response.read().decode("utf-8")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            return []
        active = document.split("## Model Details", 1)
        if len(active) != 2:
            return []
        active = active[1].split("## Model Deprecation", 1)[0]
        identifiers = []
        for model_id in re.findall(r"`([^`]+)`", active):
            model_id = model_id.strip()
            if not model_id or model_id.upper() == "N/A" or model_id in identifiers:
                continue
            identifiers.append(model_id)
        return self._normalize_remote_models(
            [
                {
                    "id": model_id,
                    "name": model_id,
                    "context_length": 256000,
                    "type": "chat",
                    "capabilities": ["chat", "stream"],
                }
                for model_id in identifiers
            ]
        )

    def _normalize_remote_model(self, raw: Any) -> Dict[str, Any] | None:
        model = super()._normalize_remote_model(raw)
        if model:
            metadata = dict(model.get("metadata") or {})
            metadata.update(
                {
                    "source": "ai21_official_model_document",
                    "source_endpoint": self.MODEL_DOCUMENT_URL,
                    "capability_confidence": "official_document",
                }
            )
            model["metadata"] = metadata
        return model
