"""Core-authority one-shot bridge for high-authority host services."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.authority.request_store import AuthorityRequestStore
from core_runtime.paths import USER_DATA_DIR
from core_runtime.runtime_locks import NamedLock

_TTL_SECONDS = 30
_LOCK = threading.RLock()
_RECEIPT_ROOT = Path(USER_DATA_DIR) / "authority" / "effect_receipts"


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
    receipt = secrets.token_urlsafe(32)
    receipt_hash = _receipt_hash(receipt)
    now = time.time()
    record = {
        "scope": scope,
        "service_pack_id": str(payload.get("service_pack_id") or "").strip(),
        "issued_at": now,
        "expires_at": now + _TTL_SECONDS,
        "status": "pending_approval",
    }
    if not record["service_pack_id"]:
        raise ValueError("host authority service pack is required")
    with _LOCK, NamedLock(_RECEIPT_ROOT, "authority-receipts"):
        _write_receipt(receipt_hash, record)
    if bool(payload.get("approval_required", False)):
        token = str(payload.get("approval_token") or "")
        request_id = str(payload.get("approval_request_id") or "").strip()
        if not token or not request_id:
            _delete_receipt(receipt_hash)
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
            _delete_receipt(receipt_hash)
            return _denied(scope, "approval_invalid_expired_or_used")
    with _LOCK, NamedLock(_RECEIPT_ROOT, "authority-receipts"):
        record["status"] = "issued"
        _write_receipt(receipt_hash, record)
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
    with _LOCK, NamedLock(_RECEIPT_ROOT, "authority-receipts"):
        _prune(now)
        record = _read_receipt(receipt_hash)
        if record is None:
            return _denied(expected_scope, "receipt_missing_or_expired")
        if record.get("status") != "issued":
            return _denied(expected_scope, "receipt_already_redeemed")
        if record["service_pack_id"] != service_pack_id:
            return _denied(expected_scope, "receipt_service_mismatch")
        if record["scope"] != expected_scope:
            return _denied(expected_scope, "receipt_scope_mismatch")
        record["status"] = "effect_committing"
        record["redeemed_at"] = now
        _write_receipt(receipt_hash, record)
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
    if not _RECEIPT_ROOT.exists():
        return
    for path in _RECEIPT_ROOT.glob("*.json"):
        value = _read_json(path)
        if float((value or {}).get("expires_at") or 0) <= now:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _receipt_path(receipt_hash: str) -> Path:
    return _RECEIPT_ROOT / f"{receipt_hash}.json"


def _read_receipt(receipt_hash: str) -> dict[str, Any] | None:
    return _read_json(_receipt_path(receipt_hash))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _write_receipt(receipt_hash: str, value: Mapping[str, Any]) -> None:
    _RECEIPT_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(_RECEIPT_ROOT, 0o700)
    descriptor, temporary = tempfile.mkstemp(
        dir=_RECEIPT_ROOT,
        prefix=".receipt-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, _receipt_path(receipt_hash))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _delete_receipt(receipt_hash: str) -> None:
    try:
        _receipt_path(receipt_hash).unlink()
    except FileNotFoundError:
        pass
