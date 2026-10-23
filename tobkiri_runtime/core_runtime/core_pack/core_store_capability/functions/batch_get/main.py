"""
store.batch_get - Built-in Capability Handler

複数キーを一度に取得する。

セキュリティ:
- grant_config.allowed_store_ids で制限
- 累計レスポンスサイズが 900KB を超えたら残りは null で返却

制限:
- 最大 100 キー
"""

from __future__ import annotations

import re
from typing import Any, Dict


_SAFE_KEY_RE = re.compile(r"^[a-zA-Z0-9_/.-]+$")


def execute(context: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    grant_config = context.get("grant_config", {})
    principal_id = context.get("principal_id", "")

    store_id = args.get("store_id", "")
    keys = args.get("keys", [])

    # --- 入力バリデーション ---
    if not store_id or not isinstance(store_id, str):
        return _error("Missing or invalid store_id", "validation_error")
    if not keys or not isinstance(keys, list):
        return _error("Missing or invalid keys", "validation_error")
    if len(keys) > 100:
        return _error(
            f"Too many keys ({len(keys)}). Maximum is 100.",
            "validation_error",
        )

    # --- key セキュリティチェック ---
    for key in keys:
        if not key or not isinstance(key, str):
            return _error(f"Invalid key in list: {key!r}", "validation_error")
        validation = _validate_key(key)
        if validation is not None:
            return validation

    # --- Grant config: allowed_store_ids ---
    allowed = grant_config.get("allowed_store_ids", [])
    if allowed and store_id not in allowed:
        return _error("Store not in allowed_store_ids", "grant_denied")

    # --- batch_get 実行 ---
    try:
        from core_runtime.store_registry import get_store_registry
        registry = get_store_registry()
        result = registry.batch_get(store_id=store_id, keys=keys)
    except Exception as e:
        return _error(f"Batch get failed: {e}", "internal_error")

    # 監査ログ
    _audit_batch_get(
        principal_id, store_id, len(keys),
        result.get("found", 0), result.get("not_found", 0),
    )

    return result


def _validate_key(key: str) -> Any:
    if ".." in key.split("/"):
        return _error("Key contains '..' (path traversal)", "security_error")
    if not _SAFE_KEY_RE.match(key):
        return _error(
            f"Key '{key}' contains invalid characters (allowed: a-zA-Z0-9_/.-)",
            "validation_error",
        )
    if key.startswith("/") or key.endswith("/"):
        return _error(
            f"Key '{key}' must not start or end with '/'",
            "validation_error",
        )
    return None


def _error(message: str, error_type: str) -> Dict[str, Any]:
    return {"success": False, "error": message, "error_type": error_type}


def _audit_batch_get(
    principal_id: str,
    store_id: str,
    requested: int,
    found: int,
    not_found: int,
) -> None:
    try:
        from core_runtime.audit_logger import get_audit_logger
        audit = get_audit_logger()
        audit.log_permission_event(
            pack_id=principal_id,
            permission_type="capability",
            action="store_batch_get",
            success=True,
            details={
                "store_id": store_id,
                "requested_keys": requested,
                "found": found,
                "not_found": not_found,
            },
        )
    except Exception:
        pass
