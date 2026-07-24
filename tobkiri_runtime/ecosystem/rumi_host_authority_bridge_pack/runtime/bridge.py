"""Core-authority one-shot bridge for high-authority host services."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from typing import Any, Callable, Mapping

from core_runtime.authority.request_store import AuthorityRequestStore

_TTL_SECONDS = 30
_RECEIPTS: dict[str, dict[str, Any]] = {}
_LOCK = threading.RLock()


def create_authority_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create authorize/redeem operations without approval authority."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name == "authorize":
            return _authorize(payload)
        if name == "redeem":
            return _redeem(payload)
        raise ValueError(f"unknown host authority operation: {name}")

    return operation


def _authorize(payload: Mapping[str, Any]) -> dict[str, Any]:
    scope = _scope(payload)
    consumer = str(payload.get("_contract_consumer_pack_id") or "").strip()
    if consumer and consumer != scope["caller_pack_id"]:
        return _denied(scope, "caller_pack_mismatch")
    if bool(payload.get("approval_required", False)):
        token = str(payload.get("approval_token") or "")
        request_id = str(payload.get("approval_request_id") or "").strip()
        if not token or not request_id:
            return {
                **_denied(scope, "approval_required"),
                "request": {
                    "principal_id": scope["caller_id"],
                    "permission_id": scope["authority"],
                    "profile_id": scope["profile_id"],
                    "resource": scope,
                    "risk_level": str(payload.get("risk") or "high"),
                },
            }
        consumed = AuthorityRequestStore().consume_one_shot(
            request_id=request_id,
            principal_id=scope["caller_id"],
            permission_id=scope["authority"],
            resource=scope,
            token=token,
        )
        if not consumed:
            return _denied(scope, "approval_invalid_expired_or_used")
    receipt = secrets.token_urlsafe(32)
    receipt_hash = _receipt_hash(receipt)
    now = time.time()
    record = {
        "scope": scope,
        "service_pack_id": str(payload.get("service_pack_id") or "").strip(),
        "issued_at": now,
        "expires_at": now + _TTL_SECONDS,
        "redeemed": False,
    }
    if not record["service_pack_id"]:
        raise ValueError("host authority service pack is required")
    with _LOCK:
        _prune(now)
        _RECEIPTS[receipt_hash] = record
    return {
        "authorized": True,
        "receipt": receipt,
        "receipt_hash": receipt_hash,
        "scope": scope,
        "service_pack_id": record["service_pack_id"],
        "expires_in_seconds": _TTL_SECONDS,
        "replay_policy": "redeem_once",
    }


def _redeem(payload: Mapping[str, Any]) -> dict[str, Any]:
    receipt = str(payload.get("receipt") or "")
    receipt_hash = _receipt_hash(receipt)
    expected_scope = _scope(payload)
    service_pack_id = str(payload.get("service_pack_id") or "").strip()
    consumer = str(payload.get("_contract_consumer_pack_id") or "").strip()
    if consumer and consumer != service_pack_id:
        return _denied(expected_scope, "service_pack_mismatch")
    now = time.time()
    with _LOCK:
        _prune(now)
        record = _RECEIPTS.get(receipt_hash)
        if record is None:
            return _denied(expected_scope, "receipt_missing_or_expired")
        if record["redeemed"]:
            return _denied(expected_scope, "receipt_already_redeemed")
        if record["service_pack_id"] != service_pack_id:
            return _denied(expected_scope, "receipt_service_mismatch")
        if record["scope"] != expected_scope:
            return _denied(expected_scope, "receipt_scope_mismatch")
        record["redeemed"] = True
        record["redeemed_at"] = now
    return {
        "authorized": True,
        "redeemed": True,
        "receipt_hash": receipt_hash,
        "scope": expected_scope,
        "service_pack_id": service_pack_id,
    }


def _scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    operation = str(payload.get("operation") or "").strip()
    authority = str(payload.get("authority") or "").strip()
    caller_id = str(payload.get("caller_id") or "").strip()
    caller_pack_id = str(payload.get("caller_pack_id") or "").strip()
    caller_function_id = str(payload.get("caller_function_id") or "").strip()
    profile_id = str(payload.get("profile_id") or "").strip()
    arguments = payload.get("arguments")
    if not isinstance(arguments, Mapping):
        raise ValueError("host authority arguments must be an object")
    if not all((operation, authority, caller_id, caller_pack_id, profile_id)):
        raise ValueError("host authority scope is incomplete")
    return {
        "operation": operation,
        "authority": authority,
        "args_hash": hashlib.sha256(
            json.dumps(
                dict(arguments),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest(),
        "caller_id": caller_id,
        "caller_pack_id": caller_pack_id,
        "caller_function_id": caller_function_id,
        "profile_id": profile_id,
        "workspace_id": str(payload.get("workspace_id") or "").strip(),
        "session_id": str(payload.get("session_id") or "").strip(),
        "replay_policy": "one_shot",
    }


def _denied(scope: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {"authorized": False, "reason": reason, "scope": dict(scope)}


def _receipt_hash(receipt: str) -> str:
    return hashlib.sha256(receipt.encode("utf-8")).hexdigest()


def _prune(now: float) -> None:
    for key in [
        key
        for key, value in _RECEIPTS.items()
        if float(value.get("expires_at") or 0) <= now
    ]:
        del _RECEIPTS[key]

