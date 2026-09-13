"""Signed generic webhook normalization and receipt-gated delivery."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
CREDENTIAL = "rumi.service.credential.resolve.v1"
SERVICE_PACK_ID = "rumi_generic_webhook_connector_pack"
ADAPTER_ID = "generic_webhook"
_MAX_RESPONSE = 64 * 1024


class GenericWebhookAdapter:
    """Verify normalized webhooks and deliver bounded HTTPS JSON messages."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke inbound verification or outbound delivery."""

        if name == "verify_normalize":
            return self._verify_normalize(payload)
        if name == "deliver":
            return self._deliver(payload)
        raise ValueError(f"unknown generic webhook adapter operation: {name}")

    def _verify_normalize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        connector = _connector(payload)
        body = str(payload.get("body") or "")
        headers = _headers(payload.get("headers"))
        secret = self._secret(
            connector,
            "connector.inbound.verify",
            "signing_secret",
        )
        supplied = headers.get("x-rumi-signature", "")
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise PermissionError("generic webhook signature is invalid")
        value = json.loads(body)
        if not isinstance(value, Mapping):
            raise ValueError("generic webhook JSON must be an object")
        event_id = str(
            value.get("event_id")
            or headers.get("x-rumi-event-id")
            or payload.get("request_id")
            or ""
        ).strip()
        if not event_id:
            raise ValueError("generic webhook event ID is required")
        return {
            "event_id": event_id,
            "type": str(value.get("type") or "message")[:120],
            "actor_id": str(value.get("actor_id") or "")[:255],
            "channel_id": str(value.get("channel_id") or "")[:255],
            "text": str(value.get("text") or "")[:100_000],
            "payload": _sanitize(value.get("payload") or {}),
        }

    def _deliver(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        arguments = {
            "connector": dict(_connector(payload)),
            "registry_revision": max(0, int(payload.get("registry_revision") or 0)),
            "delivery_id": str(payload.get("delivery_id") or ""),
            "message": dict(_mapping(payload.get("message"))),
        }
        self._redeem(payload, arguments)
        config = _mapping(arguments["connector"].get("config"))
        endpoint = str(config.get("outbound_url") or "").strip()
        _safe_endpoint(endpoint)
        token = self._secret(
            arguments["connector"],
            "connector.outbound.deliver",
            "outbound_token",
        )
        data = json.dumps(
            {
                "delivery_id": arguments["delivery_id"],
                "message": arguments["message"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Idempotency-Key": arguments["delivery_id"],
            },
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=20) as response:
                content = response.read(_MAX_RESPONSE + 1)
                if len(content) > _MAX_RESPONSE:
                    raise RuntimeError("webhook response exceeds size limit")
                return {
                    "status": "delivered",
                    "http_status": int(response.status),
                    "response_sha256": hashlib.sha256(content).hexdigest(),
                }
        except urllib.error.HTTPError as exc:
            return {"status": "failed", "http_status": int(exc.code)}

    def _secret(
        self,
        connector: Mapping[str, Any],
        scope: str,
        key: str,
    ) -> str:
        resolved = self.client.invoke(
            CREDENTIAL,
            "resolve",
            {
                "handle": str(connector.get("credential_ref") or ""),
                "provider_instance_id": ADAPTER_ID,
                "scope": scope,
            },
        )
        material = resolved.get("secret_material")
        if not isinstance(material, Mapping) or not str(material.get(key) or ""):
            raise PermissionError(f"generic webhook credential lacks {key}")
        return str(material[key])

    def _redeem(
        self,
        payload: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": "connector.adapter.deliver",
                "authority": "connector.delivery.execute",
                "caller_id": str(payload.get("caller_id") or ""),
                "caller_pack_id": str(payload.get("caller_pack_id") or ""),
                "caller_function_id": str(payload.get("caller_function_id") or ""),
                "profile_id": str(payload.get("profile_id") or "default"),
                "workspace_id": "",
                "session_id": str(payload.get("session_id") or ""),
                "arguments": dict(arguments),
            },
        )
        if not result.get("authorized"):
            raise PermissionError(str(result.get("reason") or "webhook delivery denied"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Deny redirects so an approved endpoint cannot pivot to a private host."""

        del req, fp, code, msg, headers, newurl
        return None


def create_connector_adapter(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the generic webhook connector adapter."""

    adapter = GenericWebhookAdapter(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return adapter.invoke(name, payload)

    return operation


def _safe_endpoint(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise PermissionError("generic webhook endpoint must be credential-free HTTPS")
    if parsed.port not in {None, 443}:
        raise PermissionError("generic webhook endpoint port is denied")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise RuntimeError("generic webhook endpoint cannot be resolved") from exc
    if not addresses or any(_private(address) for address in addresses):
        raise PermissionError("generic webhook endpoint resolves to a private address")


def _private(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _connector(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("connector")
    if not isinstance(value, Mapping) or value.get("adapter_id") != ADAPTER_ID:
        raise PermissionError("connector is not bound to generic webhook adapter")
    return value


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key).casefold(): str(item) for key, item in value.items()}


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _sanitize(value: Any) -> Any:
    secret_parts = ("credential", "oauth", "password", "secret", "signature", "token")
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not any(part in str(key).casefold() for part in secret_parts)
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value

