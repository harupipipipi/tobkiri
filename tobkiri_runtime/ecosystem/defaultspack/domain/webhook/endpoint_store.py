from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .component_defaults import default_endpoint_payloads, default_security_for_kind
from .endpoint import WebhookEndpoint


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_endpoint_payload(payload: dict[str, Any], *, apply_defaults: bool = True) -> dict[str, Any]:
    safe = dict(payload)
    kind = str(safe.get("kind") or "generic").strip() or "generic"

    if apply_defaults and "enabled" not in safe:
        safe["enabled"] = False

    security = safe.get("security")
    if "security" in safe or apply_defaults:
        security = safe.get("security")
    if apply_defaults and (not isinstance(security, dict) or not security):
        safe["security"] = default_security_for_kind(kind)
    if "target" in safe or apply_defaults:
        target_value = safe.get("target")
        safe["target"] = (
            {str(key): value for key, value in target_value.items()}
            if isinstance(target_value, dict)
            else {}
        )
    if "default_delivery" in safe or apply_defaults:
        default_delivery_value = safe.get("default_delivery")
        default_delivery: dict[str, object] = (
            {str(key): value for key, value in default_delivery_value.items()}
            if isinstance(default_delivery_value, dict)
            else {}
        )
        default_delivery.setdefault("action_id", str(default_delivery.get("action_id") or "chat.message"))
        safe["default_delivery"] = default_delivery
    if "allowed_delivery_actions" in safe or apply_defaults:
        default_delivery_value = safe.get("default_delivery")
        default_delivery_for_actions: dict[str, object] = (
            {str(key): value for key, value in default_delivery_value.items()}
            if isinstance(default_delivery_value, dict)
            else {}
        )
        default_action = str(
            default_delivery_for_actions.get("action_id") or "chat.message"
        ).strip() or "chat.message"
        raw_allowed = safe.get("allowed_delivery_actions")
        if isinstance(raw_allowed, str):
            allowed = [part.strip() for part in raw_allowed.split(",") if part.strip()]
        elif isinstance(raw_allowed, list):
            allowed = [str(item).strip() for item in raw_allowed if str(item or "").strip()]
        else:
            allowed = [default_action] if apply_defaults else []
        if apply_defaults and not allowed:
            allowed = [default_action]
        safe["allowed_delivery_actions"] = allowed
    if "ttl_seconds" in safe or apply_defaults:
        ttl_raw = safe.get("ttl_seconds")
        if ttl_raw in (None, "", False):
            safe["ttl_seconds"] = None
        else:
            try:
                if isinstance(ttl_raw, bool):
                    raise TypeError
                if not isinstance(ttl_raw, (int, float, str)):
                    raise TypeError
                safe["ttl_seconds"] = max(0, int(ttl_raw))
            except (TypeError, ValueError):
                safe["ttl_seconds"] = None

    return safe


class WebhookEndpointStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or self._default_path()
        self._data = self._load()

    @staticmethod
    def _default_path() -> Path:
        override = os.environ.get("RUMI_DEFAULTSPACK_WEBHOOK_ENDPOINTS_PATH", "").strip()
        if override:
            return Path(override)
        return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "webhooks" / "endpoints.json"

    def list_endpoints(self) -> list[dict[str, Any]]:
        return [endpoint.as_dict() for endpoint in self._endpoints().values()]

    def get(self, endpoint_id: str) -> WebhookEndpoint | None:
        return self._endpoints().get(str(endpoint_id or "").strip())

    def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoints = self._endpoints()
        endpoint_id = str(payload.get("id") or "").strip() or self._make_id(str(payload.get("kind") or "webhook"))
        existed = endpoint_id in endpoints
        payload = _safe_endpoint_payload(payload, apply_defaults=not existed)
        existing_payload = endpoints[endpoint_id].as_dict(redact=False) if existed else {}
        merged_payload = {**existing_payload, **payload, "id": endpoint_id}
        if not existed and merged_payload.get("ttl_seconds") and not merged_payload.get("expires_at"):
            merged_payload["expires_at"] = _now_ms() + int(merged_payload["ttl_seconds"]) * 1000
        endpoint = WebhookEndpoint.from_dict(merged_payload)
        endpoints[endpoint_id] = endpoint
        self._data["endpoints"] = {key: item.as_dict(redact=False) for key, item in endpoints.items()}
        self._save()
        return {"endpoint": endpoint.as_dict(), "created": not existed}

    def delete(self, endpoint_id: str) -> dict[str, Any]:
        endpoints = self._endpoints()
        existed = str(endpoint_id or "").strip() in endpoints
        endpoints.pop(str(endpoint_id or "").strip(), None)
        self._data["endpoints"] = {key: item.as_dict(redact=False) for key, item in endpoints.items()}
        self._save()
        return {"deleted": existed, "webhook_id": endpoint_id}

    def _endpoints(self) -> dict[str, WebhookEndpoint]:
        raw = self._data.setdefault("endpoints", {})
        if not isinstance(raw, dict):
            raw = {}
            self._data["endpoints"] = raw
        endpoints = {key: WebhookEndpoint.from_dict(value) for key, value in raw.items() if isinstance(value, dict)}
        if not endpoints:
            for endpoint in self._default_endpoints():
                endpoints[endpoint.id] = endpoint
        return endpoints

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schema_version", 1)
        data.setdefault("endpoints", {})
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["updated_at"] = _now_ms()
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _make_id(kind: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", str(kind or "webhook").lower()).strip("-") or "webhook"
        return f"{slug}-{int(time.time() * 1000)}"

    @staticmethod
    def _default_endpoints() -> list[WebhookEndpoint]:
        return [WebhookEndpoint.from_dict(payload) for payload in default_endpoint_payloads()]
