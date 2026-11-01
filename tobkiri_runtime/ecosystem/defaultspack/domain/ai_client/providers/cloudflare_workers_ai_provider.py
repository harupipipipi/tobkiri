"""Cloudflare Workers AI account catalog and native text-generation adapter."""

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

from ..base_provider import BaseProvider
from ..api_key_store import read_provider_api_key


class CloudflareWorkersAIProvider(BaseProvider):
    provider_id = "cloudflare-workers-ai"
    BASE_URL = "https://api.cloudflare.com/client/v4"
    _MODEL_INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
    _MODEL_INVENTORY_CACHE_TTL_SECONDS = 300

    def __init__(self, api_key: str | None = None):
        self._api_key = str(api_key or read_provider_api_key("cloudflare-workers-ai", "legacy") or "").strip()
        self._base_url = (
            str(os.environ.get("CLOUDFLARE_WORKERS_AI_BASE_URL", self.BASE_URL) or self.BASE_URL)
            .strip()
            .rstrip("/")
        )
        self._account_id = str(os.environ.get("CLOUDFLARE_ACCOUNT_ID", "") or "").strip()
        self._ssl_ctx = ssl.create_default_context()

    def _account_base(self) -> str:
        marker = "/accounts/"
        if marker in self._base_url:
            prefix, account_path = self._base_url.split(marker, 1)
            account_id = account_path.split("/", 1)[0].strip()
            if account_id:
                return f"{prefix}{marker}{account_id}/ai"
        if not self._account_id:
            return ""
        return f"{self._base_url}/accounts/{urllib.parse.quote(self._account_id, safe='')}/ai"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": "Bearer " + self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "RumiAI/1.0",
        }

    def _inventory_scope(self) -> str:
        material = f"{self.provider_id}\0{self._account_base()}".encode("utf-8")
        return hashlib.sha256(self._api_key.encode("utf-8") + b"\0" + material).hexdigest()

    def _request_json(
        self, method: str, path: str, body: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        if not self._api_key:
            raise RuntimeError("cloudflare-workers-ai: missing CLOUDFLARE_API_TOKEN")
        account_base = self._account_base()
        if not account_base:
            raise RuntimeError(
                "cloudflare-workers-ai: configure CLOUDFLARE_ACCOUNT_ID or an account-scoped base URL"
            )
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            account_base + path, data=data, headers=self._headers(), method=method
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Cloudflare Workers AI API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Cloudflare Workers AI connection error: {error.reason}") from error
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(
                f"Cloudflare Workers AI returned invalid JSON: {payload[:500]}"
            ) from error
        if not isinstance(decoded, dict):
            return {"result": decoded}
        if decoded.get("success") is False:
            raise RuntimeError(
                f"Cloudflare Workers AI API error: {decoded.get('errors') or decoded.get('messages') or 'request failed'}"
            )
        return decoded

    @staticmethod
    def _task_name(raw: Dict[str, Any]) -> str:
        task = raw.get("task")
        if isinstance(task, dict):
            task = task.get("name") or task.get("id")
        return (
            str(task or raw.get("task_name") or raw.get("type") or "")
            .strip()
            .lower()
            .replace("_", "-")
        )

    @classmethod
    def _model_record(cls, raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        model_id = str(raw.get("id") or raw.get("name") or raw.get("model") or "").strip()
        if not model_id:
            return None
        task = cls._task_name(raw)
        task_types = {
            "text-generation": "chat",
            "text-embedding": "embedding",
            "text-to-image": "image_gen",
            "automatic-speech-recognition": "transcription",
            "text-to-speech": "tts",
            "image-to-text": "chat",
            "image-classification": "classification",
            "text-classification": "classification",
        }
        model_type = task_types.get(task, "unknown")
        is_chat = model_type == "chat"
        return {
            "id": f"cloudflare-workers-ai/{model_id}",
            "model_id": model_id,
            "provider_id": "cloudflare-workers-ai",
            "provider": "cloudflare-workers-ai",
            "name": str(raw.get("display_name") or raw.get("name") or model_id),
            "display_name": str(raw.get("display_name") or raw.get("name") or model_id),
            "type": model_type,
            "capabilities": {
                "chat": is_chat,
                "text_input": is_chat
                or model_type in {"embedding", "image_gen", "tts", "classification"},
                "text_output": is_chat or model_type == "transcription",
                "streaming": is_chat,
                "embeddings": model_type == "embedding",
                "image_generation": model_type == "image_gen",
                "transcription": model_type == "transcription",
                "tts": model_type == "tts",
            },
            "metadata": {
                "source": "native_models_endpoint",
                "capability_source": "native_models_endpoint",
                "capability_confidence": "provider_reported",
                "task": task,
                "description": raw.get("description"),
                "deprecated": bool(raw.get("deprecated") or raw.get("is_deprecated")),
            },
        }

    def _fetch_models_page(self, page: int) -> Dict[str, Any]:
        query = urllib.parse.urlencode({"format": "openrouter", "page": page, "per_page": 100})
        return self._request_json("GET", "/models/search?" + query)

    def list_models(self) -> List[Dict[str, Any]]:
        if not self._api_key or not self._account_base():
            return []
        scope = self._inventory_scope()
        now = time.monotonic()
        cached = self._MODEL_INVENTORY_CACHE.get(scope)
        if cached and cached[0] > now:
            return [dict(item) for item in cached[1]]
        models: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for page_number in range(1, 101):
            payload = self._fetch_models_page(page_number)
            result = payload.get("result")
            if isinstance(result, dict):
                entries = result.get("data") or result.get("models") or []
            else:
                entries = result or payload.get("data") or []
            if not isinstance(entries, list):
                break
            for raw in entries:
                model = self._model_record(raw)
                if model and model["model_id"] not in seen:
                    seen.add(model["model_id"])
                    models.append(model)
            result_info = (
                payload.get("result_info") if isinstance(payload.get("result_info"), dict) else {}
            )
            total_pages = int(result_info.get("total_pages") or 0)
            if not entries or (total_pages and page_number >= total_pages) or len(entries) < 100:
                break
        if models:
            self._MODEL_INVENTORY_CACHE[scope] = (
                now + self._MODEL_INVENTORY_CACHE_TTL_SECONDS,
                [dict(item) for item in models],
            )
        return models

    def complete(self, model, messages, tools, params):
        del tools
        model_id = str(model or "").removeprefix("cloudflare-workers-ai/")
        body: Dict[str, Any] = {"messages": list(messages or [])}
        for key in ("stream", "max_tokens", "temperature", "top_p"):
            if key in (params or {}):
                body[key] = params[key]
        raw = self._request_json("POST", "/run/" + urllib.parse.quote(model_id, safe="@/.-"), body)
        result = raw.get("result") if isinstance(raw.get("result"), dict) else raw
        choices = result.get("choices") if isinstance(result.get("choices"), list) else []
        if choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else {}
            text = str(message.get("content") or "") if isinstance(message, dict) else ""
        else:
            text = (
                str(result.get("response") or result.get("result") or "")
                if isinstance(result, dict)
                else str(result or "")
            )
        return {
            "content": [{"type": "text", "text": text}],
            "finish_reason": "stop",
            "raw_extra": {"model": model_id},
        }
