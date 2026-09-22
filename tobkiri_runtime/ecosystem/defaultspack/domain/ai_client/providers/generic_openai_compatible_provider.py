from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import urllib.parse
import urllib.request

from domain.ai_client.openai_compatible_connections import resolve_connection_secret

from .openai_compatible_provider import OpenAICompatibleProvider


class GenericOpenAICompatibleProvider(OpenAICompatibleProvider):
    provider_name = "openai_compatible"
    display_name = "OpenAI Compatible"

    def __init__(
        self,
        connection: dict[str, Any] | None = None,
        *,
        pack_root: Path | None = None,
    ) -> None:
        connection = connection or {
            "connection_id": "default",
            "label": "OpenAI Compatible",
            "base_url": os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "http://127.0.0.1:8001/v1"),
            "auth_mode": "bearer" if os.environ.get("OPENAI_COMPATIBLE_API_KEY") else "none",
            "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
            "manual_models": [],
            "model_list": {"enabled": False},
        }
        self.connection = dict(connection)
        self.connection_id = str(connection.get("connection_id") or "default")
        secret, username = resolve_connection_secret(connection, pack_root=pack_root)
        self._auth_mode = str(connection.get("auth_mode") or "none")
        self._auth_header = str(connection.get("auth_header") or "X-API-Key")
        self._basic_username = username
        manual = [self._manual_model(item) for item in connection.get("manual_models", [])]
        super().__init__(
            api_key=secret,
            base_url=str(connection.get("base_url") or ""),
            provider_id="openai_compatible",
            display_name=str(connection.get("label") or self.connection_id),
            credential_required=self._auth_mode != "none",
            known_models=[item for item in manual if item],
            remote_model_discovery=False,
        )

    def _manual_model(self, raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, str):
            model_id, model_type, capabilities = raw.strip(), "unknown", {}
        elif isinstance(raw, dict):
            model_id = str(raw.get("id") or "").strip()
            model_type = str(raw.get("type") or "unknown").strip().lower()
            capabilities = dict(raw.get("capabilities") or {})
        else:
            return None
        if not model_id:
            return None
        unknowns = {key: capabilities.get(key) for key in ("text_input", "text_output", "streaming", "tool_calling", "image_input")}
        return {
            "id": f"openai_compatible/{self.connection_id}:{model_id}",
            "model_id": model_id,
            "name": model_id,
            "type": model_type,
            "capabilities": unknowns,
            "metadata": {"source": "manual_connection_inventory", "connection_id": self.connection_id, "capability_confidence": "manual" if capabilities else "unknown"},
        }

    def list_models(self) -> list[dict[str, Any]]:
        base = super().list_models()
        model_list = self.connection.get("model_list") if isinstance(self.connection.get("model_list"), dict) else {}
        if not model_list.get("enabled"):
            return base
        remote = self._remote_connection_models(model_list)
        seen = {str(item.get("model_id") or "") for item in base}
        return base + [item for item in remote if str(item.get("model_id") or "") not in seen]

    def _remote_connection_models(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        cache = self._load_remote_model_cache()
        try:
            models = self._fetch_pages(config)
        except Exception:
            return self._connection_cached(cache.get("models")) if cache else []
        self._save_remote_model_cache(models)
        return models

    def _fetch_pages(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        url = str(config.get("url") or "") or self._base_url.rstrip("/") + "/" + str(config.get("path") or "/models").lstrip("/")
        cursor = ""
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _page in range(max(1, min(100, int(config.get("max_pages") or 20)))):
            page_url = self._cursor_url(url, str(config.get("cursor_param") or "cursor"), cursor) if cursor else url
            request = urllib.request.Request(page_url, headers=self._headers(content_type=""), method="GET")
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=6) as response:
                payload = json.loads(response.read().decode("utf-8"))
            items = self._at_path(payload, str(config.get("items_path") or "data"))
            if not isinstance(items, list):
                raise ValueError("Configured model items path is not a list")
            for raw in items:
                model_id = str(raw.get("id") or raw.get("model") or "").strip() if isinstance(raw, dict) else str(raw or "").strip()
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                output.append(self._remote_unknown_model(model_id, raw))
            next_value = self._at_path(payload, str(config.get("next_path") or "next"))
            if not next_value:
                break
            if isinstance(next_value, str) and next_value.startswith(("http://", "https://")):
                url, cursor = next_value, ""
            else:
                cursor = str(next_value)
        return output

    def _remote_unknown_model(self, model_id: str, raw: Any) -> dict[str, Any]:
        explicit_type = str(raw.get("type") or "unknown").strip().lower() if isinstance(raw, dict) else "unknown"
        return {
            "id": f"openai_compatible/{self.connection_id}:{model_id}",
            "qualified_model_id": f"openai_compatible/{self.connection_id}:{model_id}",
            "provider_id": "openai_compatible",
            "provider": "openai_compatible",
            "model_id": model_id,
            "name": model_id,
            "display_name": model_id,
            "type": explicit_type,
            "capabilities": {"text_input": None, "text_output": None, "streaming": None, "tool_calling": None, "image_input": None},
            "metadata": {"source": "configured_model_list", "connection_id": self.connection_id, "capability_confidence": "unknown", "catalog_cache_state": "fresh"},
        }

    def _headers(self, content_type="application/json"):
        headers = {"User-Agent": "RumiAI/1.0", "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if self._api_key and self._auth_mode == "bearer":
            headers["Authorization"] = "Bearer " + self._api_key
        elif self._api_key and self._auth_mode == "api_key_header":
            headers[self._auth_header] = self._api_key
        elif self._api_key and self._auth_mode == "basic":
            token = base64.b64encode(f"{self._basic_username}:{self._api_key}".encode()).decode()
            headers["Authorization"] = "Basic " + token
        return headers

    def _remote_model_cache_path(self) -> Path:
        root = Path(__file__).resolve().parents[3] / "user_data" / "shared" / "provider_model_cache"
        root.mkdir(parents=True, exist_ok=True)
        endpoint = str((self.connection.get("model_list") or {}).get("url") or self._base_url)
        auth = hashlib.sha256(self._api_key.encode()).hexdigest() if self._api_key else "anonymous"
        scope = hashlib.sha256(f"{self.connection_id}|{endpoint}|{auth}".encode()).hexdigest()[:24]
        return root / f"openai-compatible.{scope}.models.json"

    def _connection_cached(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        output = []
        for item in raw:
            if isinstance(item, dict) and (item.get("metadata") or {}).get("connection_id") == self.connection_id:
                copy = dict(item)
                copy["metadata"] = {**dict(copy.get("metadata") or {}), "catalog_cache_state": "stale"}
                output.append(copy)
        return output

    @staticmethod
    def _at_path(payload: Any, path: str) -> Any:
        value = payload
        for part in [item for item in path.split(".") if item]:
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value

    @staticmethod
    def _cursor_url(url: str, name: str, cursor: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        query[name] = cursor
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ""))
