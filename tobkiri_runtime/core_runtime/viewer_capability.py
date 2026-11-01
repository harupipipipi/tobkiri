"""
viewer_capability.py - ViewerCapabilityHandler

Pack が Viewer にフロントエンドを表示できる Capability を提供する。
Grant config に基づき、Pack のフロントエンド情報（web_mount URL）と
Viewer 用の短期トークンを返す。

DockerCapabilityHandler と同じパターンで実装。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Any, Dict, Literal, Optional


class ViewerCapabilityHandler:
    """Pack からの viewer.display リクエストを検証・処理するハンドラ。"""

    # デフォルトトークン有効期間（秒）
    DEFAULT_TOKEN_LIFETIME = 3600
    # 絶対上限（Grant config でも超えられない）
    ABSOLUTE_MAX_TOKEN_LIFETIME = 86400

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # active tokens: token_hash -> {pack_id, principal_id, expires_at, ...}
        self._active_tokens: Dict[str, Dict[str, Any]] = {}

    # ================================================================== #
    # ユーティリティ
    # ================================================================== #

    @staticmethod
    def _generate_token() -> str:
        """暗号論的に安全なトークンを生成する。"""
        return secrets.token_urlsafe(48)

    @staticmethod
    def _hash_token(token: str) -> str:
        """トークンの SHA-256 ハッシュを返す（保存用）。"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _effective_token_lifetime(self, grant_config: dict) -> int:
        """実効トークン有効期間を計算する。"""
        grant_max = int(
            grant_config.get("max_token_lifetime", self.DEFAULT_TOKEN_LIFETIME)
        )
        return min(grant_max, self.ABSOLUTE_MAX_TOKEN_LIFETIME)

    def _get_web_mount_url(self, pack_id: str) -> Optional[str]:
        """Return no URL because legacy Pack API web mounts are retired."""

        del pack_id
        return None

    def _cleanup_expired_tokens(self) -> None:
        """期限切れトークンをクリーンアップする。"""
        now = time.time()
        expired = [
            h for h, info in self._active_tokens.items()
            if info.get("expires_at", 0) < now
        ]
        for h in expired:
            del self._active_tokens[h]

    def _audit_log(
        self,
        severity: Literal["info", "warning", "error", "critical"],
        action: str,
        success: bool,
        principal_id: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """監査ログを記録する（audit_logger がなくても動作する）。"""
        try:
            from .di_container import get_container
            audit = get_container().get_or_none("audit_logger")
            if audit is None:
                return
            from .audit_logger import AuditEntry
            entry = AuditEntry(
                ts=audit._now_ts(),
                category="security",
                severity=severity,
                action=action,
                success=success,
                owner_pack=principal_id,
                details=details or {},
            )
            audit.log(entry)
        except Exception:
            pass

    # ================================================================== #
    # メインハンドラ
    # ================================================================== #

    def handle_display(
        self, principal_id: str, args: dict, grant_config: dict
    ) -> dict:
        """Pack からの viewer.display リクエストを処理する。

        Args:
            principal_id: リクエスト元 Pack の識別子
            args: Pack からのリクエスト引数
                - pack_id (str): 表示対象の Pack ID
                - route (str): オプション。Pack フロントエンド内のルートパス
            grant_config: Grant config（セキュリティ制約）

        Returns:
            dict: web_mount_url, token, expires_in (, error)
        """
        # ------------------------------------------------------------ #
        # 1. 入力バリデーション
        # ------------------------------------------------------------ #
        target_pack_id = args.get("pack_id")
        if not target_pack_id:
            self._audit_log(
                "warning",
                "viewer.display.validation_failed",
                False,
                principal_id,
                {"reason": "pack_id is required"},
            )
            return {"error": "pack_id is required"}

        route = args.get("route", "/")

        # ------------------------------------------------------------ #
        # 2. 許可 Pack チェック
        # ------------------------------------------------------------ #
        allowed_packs = grant_config.get("allowed_packs", [])
        if allowed_packs and target_pack_id not in allowed_packs:
            self._audit_log(
                "warning",
                "viewer.display.pack_rejected",
                False,
                principal_id,
                {
                    "target_pack_id": target_pack_id,
                    "allowed_packs": allowed_packs,
                },
            )
            return {"error": f"Pack not allowed for display: {target_pack_id}"}

        # ------------------------------------------------------------ #
        # 3. web_mount URL 取得
        # ------------------------------------------------------------ #
        web_mount_url = self._get_web_mount_url(target_pack_id)
        if web_mount_url is None:
            self._audit_log(
                "warning",
                "viewer.display.no_web_mount",
                False,
                principal_id,
                {"target_pack_id": target_pack_id},
            )
            return {"error": f"No web_mount found for pack: {target_pack_id}"}

        # ------------------------------------------------------------ #
        # 4. トークン発行
        # ------------------------------------------------------------ #
        token_lifetime = self._effective_token_lifetime(grant_config)
        token = self._generate_token()
        token_hash = self._hash_token(token)
        expires_at = time.time() + token_lifetime

        with self._lock:
            self._cleanup_expired_tokens()
            self._active_tokens[token_hash] = {
                "pack_id": target_pack_id,
                "principal_id": principal_id,
                "route": route,
                "web_mount_url": web_mount_url,
                "expires_at": expires_at,
            }

        # ------------------------------------------------------------ #
        # 5. 監査ログ
        # ------------------------------------------------------------ #
        self._audit_log(
            "info",
            "viewer.display",
            True,
            principal_id,
            {
                "target_pack_id": target_pack_id,
                "web_mount_url": web_mount_url,
                "token_lifetime": token_lifetime,
            },
        )

        return {
            "web_mount_url": web_mount_url,
            "token": token,
            "expires_in": token_lifetime,
        }

    # ================================================================== #
    # トークン検証（API サーバーから呼ばれる）
    # ================================================================== #

    def verify_token(self, token: str) -> Optional[Dict[str, Any]]:
        """トークンを検証し、有効ならトークン情報を返す。

        Args:
            token: 検証するトークン文字列

        Returns:
            dict: pack_id, principal_id, web_mount_url, route（有効な場合）
            None: 無効または期限切れ
        """
        token_hash = self._hash_token(token)
        with self._lock:
            self._cleanup_expired_tokens()
            info = self._active_tokens.get(token_hash)
            if info is None:
                return None
            return {
                "pack_id": info["pack_id"],
                "principal_id": info["principal_id"],
                "web_mount_url": info["web_mount_url"],
                "route": info.get("route", "/"),
            }
