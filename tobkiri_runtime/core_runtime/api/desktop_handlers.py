"""Desktop App ハンドラ Mixin — Phase V-4

/api/desktop/ 配下の API を提供する。
ViewerHandlersMixin と同じパターンに準拠。

API 一覧:
  POST /api/desktop/token — Desktop App 用 Pack トークン発行
"""
from __future__ import annotations

import logging
from typing import Any, Dict, TYPE_CHECKING

from ._helpers import _log_internal_error, _SAFE_ERROR_MSG

logger = logging.getLogger(__name__)


class DesktopHandlersMixin:
    """Desktop App API のハンドラ"""

    if TYPE_CHECKING:
        def _validate_pack_id(self, pack_id: str) -> bool: ...

    def _desktop_issue_token(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/desktop/token — Desktop App 用 Pack トークンを発行する。

        リクエスト:
            { "pack_id": "some_pack" }

        レスポンス（成功）:
            { "token": "...", "port": 8765, "expires_in": 3600 }

        レスポンス（失敗）:
            { "error": "...", "status_code": 403 }
        """
        pack_id = (body.get("pack_id") or "").strip()
        if not pack_id:
            return {"error": "Missing pack_id", "status_code": 400}

        # pack_id バリデーション
        if not self._validate_pack_id(pack_id):
            return {"error": "Invalid pack_id", "status_code": 400}

        # CapabilityGrantManager で desktop_app.execute の Grant を確認
        try:
            from ..capability_grant_manager import get_capability_grant_manager
            grant_manager = get_capability_grant_manager()
            grant_result = grant_manager.check(pack_id, "desktop_app.execute")
        except Exception as e:
            _log_internal_error("desktop_issue_token.grant_check", e)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

        if not grant_result.allowed:
            return {
                "error": f"desktop_app.execute not granted for pack: {pack_id}",
                "status_code": 403,
            }

        # DesktopCapabilityHandler からトークンを発行
        try:
            from ..di_container import get_container
            handler = get_container().get_or_none("desktop_capability_handler")
            if handler is None:
                return {
                    "error": "Desktop capability handler not available",
                    "status_code": 503,
                }

            grant_config = grant_result.config or {}
            result = handler.handle_execute(
                principal_id=pack_id,
                args={"pack_id": pack_id},
                grant_config=grant_config,
            )

            if "error" in result:
                return {"error": result["error"], "status_code": 403}

            return {
                "token": result["token"],
                "port": result.get("port", 8765),
                "expires_in": result.get("expires_in", 3600),
            }

        except Exception as e:
            _log_internal_error("desktop_issue_token.handler", e)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}
