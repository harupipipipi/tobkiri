"""
store_sharing_manager.py - Pack間Store共有管理

Packが自分のStoreを他のPackに共有する仕組みを管理する。

保存先: user_data/stores/sharing.json

設計原則:
- provider_pack_id が所有する store_id を consumer_pack_id に共有
- 承認(approve) / 取消(revoke) の明示的な操作が必要
- スレッドセーフ

Wave 17-B 変更:
- HMAC 署名の生成・検証を追加
- 後方互換: 署名なし旧ファイルは WARNING のみ（RUMI_REQUIRE_HMAC=1 で拒否）
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .hmac_key_manager import generate_or_load_signing_key, compute_data_hmac, verify_data_hmac

logger = logging.getLogger(__name__)
_REQUIRE_HMAC_DEFAULT = "1"

SHARING_INDEX_PATH = "user_data/stores/sharing.json"


@dataclass
class SharingEntry:
    """単一の共有エントリ"""
    provider_pack_id: str
    consumer_pack_id: str
    store_id: str
    approved_at: str
    approved_by: str = "api_user"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_pack_id": self.provider_pack_id,
            "consumer_pack_id": self.consumer_pack_id,
            "store_id": self.store_id,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SharingEntry":
        return cls(
            provider_pack_id=data.get("provider_pack_id", ""),
            consumer_pack_id=data.get("consumer_pack_id", ""),
            store_id=data.get("store_id", ""),
            approved_at=data.get("approved_at", ""),
            approved_by=data.get("approved_by", "api_user"),
        )

    @property
    def key(self) -> str:
        """一意キー"""
        return f"{self.provider_pack_id}:{self.consumer_pack_id}:{self.store_id}"


class SharedStoreManager:
    """
    Pack間Store共有を管理する。

    データ構造 (sharing.json):
    {
        "version": "1.0",
        "updated_at": "...",
        "entries": {
            "<provider>:<consumer>:<store_id>": { ... SharingEntry ... }
        }
    }
    """

    def __init__(self, index_path: Optional[str] = None):
        self._index_path = Path(index_path or SHARING_INDEX_PATH)
        self._lock = threading.RLock()
        self._entries: Dict[str, SharingEntry] = {}

        # HMAC 署名用の秘密鍵をロード
        self._secret_key = generate_or_load_signing_key(
            self._index_path.parent / ".secret_key",
            env_var="RUMI_HMAC_SECRET",
        )

        self._load()

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # ------------------------------------------------------------------
    # 永続化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._index_path.exists():
            return
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # --- HMAC 署名検証 ---
            stored_sig = data.pop("_hmac_signature", None)
            if stored_sig:
                if not verify_data_hmac(self._secret_key, data, stored_sig):
                    logger.critical("Sharing store HMAC verification failed — possible tampering")
                    self._entries = {}
                    return
            else:
                require_hmac = os.environ.get("RUMI_REQUIRE_HMAC", _REQUIRE_HMAC_DEFAULT) == "1"
                if require_hmac:
                    logger.critical("Sharing store has no HMAC signature and RUMI_REQUIRE_HMAC=1")
                    self._entries = {}
                    return
                else:
                    logger.warning(
                        "Sharing store has no HMAC signature (legacy file). "
                        "Signature will be added on next save."
                    )

            for key, edata in data.get("entries", {}).items():
                self._entries[key] = SharingEntry.from_dict(edata)
        except Exception:
            pass

    def _save(self) -> None:
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1.0",
            "updated_at": self._now_ts(),
            "entries": {k: e.to_dict() for k, e in self._entries.items()},
        }

        # HMAC 署名を追加
        data["_hmac_signature"] = compute_data_hmac(self._secret_key, data)

        tmp = self._index_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self._index_path)

    def hmac_status(self) -> str:
        """署名状態を返す: missing / signed / invalid / absent"""
        if not self._index_path.exists():
            return "absent"
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stored_sig = data.pop("_hmac_signature", None)
            if not stored_sig:
                return "missing"
            return "signed" if verify_data_hmac(self._secret_key, data, stored_sig) else "invalid"
        except Exception:
            return "invalid"

    def migrate_hmac_signature(self) -> str:
        """署名なし sharing.json を署名付きで保存し直す。"""
        status = self.hmac_status()
        if status == "absent":
            return "absent"
        if status == "signed":
            return "already_signed"
        if status == "invalid":
            return "failed"
        with self._lock:
            self._save()
        return "migrated"

    # ------------------------------------------------------------------
    # 公開API
    # ------------------------------------------------------------------

    def approve_sharing(
        self,
        provider_pack_id: str,
        consumer_pack_id: str,
        store_id: str,
        approved_by: str = "api_user",
    ) -> Dict[str, Any]:
        """共有を承認する。"""
        if not provider_pack_id or not consumer_pack_id or not store_id:
            return {
                "success": False,
                "error": "provider_pack_id, consumer_pack_id, and store_id are required",
            }

        if provider_pack_id == consumer_pack_id:
            return {
                "success": False,
                "error": "provider and consumer must be different packs",
            }

        entry = SharingEntry(
            provider_pack_id=provider_pack_id,
            consumer_pack_id=consumer_pack_id,
            store_id=store_id,
            approved_at=self._now_ts(),
            approved_by=approved_by,
        )

        with self._lock:
            self._entries[entry.key] = entry
            self._save()

        self._audit("store_sharing_approved", True, entry.to_dict())

        return {
            "success": True,
            "provider_pack_id": provider_pack_id,
            "consumer_pack_id": consumer_pack_id,
            "store_id": store_id,
        }

    def revoke_sharing(
        self,
        provider_pack_id: str,
        consumer_pack_id: str,
        store_id: str,
    ) -> Dict[str, Any]:
        """共有を取り消す。"""
        key = f"{provider_pack_id}:{consumer_pack_id}:{store_id}"

        with self._lock:
            if key not in self._entries:
                return {
                    "success": False,
                    "error": f"No sharing entry found for {key}",
                }
            del self._entries[key]
            self._save()

        self._audit("store_sharing_revoked", True, {
            "provider_pack_id": provider_pack_id,
            "consumer_pack_id": consumer_pack_id,
            "store_id": store_id,
        })

        return {
            "success": True,
            "provider_pack_id": provider_pack_id,
            "consumer_pack_id": consumer_pack_id,
            "store_id": store_id,
        }

    def list_shared_stores(self) -> List[Dict[str, Any]]:
        """全ての共有エントリを返す。"""
        with self._lock:
            return [e.to_dict() for e in self._entries.values()]

    def is_sharing_approved(
        self,
        consumer_pack_id: str,
        store_id: str,
    ) -> bool:
        """consumer_pack_id が store_id にアクセスできるか判定する。"""
        with self._lock:
            for entry in self._entries.values():
                if entry.consumer_pack_id == consumer_pack_id and entry.store_id == store_id:
                    return True
        return False

    # ------------------------------------------------------------------
    # 監査ログ
    # ------------------------------------------------------------------

    @staticmethod
    def _audit(event_type: str, success: bool, details: Dict[str, Any]) -> None:
        try:
            from .audit_logger import get_audit_logger
            get_audit_logger().log_system_event(
                event_type=event_type, success=success, details=details,
            )
        except Exception:
            pass


# ------------------------------------------------------------------
# グローバルインスタンス
# ------------------------------------------------------------------

_global_shared_store_manager: Optional[SharedStoreManager] = None
_ssm_lock = threading.Lock()


def get_shared_store_manager() -> SharedStoreManager:
    """グローバルな SharedStoreManager を取得"""
    global _global_shared_store_manager
    if _global_shared_store_manager is None:
        with _ssm_lock:
            if _global_shared_store_manager is None:
                _global_shared_store_manager = SharedStoreManager()
    return _global_shared_store_manager


def reset_shared_store_manager(index_path: str | None = None) -> SharedStoreManager:
    """リセット（テスト用）"""
    global _global_shared_store_manager
    with _ssm_lock:
        _global_shared_store_manager = SharedStoreManager(index_path)
    return _global_shared_store_manager
