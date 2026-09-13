"""Verification for native, one-shot Launcher coding approval decisions."""

from __future__ import annotations

import hashlib
import hmac
import base64
import threading
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from domain.host_bridge.viewer_broker_client import ViewerBrokerClient


class CodingUiOperatorError(ValueError):
    pass


_LOCK = threading.RLock()
_USED_NONCES: set[str] = set()


def _message(payload: dict[str, Any]) -> bytes:
    return "\n".join(
        [
            f"v{int(payload.get('version') or 0)}",
            str(payload.get("origin") or ""),
            str(payload.get("instance_nonce") or ""),
            str(payload.get("window_label") or ""),
            str(payload.get("request_id") or ""),
            str(payload.get("expected_digest") or ""),
            str(payload.get("decision") or ""),
            str(int(payload.get("issued_at") or 0)),
            str(int(payload.get("expires_at") or 0)),
            str(payload.get("nonce") or ""),
        ]
    ).encode("utf-8")


def verify_coding_ui_operator(
    payload: Any,
    *,
    request_id: str,
    expected_digest: str,
    decision: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CodingUiOperatorError("native ui_operator is required")
    normalized = {
        "version": payload.get("version"),
        "kind": str(payload.get("kind") or ""),
        "origin": str(payload.get("origin") or ""),
        "instance_nonce": str(payload.get("instance_nonce") or ""),
        "window_label": str(payload.get("window_label") or ""),
        "request_id": str(payload.get("request_id") or ""),
        "expected_digest": str(payload.get("expected_digest") or ""),
        "decision": str(payload.get("decision") or ""),
        "issued_at": payload.get("issued_at"),
        "expires_at": payload.get("expires_at"),
        "nonce": str(payload.get("nonce") or ""),
    }
    if (
        normalized["version"] != 4
        or normalized["kind"] != "coding_ui_operator"
        or normalized["origin"] != "tauri_webview_window"
        or normalized["window_label"] != "defaultspack-main"
    ):
        raise CodingUiOperatorError("native ui_operator provenance is invalid")
    if normalized["request_id"] != request_id:
        raise CodingUiOperatorError("native ui_operator request mismatch")
    if not hmac.compare_digest(normalized["expected_digest"], expected_digest):
        raise CodingUiOperatorError("native ui_operator digest mismatch")
    if normalized["decision"] != decision:
        raise CodingUiOperatorError("native ui_operator decision mismatch")
    try:
        normalized["issued_at"] = int(normalized["issued_at"] or 0)
        normalized["expires_at"] = int(normalized["expires_at"] or 0)
    except (TypeError, ValueError) as exc:
        raise CodingUiOperatorError("native ui_operator timestamps are invalid") from exc
    now = int(time.time())
    if (
        normalized["issued_at"] > now + 5
        or normalized["expires_at"] <= now
        or normalized["expires_at"] > normalized["issued_at"] + 60
    ):
        raise CodingUiOperatorError("native ui_operator expired")
    broker = ViewerBrokerClient.from_environment()
    if (
        not broker.attestation_public_key
        or not broker.instance_nonce
        or normalized["instance_nonce"] != broker.instance_nonce
    ):
        raise CodingUiOperatorError("native ui_operator Launcher identity is unavailable")
    signature = str(payload.get("signature") or "")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.urlsafe_b64decode(broker.attestation_public_key + "===")
        )
        public_key.verify(
            base64.urlsafe_b64decode(signature + "==="),
            _message(normalized),
        )
    except (InvalidSignature, ValueError):
        raise CodingUiOperatorError("native ui_operator signature is invalid")
    nonce_key = hashlib.sha256(normalized["nonce"].encode("utf-8")).hexdigest()
    with _LOCK:
        if nonce_key in _USED_NONCES:
            raise CodingUiOperatorError("native ui_operator has already been used")
        _USED_NONCES.add(nonce_key)
    return normalized
