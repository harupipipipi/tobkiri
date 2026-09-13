"""Discord interaction verification, normalization, and message delivery."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
CREDENTIAL = "rumi.service.credential.resolve.v1"
SERVICE_PACK_ID = "rumi_discord_connector_pack"
ADAPTER_ID = "discord"
_SNOWFLAKE = re.compile(r"^[0-9]{1,32}$")


class DiscordConnector:
    """Own only Discord protocol verification and REST delivery."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke Discord inbound or outbound protocol handling."""

        if name == "verify_normalize":
            return self._verify(payload)
        if name == "deliver":
            return self._deliver(payload)
        raise ValueError(f"unknown Discord connector operation: {name}")

    def _verify(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        connector = _connector(payload)
        headers = _headers(payload.get("headers"))
        body = str(payload.get("body") or "")
        signature = headers.get("x-signature-ed25519", "")
        timestamp = headers.get("x-signature-timestamp", "")
        public_key = self._secret(
            connector,
            "connector.inbound.verify",
            "application_public_key",
        )
        _verify_ed25519(public_key, signature, timestamp + body)
        value = json.loads(body)
        if not isinstance(value, Mapping):
            raise ValueError("Discord interaction must be an object")
        event_id = str(value.get("id") or "").strip()
        if not event_id:
            raise ValueError("Discord interaction ID is required")
        member = value.get("member") if isinstance(value.get("member"), Mapping) else {}
        user = member.get("user") if isinstance(member.get("user"), Mapping) else {}
        if not user and isinstance(value.get("user"), Mapping):
            user = value["user"]
        data = value.get("data") if isinstance(value.get("data"), Mapping) else {}
        return {
            "event_id": event_id,
            "type": f"interaction.{int(value.get('type') or 0)}",
            "actor_id": str(user.get("id") or "")[:32],
            "channel_id": str(value.get("channel_id") or "")[:32],
            "guild_id": str(value.get("guild_id") or "")[:32],
            "text": str(data.get("name") or data.get("custom_id") or "")[:4_000],
            "interaction_token": str(value.get("token") or "")[:255],
        }

    def _deliver(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        arguments = _delivery_arguments(payload)
        self._redeem(payload, arguments)
        token = self._secret(
            arguments["connector"],
            "connector.outbound.deliver",
            "bot_token",
        )
        message = arguments["message"]
        channel_id = str(message.get("channel_id") or "")
        if not _SNOWFLAKE.fullmatch(channel_id):
            raise ValueError("Discord channel_id is invalid")
        text = str(message.get("text") or "").strip()
        if not text:
            raise ValueError("Discord text is required")
        endpoint = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps({"content": text[:2_000]}, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bot {token}",
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
            raise RuntimeError("Discord response exceeds size limit")
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
            raise PermissionError(f"Discord credential lacks {key}")
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
            raise PermissionError(str(result.get("reason") or "Discord delivery denied"))


def create_connector_adapter(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the Discord connector adapter."""

    adapter = DiscordConnector(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return adapter.invoke(name, payload)

    return operation


def _verify_ed25519(public_key: str, signature: str, message: str) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise RuntimeError("Discord Ed25519 verifier is unavailable") from exc
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
        key.verify(bytes.fromhex(signature), message.encode("utf-8"))
    except (ValueError, InvalidSignature) as exc:
        raise PermissionError("Discord signature is invalid") from exc


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
        raise PermissionError("connector is not bound to Discord adapter")
    return value


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key).casefold(): str(item) for key, item in value.items()}


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value

