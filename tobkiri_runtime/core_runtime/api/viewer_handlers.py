"""Viewer ハンドラ Mixin — Phase V-2

/api/viewer/ 配下の API を提供する。
有限Viewer API boundaryとして独立して提供する。

API 一覧:
  POST /api/viewer/token — Viewer 用 Pack トークン発行
"""
from __future__ import annotations

import logging
from typing import Any, Dict, TYPE_CHECKING

from ._helpers import _log_internal_error, _SAFE_ERROR_MSG

logger = logging.getLogger(__name__)


class ViewerHandlersMixin:
    """Viewer API のハンドラ"""

    if TYPE_CHECKING:
        def _validate_pack_id(self, pack_id: str) -> bool: ...

    def _viewer_issue_token(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/viewer/token — Viewer 用 Pack トークンを発行する。

        リクエスト:
            { "pack_id": "some_pack" }

        レスポンス（成功）:
            { "token": "...", "web_mount_url": "/pack-name/", "expires_in": 3600 }

        レスポンス（失敗）:
            { "error": "...", "status_code": 403 }
        """
        pack_id = (body.get("pack_id") or "").strip()
        if not pack_id:
            return {"error": "Missing pack_id", "status_code": 400}

        # pack_id バリデーション
        if not self._validate_pack_id(pack_id):
            return {"error": "Invalid pack_id", "status_code": 400}

        # CapabilityGrantManager で viewer.display の Grant を確認
        try:
            from ..capability_grant_manager import get_capability_grant_manager
            grant_manager = get_capability_grant_manager()
            grant_result = grant_manager.check(pack_id, "viewer.display")
        except Exception as e:
            _log_internal_error("viewer_issue_token.grant_check", e)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

        if not grant_result.allowed:
            return {
                "error": f"viewer.display not granted for pack: {pack_id}",
                "status_code": 403,
            }

        # ViewerCapabilityHandler からトークンを発行
        try:
            from ..di_container import get_container
            handler = get_container().get_or_none("viewer_capability_handler")
            if handler is None:
                return {
                    "error": "Viewer capability handler not available",
                    "status_code": 503,
                }

            grant_config = grant_result.config or {}
            result = handler.handle_display(
                principal_id=pack_id,
                args={"pack_id": pack_id},
                grant_config=grant_config,
            )

            if "error" in result:
                return {"error": result["error"], "status_code": 403}

            return {
                "token": result["token"],
                "web_mount_url": result["web_mount_url"],
                "expires_in": result.get("expires_in", 3600),
            }

        except Exception as e:
            _log_internal_error("viewer_issue_token.handler", e)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}
