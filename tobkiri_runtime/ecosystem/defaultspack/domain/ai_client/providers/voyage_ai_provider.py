"""Voyage AI embedding catalog from its official current-model page."""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List
from ..base_provider import BaseProvider
from ..api_key_store import read_provider_api_key


class VoyageAIProvider(BaseProvider):
    provider_id = "voyage-ai"
    BASE_URL = "https://api.voyageai.com/v1"
    CATALOG_URL = "https://docs.voyageai.com/docs/embeddings"
    _CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}

    def __init__(self, api_key: str | None = None):
        self._key = str(api_key or read_provider_api_key("voyage", "legacy") or "").strip()
        self._base_url = str(os.environ.get("VOYAGE_BASE_URL") or self.BASE_URL).strip().rstrip("/")
        self._ssl_ctx = ssl.create_default_context()

    def _request(self, method, path, body=None):
        if not self._key:
            raise RuntimeError("voyage-ai: save VOYAGE_API_KEY")
        req = urllib.request.Request(
            self._base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self._key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=120) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Voyage API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Voyage API connection error: {error.reason}") from error
        return payload if isinstance(payload, dict) else {"data": payload}

    def list_models(self) -> List[Dict[str, Any]]:
        if not self._key:
            return []
        cached = self._CACHE.get(self._key)
        if cached and cached[0] > time.monotonic():
            return [dict(item) for item in cached[1]]
        req = urllib.request.Request(
            self.CATALOG_URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
        )
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=20) as response:
                document = response.read().decode("utf-8")
        except (urllib.error.HTTPError, urllib.error.URLError):
            return []
        section = document.split("Model Choices", 1)[-1].split("Need help deciding", 1)[0]
        ids = []
        for model_id in re.findall(
            r"<code[^>]*>(voyage-[A-Za-z0-9.]+(?:-[A-Za-z0-9.]+)*)</code>", section
        ):
            if model_id not in ids:
                ids.append(model_id)
        models = [
            {
                "id": f"voyage-ai/{model_id}",
                "model_id": model_id,
                "provider_id": self.provider_id,
                "provider": self.provider_id,
                "name": model_id,
                "display_name": model_id,
                "type": "embedding",
                "capabilities": {"embeddings": True, "text_input": True},
                "metadata": {
                    "source": "voyage_official_model_document",
                    "source_endpoint": self.CATALOG_URL,
                    "capability_confidence": "official_document",
                },
            }
            for model_id in ids
        ]
        if models:
            self._CACHE[self._key] = (time.monotonic() + 86400, [dict(item) for item in models])
        return models

    def embed(self, model, input_text):
        model_id = str(model or "").removeprefix("voyage-ai/")
        payload = self._request(
            "POST", "/embeddings", {"input": [str(input_text or "")], "model": model_id}
        )
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        return list(data[0].get("embedding") or []) if data and isinstance(data[0], dict) else []
