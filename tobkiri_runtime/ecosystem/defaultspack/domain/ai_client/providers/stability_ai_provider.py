"""Stability AI account-scoped engine inventory and legacy image adapter."""

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


class StabilityAIProvider(BaseProvider):
    provider_id = "stability-ai"
    DEFAULT_BASE_URL = "https://api.stability.ai"
    _MODEL_INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}

    def __init__(self):
        self._connection = self._configured_connection()
        self._api_key = self._configured_api_key(self._connection)
        self._base_url = (
            str(
                self._connection.get("base_url") or self.DEFAULT_BASE_URL
            )
            .strip()
            .rstrip("/")
        )
        self._ssl_ctx = ssl.create_default_context()

    @classmethod
    def _configured_connection(cls) -> Dict[str, Any]:
        for connection in provider_named_api_keys(cls.provider_id):
            if connection.get("configured"):
                return dict(connection)
        return {}

    @classmethod
    def _configured_api_key(cls, connection: Dict[str, Any]) -> str:
        api_id = str(connection.get("api_id") or "").strip()
        return str(read_provider_api_key(cls.provider_id, api_id) or "").strip() if api_id else ""

    def _scope(self) -> str:
        return hashlib.sha256((self._api_key + "\0" + self._base_url).encode("utf-8")).hexdigest()

    def _request_json(
        self, method: str, path: str, body: Dict[str, Any] | None = None
    ) -> Dict[str, Any] | List[Any]:
        if not self._api_key:
            raise RuntimeError("stability-ai: save a Stability API key")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self._base_url + path,
            data=data,
            headers={
                "Authorization": "Bearer " + self._api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "RumiAI/1.0",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Stability AI API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Stability AI connection error: {error.reason}") from error
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"Stability AI returned invalid JSON: {raw[:500]}") from error

    @staticmethod
    def _engine_record(raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        engine_id = str(raw.get("id") or "").strip()
        if not engine_id:
            return None
        engine_type = str(raw.get("type") or "").upper()
        is_image = engine_type in {"PICTURE", "IMAGE"} or "diffusion" in engine_id.lower()
        return {
            "id": f"stability-ai/{engine_id}",
            "model_id": engine_id,
            "provider_id": "stability-ai",
            "provider": "stability-ai",
            "name": str(raw.get("name") or engine_id),
            "display_name": str(raw.get("name") or engine_id),
            "type": "image_gen" if is_image else "chat",
            "capabilities": {"image_generation": is_image, "text_input": is_image},
            "metadata": {
                "source": "stability_ai_engines_api",
                "capability_source": "provider_reported",
                "capability_confidence": "provider_reported",
                "engine_type": engine_type,
                "description": raw.get("description"),
            },
        }

    def list_models(self) -> List[Dict[str, Any]]:
        if not self._api_key:
            return []
        scope = self._scope()
        now = time.monotonic()
        cached = self._MODEL_INVENTORY_CACHE.get(scope)
        if cached and cached[0] > now:
            return [dict(item) for item in cached[1]]
        payload = self._request_json("GET", "/v1/engines/list")
        engines = (
            payload
            if isinstance(payload, list)
            else payload.get("engines", [])
            if isinstance(payload, dict)
            else []
        )
        models = [item for raw in engines if (item := self._engine_record(raw)) is not None]
        if models:
            self._MODEL_INVENTORY_CACHE[scope] = (now + 300, [dict(item) for item in models])
        return models

    def image_gen(self, model, prompt, params):
        engine_id = str(model or "").removeprefix("stability-ai/")
        body: Dict[str, Any] = {"text_prompts": [{"text": str(prompt or ""), "weight": 1}]}
        for key in (
            "cfg_scale",
            "height",
            "width",
            "sampler",
            "samples",
            "seed",
            "steps",
            "style_preset",
        ):
            if key in (params or {}):
                body[key] = params[key]
        payload = self._request_json(
            "POST",
            "/v1/generation/" + urllib.parse.quote(engine_id, safe="-_.~") + "/text-to-image",
            body,
        )
        artifacts = (
            payload.get("artifacts")
            if isinstance(payload, dict) and isinstance(payload.get("artifacts"), list)
            else []
        )
        images = []
        for artifact in artifacts:
            if isinstance(artifact, list):
                images.extend(artifact)
            else:
                images.append(artifact)
        data_urls = [
            "data:image/png;base64," + str(item.get("base64"))
            for item in images
            if isinstance(item, dict) and str(item.get("base64") or "")
        ]
        return {"images": data_urls, "raw_extra": {"model": engine_id}}
