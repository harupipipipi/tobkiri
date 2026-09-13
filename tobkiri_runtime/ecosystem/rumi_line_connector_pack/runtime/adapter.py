"""LINE signature verification, normalization, and bounded delivery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
CREDENTIAL = "rumi.service.credential.resolve.v1"
SERVICE_PACK_ID = "rumi_line_connector_pack"
ADAPTER_ID = "line"
_REPLY = "https://api.line.me/v2/bot/message/reply"
_PUSH = "https://api.line.me/v2/bot/message/push"


class LineConnector:
    """Own only LINE webhook protocol and Messaging API delivery."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke LINE inbound or outbound protocol handling."""

        if name == "verify_normalize":
            return self._verify(payload)
        if name == "deliver":
            return self._deliver(payload)
        raise ValueError(f"unknown LINE connector operation: {name}")

    def _verify(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        connector = _connector(payload)
        headers = _headers(payload.get("headers"))
        body = str(payload.get("body") or "")
        secret = self._secret(connector, "connector.inbound.verify", "channel_secret")
        expected = base64.b64encode(
            hmac.new(
                secret.encode("utf-8"),
                body.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        supplied = headers.get("x-line-signature", "")
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise PermissionError("LINE signature is invalid")
        value = json.loads(body)
        events = value.get("events") if isinstance(value, Mapping) else None
        if not isinstance(events, list) or len(events) != 1:
            raise ValueError("LINE request must contain exactly one event")
        event = events[0]
        if not isinstance(event, Mapping):
            raise ValueError("LINE event must be an object")
        source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
        message = event.get("message") if isinstance(event.get("message"), Mapping) else {}
        event_id = str(event.get("webhookEventId") or message.get("id") or "").strip()
        if not event_id:
            raise ValueError("LINE webhook event ID is required")
        source_id = str(
            source.get("userId") or source.get("groupId") or source.get("roomId") or ""
        )
        return {
            "event_id": event_id,
            "type": str(event.get("type") or "event")[:120],
            "actor_id": str(source.get("userId") or "")[:255],
            "channel_id": source_id[:255],
            "source_type": str(source.get("type") or "")[:40],
            "text": str(message.get("text") or "")[:5_000],
            "message_type": str(message.get("type") or "")[:40],
            "reply_token": str(event.get("replyToken") or "")[:255],
            "delivery_context": {
                "mode": str(event.get("mode") or "active")[:40],
                "redelivery": bool(event.get("deliveryContext", {}).get("isRedelivery"))
                if isinstance(event.get("deliveryContext"), Mapping)
                else False,
            },
        }

    def _deliver(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        arguments = _delivery_arguments(payload)
        self._redeem(payload, arguments)
        token = self._secret(
            arguments["connector"],
            "connector.outbound.deliver",
            "channel_access_token",
        )
        message = arguments["message"]
        text = str(message.get("text") or "").strip()
        if not text:
            raise ValueError("LINE text is required")
        messages = [{"type": "text", "text": text[:5_000]}]
        reply_token = str(message.get("reply_token") or "").strip()
        if reply_token:
            endpoint = _REPLY
            body = {"replyToken": reply_token, "messages": messages}
        else:
            target_id = str(message.get("target_id") or message.get("channel_id") or "")
            if not target_id:
                raise ValueError("LINE reply_token or target_id is required")
            endpoint = _PUSH
            body = {"to": target_id, "messages": messages}
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                content = response.read(64 * 1024 + 1)
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            return {"status": "failed", "http_status": int(exc.code)}
        if len(content) > 64 * 1024:
            raise RuntimeError("LINE response exceeds size limit")
        return {
            "status": "delivered" if 200 <= status < 300 else "failed",
            "http_status": status,
            "response_sha256": hashlib.sha256(content).hexdigest(),
        }

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
            raise PermissionError(f"LINE credential lacks {key}")
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
            raise PermissionError(str(result.get("reason") or "LINE delivery denied"))


def create_connector_adapter(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the LINE connector adapter."""

    adapter = LineConnector(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return adapter.invoke(name, payload)

    return operation


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
        raise PermissionError("connector is not bound to LINE adapter")
    return value


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key).casefold(): str(item) for key, item in value.items()}


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value

