"""Azure OpenAI deployment discovery and data-plane task adapter."""

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


class AzureOpenAIProvider(BaseProvider):
    provider_id = "azure-openai"
    DEFAULT_API_VERSION = "2024-10-21"
    _MODEL_INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
    _MODEL_INVENTORY_CACHE_TTL_SECONDS = 300

    def __init__(self, api_key: str | None = None):
        self._api_key = str(api_key or read_provider_api_key(self.provider_id, "legacy") or "").strip()
        self._base_url = self._configured_base_url()
        self._api_version = str(self.DEFAULT_API_VERSION).strip()
        self._ssl_ctx = ssl.create_default_context()

    @classmethod
    def _configured_base_url(cls) -> str:
        for connection in provider_named_api_keys(cls.provider_id):
            base_url = str(connection.get("base_url") or "").strip()
            if connection.get("configured") and base_url:
                return base_url.rstrip("/")
        return ""

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
        self, method: str, path: str, body: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        if not self._api_key:
            raise RuntimeError("azure-openai: missing AZURE_OPENAI_API_KEY")
        if not self._base_url:
            raise RuntimeError(
                "azure-openai: configure AZURE_OPENAI_ENDPOINT or an endpoint URL on the saved API"
            )
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self._base_url + path, data=data, headers=self._headers(), method=method
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Azure OpenAI API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Azure OpenAI connection error: {error.reason}") from error
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"Azure OpenAI returned invalid JSON: {payload[:500]}") from error
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    def _api_path(self, suffix: str) -> str:
        separator = "&" if "?" in suffix else "?"
        return suffix + separator + urllib.parse.urlencode({"api-version": self._api_version})

    @classmethod
    def _deployment_record(cls, raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        deployment = str(raw.get("id") or raw.get("name") or raw.get("deployment") or "").strip()
        if not deployment:
            return None
        model = (
            raw.get("model")
            if isinstance(raw.get("model"), dict)
            else raw.get("properties", {}).get("model", {})
            if isinstance(raw.get("properties"), dict)
            else {}
        )
        model_name = str(model.get("name") or raw.get("model_name") or deployment).strip()
        lowered = f"{deployment} {model_name}".lower()
        model_type = "embedding" if "embed" in lowered else "chat"
        properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
        state = str(properties.get("provisioningState") or raw.get("status") or "").strip()
        return {
            "id": f"azure-openai/{deployment}",
            "model_id": deployment,
            "provider_id": "azure-openai",
            "provider": "azure-openai",
            "name": deployment,
            "display_name": f"{deployment} ({model_name})"
            if model_name != deployment
            else deployment,
            "type": model_type,
            "capabilities": {
                "chat": model_type == "chat",
                "text_input": True,
                "text_output": model_type == "chat",
                "streaming": model_type == "chat",
                "embeddings": model_type == "embedding",
            },
            "metadata": {
                "source": "native_models_endpoint",
                "capability_source": "native_models_endpoint",
                "capability_confidence": "provider_reported",
                "deployment": deployment,
                "underlying_model": model_name,
                "model_version": model.get("version"),
                "status": state,
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
        payload = self._request_json("GET", self._api_path("/openai/deployments"))
        entries = payload.get("data") or payload.get("value") or payload.get("deployments") or []
        models = (
            [item for raw in entries if (item := self._deployment_record(raw)) is not None]
            if isinstance(entries, list)
            else []
        )
        if models:
            self._MODEL_INVENTORY_CACHE[scope] = (
                now + self._MODEL_INVENTORY_CACHE_TTL_SECONDS,
                [dict(item) for item in models],
            )
        return models

    def complete(self, model, messages, tools, params):
        deployment = str(model or "").removeprefix("azure-openai/")
        body: Dict[str, Any] = {"messages": list(messages or [])}
        if tools:
            body["tools"] = list(tools)
        for key in ("stream", "max_tokens", "temperature", "top_p", "stop", "response_format"):
            if key in (params or {}):
                body[key] = params[key]
        payload = self._request_json(
            "POST",
            self._api_path(
                "/openai/deployments/"
                + urllib.parse.quote(deployment, safe="")
                + "/chat/completions"
            ),
            body,
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
        deployment = str(model or "").removeprefix("azure-openai/")
        inputs = [input_text] if isinstance(input_text, str) else list(input_text or [])
        payload = self._request_json(
            "POST",
            self._api_path(
                "/openai/deployments/" + urllib.parse.quote(deployment, safe="") + "/embeddings"
            ),
            {"input": inputs},
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
