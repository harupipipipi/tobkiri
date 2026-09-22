"""Replicate's live model registry and schema-driven prediction adapter."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List

from ..base_provider import BaseProvider
from ..api_key_store import read_provider_api_key


class ReplicateProvider(BaseProvider):
    """Discover every paginated model record and run its current version.

    Replicate models have distinct input schemas.  The adapter consequently
    keeps the input supplied by the live registry/default example and maps a
    normal text prompt only to common text fields.  Callers may pass an exact
    ``replicate_input`` mapping through ``extra_body`` for any other schema.
    """

    provider_id = "replicate"
    BASE_URL = "https://api.replicate.com/v1"
    _INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]], Dict[str, Dict[str, Any]]]] = {}
    _INVENTORY_CACHE_TTL_SECONDS = 300

    def __init__(self, api_key: str | None = None):
        self._api_key = str(api_key or read_provider_api_key("replicate", "legacy") or "").strip()
        self._base_url = (
            str(os.environ.get("REPLICATE_BASE_URL", self.BASE_URL) or self.BASE_URL)
            .strip()
            .rstrip("/")
        )
        self._ssl_ctx = ssl.create_default_context()
        self._model_records: Dict[str, Dict[str, Any]] = {}

    def _headers(self, *, wait: bool = False) -> Dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "RumiAI/1.0",
        }
        if wait:
            headers["Prefer"] = "wait=60"
        return headers

    def _scope(self) -> str:
        return hashlib.sha256(f"{self._api_key}\0{self._base_url}".encode("utf-8")).hexdigest()

    def _request_json(
        self,
        method: str,
        url_or_path: str,
        body: Dict[str, Any] | None = None,
        *,
        wait: bool = False,
    ) -> Dict[str, Any]:
        if not self._api_key:
            raise RuntimeError("replicate: missing REPLICATE_API_TOKEN")
        url = (
            url_or_path
            if str(url_or_path).startswith(("https://", "http://"))
            else self._base_url + "/" + str(url_or_path).lstrip("/")
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=self._headers(wait=wait),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Replicate API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Replicate API connection error: {error.reason}") from error
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"Replicate API returned invalid JSON: {payload[:500]}") from error
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    @staticmethod
    def _model_id(raw: Dict[str, Any]) -> str:
        owner = str(raw.get("owner") or "").strip()
        name = str(raw.get("name") or "").strip()
        return f"{owner}/{name}" if owner and name else ""

    @staticmethod
    def _task_type(raw: Dict[str, Any]) -> str:
        schema = (
            ((raw.get("latest_version") or {}).get("openapi_schema") or {})
            if isinstance(raw.get("latest_version"), dict)
            else {}
        )
        serialized = (
            json.dumps(schema, ensure_ascii=False).lower() if isinstance(schema, dict) else ""
        )
        if "image" in serialized and "prompt" in serialized:
            return "image_gen"
        if "audio" in serialized and "text" in serialized:
            return "tts"
        return "chat"

    @classmethod
    def _model_record(cls, raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        model_id = cls._model_id(raw)
        if not model_id:
            return None
        latest = raw.get("latest_version") if isinstance(raw.get("latest_version"), dict) else {}
        example = raw.get("default_example") if isinstance(raw.get("default_example"), dict) else {}
        default_input = example.get("input") if isinstance(example.get("input"), dict) else {}
        version_id = str(latest.get("id") or "").strip()
        model_type = cls._task_type(raw)
        return {
            "id": f"replicate/{model_id}",
            "model_id": model_id,
            "provider_id": "replicate",
            "provider": "replicate",
            "name": model_id,
            "display_name": model_id,
            "type": model_type,
            "capabilities": {
                "chat": model_type == "chat",
                "text_input": model_type == "chat",
                "text_output": model_type == "chat",
                "image_generation": model_type == "image_gen",
                "tts": model_type == "tts",
            },
            "metadata": {
                "source": "native_models_endpoint",
                "capability_source": "live_model_schema",
                "capability_confidence": "provider_reported",
                "version_id": version_id,
                "default_input": default_input,
                "visibility": raw.get("visibility"),
                "description": raw.get("description"),
                "schema": latest.get("openapi_schema")
                if isinstance(latest.get("openapi_schema"), dict)
                else {},
            },
        }

    def list_models(self) -> List[Dict[str, Any]]:
        if not self._api_key:
            return []
        scope = self._scope()
        now = time.monotonic()
        cached = self._INVENTORY_CACHE.get(scope)
        if cached and cached[0] > now:
            self._model_records = {key: dict(value) for key, value in cached[2].items()}
            return [dict(item) for item in cached[1]]
        models: List[Dict[str, Any]] = []
        records: Dict[str, Dict[str, Any]] = {}
        next_url = "models"
        seen_urls: set[str] = set()
        while next_url and next_url not in seen_urls:
            seen_urls.add(next_url)
            page = self._request_json("GET", next_url)
            for raw in page.get("results") if isinstance(page.get("results"), list) else []:
                model = self._model_record(raw)
                if model and model["model_id"] not in records:
                    records[model["model_id"]] = dict(raw)
                    models.append(model)
            next_url = str(page.get("next") or "").strip()
        self._model_records = {key: dict(value) for key, value in records.items()}
        if models:
            self._INVENTORY_CACHE[scope] = (
                now + self._INVENTORY_CACHE_TTL_SECONDS,
                [dict(item) for item in models],
                {key: dict(value) for key, value in records.items()},
            )
        return models

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
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
        return ""

    def _input_for(
        self, model: str, messages: Iterable[Dict[str, Any]], params: Dict[str, Any]
    ) -> Dict[str, Any]:
        extra = params.get("extra_body") if isinstance(params.get("extra_body"), dict) else {}
        explicit = (
            extra.get("replicate_input") if isinstance(extra.get("replicate_input"), dict) else {}
        )
        if explicit:
            return dict(explicit)
        raw = self._model_records.get(model)
        if raw is None:
            self.list_models()
            raw = self._model_records.get(model, {})
        example = (
            raw.get("default_example")
            if isinstance(raw, dict) and isinstance(raw.get("default_example"), dict)
            else {}
        )
        input_data = (
            dict(example.get("input") or {}) if isinstance(example.get("input"), dict) else {}
        )
        prompt = self._prompt(messages)
        for key in ("prompt", "text", "input", "query", "message"):
            if key in input_data:
                input_data[key] = prompt
                return input_data
        input_data["prompt"] = prompt
        return input_data

    def _version_ref(self, model: str) -> str:
        raw = self._model_records.get(model, {})
        latest = raw.get("latest_version") if isinstance(raw.get("latest_version"), dict) else {}
        version = str(latest.get("id") or "").strip()
        if not version:
            return model
        return f"{model}:{version}"

    @staticmethod
    def _text_output(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(ReplicateProvider._text_output(item) for item in value)
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False)

    def _run_prediction(self, model_id: str, input_data: Dict[str, Any]) -> Dict[str, Any]:
        prediction = self._request_json(
            "POST",
            "predictions",
            {"version": self._version_ref(model_id), "input": input_data},
            wait=True,
        )
        status = str(prediction.get("status") or "").lower()
        if status in {"starting", "processing"}:
            poll_url = (
                ((prediction.get("urls") or {}).get("get"))
                if isinstance(prediction.get("urls"), dict)
                else ""
            )
            for _ in range(60):
                time.sleep(1)
                prediction = self._request_json("GET", str(poll_url)) if poll_url else prediction
                status = str(prediction.get("status") or "").lower()
                if status not in {"starting", "processing"}:
                    break
        if status != "succeeded":
            raise RuntimeError(
                f"Replicate prediction did not succeed: {prediction.get('error') or status or 'unknown status'}"
            )
        return prediction

    def complete(self, model, messages, tools, params):
        del tools
        model_id = str(model or "").removeprefix("replicate/")
        input_data = self._input_for(model_id, messages, dict(params or {}))
        prediction = self._run_prediction(model_id, input_data)
        text = self._text_output(prediction.get("output"))
        return {
            "content": [{"type": "text", "text": text}],
            "finish_reason": "stop",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "raw_extra": {"id": prediction.get("id", ""), "model": model_id},
        }

    def image_gen(self, model, prompt, params):
        model_id = str(model or "").removeprefix("replicate/")
        input_data = self._input_for(
            model_id,
            [{"role": "user", "content": str(prompt or "")}],
            dict(params or {}),
        )
        prediction = self._run_prediction(model_id, input_data)
        output = prediction.get("output")
        if isinstance(output, list):
            images = [str(item) for item in output if isinstance(item, str)]
        elif isinstance(output, str):
            images = [output]
        else:
            images = []
        return {"images": images, "raw_extra": {"id": prediction.get("id", ""), "model": model_id}}

    def stream(self, model, messages, tools, params):
        response = self.complete(model, messages, tools, params)
        for part in response.get("content", []):
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                yield {
                    "type": "content_delta",
                    "delta": {"type": "text", "text": str(part["text"])},
                }
        yield {
            "type": "stream_end",
            "finish_reason": response["finish_reason"],
            "usage": response["usage"],
        }
