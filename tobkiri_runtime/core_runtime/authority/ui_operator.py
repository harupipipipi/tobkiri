"""Signed UI-operator provenance for authority approvals."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Any

from ..host_contract import host_contract_value

UI_OPERATOR_ORIGIN = "tauri_webview_window"
UI_OPERATOR_WINDOW_LABEL = "authority-approval"
UI_OPERATOR_VERSION = 1
BROWSER_UI_OPERATOR_VERSION = 2
INTERACTIVE_UI_OPERATOR_VERSION = 3
UI_OPERATOR_TTL_SECONDS = 180
_UNTAGGED_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def _operator_message(payload: dict[str, Any]) -> bytes:
    version = int(payload.get("version") or UI_OPERATOR_VERSION)
    fields = [
        f"v{version}",
        str(payload.get("origin") or ""),
        str(payload.get("window_label") or ""),
        str(payload.get("request_id") or ""),
    ]
    if version == INTERACTIVE_UI_OPERATOR_VERSION:
        fields.extend(
            [
                str(payload.get("decision") or ""),
                str(payload.get("request_snapshot_digest") or ""),
                str(payload.get("typed_confirmation_digest") or ""),
            ]
        )
    fields.extend(
        [
            str(int(payload.get("issued_at") or 0)),
            str(int(payload.get("expires_at") or 0)),
            str(payload.get("nonce") or ""),
        ]
    )
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
    decision: str | None = None,
    request_snapshot_digest: str | None = None,
    typed_confirmation_digest: str | None = None,
) -> dict[str, Any]:
    """Build a signed provenance payload produced by a Host-owned UI.

    V1 and browser V2 remain available for their established call sites.  V3
    is deliberately selected only when the caller supplies the complete
    interactive-approval decision binding; a partial binding is rejected so
    tests cannot accidentally mint an action-agnostic v3 proof.
    """

    interactive_fields_supplied = any(
        value is not None
        for value in (
            decision,
            request_snapshot_digest,
            typed_confirmation_digest,
        )
    )
    if interactive_fields_supplied:
        if browser_audience is not None:
            raise ValueError("interactive ui_operator cannot be browser-bound")
        if decision not in {"approve", "deny"}:
            raise ValueError("interactive ui_operator decision is invalid")
        if not _is_untagged_sha256(request_snapshot_digest):
            raise ValueError("interactive ui_operator request snapshot is invalid")
        if typed_confirmation_digest is not None and not _is_untagged_sha256(
            typed_confirmation_digest
        ):
            raise ValueError("interactive ui_operator confirmation digest is invalid")
        if decision == "deny" and typed_confirmation_digest is not None:
            raise ValueError("interactive ui_operator deny confirmation is invalid")
    issued_at = int(now if now is not None else time.time())
    expires_at = issued_at + max(15, int(ttl_seconds or UI_OPERATOR_TTL_SECONDS))
    payload = {
        "version": (
            INTERACTIVE_UI_OPERATOR_VERSION
            if interactive_fields_supplied
            else (
                BROWSER_UI_OPERATOR_VERSION if browser_audience else UI_OPERATOR_VERSION
            )
        ),
        "kind": "ui_operator",
        "origin": UI_OPERATOR_ORIGIN,
        "window_label": UI_OPERATOR_WINDOW_LABEL,
        "request_id": str(request_id or ""),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": nonce or secrets.token_urlsafe(24),
    }
    if interactive_fields_supplied:
        payload.update(
            {
                "decision": decision,
                "request_snapshot_digest": request_snapshot_digest,
                "typed_confirmation_digest": typed_confirmation_digest,
            }
        )
    if browser_audience:
        payload.update(
            {
                "principal_id": str(browser_audience.get("principal_id") or ""),
                "device_id": str(browser_audience.get("device_id") or ""),
                "browser_origin": str(browser_audience.get("browser_origin") or ""),
                "browser_window_id": str(
                    browser_audience.get("browser_window_id") or ""
                ),
                "exchange_nonce": str(browser_audience.get("exchange_nonce") or ""),
            }
        )
    secret = _signing_secret()
    if not secret:
        payload["signature"] = ""
        return payload
    payload["signature"] = hmac.new(
        secret, _operator_message(payload), hashlib.sha256
    ).hexdigest()
    return payload


def verify_ui_operator(
    payload: Any,
    *,
    request_id: str,
    now: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Verify a legacy v1 or browser v2 approval-window provenance payload."""

    return _verify_ui_operator(
        payload,
        request_id=request_id,
        now=now,
        allowed_versions={UI_OPERATOR_VERSION, BROWSER_UI_OPERATOR_VERSION},
    )


def verify_interactive_ui_operator(
    payload: Any,
    *,
    request_id: str,
    decision: str,
    request_snapshot_digest: str,
    typed_confirmation_digest: str | None,
    now: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Verify a v3 proof for one exact interactive approval decision.

    The expected values come from the immutable Host request, never from the
    webview payload.  ``typed_confirmation_digest`` is intentionally null for
    a deny decision and for approvals that do not require typed confirmation.
    """

    if decision not in {"approve", "deny"}:
        return False, "interactive ui_operator decision is invalid", {}
    if not _is_untagged_sha256(request_snapshot_digest):
        return False, "interactive ui_operator request snapshot is invalid", {}
    if typed_confirmation_digest is not None and not _is_untagged_sha256(
        typed_confirmation_digest
    ):
        return False, "interactive ui_operator confirmation digest is invalid", {}
    if decision == "deny" and typed_confirmation_digest is not None:
        return False, "interactive ui_operator deny confirmation is invalid", {}

    verified, reason, normalized = _verify_ui_operator(
        payload,
        request_id=request_id,
        now=now,
        allowed_versions={INTERACTIVE_UI_OPERATOR_VERSION},
    )
    if not verified:
        return verified, reason, normalized
    if normalized["decision"] != decision:
        return False, "interactive ui_operator decision mismatch", {}
    if not hmac.compare_digest(
        normalized["request_snapshot_digest"], request_snapshot_digest
    ):
        return False, "interactive ui_operator request snapshot mismatch", {}
    supplied_confirmation_digest = normalized["typed_confirmation_digest"]
    if typed_confirmation_digest is None:
        if supplied_confirmation_digest is not None:
            return False, "interactive ui_operator confirmation mismatch", {}
    elif not isinstance(supplied_confirmation_digest, str) or not hmac.compare_digest(
        supplied_confirmation_digest, typed_confirmation_digest
    ):
        return False, "interactive ui_operator confirmation mismatch", {}
    return True, "", normalized


def _verify_ui_operator(
    payload: Any,
    *,
    request_id: str,
    now: int | None,
    allowed_versions: set[int],
) -> tuple[bool, str, dict[str, Any]]:
    """Verify the shared signed envelope for an explicit version set."""

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
    if (
        normalized["version"] not in allowed_versions
        or normalized["kind"] != "ui_operator"
    ):
        return False, "ui_operator version is invalid", {}
    if (
        normalized["origin"] != UI_OPERATOR_ORIGIN
        or normalized["window_label"] != UI_OPERATOR_WINDOW_LABEL
    ):
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
    elif normalized["version"] == INTERACTIVE_UI_OPERATOR_VERSION:
        normalized["decision"] = str(payload.get("decision") or "")
        normalized["request_snapshot_digest"] = str(
            payload.get("request_snapshot_digest") or ""
        )
        raw_confirmation_digest = payload.get("typed_confirmation_digest")
        normalized["typed_confirmation_digest"] = raw_confirmation_digest
        if normalized["decision"] not in {"approve", "deny"}:
            return False, "interactive ui_operator decision is invalid", {}
        if not _is_untagged_sha256(normalized["request_snapshot_digest"]):
            return False, "interactive ui_operator request snapshot is invalid", {}
        if raw_confirmation_digest is not None and (
            not isinstance(raw_confirmation_digest, str)
            or not _is_untagged_sha256(raw_confirmation_digest)
        ):
            return False, "interactive ui_operator confirmation digest is invalid", {}

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

    expected = hmac.new(
        secret, _operator_message(normalized), hashlib.sha256
    ).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        return False, "ui_operator signature is invalid", {}

    return True, "", normalized


def ui_operator_audit_record(payload: dict[str, Any]) -> dict[str, Any]:
    nonce = str(payload.get("nonce") or "")
    record = {
        "ui_operator": True,
        "version": payload.get("version"),
        "origin": payload.get("origin"),
        "window_label": payload.get("window_label"),
        "issued_at": payload.get("issued_at"),
        "expires_at": payload.get("expires_at"),
        "nonce_hash": (
            hashlib.sha256(nonce.encode("utf-8")).hexdigest() if nonce else ""
        ),
        "browser_bound": payload.get("version") == BROWSER_UI_OPERATOR_VERSION,
        "principal_id": payload.get("principal_id"),
        "device_id": payload.get("device_id"),
        "browser_origin": payload.get("browser_origin"),
        "browser_window_id": payload.get("browser_window_id"),
    }
    if payload.get("version") == INTERACTIVE_UI_OPERATOR_VERSION:
        record.update(
            {
                "decision": payload.get("decision"),
                "request_snapshot_digest": payload.get("request_snapshot_digest"),
                "typed_confirmation_digest": payload.get("typed_confirmation_digest"),
            }
        )
    return record


def _is_untagged_sha256(value: object) -> bool:
    """Return whether ``value`` is the wire's exact lowercase SHA-256 hex."""

    return isinstance(value, str) and _UNTAGGED_SHA256_RE.fullmatch(value) is not None
