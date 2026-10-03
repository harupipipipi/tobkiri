"""Signed email webhook normalization and TLS SMTP delivery."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import smtplib
import socket
import ssl
from email.message import EmailMessage
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
CREDENTIAL = "rumi.service.credential.resolve.v1"
SERVICE_PACK_ID = "rumi_email_connector_pack"
ADAPTER_ID = "email"


class EmailConnector:
    """Own signed email normalization and public TLS SMTP delivery."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke email inbound or outbound protocol handling."""

        if name == "verify_normalize":
            return self._verify(payload)
        if name == "deliver":
            return self._deliver(payload)
        raise ValueError(f"unknown email connector operation: {name}")

    def _verify(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        connector = _connector(payload)
        body = str(payload.get("body") or "")
        headers = _headers(payload.get("headers"))
        secret = self._secret(connector, "connector.inbound.verify", "webhook_secret")
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        supplied = headers.get("x-rumi-email-signature", "")
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise PermissionError("email webhook signature is invalid")
        value = json.loads(body)
        if not isinstance(value, Mapping):
            raise ValueError("email webhook must be an object")
        event_id = str(value.get("message_id") or value.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("email message_id is required")
        return {
            "event_id": event_id,
            "type": "email.message",
            "actor_id": str(value.get("from") or "")[:320],
            "channel_id": str(value.get("mailbox") or value.get("to") or "")[:320],
            "subject": str(value.get("subject") or "")[:998],
            "text": str(value.get("text") or "")[:100_000],
            "in_reply_to": str(value.get("in_reply_to") or "")[:998],
            "attachment_metadata": _attachment_metadata(value.get("attachments")),
        }

    def _deliver(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        arguments = _delivery_arguments(payload)
        self._redeem(payload, arguments)
        connector = arguments["connector"]
        config = _mapping(connector.get("config"))
        host = str(config.get("smtp_host") or "").strip()
        port = int(config.get("smtp_port") or 465)
        _safe_smtp(host, port)
        material = self._material(connector, "connector.outbound.deliver")
        if not material.get("username") or not material.get("password"):
            raise PermissionError("email credential lacks SMTP username or password")
        message = arguments["message"]
        sender = str(message.get("from") or config.get("from_address") or "").strip()
        recipient = str(message.get("to") or "").strip()
        if not sender or not recipient:
            raise ValueError("email from and to are required")
        mail = EmailMessage()
        mail["From"] = sender
        mail["To"] = recipient
        mail["Subject"] = str(message.get("subject") or "")[:998]
        mail["Message-ID"] = f"<{arguments['delivery_id']}@rumi.local>"
        in_reply_to = str(message.get("in_reply_to") or "").strip()
        if in_reply_to:
            mail["In-Reply-To"] = in_reply_to[:998]
        mail.set_content(str(message.get("text") or "")[:500_000])
        context = ssl.create_default_context()
        if port == 465:
            client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=20)
        with client:
            if port == 587:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            client.login(
                str(material.get("username") or ""),
                str(material.get("password") or ""),
            )
            refused = client.send_message(mail)
        return {
            "status": "delivered" if not refused else "failed",
            "refused_recipient_count": len(refused),
        }

    def _material(
        self,
        connector: Mapping[str, Any],
        scope: str,
    ) -> Mapping[str, Any]:
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
        if not isinstance(material, Mapping):
            raise PermissionError("email credential is unavailable")
        return material

    def _secret(
        self,
        connector: Mapping[str, Any],
        scope: str,
        key: str,
    ) -> str:
        material = self._material(connector, scope)
        if not str(material.get(key) or ""):
            raise PermissionError(f"email credential lacks {key}")
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
            raise PermissionError(str(result.get("reason") or "email delivery denied"))


def create_connector_adapter(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the email connector adapter."""

    adapter = EmailConnector(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return adapter.invoke(name, payload)

    return operation


def _safe_smtp(host: str, port: int) -> None:
    if not host or port not in {465, 587}:
        raise PermissionError("SMTP requires a host and TLS port 465 or 587")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise RuntimeError("SMTP host cannot be resolved") from exc
    if not addresses or any(_private(address) for address in addresses):
        raise PermissionError("SMTP host resolves to a private address")


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


def _attachment_metadata(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "name": str(item.get("name") or "")[:255],
            "content_type": str(item.get("content_type") or "")[:120],
            "size": max(0, int(item.get("size") or 0)),
        }
        for item in value[:100]
        if isinstance(item, Mapping)
    ]


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
        raise PermissionError("connector is not bound to email adapter")
    return value


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key).casefold(): str(item) for key, item in value.items()}


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value

