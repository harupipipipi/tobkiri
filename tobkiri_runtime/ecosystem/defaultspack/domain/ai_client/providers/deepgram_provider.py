"""Deepgram live STT/TTS model inventory and native task adapter."""

from __future__ import annotations

import base64
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


class DeepgramProvider(BaseProvider):
    provider_id = "deepgram"
    BASE_URL = "https://api.deepgram.com"
    _MODEL_INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
    _MODEL_INVENTORY_CACHE_TTL_SECONDS = 300

    def __init__(self, api_key: str | None = None):
        self._api_key = str(api_key or read_provider_api_key("deepgram", "legacy") or "").strip()
        self._base_url = (
            str(os.environ.get("DEEPGRAM_BASE_URL", self.BASE_URL) or self.BASE_URL)
            .strip()
            .rstrip("/")
        )
        self._ssl_ctx = ssl.create_default_context()

    def _headers(
        self, content_type: str = "application/json", accept: str = "application/json"
    ) -> Dict[str, str]:
        headers = {
            "Authorization": "Token " + self._api_key,
            "Accept": accept,
            "User-Agent": "RumiAI/1.0",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _inventory_scope(self) -> str:
        material = f"{self.provider_id}\0{self._base_url}".encode("utf-8")
        return hashlib.sha256(self._api_key.encode("utf-8") + b"\0" + material).hexdigest()

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        content_type: str = "application/json",
        accept: str = "application/json",
    ) -> bytes:
        if not self._api_key:
            raise RuntimeError("deepgram: missing DEEPGRAM_API_KEY")
        data = (
            json.dumps(body).encode("utf-8")
            if content_type == "application/json" and body is not None
            else body
        )
        request = urllib.request.Request(
            self._base_url + path,
            data=data,
            headers=self._headers(content_type, accept),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Deepgram API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Deepgram API connection error: {error.reason}") from error

    def _request_json(self, method: str, path: str, body: Any = None) -> Dict[str, Any]:
        payload = self._request(method, path, body)
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"Deepgram API returned invalid JSON: {payload[:500]!r}") from error
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    @staticmethod
    def _model_record(raw: Any, task: str) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        model_id = str(raw.get("canonical_name") or raw.get("name") or raw.get("id") or "").strip()
        if not model_id:
            return None
        model_type = "transcription" if task == "stt" else "tts"
        return {
            "id": f"deepgram/{model_id}",
            "model_id": model_id,
            "provider_id": "deepgram",
            "provider": "deepgram",
            "name": model_id,
            "display_name": model_id,
            "type": model_type,
            "capabilities": {
                "transcription": task == "stt",
                "tts": task == "tts",
                "audio_input": task == "stt",
                "text_output": task == "stt",
                "text_input": task == "tts",
                "streaming": bool(raw.get("streaming", False)) if task == "stt" else False,
            },
            "metadata": {
                "source": "native_models_endpoint",
                "capability_source": "native_models_endpoint",
                "capability_confidence": "provider_reported",
                "task": task,
                "architecture": raw.get("architecture"),
                "version": raw.get("version"),
                "languages": list(raw.get("languages") or [])
                if isinstance(raw.get("languages"), list)
                else [],
                "batch": bool(raw.get("batch", False)),
                "streaming": bool(raw.get("streaming", False)),
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
        payload = self._request_json("GET", "/v1/models?include_outdated=true")
        models: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for task in ("stt", "tts"):
            for raw in payload.get(task, []) if isinstance(payload.get(task), list) else []:
                model = self._model_record(raw, task)
                if model and model["model_id"] not in seen:
                    seen.add(model["model_id"])
                    models.append(model)
        if models:
            self._MODEL_INVENTORY_CACHE[scope] = (
                now + self._MODEL_INVENTORY_CACHE_TTL_SECONDS,
                [dict(item) for item in models],
            )
        return models

    def complete(self, model, messages, tools, params):
        del model, messages, tools, params
        raise NotImplementedError("Deepgram models are audio tasks; use transcribe or tts.")

    def transcribe(self, model, audio, params):
        model_id = str(model or "").removeprefix("deepgram/")
        query: Dict[str, Any] = {"model": model_id}
        for key in ("language", "punctuate", "diarize", "smart_format"):
            if key in (params or {}):
                query[key] = params[key]
        audio_value = str(audio or "")
        path = "/v1/listen?" + urllib.parse.urlencode(query)
        if audio_value.startswith("http://") or audio_value.startswith("https://"):
            raw = self._request_json("POST", path, {"url": audio_value})
        else:
            encoded = (
                audio_value.split(",", 1)[1]
                if audio_value.startswith("data:") and "," in audio_value
                else audio_value
            )
            try:
                audio_bytes = base64.b64decode(encoded)
            except ValueError as error:
                raise RuntimeError("deepgram: audio must be a URL or base64 data") from error
            payload = self._request(
                "POST",
                path,
                audio_bytes,
                content_type=str((params or {}).get("mime_type") or "audio/wav"),
            )
            try:
                raw = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, ValueError) as error:
                raise RuntimeError(
                    f"Deepgram API returned invalid JSON: {payload[:500]!r}"
                ) from error
        channels = (
            ((raw.get("results") or {}).get("channels") or []) if isinstance(raw, dict) else []
        )
        alternatives = (
            channels[0].get("alternatives") if channels and isinstance(channels[0], dict) else []
        )
        transcript = (
            str(alternatives[0].get("transcript") or "")
            if alternatives and isinstance(alternatives[0], dict)
            else ""
        )
        return {"text": transcript}

    def tts(self, model, text, voice):
        del voice
        model_id = str(model or "").removeprefix("deepgram/")
        audio = self._request(
            "POST",
            "/v1/speak?" + urllib.parse.urlencode({"model": model_id}),
            {"text": str(text or "")},
            accept="audio/mpeg",
        )
        return {"audio": "data:audio/mpeg;base64," + base64.b64encode(audio).decode("ascii")}
