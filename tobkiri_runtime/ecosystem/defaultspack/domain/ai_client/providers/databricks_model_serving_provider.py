"""Databricks Model Serving live endpoint inventory and invocation adapter."""

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

from ..api_key_store import provider_named_api_keys
from ..base_provider import BaseProvider
from ..api_key_store import read_provider_api_key


class DatabricksModelServingProvider(BaseProvider):
    """Use the workspace's Serving Endpoints API as the model source of truth."""

    provider_id = "databricks-model-serving"
    _MODEL_INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
    _MODEL_INVENTORY_CACHE_TTL_SECONDS = 300

    def __init__(self, api_key: str | None = None):
        self._api_key = str(api_key or read_provider_api_key("databricks-model-serving", "legacy") or "").strip()
        self._base_url = self._configured_base_url()
        self._ssl_ctx = ssl.create_default_context()

    @classmethod
    def _configured_base_url(cls) -> str:
        configured = str(
            os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_BASE_URL") or ""
        ).strip()
        if configured:
            return configured.rstrip("/")
        # Named API connections retain their workspace URL as protected
        # metadata. This mirrors the key loader's first configured connection
        # selection without exposing credential material.
        for connection in provider_named_api_keys(cls.provider_id):
            base_url = str(connection.get("base_url") or "").strip()
            if connection.get("configured") and base_url:
                return base_url.rstrip("/")
        return ""

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": "Bearer " + self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "RumiAI/1.0",
        }

    def _inventory_scope(self) -> str:
        material = f"{self.provider_id}\0{self._base_url}".encode("utf-8")
        return hashlib.sha256(self._api_key.encode("utf-8") + b"\0" + material).hexdigest()

    def _request_json(
        self, method: str, path: str, body: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        if not self._api_key:
            raise RuntimeError("databricks-model-serving: missing DATABRICKS_TOKEN")
        if not self._base_url:
            raise RuntimeError(
                "databricks-model-serving: configure DATABRICKS_HOST or a workspace URL on the saved API"
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
                f"Databricks Model Serving API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(
                f"Databricks Model Serving connection error: {error.reason}"
            ) from error
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(
                f"Databricks Model Serving returned invalid JSON: {payload[:500]}"
            ) from error
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    @staticmethod
    def _endpoint_record(raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        model_id = str(raw.get("name") or raw.get("id") or "").strip()
        if not model_id:
            return None
        config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
        served_entities = (
            config.get("served_entities") if isinstance(config.get("served_entities"), list) else []
        )
        served_models = (
            config.get("served_models") if isinstance(config.get("served_models"), list) else []
        )
        entity_names = [
            str(item.get("name") or item.get("entity_name") or "").strip()
            for item in served_entities + served_models
            if isinstance(item, dict)
            and str(item.get("name") or item.get("entity_name") or "").strip()
        ]
        task = str(raw.get("task") or "").strip().lower()
        lowered = " ".join([model_id, task, *entity_names]).lower()
        model_type = "embedding" if "embed" in lowered else "chat"
        state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
        ready = str(state.get("ready") or "").upper() == "READY"
        return {
            "id": f"databricks-model-serving/{model_id}",
            "model_id": model_id,
            "provider_id": "databricks-model-serving",
            "provider": "databricks-model-serving",
            "name": str(raw.get("display_name") or model_id),
            "display_name": str(raw.get("display_name") or model_id),
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
                "ready": ready,
                "task": task,
                "served_entities": entity_names,
                "endpoint_id": raw.get("id"),
                "endpoint_type": raw.get("endpoint_type"),
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
        payload = self._request_json("GET", "/api/2.0/serving-endpoints")
        raw_endpoints = (
            payload.get("endpoints") if isinstance(payload.get("endpoints"), list) else []
        )
        models = [
            item
            for endpoint in raw_endpoints
            if (item := self._endpoint_record(endpoint)) is not None
        ]
        if models:
            self._MODEL_INVENTORY_CACHE[scope] = (
                now + self._MODEL_INVENTORY_CACHE_TTL_SECONDS,
                [dict(item) for item in models],
            )
        return models

    def complete(self, model, messages, tools, params):
        model_id = str(model or "").removeprefix("databricks-model-serving/")
        body: Dict[str, Any] = {"messages": list(messages or [])}
        if tools:
            body["tools"] = list(tools)
        for key in ("stream", "max_tokens", "temperature", "top_p", "stop", "response_format"):
            if key in (params or {}):
                body[key] = params[key]
        payload = self._request_json(
            "POST",
            "/serving-endpoints/" + urllib.parse.quote(model_id, safe="") + "/invocations",
            body,
        )
        choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        text = str(message.get("content") or payload.get("predictions") or "")
        return {
            "content": [{"type": "text", "text": text}],
            "finish_reason": str(choice.get("finish_reason") or "stop"),
            "raw_extra": {"model": model_id},
        }
