"""Azure AI Foundry project deployment inventory and OpenAI inference adapter.

Foundry exposes deployable catalog models and models actually available to a
project separately.  Only the latter can be invoked, so this provider lists
the project's live deployments instead of publishing a static Azure catalog.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from ..api_key_store import provider_named_api_keys, read_provider_api_key
from ..base_provider import BaseProvider


class AzureAIFoundryProvider(BaseProvider):
    provider_id = "azure-ai-foundry"
    DEFAULT_API_VERSION = "v1"
    DEFAULT_INFERENCE_API_VERSION = "2024-10-21"
    _MODEL_INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
    _MODEL_INVENTORY_CACHE_TTL_SECONDS = 300

    def __init__(self):
        self._connection = self._configured_connection()
        self._api_key = self._configured_api_key(self._connection)
        self._base_url = str(self._connection.get("base_url") or "").strip().rstrip("/")
        self._api_version = str(self.DEFAULT_API_VERSION).strip()
        self._inference_api_version = str(self.DEFAULT_INFERENCE_API_VERSION).strip()
        self._ssl_ctx = ssl.create_default_context()

    @classmethod
    def _configured_connection(cls) -> Dict[str, Any]:
        for connection in provider_named_api_keys(cls.provider_id):
            if connection.get("configured") and str(connection.get("base_url") or "").strip():
                return dict(connection)
        return {}

    @classmethod
    def _configured_api_key(cls, connection: Dict[str, Any]) -> str:
        api_id = str(connection.get("api_id") or "").strip()
        return str(read_provider_api_key(cls.provider_id, api_id) or "").strip() if api_id else ""

    def _headers(self) -> Dict[str, str]:
        return {
            "api-key": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "RumiAI/1.0",
        }

    def _inventory_scope(self) -> str:
        material = f"{self.provider_id}\0{self._base_url}\0{self._api_version}".encode("utf-8")
        return hashlib.sha256(self._api_key.encode("utf-8") + b"\0" + material).hexdigest()

    def _request_json(
        self, method: str, url: str, body: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        if not self._api_key:
            raise RuntimeError(
                "azure-ai-foundry: save an API key for the selected project connection"
            )
        if not self._base_url:
            raise RuntimeError(
                "azure-ai-foundry: save the Foundry Project endpoint URL on the API connection"
            )
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Azure AI Foundry API error {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Azure AI Foundry connection error: {error.reason}") from error
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(
                f"Azure AI Foundry returned invalid JSON: {payload[:500]}"
            ) from error
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    def _project_url(self, path: str) -> str:
        separator = "&" if "?" in path else "?"
        return (
            self._base_url
            + path
            + separator
            + urllib.parse.urlencode({"api-version": self._api_version})
        )

    @staticmethod
    def _deployment_record(raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        deployment = str(
            raw.get("name") or raw.get("id") or raw.get("deploymentName") or ""
        ).strip()
        if not deployment:
            return None
        properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
        model = (
            raw.get("model") if isinstance(raw.get("model"), dict) else properties.get("model", {})
        )
        model = model if isinstance(model, dict) else {}
        model_name = str(model.get("name") or raw.get("modelName") or deployment).strip()
        capabilities = raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else {}
        modalities = raw.get("modalities") if isinstance(raw.get("modalities"), dict) else {}
        lowered = (
            f"{deployment} {model_name} {json.dumps(capabilities)} {json.dumps(modalities)}".lower()
        )
        model_type = (
            "embedding"
            if "embed" in lowered
            else "image_gen"
            if "image" in lowered and "generation" in lowered
            else "chat"
        )
        return {
            "id": f"azure-ai-foundry/{deployment}",
            "model_id": deployment,
            "provider_id": "azure-ai-foundry",
            "provider": "azure-ai-foundry",
            "name": deployment,
            "display_name": f"{deployment} ({model_name})"
            if model_name != deployment
            else deployment,
            "type": model_type,
            "capabilities": {
                "chat": model_type == "chat",
                "text_input": model_type in {"chat", "embedding"},
                "text_output": model_type == "chat",
                "streaming": model_type == "chat",
                "embeddings": model_type == "embedding",
                "image_generation": model_type == "image_gen",
            },
            "metadata": {
                "source": "azure_ai_foundry_project_deployments",
                "capability_source": "provider_reported",
                "capability_confidence": "provider_reported",
                "deployment": deployment,
                "underlying_model": model_name,
                "model_version": model.get("version"),
                "provisioning_state": properties.get("provisioningState") or raw.get("status"),
                "inference_endpoint": raw.get("endpoint") or properties.get("endpoint"),
            },
        }

    def list_models(self) -> List[Dict[str, Any]]:
        if not self._api_key or not self._base_url:
            return []
        scope = self._inventory_scope()
        now = time.monotonic()
        cached = self._MODEL_INVENTORY_CACHE.get(scope)
        if cached and cached[0] > now:
            return [dict(item) for item in cached[1]]
        url = self._project_url("/deployments")
        raw_deployments: List[Any] = []
        visited: set[str] = set()
        while url and url not in visited:
            visited.add(url)
            payload = self._request_json("GET", url)
            values = payload.get("value") or payload.get("data") or payload.get("deployments") or []
            if isinstance(values, list):
                raw_deployments.extend(values)
            next_url = str(payload.get("nextLink") or payload.get("next_link") or "").strip()
            # Foundry's nextLink is an absolute ARM-style URL. Do not follow an
            # arbitrary host supplied by a response.
            if (
                next_url
                and urllib.parse.urlsplit(next_url).netloc
                == urllib.parse.urlsplit(self._base_url).netloc
            ):
                url = next_url
            else:
                url = ""
        models = [
            item for raw in raw_deployments if (item := self._deployment_record(raw)) is not None
        ]
        if models:
            self._MODEL_INVENTORY_CACHE[scope] = (
                now + self._MODEL_INVENTORY_CACHE_TTL_SECONDS,
                [dict(item) for item in models],
            )
        return models

    def _inference_url(self, deployment: str, suffix: str) -> str:
        encoded = urllib.parse.quote(deployment, safe="")
        # Deployment discovery is scoped to ``/api/projects/<project>`` but
        # Foundry's OpenAI inference route lives at the AI-service resource
        # root.  Keeping those surfaces distinct avoids sending a valid
        # project-list request to an invalid inference URL.
        project_marker = "/api/projects/"
        resource_base_url = self._base_url.split(project_marker, 1)[0].rstrip("/")
        return (
            resource_base_url
            + f"/openai/deployments/{encoded}/{suffix}?"
            + urllib.parse.urlencode({"api-version": self._inference_api_version})
        )

    def complete(self, model, messages, tools, params):
        deployment = str(model or "").removeprefix("azure-ai-foundry/")
        body: Dict[str, Any] = {"messages": list(messages or [])}
        if tools:
            body["tools"] = list(tools)
        for key in ("stream", "max_tokens", "temperature", "top_p", "stop", "response_format"):
            if key in (params or {}):
                body[key] = params[key]
        payload = self._request_json(
            "POST", self._inference_url(deployment, "chat/completions"), body
        )
        choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        return {
            "content": [{"type": "text", "text": str(message.get("content") or "")}],
            "finish_reason": str(choice.get("finish_reason") or "stop"),
            "raw_extra": {"model": deployment},
        }

    def embed(self, model, input_text):
        deployment = str(model or "").removeprefix("azure-ai-foundry/")
        inputs = [input_text] if isinstance(input_text, str) else list(input_text or [])
        payload = self._request_json(
            "POST", self._inference_url(deployment, "embeddings"), {"input": inputs}
        )
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return {
            "embeddings": [
                item.get("embedding")
                for item in data
                if isinstance(item, dict) and isinstance(item.get("embedding"), list)
            ],
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
