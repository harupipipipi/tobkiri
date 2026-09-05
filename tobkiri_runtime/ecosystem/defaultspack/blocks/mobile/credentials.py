"""Secure provider credential transfer between the PC and a paired device."""

from __future__ import annotations

import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from blocks.p2p._helpers import settings_from
from core_runtime.profile_credentials import active_profile_id
from core_runtime.runtime_audit_helpers import audit_event
from domain.ai_client.api_key_store import provider_api_metadata, read_provider_api_key
from domain.mobile.contract import mobile_feature_enabled
from domain.p2p.credential_transfer import CredentialTransferStore
from domain.p2p.device_store import DeviceStore


_TRANSFER_ID_RE = re.compile(r"^ctr_[0-9a-f]{32}$")


def _merged(input_data: dict) -> dict:
    if not isinstance(input_data, dict):
        return {}
    merged: dict = {}
    for container_key in ("query_params", "params", "body", "path_params", "query"):
        value = input_data.get(container_key)
        if isinstance(value, dict):
            merged.update(value)
    for key, value in input_data.items():
        if key not in {"query_params", "params", "body", "path_params", "query"}:
            merged[key] = value
    return merged


def _authenticated_device_id(context) -> str:
    if not isinstance(context, dict):
        return ""
    return str(context.get("_authenticated_device_id") or context.get("authenticated_device_id") or "").strip()


def _profile_id(input_data: dict, context) -> str:
    values = [
        (context or {}).get("profile_id") if isinstance(context, dict) else None,
        active_profile_id(),
    ]
    for value in values:
        if str(value or "").strip():
            return str(value).strip()
    return "default"


def _store(input_data, context) -> CredentialTransferStore:
    return CredentialTransferStore(settings_from(input_data, context).store_path)


def _audit(context, action: str, record: dict[str, Any], *, success: bool = True) -> None:
    audit_event(context, action, {
        "success": success,
        "transfer_id": record.get("transfer_id"),
        "status": record.get("status"),
        "device_id": record.get("device_id"),
        "profile_id": record.get("profile_id"),
        "provider_id": record.get("provider_id"),
        "api_id": record.get("api_id"),
    })


def _audit_failure(context, action: str, args: dict[str, Any], exc: Exception) -> None:
    """Audit an attempt without copying attacker-controlled text or secrets."""
    if isinstance(exc, KeyError):
        outcome = "not_found"
    elif "recipient proof" in str(exc).lower():
        outcome = "recipient_proof_rejected"
    elif isinstance(exc, PermissionError):
        outcome = "forbidden"
    elif "expired" in str(exc).lower():
        outcome = "expired"
    else:
        outcome = "invalid_state"
    fields: dict[str, Any] = {
        "success": False,
        "outcome": outcome,
    }
    transfer_id = str(args.get("transfer_id") or args.get("id") or "")
    if _TRANSFER_ID_RE.fullmatch(transfer_id):
        fields["transfer_id"] = transfer_id
    audit_event(context, action, fields)


def _audit_claimed_expiries(context, store: CredentialTransferStore, **scope: str) -> None:
    for record in store.claim_expiry_audits(**scope):
        _audit(context, "credential_transfer.expired", record)


def _failure(exc: Exception):
    if isinstance(exc, KeyError):
        return error("transfer not found", "NOT_FOUND")
    message = str(exc)
    if "recipient proof" in message:
        return error("recipient proof rejected", "RECIPIENT_PROOF_REJECTED")
    if isinstance(exc, PermissionError):
        return error("credential transfer is forbidden", "FORBIDDEN")
    if "expired" in message:
        return error("transfer expired", "EXPIRED")
    return error(message or "credential transfer failed", "INVALID_STATE")


def create_transfer(input_data, context=None):
    if _authenticated_device_id(context):
        return error("credential transfers must be created from the PC", "FORBIDDEN")
    args = _merged(input_data)
    device_id = str(args.get("device_id") or "").strip()
    provider_id = str(args.get("provider_id") or "").strip()
    api_id = str(args.get("api_id") or "").strip()
    if not device_id or not provider_id or not api_id:
        return error("device_id, provider_id and api_id are required", "INVALID_INPUT")
    if any(key in args for key in ("api_key", "secret", "plaintext", "ciphertext", "nonce")):
        return error("client-supplied credential material is forbidden", "INVALID_INPUT")

    settings = settings_from(input_data, context)
    device = DeviceStore(settings.store_path).get_device(device_id)
    if device is None or not device.active:
        return error("recipient device is not active", "DEVICE_NOT_ACTIVE")
    if "credentials.request" not in device.scopes:
        return error("recipient device is not allowed to receive credentials", "SCOPE_NOT_ALLOWED")
    if device.profile_id != _profile_id(input_data, context):
        return error("recipient profile does not match", "WRONG_PROFILE")
    if not device.encryption_public_key or not device.public_key:
        return error("recipient must pair again to register secure transfer keys", "RECIPIENT_KEY_REQUIRED")

    metadata = provider_api_metadata(provider_id, api_id)
    if not metadata and read_provider_api_key(provider_id, api_id) is None:
        return error("provider credential not found", "CREDENTIAL_NOT_FOUND")
    try:
        record = _store(input_data, context).create(
            device_id=device.device_id,
            device_label=device.label or "Rumi Mobile",
            profile_id=device.profile_id,
            provider_id=provider_id,
            api_id=api_id,
            provider_label=str(metadata.get("name") or metadata.get("label") or provider_id),
            recipient_public_key=device.encryption_public_key,
            recipient_signing_key=device.public_key,
            ttl_seconds=int(args.get("ttl_seconds") or 90),
        )
    except Exception as exc:
        _audit_failure(context, "credential_transfer.create_failed", args, exc)
        return _failure(exc)
    _audit(context, "credential_transfer.created", record)
    return ok({"transfer": record})


def confirm_transfer(input_data, context=None):
    if _authenticated_device_id(context):
        return error("credential transfers must be confirmed on the PC", "FORBIDDEN")
    args = _merged(input_data)
    transfer_id = str(args.get("transfer_id") or args.get("id") or "").strip()
    if args.get("user_confirmed") is not True:
        return error("explicit user confirmation is required", "CONFIRMATION_REQUIRED")
    try:
        current = _store(input_data, context).get_admin(transfer_id)
        expected = {key: str(args.get(key) or "").strip() for key in ("device_id", "provider_id", "api_id")}
        secret = read_provider_api_key(current["provider_id"], current["api_id"])
        if not secret:
            return error("provider credential not found", "CREDENTIAL_NOT_FOUND")
        metadata = provider_api_metadata(current["provider_id"], current["api_id"])
        payload = {
            "provider_id": current["provider_id"],
            "api_id": current["api_id"],
            "api_key": secret,
            "label": current["provider_label"],
            "base_url": str(metadata.get("base_url") or ""),
            "default_model": str(metadata.get("default_model") or ""),
            "expires_at": current["expires_at"],
        }
        record = _store(input_data, context).confirm(transfer_id, payload=payload, expected=expected)
        payload.clear()
        secret = ""
    except Exception as exc:
        _audit_failure(context, "credential_transfer.confirm_failed", args, exc)
        return _failure(exc)
    _audit(context, "credential_transfer.confirmed", record)
    return ok({"transfer": record})


def list_transfers(input_data, context=None):
    device_id = _authenticated_device_id(context)
    if not device_id:
        return error("device authentication is required", "FORBIDDEN")
    store = _store(input_data, context)
    transfers = store.list_for_device(device_id)
    _audit_claimed_expiries(context, store, device_id=device_id)
    return ok({"transfers": transfers})


def get_status(input_data, context=None):
    if _authenticated_device_id(context):
        return error("transfer status is only available on the PC", "FORBIDDEN")
    args = _merged(input_data)
    try:
        store = _store(input_data, context)
        transfer_id = str(args.get("transfer_id") or args.get("id") or "")
        record = store.get_admin(transfer_id)
        _audit_claimed_expiries(context, store, transfer_id=transfer_id)
    except Exception as exc:
        _audit_failure(context, "credential_transfer.status_failed", args, exc)
        return _failure(exc)
    return ok({"transfer": record})


def redeem_transfer(input_data, context=None):
    device_id = _authenticated_device_id(context)
    if not device_id:
        return error("device authentication is required", "FORBIDDEN")
    args = _merged(input_data)
    try:
        result = _store(input_data, context).redeem(
            str(args.get("transfer_id") or args.get("id") or ""),
            device_id=device_id,
            signature=str(args.get("signature") or ""),
        )
    except Exception as exc:
        _audit_failure(context, "credential_transfer.redeem_failed", args, exc)
        return _failure(exc)
    _audit(context, "credential_transfer.accepted", result["transfer"])
    return ok(result)


def _transition(input_data, context, status: str):
    args = _merged(input_data)
    device_id = _authenticated_device_id(context)
    if status in {"rejected", "completed"} and not device_id:
        return error("only the recipient can update this transfer", "FORBIDDEN")
    if status in {"cancelled", "revoked"} and device_id:
        return error("only the PC can cancel or revoke a transfer", "FORBIDDEN")
    try:
        record = _store(input_data, context).transition(
            str(args.get("transfer_id") or args.get("id") or ""),
            status=status,
            actor_device_id=device_id,
            reason=str(args.get("reason") or status),
        )
    except Exception as exc:
        _audit_failure(context, f"credential_transfer.{status}_failed", args, exc)
        return _failure(exc)
    _audit(context, f"credential_transfer.{status}", record)
    return ok({"transfer": record})


def run(input_data, context=None):
    if not mobile_feature_enabled("credential_transfer"):
        return error("mobile credential transfer is disabled", "FEATURE_DISABLED")
    action = str(_merged(input_data).get("action") or "").strip().lower()
    handlers = {
        "create": create_transfer,
        "confirm": confirm_transfer,
        "list": list_transfers,
        "status": get_status,
        "redeem": redeem_transfer,
        "reject": lambda data, ctx=None: _transition(data, ctx, "rejected"),
        "cancel": lambda data, ctx=None: _transition(data, ctx, "cancelled"),
        "revoke": lambda data, ctx=None: _transition(data, ctx, "revoked"),
        "ack": lambda data, ctx=None: _transition(data, ctx, "completed"),
    }
    handler = handlers.get(action)
    if handler is None:
        return error(f"unknown credential action: {action}", "UNKNOWN_ACTION")
    return handler(input_data, context)
