"""
共通ヘルパー — 全ハンドラ Mixin から使用。
pack_api_server.py との循環 import を回避するため独立モジュールとした。
監査ログ共通関数もここで提供する。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["_SAFE_ERROR_MSG", "_log_internal_error", "_log_audit_event"]

_SAFE_ERROR_MSG = "Internal server error"


def _log_internal_error(context: str, exc: Exception) -> None:
    """Log exception details to audit log (or fallback to logger) without exposing to client."""
    try:
        from ..audit_logger import get_audit_logger
        audit = get_audit_logger()
        audit.log_system_event(
            event_type="api_internal_error",
            success=False,
            details={"context": context, "error": str(exc), "type": type(exc).__name__},
        )
        # Internal API failures are rare and immediately actionable.  Persist
        # them now instead of leaving the only diagnostic in the audit
        # logger's in-memory batch until process shutdown.
        audit.flush()
    except Exception:
        logger.exception(f"Internal error in {context}: {exc}")


# severity 文字列 → logging レベルのマッピング
_SEVERITY_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def _log_audit_event(
    category: str,
    action: str,
    details: dict | None = None,
    *,
    success: bool = True,
    severity: str = "info",
) -> None:
    """各ハンドラから呼べる共通監査ログ関数。

    audit_logger が利用可能ならそちらを使い、
    利用不可（import エラー等）なら標準 logger にフォールバックする。

    Args:
        category: イベントカテゴリ (例: "route", "store", "secret")
        action: 実行されたアクション (例: "create", "delete", "reload")
        details: 追加の詳細情報 dict（任意）
        success: 操作が成功したかどうか
        severity: ログの重要度 ("debug" | "info" | "warning" | "error" | "critical")
    """
    event_type = f"{category}.{action}"
    merged_details: dict = {
        "category": category,
        "action": action,
    }
    if details:
        merged_details.update(details)

    # 1. audit_logger を試みる
    try:
        from ..audit_logger import get_audit_logger

        audit = get_audit_logger()
        audit.log_system_event(
            event_type=event_type,
            success=success,
            details=merged_details,
        )
        return
    except Exception:
        pass

    # 2. フォールバック: 標準 logger
    level = _SEVERITY_MAP.get(severity.lower(), logging.INFO)
    logger.log(
        level,
        "Audit[%s] success=%s details=%s",
        event_type,
        success,
        merged_details,
    )
