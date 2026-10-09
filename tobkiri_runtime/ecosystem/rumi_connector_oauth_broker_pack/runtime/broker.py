"""PKCE-bound, one-shot connector OAuth coordination over global contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

AUTHORITY = "rumi.service.host.authorize.v1"
REGISTRY_RESOURCE = "rumi.resource.connector.registry.v1"
REGISTRY_ACTION = "rumi.action.connector.registry.v1"
CREDENTIAL_MANAGE = "rumi.action.credential.manage.v1"
OAUTH_PROVIDER = "rumi.service.connector.oauth.provider.v1"
SERVICE_PACK_ID = "rumi_connector_oauth_broker_pack"
REGISTRY_PACK_ID = "rumi_connector_registry_service_pack"
VERSION = "rumi.connector-oauth.v1"
TTL_SECONDS = 10 * 60


class OAuthStateStore:
    """Own pending PKCE flows, never OAuth codes or credential material."""

    def __init__(self, profile_id: str) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(USER_DATA_DIR)
            / "packs"
            / SERVICE_PACK_ID
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "pending-flows.json"
        self.lock_root = self.root / "locks"

    def save(self, state: str, record: Mapping[str, Any]) -> None:
        """Store one hashed-state flow with a short expiry."""

        with NamedLock(self.lock_root, "oauth"):
            value = self._read()
            _prune(value["flows"])
            value["flows"][_hash(state)] = dict(record)
            self._write(value)

    def consume(self, state: str) -> dict[str, Any]:
        """Consume exactly one unexpired state before token exchange."""

        with NamedLock(self.lock_root, "oauth"):
            value = self._read()
            _prune(value["flows"])
            record = value["flows"].pop(_hash(state), None)
            self._write(value)
        if not isinstance(record, Mapping):
            raise PermissionError("OAuth state is missing, expired, or already used")
        return dict(record)

    def cancel(self, state: str) -> bool:
        """Cancel one exact state without exposing its verifier."""

        with NamedLock(self.lock_root, "oauth"):
            value = self._read()
            _prune(value["flows"])
            removed = value["flows"].pop(_hash(state), None) is not None
            self._write(value)
        return removed

    def status(self) -> dict[str, Any]:
        """Return only aggregate pending-flow status."""

        with NamedLock(self.lock_root, "oauth"):
            value = self._read()
            _prune(value["flows"])
            self._write(value)
        return {"profile_id": self.profile_id, "pending_count": len(value["flows"])}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": VERSION, "profile_id": self.profile_id, "flows": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, Mapping)
            or value.get("version") != VERSION
            or value.get("profile_id") != self.profile_id
            or not isinstance(value.get("flows"), Mapping)
        ):
            raise ValueError("OAuth pending state is invalid")
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "flows": dict(value["flows"]),
        }

    def _write(self, value: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".oauth-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class OAuthBroker:
    """Coordinate receipt-gated OAuth begin and one-shot callback completion."""

    def __init__(self, client: Any, profile_id: str) -> None:
        self.client = client
        self.profile_id = validate_profile_id(profile_id)
        self.store = OAuthStateStore(self.profile_id)

    def resource(self, name: str) -> dict[str, Any]:
        """Read redacted OAuth process status."""

        if name != "status":
            raise ValueError(f"unknown OAuth resource operation: {name}")
        return self.store.status()

    def action(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Begin or cancel a receipt-gated OAuth flow."""

        if name == "begin":
            arguments = _begin_arguments(payload)
            self._redeem(payload, name, arguments)
            return self._begin(arguments)
        if name == "cancel":
            arguments = {"state": _required_text(payload.get("state"), "state")}
            self._redeem(payload, name, arguments)
            return {"cancelled": self.store.cancel(arguments["state"])}
        raise ValueError(f"unknown OAuth action: {name}")

    def callback(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Consume a state once and exchange its code through its exact provider."""

        state = _required_text(payload.get("state"), "state")
        code = _required_text(payload.get("code"), "code")
        record = self.store.consume(state)
        provider = _provider(
            self.client.providers(OAUTH_PROVIDER),
            str(record["adapter_id"]),
        )
        exchanged = self.client.invoke(
            OAUTH_PROVIDER,
            "exchange",
            {
                "profile_id": self.profile_id,
                "connector_id": record["connector_id"],
                "connector": record["connector"],
                "redirect_uri": record["redirect_uri"],
                "code": code,
                "code_verifier": record["code_verifier"],
                "client_credential_ref": record["client_credential_ref"],
                "scopes": record["scopes"],
            },
            provider_instance_id=str(provider["provider_instance_id"]),
        )
        result = _exchange_result(exchanged, provider)
        credential = self.client.invoke(
            CREDENTIAL_MANAGE,
            "create",
            {
                "secret_material": result["secret_material"],
                "consumer_pack_id": result["consumer_pack_id"],
                "provider_instance_id": str(record["adapter_id"]),
                "scopes": result["credential_scopes"],
                "label": f"OAuth: {record['connector_id']}",
                "expires_at": result["expires_at"],
            },
        )
        handle = str(credential.get("handle") or "")
        if not handle:
            raise RuntimeError("OAuth credential creation did not return a handle")
        try:
            self._bind_credential(record, handle)
        except Exception:
            self.client.invoke(CREDENTIAL_MANAGE, "revoke", {"handle": handle})
            raise
        return {
            "status": "connected",
            "connector_id": record["connector_id"],
            "adapter_id": record["adapter_id"],
            "credential_configured": True,
        }

    def _begin(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        connector = self._connector(arguments["connector_id"], arguments["adapter_id"])
        provider = _provider(
            self.client.providers(OAUTH_PROVIDER),
            str(arguments["adapter_id"]),
        )
        state = _token()
        verifier = _token()
        challenge = _challenge(verifier)
        prepared = self.client.invoke(
            OAUTH_PROVIDER,
            "prepare",
            {
                "profile_id": self.profile_id,
                "connector_id": arguments["connector_id"],
                "connector": connector,
                "redirect_uri": arguments["redirect_uri"],
                "state": state,
                "code_challenge": challenge,
                "scopes": arguments["scopes"],
                "client_credential_ref": arguments["client_credential_ref"],
            },
            provider_instance_id=str(provider["provider_instance_id"]),
        )
        authorization_url = _authorization_url(prepared)
        self.store.save(
            state,
            {
                "connector_id": arguments["connector_id"],
                "adapter_id": arguments["adapter_id"],
                "connector": connector,
                "redirect_uri": arguments["redirect_uri"],
                "scopes": arguments["scopes"],
                "client_credential_ref": arguments["client_credential_ref"],
                "code_verifier": verifier,
                "expires_at": int(time.time()) + TTL_SECONDS,
            },
        )
        return {
            "status": "pending",
            "authorization_url": authorization_url,
            "state": state,
            "expires_in_seconds": TTL_SECONDS,
        }

    def _connector(self, connector_id: str, adapter_id: str) -> dict[str, Any]:
        value = self.client.invoke(
            REGISTRY_RESOURCE,
            "get",
            {"profile_id": self.profile_id, "connector_id": connector_id},
        )
        if not isinstance(value, Mapping) or not value.get("enabled"):
            raise PermissionError("connector is unknown or disabled")
        if str(value.get("adapter_id") or "") != adapter_id:
            raise PermissionError("connector does not match OAuth provider")
        return {
            "id": connector_id,
            "adapter_id": adapter_id,
            "display_name": str(value.get("display_name") or ""),
            "config": _public_config(value.get("config")),
        }

    def _bind_credential(self, record: Mapping[str, Any], handle: str) -> None:
        snapshot = self.client.invoke(
            REGISTRY_RESOURCE,
            "list",
            {"profile_id": self.profile_id},
        )
        arguments = {
            "connector_id": str(record["connector_id"]),
            "expected_revision": int(snapshot.get("revision") or 0),
            "updates": {"credential_ref": handle},
        }
        issued = self.client.invoke(
            AUTHORITY,
            "authorize",
            {
                "service_pack_id": REGISTRY_PACK_ID,
                "operation": "connector.registry.update",
                "authority": "connector.registry.manage",
                "caller_id": "connector.oauth.broker",
                "caller_pack_id": SERVICE_PACK_ID,
                "caller_function_id": "connector.oauth.callback",
                "profile_id": self.profile_id,
                "workspace_id": "",
                "session_id": "",
                "arguments": arguments,
                "approval_required": False,
            },
        )
        if not issued.get("authorized") or not issued.get("receipt"):
            raise PermissionError(
                str(issued.get("reason") or "connector update denied")
            )
        self.client.invoke(
            REGISTRY_ACTION,
            "update",
            {
                **arguments,
                "profile_id": self.profile_id,
                "authority_receipt": str(issued["receipt"]),
                "caller_id": "connector.oauth.broker",
                "caller_pack_id": SERVICE_PACK_ID,
                "caller_function_id": "connector.oauth.callback",
                "session_id": "",
            },
        )

    def _redeem(
        self,
        payload: Mapping[str, Any],
        name: str,
        arguments: Mapping[str, Any],
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": f"connector.oauth.{name}",
                "authority": "connector.oauth.manage",
                "caller_id": str(payload.get("caller_id") or ""),
                "caller_pack_id": str(payload.get("caller_pack_id") or ""),
                "caller_function_id": str(payload.get("caller_function_id") or ""),
                "profile_id": self.profile_id,
                "workspace_id": "",
                "session_id": str(payload.get("session_id") or ""),
                "arguments": dict(arguments),
            },
        )
        if not result.get("authorized"):
            raise PermissionError(str(result.get("reason") or "OAuth action denied"))


def create_oauth_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create redacted OAuth resource operations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        profile_id = str(payload.get("profile_id") or "default")
        return OAuthBroker(client, profile_id).resource(name)

    return operation


def create_oauth_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated OAuth begin and cancellation operations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return OAuthBroker(client, str(payload.get("profile_id") or "default")).action(
            name,
            payload,
        )

    return operation


def create_oauth_transport(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create one-shot OAuth callback transport operations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "callback":
            raise ValueError(f"unknown OAuth transport operation: {name}")
        profile_id = str(payload.get("profile_id") or "default")
        return OAuthBroker(client, profile_id).callback(payload)

    return operation


def _begin_arguments(payload: Mapping[str, Any]) -> dict[str, Any]:
    redirect_uri = _required_text(payload.get("redirect_uri"), "redirect_uri")
    _validate_redirect_uri(redirect_uri)
    scopes = sorted(
        {_required_text(item, "scope") for item in payload.get("scopes") or []}
    )
    if not scopes:
        raise ValueError("at least one OAuth scope is required")
    return {
        "connector_id": _required_text(payload.get("connector_id"), "connector_id"),
        "adapter_id": _required_text(payload.get("adapter_id"), "adapter_id"),
        "redirect_uri": redirect_uri,
        "client_credential_ref": _required_text(
            payload.get("client_credential_ref"),
            "client_credential_ref",
        ),
        "scopes": scopes,
    }


def _provider(
    providers: tuple[dict[str, Any], ...],
    instance_key: str,
) -> Mapping[str, Any]:
    matches = [
        item
        for item in providers
        if str(item.get("instance_key") or "") == instance_key
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one selected OAuth provider for {instance_key}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _exchange_result(value: Any, provider: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError("OAuth provider returned an invalid exchange result")
    material = value.get("secret_material")
    scopes = value.get("credential_scopes")
    if not isinstance(material, Mapping) or not material:
        raise RuntimeError("OAuth provider did not return credential material")
    if not isinstance(scopes, list) or not scopes:
        raise RuntimeError("OAuth provider did not return credential scopes")
    consumer_pack_id = str(
        value.get("consumer_pack_id") or provider.get("source_pack_id") or ""
    )
    if not consumer_pack_id:
        raise RuntimeError("OAuth provider did not identify credential consumer")
    expires_at = value.get("expires_at")
    if expires_at is not None:
        expires_at = float(expires_at)
    return {
        "secret_material": dict(material),
        "credential_scopes": [str(item) for item in scopes],
        "consumer_pack_id": consumer_pack_id,
        "expires_at": expires_at,
    }


def _authorization_url(value: Any) -> str:
    if not isinstance(value, Mapping):
        raise RuntimeError("OAuth provider returned an invalid authorization URL")
    url = str(value.get("authorization_url") or "")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
        raise PermissionError("OAuth authorization URL must be HTTPS without fragment")
    return url


def _validate_redirect_uri(value: str) -> None:
    parsed = urlsplit(value)
    local = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "::1",
        "localhost",
    }
    custom = parsed.scheme == "rumi" and bool(parsed.netloc)
    secure = parsed.scheme == "https" and bool(parsed.netloc)
    if not (local or custom or secure) or parsed.fragment:
        raise ValueError("OAuth redirect_uri is not allowed")


def _public_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    secret_parts = ("credential", "oauth", "password", "secret", "signature", "token")
    return {
        str(key): _public_value(item)
        for key, item in value.items()
        if not any(part in str(key).casefold() for part in secret_parts)
    }


def _public_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _public_config(value)
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 4096 or "\x00" in text:
        raise ValueError(f"{label} is invalid")
    return text


def _token() -> str:
    return secrets.token_urlsafe(48)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _prune(flows: dict[str, Any]) -> None:
    now = int(time.time())
    for key in [
        key
        for key, value in flows.items()
        if not isinstance(value, Mapping) or int(value.get("expires_at") or 0) <= now
    ]:
        flows.pop(key, None)

