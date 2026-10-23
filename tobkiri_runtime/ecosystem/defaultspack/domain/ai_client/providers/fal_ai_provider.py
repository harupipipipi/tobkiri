"""fal.ai's live model registry and queue-backed universal invocation."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List

from ..api_key_store import provider_named_api_keys, read_provider_api_key
from ..base_provider import BaseProvider


class FalAIProvider(BaseProvider):
    """Discover and invoke every endpoint made available by fal.ai.

    fal's model search endpoint is paginated and the queue protocol works for
    every Model API endpoint (including custom Serverless deployments).  A
    caller can pass an exact endpoint schema payload as ``extra_body.fal_input``;
    the simple prompt mapping is only a convenience for prompt-shaped models.
    """

    provider_id = "fal-ai"
    MODELS_BASE_URL = "https://api.fal.ai/v1"
    QUEUE_BASE_URL = "https://queue.fal.run"
    _INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
    _CACHE_TTL_SECONDS = 300

    def __init__(self) -> None:
        self._connection = self._configured_connection()
        self._api_key = self._configured_api_key(self._connection)
        self._models_base_url = (
            str(
                self._connection.get("base_url") or self.MODELS_BASE_URL
            )
            .strip()
            .rstrip("/")
        )
        self._queue_base_url = (
            str(self.QUEUE_BASE_URL).strip().rstrip("/")
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
        return hashlib.sha256(
            (self._api_key + "\0" + self._models_base_url + "\0" + self._queue_base_url).encode(
                "utf-8"
            )
        ).hexdigest()

    def _headers(self) -> Dict[str, str]:
        if not self._api_key:
            raise RuntimeError("fal-ai: save a fal API key")
        return {
            "Authorization": "Key " + self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "RumiAI/1.0",
        }

    def _request_json(
        self, method: str, url: str, body: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=self._headers(),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"fal.ai API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"fal.ai connection error: {error.reason}") from error
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"fal.ai returned invalid JSON: {raw[:500]}") from error
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    @staticmethod
    def _model_type(raw: Dict[str, Any]) -> str:
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        category = (
            str(metadata.get("category") or raw.get("category") or "").lower().replace("_", "-")
        )
        if "image" in category:
            return "image_gen"
        if "video" in category:
            return "video_gen"
        if any(
            token in category for token in ("speech", "text-to-speech", "audio-generation", "music")
        ):
            return "tts"
        if any(token in category for token in ("transcri", "speech-to-text")):
            return "transcription"
        return "chat"

    @classmethod
    def _model_record(cls, raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        model_id = str(raw.get("endpoint_id") or raw.get("id") or "").strip()
        if not model_id:
            return None
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        model_type = cls._model_type(raw)
        return {
            "id": f"fal-ai/{model_id}",
            "model_id": model_id,
            "provider_id": cls.provider_id,
            "provider": cls.provider_id,
            "name": str(metadata.get("display_name") or raw.get("name") or model_id),
            "display_name": str(metadata.get("display_name") or raw.get("name") or model_id),
            "type": model_type,
            "capabilities": {
                "chat": model_type == "chat",
                "text_input": model_type in {"chat", "image_gen", "video_gen", "tts"},
                "text_output": model_type == "chat",
                "image_generation": model_type == "image_gen",
                "video_generation": model_type == "video_gen",
                "tts": model_type == "tts",
                "transcription": model_type == "transcription",
            },
            "metadata": {
                "source": "fal_models_api",
                "capability_source": "fal_models_api",
                "capability_confidence": "provider_reported",
                "category": metadata.get("category") or raw.get("category"),
                "status": metadata.get("status") or raw.get("status"),
                "description": metadata.get("description") or raw.get("description"),
                "model_url": metadata.get("model_url") or raw.get("model_url"),
                "tags": list(metadata.get("tags") or [])
                if isinstance(metadata.get("tags"), list)
                else [],
                "input_schema": "pass exact input as extra_body.fal_input",
            },
        }

    def list_models(self) -> List[Dict[str, Any]]:
        if not self._api_key:
            return []
        scope = self._scope()
        now = time.monotonic()
        cached = self._INVENTORY_CACHE.get(scope)
        if cached and cached[0] > now:
            return [dict(item) for item in cached[1]]
        records: List[Dict[str, Any]] = []
        seen: set[str] = set()
        cursor = ""
        for _ in range(1000):
            query = urllib.parse.urlencode({"cursor": cursor}) if cursor else ""
            payload = self._request_json(
                "GET", self._models_base_url + "/models" + ("?" + query if query else "")
            )
            raw_models = payload.get("models") if isinstance(payload.get("models"), list) else []
            for raw in raw_models:
                model = self._model_record(raw)
                if model and model["model_id"] not in seen:
                    seen.add(model["model_id"])
                    records.append(model)
            next_cursor = str(payload.get("next_cursor") or "").strip()
            if not next_cursor:
                break
            cursor = next_cursor
        if records:
            self._INVENTORY_CACHE[scope] = (
                now + self._CACHE_TTL_SECONDS,
                [dict(item) for item in records],
            )
        return records

    @staticmethod
    def _prompt(messages: Iterable[Dict[str, Any]]) -> str:
        for message in reversed(list(messages or [])):
            if not isinstance(message, dict) or str(message.get("role") or "") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
        return ""

    def _input_for(
        self, messages: Iterable[Dict[str, Any]], params: Dict[str, Any]
    ) -> Dict[str, Any]:
        extra = params.get("extra_body") if isinstance(params.get("extra_body"), dict) else {}
        explicit = extra.get("fal_input") if isinstance(extra.get("fal_input"), dict) else {}
        return dict(explicit) if explicit else {"prompt": self._prompt(messages)}

    @staticmethod
    def _safe_queue_url(value: Any) -> str:
        url = str(value or "").strip()
        parsed = urllib.parse.urlsplit(url)
        return url if parsed.scheme == "https" and parsed.netloc.endswith("fal.run") else ""

    def _run(self, model_id: str, input_data: Dict[str, Any]) -> Dict[str, Any]:
        endpoint = urllib.parse.quote(model_id.strip("/"), safe="/-_.~")
        submitted = self._request_json("POST", self._queue_base_url + "/" + endpoint, input_data)
        response_url = self._safe_queue_url(submitted.get("response_url"))
        status_url = self._safe_queue_url(submitted.get("status_url"))
        if not response_url:
            # A direct result is also valid for a synchronous or mocked queue.
            return submitted
        for _ in range(120):
            status = (
                self._request_json("GET", status_url) if status_url else {"status": "COMPLETED"}
            )
            state = str(status.get("status") or "").upper()
            if state == "COMPLETED":
                if status.get("error"):
                    raise RuntimeError(f"fal.ai request failed: {status['error']}")
                return self._request_json("GET", response_url)
            if state in {"FAILED", "CANCELLED"}:
                raise RuntimeError(f"fal.ai request failed: {status.get('error') or state}")
            time.sleep(0.5)
        raise RuntimeError("fal.ai request timed out while waiting for the queue")

    @staticmethod
    def _urls(value: Any) -> List[str]:
        if isinstance(value, str):
            return [value] if value.startswith(("https://", "http://", "data:")) else []
        if isinstance(value, list):
            return [url for item in value for url in FalAIProvider._urls(item)]
        if isinstance(value, dict):
            return [url for item in value.values() for url in FalAIProvider._urls(item)]
        return []

    def complete(self, model, messages, tools, params):
        del tools
        model_id = str(model or "").removeprefix("fal-ai/")
        result = self._run(model_id, self._input_for(messages, dict(params or {})))
        text = str(result.get("text") or result.get("output") or result.get("message") or "")
        if not text:
            text = json.dumps(result, ensure_ascii=False)
        return {
            "content": [{"type": "text", "text": text}],
            "finish_reason": "stop",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "raw_extra": {"model": model_id},
        }

    def image_gen(self, model, prompt, params):
        model_id = str(model or "").removeprefix("fal-ai/")
        result = self._run(
            model_id,
            self._input_for([{"role": "user", "content": str(prompt or "")}], dict(params or {})),
        )
        return {
            "images": self._urls(result.get("images") or result.get("image") or result),
            "raw_extra": {"model": model_id},
        }

    def stream(self, model, messages, tools, params):
        response = self.complete(model, messages, tools, params)
        for part in response["content"]:
            yield {"type": "content_delta", "delta": part}
        yield {
            "type": "stream_end",
            "finish_reason": response["finish_reason"],
            "usage": response["usage"],
        }
