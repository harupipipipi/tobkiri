"""Project-scoped Google Vertex AI deployment inventory."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List
from ..api_key_store import provider_named_api_keys, read_provider_api_key
from ..base_provider import BaseProvider


class GoogleVertexAIProvider(BaseProvider):
    provider_id = "google-vertex-ai"

    def __init__(self, api_key: str | None = None):
        connection = next(
            (dict(x) for x in provider_named_api_keys(self.provider_id) if x.get("configured")), {}
        )
        self._token = str(api_key or "").strip()
        if connection.get("api_id"):
            self._token = str(
                read_provider_api_key(self.provider_id, str(connection["api_id"])) or ""
            ).strip()
        self._base_url = (
            str(connection.get("base_url") or "")
            .strip()
            .rstrip("/")
        )
        self._ssl_ctx = ssl.create_default_context()

    def _request(self, method, path, body=None):
        if not self._token:
            raise RuntimeError("google-vertex-ai: save a Google OAuth access token")
        if not self._base_url:
            raise RuntimeError("google-vertex-ai: configure a project and location base URL")
        req = urllib.request.Request(
            self._base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self._token,
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
                f"Vertex AI API error {error.code}: {error.read().decode('utf-8', errors='replace')}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Vertex AI connection error: {error.reason}") from error
        return payload if isinstance(payload, dict) else {"data": payload}

    def list_models(self) -> List[Dict[str, Any]]:
        if not self._token or not self._base_url:
            return []
        payload = self._request("GET", "/endpoints")
        records = []
        for endpoint in (
            payload.get("endpoints", []) if isinstance(payload.get("endpoints"), list) else []
        ):
            if not isinstance(endpoint, dict):
                continue
            endpoint_name = str(endpoint.get("name") or "").strip()
            endpoint_id = endpoint_name.rsplit("/", 1)[-1]
            for deployment in (
                endpoint.get("deployedModels", [])
                if isinstance(endpoint.get("deployedModels"), list)
                else []
            ):
                if not isinstance(deployment, dict):
                    continue
                deployment_id = str(deployment.get("id") or "").strip()
                if not endpoint_id or not deployment_id:
                    continue
                model_id = f"{endpoint_id}/{deployment_id}"
                records.append(
                    {
                        "id": f"google-vertex-ai/{model_id}",
                        "model_id": model_id,
                        "provider_id": self.provider_id,
                        "provider": self.provider_id,
                        "name": str(
                            deployment.get("displayName") or deployment.get("model") or model_id
                        ),
                        "display_name": str(
                            deployment.get("displayName") or deployment.get("model") or model_id
                        ),
                        "type": "chat",
                        "capabilities": {"chat": True, "text_input": True, "text_output": True},
                        "metadata": {
                            "source": "vertex_endpoint_deployments_api",
                            "capability_source": "project_deployment",
                            "capability_confidence": "provider_reported",
                            "endpoint_name": endpoint_name,
                            "deployed_model_id": deployment_id,
                            "model_resource": deployment.get("model"),
                            "input_schema": "pass exact instances as extra_body.vertex_instances",
                        },
                    }
                )
        return records

    def complete(self, model, messages, tools, params):
        del tools
        model_id = str(model or "").removeprefix("google-vertex-ai/")
        endpoint_id = model_id.split("/", 1)[0]
        extra = (
            (params or {}).get("extra_body")
            if isinstance((params or {}).get("extra_body"), dict)
            else {}
        )
        instances = (
            extra.get("vertex_instances")
            if isinstance(extra.get("vertex_instances"), list)
            else [
                {
                    "prompt": "\n".join(
                        str(x.get("content") or "") for x in messages if isinstance(x, dict)
                    )
                }
            ]
        )
        payload = self._request(
            "POST",
            f"/endpoints/{urllib.parse.quote(endpoint_id, safe='-_.~')}:predict",
            {"instances": instances},
        )
        text = json.dumps(payload.get("predictions") or payload, ensure_ascii=False)
        return {
            "content": [{"type": "text", "text": text}],
            "finish_reason": "stop",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "raw_extra": {"model": model_id},
        }

    def stream(self, model, messages, tools, params):
        response = self.complete(model, messages, tools, params)
        yield {"type": "content_delta", "delta": response["content"][0]}
        yield {"type": "stream_end", "finish_reason": "stop", "usage": response["usage"]}
