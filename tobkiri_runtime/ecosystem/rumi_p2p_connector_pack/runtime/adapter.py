"""Timestamped P2P verification, normalization, and HTTPS delivery."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
CREDENTIAL = "rumi.service.credential.resolve.v1"
SERVICE_PACK_ID = "rumi_p2p_connector_pack"
ADAPTER_ID = "p2p"
_MAX_SKEW_MS = 5 * 60 * 1000


class P2PConnector:
    """Own authenticated peer envelopes without routing implementations."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke peer inbound or outbound protocol handling."""

        if name == "verify_normalize":
            return self._verify(payload)
        if name == "deliver":
            return self._deliver(payload)
        raise ValueError(f"unknown P2P connector operation: {name}")

    def _verify(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        connector = _connector(payload)
        headers = _headers(payload.get("headers"))
        body = str(payload.get("body") or "")
        timestamp = int(headers.get("x-rumi-peer-timestamp") or 0)
        if abs(_now_ms() - timestamp) > _MAX_SKEW_MS:
            raise PermissionError("peer request timestamp is outside replay window")
        secret = self._secret(connector, "connector.inbound.verify")
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}.{body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        supplied = headers.get("x-rumi-peer-signature", "")
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise PermissionError("peer signature is invalid")
        value = json.loads(body)
        if not isinstance(value, Mapping):
            raise ValueError("peer envelope must be an object")
        event_id = str(value.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("peer event_id is required")
        return {
            "event_id": event_id,
            "type": str(value.get("type") or "peer.message")[:120],
            "actor_id": str(value.get("peer_id") or "")[:255],
            "channel_id": str(value.get("channel_id") or "")[:255],
            "text": str(value.get("text") or "")[:100_000],
            "payload": _sanitize(value.get("payload") or {}),
        }

    def _deliver(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        arguments = _delivery_arguments(payload)
        self._redeem(payload, arguments)
        connector = arguments["connector"]
        endpoint = str(_mapping(connector.get("config")).get("peer_url") or "")
        _safe_endpoint(endpoint)
        secret = self._secret(connector, "connector.outbound.deliver")
        body = json.dumps(
            {
                "event_id": arguments["delivery_id"],
                "type": "peer.message",
                "payload": arguments["message"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        timestamp = _now_ms()
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}.{body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        request = urllib.request.Request(
            endpoint,
            data=body.encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Rumi-Peer-Timestamp": str(timestamp),
                "X-Rumi-Peer-Signature": signature,
                "Idempotency-Key": arguments["delivery_id"],
            },
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=20) as response:
                content = response.read(64 * 1024 + 1)
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            return {"status": "failed", "http_status": int(exc.code)}
        if len(content) > 64 * 1024:
            raise RuntimeError("peer response exceeds size limit")
        return {
            "status": "delivered" if 200 <= status < 300 else "failed",
            "http_status": status,
            "response_sha256": hashlib.sha256(content).hexdigest(),
        }

    def _secret(self, connector: Mapping[str, Any], scope: str) -> str:
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
        if not isinstance(material, Mapping) or not str(material.get("peer_secret") or ""):
            raise PermissionError("P2P credential lacks peer_secret")
        return str(material["peer_secret"])

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
            raise PermissionError(str(result.get("reason") or "peer delivery denied"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Deny redirects to preserve the approved peer endpoint."""

        del req, fp, code, msg, headers, newurl
        return None


def create_connector_adapter(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the P2P connector adapter."""

    adapter = P2PConnector(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return adapter.invoke(name, payload)

    return operation


def _safe_endpoint(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise PermissionError("peer endpoint must be credential-free HTTPS")
    if parsed.port not in {None, 443}:
        raise PermissionError("peer endpoint port is denied")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise RuntimeError("peer endpoint cannot be resolved") from exc
    if not addresses or any(_private(address) for address in addresses):
        raise PermissionError("peer endpoint resolves to a private address")


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


def _delivery_arguments(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "connector": dict(_connector(payload)),
        "registry_revision": max(0, int(payload.get("registry_revision") or 0)),
        "delivery_id": str(payload.get("delivery_id") or ""),
        "message": dict(_mapping(payload.get("message"))),
    }


def _connector(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("connector")
    if not isinstance(value, Mapping) or value.get("adapter_id") != ADAPTER_ID:
        raise PermissionError("connector is not bound to P2P adapter")
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


def _now_ms() -> int:
    return int(time.time() * 1000)

