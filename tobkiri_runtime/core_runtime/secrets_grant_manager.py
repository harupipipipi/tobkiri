"""
secrets_grant_manager.py - Secret アクセス権限管理

Pack 単位で Secret へのアクセスを Grant/Revoke する。
Grant された Secret のみ、コンテナ起動時に注入される。

保存先: user_data/permissions/secrets/{pack_id}.json
HMAC 署名で改ざん検知。

設計原則:
- Pack 単位での Grant（運用を簡単に）
- HMAC 署名で改ざん検知
- 監査ログに全ての操作を記録
- NetworkGrantManager と同じパターン
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .hmac_key_manager import (
    generate_or_load_signing_key,
    compute_data_hmac,
    verify_data_hmac,
)

logger = logging.getLogger(__name__)


def get_secrets_store():
    from .secrets_store import get_secrets_store as _get
    return _get()


@dataclass
class SecretGrant:
    """Secret アクセス権限 Grant"""
    pack_id: str
    granted_keys: List[str]
    granted_at: str
    updated_at: str
    granted_by: str = "user"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "granted_keys": self.granted_keys,
            "granted_at": self.granted_at,
            "updated_at": self.updated_at,
            "granted_by": self.granted_by,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SecretGrant":
        return cls(
            pack_id=data.get("pack_id", ""),
            granted_keys=data.get("granted_keys", []),
            granted_at=data.get("granted_at", ""),
            updated_at=data.get("updated_at", ""),
            granted_by=data.get("granted_by", "user"),
        )


class SecretsGrantManager:
    """
    Secret アクセス権限 Grant 管理

    user_data/permissions/secrets/{pack_id}.json で Grant 情報を永続化。
    """

    GRANTS_DIR = "user_data/permissions/secrets"
    SECRET_KEY_FILE = "user_data/permissions/.secret_key"

    def __init__(
        self,
        grants_dir: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        if grants_dir:
            self._grants_dir = Path(grants_dir)
        else:
            from .paths import USER_DATA_DIR
            self._grants_dir = USER_DATA_DIR / "permissions" / "secrets"
        if secret_key:
            self._secret_key: bytes = secret_key.encode("utf-8")
        else:
            from .paths import USER_DATA_DIR as _UDD
            self._secret_key = generate_or_load_signing_key(
                _UDD / "permissions" / ".secret_key",
            )
        self._grants: Dict[str, SecretGrant] = {}
        self._lock = threading.RLock()

        self._ensure_dir()
        self._load_all_grants()

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _ensure_dir(self) -> None:
        """ディレクトリを作成"""
        self._grants_dir.mkdir(parents=True, exist_ok=True)

    def _get_grant_file(self, pack_id: str) -> Path:
        """Pack ID から Grant ファイルパスを取得"""
        safe_id = pack_id.replace("/", "_").replace(":", "_")
        return self._grants_dir / f"{safe_id}.json"

    # ------------------------------------------------------------------
    # ロード / セーブ
    # ------------------------------------------------------------------

    def _load_all_grants(self) -> None:
        """全 Grant をロード"""
        with self._lock:
            self._grants.clear()
            if not self._grants_dir.exists():
                return
            for grant_file in self._grants_dir.glob("*.json"):
                try:
                    self._load_grant_file(grant_file)
                except Exception as e:
                    logger.warning(
                        "Failed to load grant file %s: %s", grant_file, e
                    )

    def _load_grant_file(self, file_path: Path) -> Optional[SecretGrant]:
        """単一の Grant ファイルをロード（HMAC 検証付き）"""
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        stored_sig = data.pop("_hmac_signature", None)
        pack_id = data.get("pack_id", file_path.stem)

        if not stored_sig:
            logger.warning(
                "Missing HMAC signature for secret grant file %s", file_path
            )
            self._log_grant_event(pack_id, "hmac_missing", False, {
                "file_path": str(file_path),
                "reason": "missing_signature",
            })
            return None

        if not verify_data_hmac(self._secret_key, data, stored_sig):
            logger.warning(
                "HMAC verification failed for secret grant file %s", file_path
            )
            self._log_grant_event(pack_id, "hmac_mismatch", False, {
                "file_path": str(file_path),
                "reason": "signature_mismatch",
            })
            return None

        grant = SecretGrant.from_dict(data)
        self._grants[grant.pack_id] = grant
        return grant

    def _save_grant(self, grant: SecretGrant) -> bool:
        """Grant を HMAC 署名付きで保存"""
        try:
            data = grant.to_dict()
            data["_hmac_signature"] = compute_data_hmac(self._secret_key, data)

            file_path = self._get_grant_file(grant.pack_id)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            return True
        except Exception as e:
            logger.error(
                "Failed to save secret grant for %s: %s", grant.pack_id, e
            )
            return False

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def grant_secret_access(
        self,
        pack_id: str,
        secret_keys: List[str],
        granted_by: str = "user",
    ) -> SecretGrant:
        """Pack に指定 Secret へのアクセスを許可"""
        with self._lock:
            now = self._now_ts()
            existing = self._grants.get(pack_id)

            if existing:
                # 既存 Grant にキーを追加（重複排除）
                merged_keys = list(dict.fromkeys(
                    existing.granted_keys + secret_keys
                ))
                grant = SecretGrant(
                    pack_id=pack_id,
                    granted_keys=merged_keys,
                    granted_at=existing.granted_at,
                    updated_at=now,
                    granted_by=granted_by,
                )
            else:
                grant = SecretGrant(
                    pack_id=pack_id,
                    granted_keys=list(dict.fromkeys(secret_keys)),
                    granted_at=now,
                    updated_at=now,
                    granted_by=granted_by,
                )

            self._grants[pack_id] = grant
            self._save_grant(grant)

            self._log_grant_event(pack_id, "grant", True, {
                "secret_keys": secret_keys,
                "granted_by": granted_by,
                "total_granted_keys": grant.granted_keys,
            })

            return grant

    def revoke_secret_access(
        self, pack_id: str, secret_keys: List[str]
    ) -> bool:
        """Pack から指定 Secret へのアクセスを取消"""
        with self._lock:
            grant = self._grants.get(pack_id)
            if not grant:
                return False

            grant.granted_keys = [
                k for k in grant.granted_keys if k not in secret_keys
            ]
            grant.updated_at = self._now_ts()

            self._save_grant(grant)

            self._log_grant_event(pack_id, "revoke", True, {
                "revoked_keys": secret_keys,
                "remaining_keys": grant.granted_keys,
            })

            return True

    def revoke_all(self, pack_id: str) -> bool:
        """Pack の全 Secret アクセスを取消"""
        with self._lock:
            grant = self._grants.get(pack_id)
            if not grant:
                return False

            grant.granted_keys = []
            grant.updated_at = self._now_ts()

            self._save_grant(grant)

            self._log_grant_event(pack_id, "revoke_all", True, {})

            return True

    def get_granted_keys(self, pack_id: str) -> List[str]:
        """Pack に Grant された Secret キー名のリストを返す"""
        with self._lock:
            grant = self._grants.get(pack_id)
            if not grant:
                return []
            return list(grant.granted_keys)

    def get_granted_secrets(self, pack_id: str) -> Dict[str, str]:
        """Pack に Grant された Secret のキーと復号済み値を返す。

        SecretsStore._internal_read_value() を内部的に呼び出す。
        Grant されていないキーは含まない。
        存在しない Secret キーはスキップ（ログ出力）。
        """
        granted_keys = self.get_granted_keys(pack_id)
        if not granted_keys:
            return {}

        try:
            store = get_secrets_store()
        except Exception as e:
            logger.error("Failed to get SecretsStore: %s", e)
            return {}

        result: Dict[str, str] = {}
        for key in granted_keys:
            try:
                value = store._internal_read_value(
                    key, caller_id=f"secrets_grant_manager:{pack_id}"
                )
                if value is not None:
                    result[key] = value
                else:
                    logger.info(
                        "Secret key '%s' granted to pack '%s' does not exist "
                        "or is deleted; skipping.",
                        key, pack_id,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to read secret '%s' for pack '%s': %s",
                    key, pack_id, e,
                )

        return result

    def has_grant(self, pack_id: str, secret_key: str) -> bool:
        """Pack が指定 Secret への Grant を持つか"""
        with self._lock:
            grant = self._grants.get(pack_id)
            if not grant:
                return False
            return secret_key in grant.granted_keys

    def list_all_grants(self) -> Dict[str, SecretGrant]:
        """全 Grant 一覧"""
        with self._lock:
            return dict(self._grants)

    def delete_grant(self, pack_id: str) -> bool:
        """Grant ファイルを削除"""
        with self._lock:
            if pack_id not in self._grants:
                return False

            del self._grants[pack_id]

            file_path = self._get_grant_file(pack_id)
            if file_path.exists():
                file_path.unlink()

            self._log_grant_event(pack_id, "delete", True, {})

            return True

    # ------------------------------------------------------------------
    # 監査ログ
    # ------------------------------------------------------------------

    def _log_grant_event(
        self,
        pack_id: str,
        action: str,
        success: bool,
        details: Dict[str, Any],
    ) -> None:
        """Grant 操作を監査ログに記録"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_permission_event(
                pack_id=pack_id,
                permission_type="secret",
                action=action,
                success=success,
                details=details,
            )
        except Exception:
            pass


def get_secrets_grant_manager() -> SecretsGrantManager:
    """
    グローバルな SecretsGrantManager を取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。

    Returns:
        SecretsGrantManager インスタンス
    """
    from .di_container import get_container
    return get_container().get("secrets_grant_manager")


def reset_secrets_grant_manager(
    grants_dir: Optional[str] = None,
) -> SecretsGrantManager:
    """
    SecretsGrantManager をリセットする（テスト用）。

    新しいインスタンスを生成し、DI コンテナのキャッシュを置き換える。

    Args:
        grants_dir: Grant ファイルの保存ディレクトリ（省略時はデフォルト）

    Returns:
        新しい SecretsGrantManager インスタンス
    """
    from .di_container import get_container
    new_instance = SecretsGrantManager(grants_dir)
    container = get_container()
    container.set_instance("secrets_grant_manager", new_instance)
    return new_instance
