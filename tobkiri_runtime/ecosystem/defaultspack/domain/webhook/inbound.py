from __future__ import annotations

import hmac
import time
from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.external.normalizer import normalize_generic_webhook
from domain.external.pipeline import dispatch_external_event
from domain.external.token_store import read_external_token
from domain.webhook.endpoint_store import WebhookEndpointStore


def handle_inbound_webhook(
    webhook_id: str, input_data: dict[str, Any],
    context: dict[str, Any] | None = None, *,
    settings_owner: SettingsOwnerPort | None = None,
) -> dict[str, Any]:
    endpoint = WebhookEndpointStore().get(webhook_id)
    if endpoint is None:
        return {"status": "error", "error": "webhook endpoint not found", "_http_status": 404}
    if not endpoint.enabled:
        return {"status": "error", "error": "webhook endpoint disabled", "_http_status": 403}
    if _is_endpoint_expired(endpoint):
        return {"status": "error", "error": "webhook endpoint expired", "code": "WEBHOOK_EXPIRED", "_http_status": 410}
    verification = verify_endpoint_security(endpoint.as_dict(redact=False), input_data)
    if not verification["ok"]:
        return {"status": "error", "error": verification["reason"], "code": "WEBHOOK_UNAUTHORIZED", "_http_status": 401}
    event = normalize_generic_webhook(input_data, webhook_id=endpoint.id, verified=verification["verified"])
    delivery_override = _requested_delivery_override(input_data)
    if delivery_override and not _delivery_override_allowed(endpoint, delivery_override):
        return {
            "status": "error",
            "error": "delivery action is not allowed for this endpoint",
            "code": "WEBHOOK_DELIVERY_ACTION_NOT_ALLOWED",
            "_http_status": 403,
        }
    runtime_context = dict(context or {})
    runtime_context.setdefault("webhook_endpoint", endpoint.as_dict())
    if endpoint.response_profile_id:
        runtime_context.setdefault("output_profile_id", endpoint.response_profile_id)
    result = dispatch_external_event(
        event,
        input_profile_id=endpoint.input_profile_id,
        audience_policy={"default": "allow", "require": {"verified": endpoint.security.get("mode") != "none"}},
        context=runtime_context,
        send_response=True,
        settings_owner=settings_owner,
        envelope_overrides={
            "target": dict(endpoint.target),
            "delivery": {
                **(endpoint.default_delivery if isinstance(endpoint.default_delivery, dict) else {}),
                **(delivery_override or {}),
            },
            "attachments": input_data.get("attachments") if isinstance(input_data.get("attachments"), list) else [],
        },
    )
    return {"status": "ok", "endpoint": endpoint.as_dict(), "result": result}


def verify_endpoint_security(endpoint: dict[str, Any], input_data: dict[str, Any]) -> dict[str, Any]:
    security = endpoint.get("security") if isinstance(endpoint.get("security"), dict) else {}
    mode = str(security.get("mode") or "none").strip()
    if mode in {"", "none"}:
        return {"ok": True, "verified": False, "reason": "no security configured"}
    if mode == "shared_secret":
        header = str(security.get("header") or "x-rumi-webhook-token").lower()
        headers = input_data.get("_headers") if isinstance(input_data.get("_headers"), dict) else {}
        provided = str(headers.get(header) or headers.get(header.lower()) or input_data.get("token") or "").strip()
        expected = read_external_token(
            str(endpoint.get("kind") or "generic"),
            token_id=str(security.get("token_id") or endpoint.get("id") or "main"),
            kind="webhook_shared_secret",
        )
        if not expected:
            expected = str(security.get("token") or "").strip()
        if not expected:
            return {"ok": False, "verified": False, "reason": "shared secret not configured"}
        if not provided or not hmac.compare_digest(provided, expected):
            return {"ok": False, "verified": False, "reason": "shared secret mismatch"}
        return {"ok": True, "verified": True, "reason": ""}
    if mode == "provider_signature":
        return {"ok": False, "verified": False, "reason": "provider signatures are handled by provider routes"}
    return {"ok": False, "verified": False, "reason": "unsupported webhook security mode"}


def _requested_delivery_override(input_data: dict[str, Any]) -> dict[str, Any]:
    delivery = input_data.get("delivery") if isinstance(input_data.get("delivery"), dict) else {}
    action_id = str(delivery.get("action_id") or input_data.get("action_id") or "").strip()
    if action_id:
        return {**delivery, "action_id": action_id}
    return dict(delivery)


def _delivery_override_allowed(endpoint, delivery_override: dict[str, Any]) -> bool:
    action_id = str(delivery_override.get("action_id") or "").strip()
    if not action_id:
        return True
    allowed = [
        str(item).strip()
        for item in (endpoint.allowed_delivery_actions if isinstance(endpoint.allowed_delivery_actions, list) else [])
        if str(item or "").strip()
    ]
    if allowed:
        return action_id in set(allowed)
    default_delivery = endpoint.default_delivery if isinstance(endpoint.default_delivery, dict) else {}
    default_action = str(default_delivery.get("action_id") or "chat.message").strip() or "chat.message"
    return action_id == default_action


def _is_endpoint_expired(endpoint) -> bool:
    expires_at = int(endpoint.expires_at) if isinstance(endpoint.expires_at, int) else None
    if expires_at is None:
        return False
    return expires_at <= int(time.time() * 1000)
