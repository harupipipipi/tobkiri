"""IBM watsonx.ai foundation model inventory and text adapter."""

from __future__ import annotations

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


class IBMWatsonxProvider(BaseProvider):
    provider_id = "ibm-watsonx"
    API_VERSION = "2024-05-31"
    _CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}

    def __init__(self):
        connection = next(
            (
                dict(item)
                for item in provider_named_api_keys(self.provider_id)
                if item.get("configured")
            ),
            {},
        )
        self._key = ""
        if not self._key and connection.get("api_id"):
            self._key = str(
                read_provider_api_key(self.provider_id, str(connection["api_id"])) or ""
            ).strip()
        self._base_url = (
            str(connection.get("base_url") or "")
            .strip()
            .rstrip("/")
        )
        self._project_id = str(connection.get("project_id") or "").strip()
        self._token = str(connection.get("access_token") or "").strip()
        self._ssl_ctx = ssl.create_default_context()

    def _access_token(self) -> str:
        if self._token:
            return self._token
        if not self._key:
            raise RuntimeError("ibm-watsonx: save an IBM Cloud API key")
        body = urllib.parse.urlencode(
            {"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": self._key}
        ).encode()
        req = urllib.request.Request(
            "https://iam.cloud.ibm.com/identity/token",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=30) as resp:
                payload = json.loads(resp.read().decode())
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise RuntimeError("ibm-watsonx: could not exchange IBM Cloud API key") from error
        self._token = str(payload.get("access_token") or "") if isinstance(payload, dict) else ""
        if not self._token:
            raise RuntimeError("ibm-watsonx: IAM did not return an access token")
        return self._token

    def _request(
        self, method: str, path: str, body: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        if not self._base_url:
            raise RuntimeError("ibm-watsonx: configure the regional watsonx.ai base URL")
        req = urllib.request.Request(
            self._base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self._access_token(),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=120) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"ibm-watsonx API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"ibm-watsonx connection error: {error.reason}") from error
        return payload if isinstance(payload, dict) else {"data": payload}

    @classmethod
    def _record(cls, raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        model_id = str(raw.get("model_id") or raw.get("id") or "").strip()
        if not model_id:
            return None
        tasks = (
            {str(value).lower() for value in raw.get("tasks", [])}
            if isinstance(raw.get("tasks"), list)
            else set()
        )
        embedding = any("embed" in value for value in tasks)
        limits = raw.get("model_limits") if isinstance(raw.get("model_limits"), dict) else {}
        result = {
            "id": f"ibm-watsonx/{model_id}",
            "model_id": model_id,
            "provider_id": cls.provider_id,
            "provider": cls.provider_id,
            "name": str(raw.get("label") or model_id),
            "display_name": str(raw.get("label") or model_id),
            "type": "embedding" if embedding else "chat",
            "capabilities": {
                "chat": not embedding,
                "text_input": True,
                "text_output": not embedding,
                "embeddings": embedding,
                "streaming": not embedding,
            },
            "metadata": {
                "source": "watsonx_foundation_model_specs_api",
                "capability_source": "provider_reported",
                "capability_confidence": "provider_reported",
                "tasks": sorted(tasks),
                "provider": raw.get("provider"),
            },
        }
        for key in ("max_sequence_length", "max_input_tokens"):
            try:
                value = int(limits.get(key) or raw.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value:
                result.update(
                    {"context_window": value, "max_context": value, "max_context_tokens": value}
                )
                break
        return result

    def list_models(self) -> List[Dict[str, Any]]:
        if not self._key or not self._base_url:
            return []
        scope = self._base_url + "\0" + self._key
        cached = self._CACHE.get(scope)
        if cached and cached[0] > time.monotonic():
            return [dict(item) for item in cached[1]]
        payload = self._request(
            "GET", f"/ml/v1/foundation_model_specs?version={self.API_VERSION}&tech_preview=true"
        )
        models = [
            record
            for raw in (payload.get("resources") or payload.get("models") or [])
            if (record := self._record(raw))
        ]
        if models:
            self._CACHE[scope] = (time.monotonic() + 300, [dict(item) for item in models])
        return models

    def _project(self, params: Dict[str, Any]) -> str:
        extra = params.get("extra_body") if isinstance(params.get("extra_body"), dict) else {}
        return str(extra.get("watsonx_project_id") or self._project_id or "").strip()

    def complete(self, model, messages, tools, params):
        del tools
        project_id = self._project(dict(params or {}))
        if not project_id:
            raise RuntimeError("ibm-watsonx: configure WATSONX_PROJECT_ID")
        text = "\n".join(
            str(item.get("content") or "") for item in messages if isinstance(item, dict)
        )
        model_id = str(model or "").removeprefix("ibm-watsonx/")
        payload = self._request(
            "POST",
            f"/ml/v1/text/generation?version={self.API_VERSION}",
            {"model_id": model_id, "input": text, "project_id": project_id},
        )
        results = payload.get("results") if isinstance(payload.get("results"), list) else []
        output = (
            str(results[0].get("generated_text") or "")
            if results and isinstance(results[0], dict)
            else ""
        )
        return {
            "content": [{"type": "text", "text": output}],
            "finish_reason": "stop",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "raw_extra": {"model": model_id},
        }

    def stream(self, model, messages, tools, params):
        response = self.complete(model, messages, tools, params)
        yield {"type": "content_delta", "delta": response["content"][0]}
        yield {
            "type": "stream_end",
            "finish_reason": response["finish_reason"],
            "usage": response["usage"],
        }
