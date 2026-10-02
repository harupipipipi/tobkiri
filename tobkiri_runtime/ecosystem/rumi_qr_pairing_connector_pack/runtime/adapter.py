"""One-time QR pairing challenges and optional UI presentation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

AUTHORITY = "rumi.service.host.authorize.v1"
CREDENTIAL = "rumi.service.credential.resolve.v1"
QR_UI = "rumi.ui.qr.present.v1"
SERVICE_PACK_ID = "rumi_qr_pairing_connector_pack"
ADAPTER_ID = "qr_pairing"
VERSION = "rumi.qr-pairing-nonces.v1"
_MAX_SKEW_MS = 5 * 60 * 1000


class QrPairingConnector:
    """Own QR challenge replay state without frontend implementation."""

    def __init__(self, client: Any, profile_id: str) -> None:
        self.client = client
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(USER_DATA_DIR)
            / "packs"
            / SERVICE_PACK_ID
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "nonces.json"
        self.lock_root = self.root / "locks"

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Verify an inbound challenge or present a signed outbound QR payload."""

        if name == "verify_normalize":
            return self._verify(payload)
        if name == "deliver":
            return self._present(payload)
        raise ValueError(f"unknown QR pairing operation: {name}")

    def _verify(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        connector = _connector(payload)
        body = str(payload.get("body") or "")
        value = json.loads(body)
        if not isinstance(value, Mapping):
            raise ValueError("QR pairing payload must be an object")
        qr_id = str(value.get("qr_id") or "").strip()
        nonce = str(value.get("nonce") or "").strip()
        device_id = str(value.get("device_id") or "").strip()
        timestamp = int(value.get("timestamp_ms") or 0)
        if not qr_id or not nonce or not device_id:
            raise ValueError("QR pairing identity and nonce are required")
        if abs(_now_ms() - timestamp) > _MAX_SKEW_MS:
            raise PermissionError("QR pairing timestamp is outside replay window")
        signature = _headers(payload.get("headers")).get("x-rumi-qr-signature", "")
        expected = _signature(
            self._secret(connector, "connector.inbound.verify"),
            body,
        )
        if not signature or not hmac.compare_digest(signature, expected):
            raise PermissionError("QR pairing signature is invalid")
        self._consume(qr_id, nonce, timestamp)
        return {
            "event_id": str(value.get("event_id") or f"{qr_id}:{nonce}"),
            "type": "qr.pairing",
            "actor_id": device_id[:255],
            "channel_id": qr_id[:255],
            "text": "",
            "payload": _sanitize(value.get("payload") or {}),
        }

    def _present(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        arguments = _delivery_arguments(payload)
        self._redeem(payload, arguments)
        connector = arguments["connector"]
        config = _mapping(connector.get("config"))
        qr_id = str(config.get("qr_id") or uuid.uuid4())
        value = {
            "event_id": arguments["delivery_id"],
            "qr_id": qr_id,
            "nonce": str(uuid.uuid4()),
            "timestamp_ms": _now_ms(),
            "payload": arguments["message"],
        }
        body = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        signature = _signature(
            self._secret(connector, "connector.outbound.deliver"),
            body,
        )
        providers = self.client.providers(QR_UI)
        if len(providers) != 1:
            raise RuntimeError(
                f"expected one selected QR presenter; found {len(providers)}"
            )
        result = self.client.invoke(
            QR_UI,
            "present",
            {
                "profile_id": self.profile_id,
                "qr_id": qr_id,
                "body": body,
                "signature": signature,
                "expires_at_ms": value["timestamp_ms"] + _MAX_SKEW_MS,
            },
            provider_instance_id=str(providers[0]["provider_instance_id"]),
        )
        succeeded = isinstance(result, Mapping) and result.get("status") in {
            "ok",
            "accepted",
            "presented",
        }
        return {
            "status": "delivered" if succeeded else "failed",
            "presentation": _bounded(result),
        }

    def _consume(self, qr_id: str, nonce: str, timestamp: int) -> None:
        key = hashlib.sha256(f"{qr_id}\0{nonce}".encode("utf-8")).hexdigest()
        with NamedLock(self.lock_root, "qr-pairing"):
            state = self._read()
            if key in state["nonces"]:
                raise PermissionError("QR pairing nonce was already consumed")
            state["nonces"][key] = {
                "consumed_at_ms": _now_ms(),
                "payload_timestamp_ms": timestamp,
            }
            _prune(state["nonces"])
            _atomic_json(self.path, state)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": VERSION, "nonces": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("version") != VERSION:
            raise ValueError("QR pairing nonce state is invalid")
        nonces = value.get("nonces")
        if not isinstance(nonces, Mapping):
            raise ValueError("QR pairing nonce records are invalid")
        return {"version": VERSION, "nonces": dict(nonces)}

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
        if not isinstance(material, Mapping) or not str(
            material.get("pairing_secret") or ""
        ):
            raise PermissionError("QR pairing credential lacks pairing_secret")
        return str(material["pairing_secret"])

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
                "profile_id": self.profile_id,
                "workspace_id": "",
                "session_id": str(payload.get("session_id") or ""),
                "arguments": dict(arguments),
            },
        )
        if not result.get("authorized"):
            raise PermissionError(str(result.get("reason") or "QR presentation denied"))


def create_connector_adapter(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the QR pairing connector adapter."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return QrPairingConnector(
            client,
            str(payload.get("profile_id") or "default"),
        ).invoke(name, payload)

    return operation


def _signature(secret: str, body: str) -> str:
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()


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
        raise PermissionError("connector is not bound to QR pairing adapter")
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


def _bounded(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        return {
            "status": "truncated",
            "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        }
    return json.loads(encoded)


def _prune(nonces: dict[str, Any]) -> None:
    if len(nonces) <= 10_000:
        return
    ordered = sorted(nonces.items(), key=lambda item: int(item[1]["consumed_at_ms"]))
    for key, _value in ordered[: len(nonces) - 10_000]:
        nonces.pop(key, None)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".qr-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

