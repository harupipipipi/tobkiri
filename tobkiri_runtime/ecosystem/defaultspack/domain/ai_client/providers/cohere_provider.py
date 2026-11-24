"""Cohere's native API adapter with an account-scoped live model inventory."""

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

from ..base_provider import BaseProvider
from ..api_key_store import read_provider_api_key


class CohereProvider(BaseProvider):
    """Use Cohere's Models and V2 Chat APIs without a checked-in model list."""

    provider_id = "cohere"
    BASE_URL = "https://api.cohere.com"
    _MODEL_INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
    _MODEL_INVENTORY_CACHE_TTL_SECONDS = 300

    def __init__(self, api_key: str | None = None):
        self._api_key = str(api_key or read_provider_api_key("cohere", "legacy") or "").strip()
        self._base_url = (
            str(os.environ.get("COHERE_BASE_URL", self.BASE_URL) or self.BASE_URL)
            .strip()
            .rstrip("/")
        )
        self._ssl_ctx = ssl.create_default_context()

    def _headers(self, content_type: str = "application/json") -> Dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self._api_key,
            "Accept": "application/json",
            "User-Agent": "RumiAI/1.0",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _inventory_scope(self) -> str:
        material = f"{self.provider_id}\0{self._base_url}".encode("utf-8")
        return hashlib.sha256(self._api_key.encode("utf-8") + b"\0" + material).hexdigest()

    def _request_json(
        self, method: str, path: str, body: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        if not self._api_key:
            raise RuntimeError("cohere: missing COHERE_API_KEY")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self._base_url + path,
            data=data,
            headers=self._headers("application/json" if body is not None else ""),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Cohere API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Cohere API connection error: {error.reason}") from error
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"Cohere API returned invalid JSON: {payload[:500]}") from error
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    def _fetch_models_page(self, page_token: str = "") -> Dict[str, Any]:
        query = {"page_size": "1000"}
        if page_token:
            query["page_token"] = page_token
        return self._request_json("GET", "/v1/models?" + urllib.parse.urlencode(query))

    @staticmethod
    def _model_record(raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        model_id = str(raw.get("name") or raw.get("id") or "").strip()
        if not model_id:
            return None
        endpoints = {
            str(item).strip().lower() for item in raw.get("endpoints", []) if str(item).strip()
        }
        features = {
            str(item).strip().lower() for item in raw.get("features", []) if str(item).strip()
        }
        is_embedding = bool({"embed", "embeddings"} & endpoints) or any(
            "embed" in item for item in features
        )
        is_rerank = "rerank" in endpoints or any("rerank" in item for item in features)
        is_chat = bool({"chat", "chat-completions"} & endpoints) or any(
            "chat" in item for item in features
        )
        model_type = "embedding" if is_embedding else ("rerank" if is_rerank else "chat")
        context = raw.get("context_length") or 0
        try:
            context = int(context)
        except (TypeError, ValueError):
            context = 0
        return {
            "id": f"cohere/{model_id}",
            "model_id": model_id,
            "provider_id": "cohere",
            "provider": "cohere",
            "name": model_id,
            "display_name": model_id,
            "type": model_type,
            "context_window": context,
            "max_context": context,
            "capabilities": {
                "chat": is_chat,
                "text_input": is_chat or is_embedding or is_rerank,
                "text_output": is_chat,
                "streaming": is_chat,
                "embeddings": is_embedding,
                "rerank": is_rerank,
                "thinking": False,
                "reasoning": False,
                "tool_calling": "tools" in features,
                "tool_calls": "tools" in features,
            },
            "metadata": {
                "source": "native_models_endpoint",
                "capability_source": "native_models_endpoint",
                "capability_confidence": "provider_reported",
                "deprecated": bool(raw.get("is_deprecated", False)),
                "endpoints": sorted(endpoints),
                "features": sorted(features),
            },
        }

    def list_models(self) -> List[Dict[str, Any]]:
        if not self._api_key:
            return []
        scope = self._inventory_scope()
        now = time.monotonic()
        cached = self._MODEL_INVENTORY_CACHE.get(scope)
        if cached and cached[0] > now:
            return [dict(item) for item in cached[1]]
        models: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        page_token = ""
        seen_tokens: set[str] = set()
        for _ in range(100):
            page = self._fetch_models_page(page_token)
            entries = page.get("models") if isinstance(page.get("models"), list) else []
            for raw in entries:
                model = self._model_record(raw)
                if model and model["model_id"] not in seen_ids:
                    seen_ids.add(model["model_id"])
                    models.append(model)
            next_token = str(page.get("next_page_token") or "").strip()
            if not next_token or next_token in seen_tokens:
                break
            seen_tokens.add(next_token)
            page_token = next_token
        if models:
            self._MODEL_INVENTORY_CACHE[scope] = (
                now + self._MODEL_INVENTORY_CACHE_TTL_SECONDS,
                [dict(item) for item in models],
            )
        return models

    @staticmethod
    def _message_content(value: Any) -> Any:
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            return str(value or "")
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)

    def _chat_messages(self, messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user").lower()
            if role not in {"user", "assistant", "system", "tool"}:
                role = "user"
            result.append(
                {"role": role, "content": self._message_content(message.get("content", ""))}
            )
        return result

    @staticmethod
    def _usage(raw: Dict[str, Any]) -> Dict[str, int]:
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        tokens = (
            usage.get("tokens")
            if isinstance(usage.get("tokens"), dict)
            else usage.get("billed_units", {})
        )
        tokens = tokens if isinstance(tokens, dict) else {}
        input_tokens = int(tokens.get("input_tokens") or 0)
        output_tokens = int(tokens.get("output_tokens") or 0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    def complete(self, model, messages, tools, params):
        body: Dict[str, Any] = {"model": model, "messages": self._chat_messages(messages)}
        for key in (
            "temperature",
            "max_tokens",
            "p",
            "k",
            "seed",
            "stop_sequences",
            "response_format",
        ):
            if key in (params or {}):
                body[key] = params[key]
        if tools:
            body["tools"] = tools
        raw = self._request_json("POST", "/v2/chat", body)
        message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        normalized_content = [dict(item) for item in content if isinstance(item, dict)]
        return {
            "content": normalized_content,
            "finish_reason": str(raw.get("finish_reason") or "complete").lower(),
            "usage": self._usage(raw),
            "raw_extra": {"id": raw.get("id", ""), "model": model},
        }

    def embed(self, model, input_text):
        values = [input_text] if isinstance(input_text, str) else list(input_text or [])
        inputs = [{"content": [{"type": "text", "text": str(value)}]} for value in values]
        raw = self._request_json(
            "POST",
            "/v2/embed",
            {
                "model": model,
                "inputs": inputs,
                "input_type": "search_document",
                "embedding_types": ["float"],
            },
        )
        embeddings = raw.get("embeddings") if isinstance(raw.get("embeddings"), dict) else {}
        vectors = embeddings.get("float") if isinstance(embeddings.get("float"), list) else []
        usage = self._usage(raw)
        return {
            "embeddings": vectors,
            "usage": {"input_tokens": usage["input_tokens"], "total_tokens": usage["total_tokens"]},
        }

    def stream(self, model, messages, tools, params):
        body: Dict[str, Any] = {
            "model": model,
            "messages": self._chat_messages(messages),
            "stream": True,
        }
        for key in (
            "temperature",
            "max_tokens",
            "p",
            "k",
            "seed",
            "stop_sequences",
            "response_format",
        ):
            if key in (params or {}):
                body[key] = params[key]
        if tools:
            body["tools"] = tools
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self._base_url + "/v2/chat", data=data, headers=self._headers(), method="POST"
        )
        try:
            response = urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120)
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Cohere API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Cohere API connection error: {error.reason}") from error
        event_name = ""
        try:
            for line in response:
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded.startswith("event:"):
                    event_name = decoded.split(":", 1)[1].strip()
                    continue
                if not decoded.startswith("data:"):
                    continue
                try:
                    payload = json.loads(decoded.split(":", 1)[1].strip())
                except (json.JSONDecodeError, ValueError):
                    continue
                if event_name == "content-delta":
                    text = (
                        ((payload.get("delta") or {}).get("message") or {}).get("content") or {}
                    ).get("text")
                    if text:
                        yield {
                            "type": "content_delta",
                            "delta": {"type": "text", "text": str(text)},
                        }
                elif event_name == "message-end":
                    delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
                    yield {
                        "type": "stream_end",
                        "finish_reason": str(delta.get("finish_reason") or "complete").lower(),
                        "usage": self._usage(delta),
                    }
                event_name = ""
        finally:
            response.close()
