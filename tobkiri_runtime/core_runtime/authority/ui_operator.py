"""Signed UI-operator provenance for authority approvals."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

from ..host_contract import host_contract_value


UI_OPERATOR_ORIGIN = "tauri_webview_window"
UI_OPERATOR_WINDOW_LABEL = "authority-approval"
UI_OPERATOR_VERSION = 1
BROWSER_UI_OPERATOR_VERSION = 2
UI_OPERATOR_TTL_SECONDS = 180


def _operator_message(payload: dict[str, Any]) -> bytes:
    version = int(payload.get("version") or UI_OPERATOR_VERSION)
    fields = [
            f"v{version}",
            str(payload.get("origin") or ""),
            str(payload.get("window_label") or ""),
            str(payload.get("request_id") or ""),
            str(int(payload.get("issued_at") or 0)),
            str(int(payload.get("expires_at") or 0)),
            str(payload.get("nonce") or ""),
        ]
    if version == BROWSER_UI_OPERATOR_VERSION:
        fields.extend(
            [
                str(payload.get("principal_id") or ""),
                str(payload.get("device_id") or ""),
                str(payload.get("browser_origin") or ""),
                str(payload.get("browser_window_id") or ""),
                str(payload.get("exchange_nonce") or ""),
            ]
        )
    return "\n".join(fields).encode("utf-8")


def _signing_secret() -> bytes:
    return host_contract_value("panel_bootstrap_secret").encode("utf-8")


def sign_ui_operator(
    request_id: str,
    *,
    now: int | None = None,
    nonce: str | None = None,
    ttl_seconds: int = UI_OPERATOR_TTL_SECONDS,
    browser_audience: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the same signed provenance payload produced by the Tauri window."""
    issued_at = int(now if now is not None else time.time())
    expires_at = issued_at + max(15, int(ttl_seconds or UI_OPERATOR_TTL_SECONDS))
    payload = {
        "version": (
            BROWSER_UI_OPERATOR_VERSION if browser_audience else UI_OPERATOR_VERSION
        ),
        "kind": "ui_operator",
        "origin": UI_OPERATOR_ORIGIN,
        "window_label": UI_OPERATOR_WINDOW_LABEL,
        "request_id": str(request_id or ""),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": nonce or secrets.token_urlsafe(24),
    }
    if browser_audience:
        payload.update(
            {
                "principal_id": str(browser_audience.get("principal_id") or ""),
                "device_id": str(browser_audience.get("device_id") or ""),
                "browser_origin": str(browser_audience.get("browser_origin") or ""),
                "browser_window_id": str(
                    browser_audience.get("browser_window_id") or ""
                ),
                "exchange_nonce": str(
                    browser_audience.get("exchange_nonce") or ""
                ),
            }
        )
    secret = _signing_secret()
    if not secret:
        payload["signature"] = ""
        return payload
    payload["signature"] = hmac.new(secret, _operator_message(payload), hashlib.sha256).hexdigest()
    return payload


def verify_ui_operator(
    payload: Any,
    *,
    request_id: str,
    now: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Verify a Tauri approval-window provenance payload."""
    if not isinstance(payload, dict):
        return False, "ui_operator is required", {}

    secret = _signing_secret()
    if not secret:
        return False, "ui_operator signing secret is unavailable", {}

    normalized = {
        "version": payload.get("version"),
        "kind": str(payload.get("kind") or ""),
        "origin": str(payload.get("origin") or ""),
        "window_label": str(payload.get("window_label") or ""),
        "request_id": str(payload.get("request_id") or ""),
        "issued_at": payload.get("issued_at"),
        "expires_at": payload.get("expires_at"),
        "nonce": str(payload.get("nonce") or ""),
    }
    signature = str(payload.get("signature") or "")
    if normalized["version"] not in {
        UI_OPERATOR_VERSION,
        BROWSER_UI_OPERATOR_VERSION,
    } or normalized["kind"] != "ui_operator":
        return False, "ui_operator version is invalid", {}
    if normalized["origin"] != UI_OPERATOR_ORIGIN or normalized["window_label"] != UI_OPERATOR_WINDOW_LABEL:
        return False, "ui_operator source is invalid", {}
    if normalized["request_id"] != str(request_id or ""):
        return False, "ui_operator request mismatch", {}
    if not normalized["nonce"]:
        return False, "ui_operator nonce is missing", {}
    if normalized["version"] == BROWSER_UI_OPERATOR_VERSION:
        for key in (
            "principal_id",
            "device_id",
            "browser_origin",
            "browser_window_id",
            "exchange_nonce",
        ):
            normalized[key] = str(payload.get(key) or "")
            if not normalized[key]:
                return False, f"ui_operator {key} is missing", {}

    try:
        issued_at = int(normalized["issued_at"] or 0)
        expires_at = int(normalized["expires_at"] or 0)
    except (TypeError, ValueError):
        return False, "ui_operator timestamps are invalid", {}
    normalized["issued_at"] = issued_at
    normalized["expires_at"] = expires_at

    current = int(now if now is not None else time.time())
    if expires_at <= current:
        return False, "ui_operator expired", {}
    if issued_at > current + 30:
        return False, "ui_operator issued_at is invalid", {}

    expected = hmac.new(secret, _operator_message(normalized), hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        return False, "ui_operator signature is invalid", {}

    return True, "", normalized


def ui_operator_audit_record(payload: dict[str, Any]) -> dict[str, Any]:
    nonce = str(payload.get("nonce") or "")
    return {
        "ui_operator": True,
        "origin": payload.get("origin"),
        "window_label": payload.get("window_label"),
        "issued_at": payload.get("issued_at"),
        "expires_at": payload.get("expires_at"),
        "nonce_hash": hashlib.sha256(nonce.encode("utf-8")).hexdigest() if nonce else "",
        "browser_bound": payload.get("version") == BROWSER_UI_OPERATOR_VERSION,
        "principal_id": payload.get("principal_id"),
        "device_id": payload.get("device_id"),
        "browser_origin": payload.get("browser_origin"),
        "browser_window_id": payload.get("browser_window_id"),
    }
