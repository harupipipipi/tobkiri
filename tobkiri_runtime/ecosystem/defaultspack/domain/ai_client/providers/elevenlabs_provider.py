"""ElevenLabs native adapter with an account-visible live model inventory."""

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


class ElevenLabsProvider(BaseProvider):
    """Discover exactly the models visible to an ElevenLabs API key and run TTS."""

    provider_id = "elevenlabs"
    BASE_URL = "https://api.elevenlabs.io"
    _MODEL_INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
    _MODEL_INVENTORY_CACHE_TTL_SECONDS = 300

    def __init__(self, api_key: str | None = None):
        self._api_key = str(api_key or read_provider_api_key("elevenlabs", "legacy") or "").strip()
        self._base_url = (
            str(os.environ.get("ELEVENLABS_BASE_URL", self.BASE_URL) or self.BASE_URL)
            .strip()
            .rstrip("/")
        )
        self._ssl_ctx = ssl.create_default_context()

    def _headers(
        self, content_type: str = "application/json", accept: str = "application/json"
    ) -> Dict[str, str]:
        headers = {"xi-api-key": self._api_key, "Accept": accept, "User-Agent": "RumiAI/1.0"}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _inventory_scope(self) -> str:
        material = f"{self.provider_id}\0{self._base_url}".encode("utf-8")
        return hashlib.sha256(self._api_key.encode("utf-8") + b"\0" + material).hexdigest()

    def _request_json(self, method: str, path: str, body: Dict[str, Any] | None = None) -> Any:
        if not self._api_key:
            raise RuntimeError("elevenlabs: missing ELEVENLABS_API_KEY")
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
                f"ElevenLabs API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"ElevenLabs API connection error: {error.reason}") from error
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"ElevenLabs API returned invalid JSON: {payload[:500]}") from error

    def _request_audio(self, path: str, body: Dict[str, Any]) -> bytes:
        if not self._api_key:
            raise RuntimeError("elevenlabs: missing ELEVENLABS_API_KEY")
        request = urllib.request.Request(
            self._base_url + path,
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers("application/json", "audio/mpeg"),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"ElevenLabs API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"ElevenLabs API connection error: {error.reason}") from error

    @staticmethod
    def _model_record(raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        model_id = str(raw.get("model_id") or raw.get("id") or "").strip()
        if not model_id:
            return None
        is_tts = bool(raw.get("can_do_text_to_speech"))
        is_transcription = bool(raw.get("can_do_speech_to_text") or raw.get("can_do_transcription"))
        model_type = "tts" if is_tts else ("transcription" if is_transcription else "audio")
        max_characters = raw.get("maximum_text_length_per_request") or 0
        try:
            max_characters = int(max_characters)
        except (TypeError, ValueError):
            max_characters = 0
        return {
            "id": f"elevenlabs/{model_id}",
            "model_id": model_id,
            "provider_id": "elevenlabs",
            "provider": "elevenlabs",
            "name": str(raw.get("name") or model_id),
            "display_name": str(raw.get("name") or model_id),
            "type": model_type,
            "capabilities": {
                "tts": is_tts,
                "transcription": is_transcription,
                "text_input": is_tts,
                "audio_input": is_transcription,
                "text_output": is_transcription,
                "streaming": False,
            },
            "metadata": {
                "source": "native_models_endpoint",
                "capability_source": "native_models_endpoint",
                "capability_confidence": "provider_reported",
                "can_be_finetuned": bool(raw.get("can_be_finetuned")),
                "requires_alpha_access": bool(raw.get("requires_alpha_access")),
                "maximum_text_length_per_request": max_characters,
                "languages": list(raw.get("languages") or [])
                if isinstance(raw.get("languages"), list)
                else [],
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
        payload = self._request_json("GET", "/v1/models")
        entries = (
            payload
            if isinstance(payload, list)
            else (payload.get("models") if isinstance(payload, dict) else [])
        )
        models = [
            model
            for model in (self._model_record(raw) for raw in entries or [])
            if model is not None
        ]
        if models:
            self._MODEL_INVENTORY_CACHE[scope] = (
                now + self._MODEL_INVENTORY_CACHE_TTL_SECONDS,
                [dict(item) for item in models],
            )
        return models

    def complete(self, model, messages, tools, params):
        del model, messages, tools, params
        raise NotImplementedError("ElevenLabs models are audio tasks; use tts or transcribe.")

    def tts(self, model, text, voice):
        voice_id = str(voice or "").strip()
        if not voice_id:
            raise RuntimeError("elevenlabs: a voice_id is required for text-to-speech")
        model_id = str(model or "").removeprefix("elevenlabs/")
        payload = {"text": str(text or ""), "model_id": model_id}
        audio = self._request_audio(
            "/v1/text-to-speech/" + urllib.parse.quote(voice_id, safe=""), payload
        )
        return {"audio": "data:audio/mpeg;base64," + base64.b64encode(audio).decode("ascii")}
