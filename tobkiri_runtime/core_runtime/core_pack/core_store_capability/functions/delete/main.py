"""
store.delete - Built-in Capability Handler

Store からキーに対応するデータを削除する。

セキュリティ:
- grant_config.allowed_store_ids で制限
- key の '..' 即拒否 + normpath + is_path_within で boundary チェック
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict


_SAFE_KEY_RE = re.compile(r"^[a-zA-Z0-9_/.-]+$")


def execute(context: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    grant_config = context.get("grant_config", {})
    principal_id = context.get("principal_id", "")

    store_id = args.get("store_id", "")
    key = args.get("key", "")

    # --- 入力バリデーション ---
    if not store_id or not isinstance(store_id, str):
        return _error("Missing or invalid store_id", "validation_error")
    if not key or not isinstance(key, str):
        return _error("Missing or invalid key", "validation_error")

    # --- key セキュリティチェック ---
    validation = _validate_key(key)
    if validation is not None:
        return validation

    # --- Grant config: allowed_store_ids ---
    allowed = grant_config.get("allowed_store_ids", [])
    if allowed and store_id not in allowed:
        return _error("Store not in allowed_store_ids", "grant_denied")

    # --- Store 解決 ---
    store_root = _resolve_store_root(store_id)
    if store_root is None:
        return _error("Store not found: " + store_id, "store_not_found")

    # --- ファイルパス構築 + boundary チェック ---
    file_path = store_root / (key + ".json")
    file_path = Path(os.path.normpath(file_path))

    if not _is_path_within(file_path, store_root):
        return _error("Path traversal detected", "security_error")

    # --- 削除 ---
    if not file_path.exists():
        return _error("Key not found: " + key, "key_not_found")

    try:
        file_path.unlink()
    except OSError as e:
        return _error("Failed to delete: " + str(e), "delete_error")

    # 空のサブディレクトリを掃除（store root 自体は削除しない）
    _cleanup_empty_parents(file_path.parent, store_root)

    _audit_store_delete(principal_id, store_id, key)

    return {"success": True}


def _validate_key(key: str) -> Any:
    if ".." in key.split("/"):
        return _error("Key contains '..' (path traversal)", "security_error")
    if not _SAFE_KEY_RE.match(key):
        return _error(
            "Key contains invalid characters (allowed: a-zA-Z0-9_/.-)",
            "validation_error",
        )
    if key.startswith("/") or key.endswith("/"):
        return _error("Key must not start or end with '/'", "validation_error")
    return None


def _resolve_store_root(store_id: str) -> Any:
    try:
        from core_runtime.store_registry import get_store_registry
        registry = get_store_registry()
        store_def = registry.get_store(store_id)
        if store_def is None:
            return None
        root = Path(store_def.root_path)
        if not root.is_dir():
            return None
        return root.resolve()
    except Exception:
        return None


def _is_path_within(target: Path, boundary: Path) -> bool:
    try:
        resolved_target = target.resolve()
        resolved_boundary = boundary.resolve()
        resolved_target.relative_to(resolved_boundary)
        return True
    except (ValueError, OSError):
        return False


def _cleanup_empty_parents(directory: Path, stop_at: Path) -> None:
    """空のサブディレクトリを再帰的に削除（stop_at は削除しない）"""
    try:
        current = directory.resolve()
        stop = stop_at.resolve()
        while current != stop:
            if not current.is_dir():
                break
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
    except OSError:
        pass


def _error(message: str, error_type: str) -> Dict[str, Any]:
    return {"success": False, "error": message, "error_type": error_type}


def _audit_store_delete(principal_id: str, store_id: str, key: str) -> None:
    try:
        from core_runtime.audit_logger import get_audit_logger
        audit = get_audit_logger()
        audit.log_permission_event(
            pack_id=principal_id,
            permission_type="capability",
            action="store_delete",
            success=True,
            details={"store_id": store_id, "key": key},
        )
    except Exception:
        pass
