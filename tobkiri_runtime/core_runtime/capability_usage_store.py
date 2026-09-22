"""
capability_usage_store.py - Capability 使用回数永続化ストア

principal_id x permission_id x scope_key ごとの使用回数を永続管理する。

保存先: user_data/permissions/capability_usage/<safe_principal_id>.json

設計原則:
- 再起動しても使用回数は復活しない (永続)
- HMAC 署名で改ざん検知
- fail-closed: 読み込みエラー時は使用済みとして扱う
- atomic write (tmp -> rename) でファイル破損を防止
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile as _tf
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from .compat import safe_chmod
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class UsageRecord:
    """単一の permission x scope の使用記録"""
    permission_id: str
    scope_key: str
    used_count: int = 0
    last_used_ts: Optional[str] = None
    daily_counts: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "permission_id": self.permission_id,
            "scope_key": self.scope_key,
            "used_count": self.used_count,
            "last_used_ts": self.last_used_ts,
            "daily_counts": self.daily_counts,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UsageRecord":
        return cls(
            permission_id=data.get("permission_id", ""),
            scope_key=data.get("scope_key", ""),
            used_count=data.get("used_count", 0),
            last_used_ts=data.get("last_used_ts"),
            daily_counts=data.get("daily_counts", {}),
        )


@dataclass
class ConsumeResult:
    """消費結果"""
    allowed: bool
    reason: Optional[str] = None
    used_count: int = 0
    max_count: int = 0
    scope_key: str = ""
    remaining: int = 0


class CapabilityUsageStore:
    """Capability 使用回数永続化ストア"""

    USAGE_DIR = "user_data/permissions/capability_usage"
    SECRET_KEY_FILE = "user_data/permissions/.secret_key"

    def __init__(
        self,
        usage_dir: Optional[str] = None,
        secret_key: Optional[str] = None,
    ) -> None:
        self._usage_dir = Path(usage_dir) if usage_dir else Path(self.USAGE_DIR)
        self._secret_key = secret_key or self._load_secret_key()
        self._lock = threading.RLock()
        self._cache: Dict[str, Dict[str, UsageRecord]] = {}
        self._usage_dir.mkdir(parents=True, exist_ok=True)

    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _today_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load_secret_key(self) -> str:
        key_file = Path(self.SECRET_KEY_FILE)
        if key_file.exists():
            try:
                return key_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        key = hashlib.sha256(os.urandom(32)).hexdigest()
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key, encoding="utf-8")
        try:
            safe_chmod(key_file, 0o600)
        except (OSError, AttributeError):
            pass
        return key

    def _safe_principal_id(self, principal_id: str) -> str:
        return principal_id.replace("/", "_").replace(":", "_").replace("..", "_")

    def _get_file_path(self, principal_id: str) -> Path:
        return self._usage_dir / (self._safe_principal_id(principal_id) + ".json")

    def _compute_hmac(self, data: Dict[str, Any]) -> str:
        data_copy = {k: v for k, v in data.items() if not k.startswith("_hmac")}
        payload = json.dumps(data_copy, sort_keys=True, ensure_ascii=False)
        return hmac.new(
            self._secret_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _load_principal(self, principal_id: str) -> Dict[str, UsageRecord]:
        if principal_id in self._cache:
            return self._cache[principal_id]
        file_path = self._get_file_path(principal_id)
        if not file_path.exists():
            self._cache[principal_id] = {}
            return self._cache[principal_id]
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stored_sig = data.pop("_hmac_signature", None)
            if stored_sig:
                computed_sig = self._compute_hmac(data)
                if not hmac.compare_digest(stored_sig, computed_sig):
                    self._audit_tamper_detected(principal_id, file_path)
                    self._cache[principal_id] = {}
                    return self._cache[principal_id]
            records: Dict[str, UsageRecord] = {}
            for key, rec_data in data.get("records", {}).items():
                records[key] = UsageRecord.from_dict(rec_data)
            self._cache[principal_id] = records
            return records
        except Exception:
            self._cache[principal_id] = {}
            return self._cache[principal_id]

    def _save_principal(self, principal_id: str) -> bool:
        """atomic write: tmp -> os.replace"""
        records = self._cache.get(principal_id, {})
        data = {
            "principal_id": principal_id,
            "updated_at": self._now_ts(),
            "records": {key: rec.to_dict() for key, rec in records.items()},
        }
        data["_hmac_signature"] = self._compute_hmac(data)
        file_path = self._get_file_path(principal_id)
        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = _tf.mkstemp(dir=str(file_path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(file_path))
            return True
        except Exception:
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            return False

    def _audit_tamper_detected(self, principal_id: str, file_path: Path) -> None:
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_security_event(
                event_type="capability_usage_tamper_detected",
                severity="error",
                description="HMAC verification failed for: " + str(file_path),
                details={"principal_id": principal_id, "file": str(file_path)},
            )
        except Exception:
            pass

    def _record_key(self, permission_id: str, scope_key: str) -> str:
        return permission_id + ":" + scope_key

    def check_and_consume(
        self,
        principal_id: str,
        permission_id: str,
        scope_key: str,
        max_count: int,
        max_daily_count: int = 0,
        expires_at_epoch: float = 0,
    ) -> ConsumeResult:
        with self._lock:
            if expires_at_epoch > 0 and time.time() > expires_at_epoch:
                return ConsumeResult(
                    allowed=False, reason="expired",
                    scope_key=scope_key, max_count=max_count,
                )
            records = self._load_principal(principal_id)
            key = self._record_key(permission_id, scope_key)
            record = records.get(key)
            if record is None:
                record = UsageRecord(permission_id=permission_id, scope_key=scope_key)
                records[key] = record
            if max_count > 0 and record.used_count >= max_count:
                return ConsumeResult(
                    allowed=False, reason="max_count_exceeded",
                    used_count=record.used_count, max_count=max_count,
                    scope_key=scope_key, remaining=0,
                )
            if max_daily_count > 0:
                today = self._today_str()
                daily_used = record.daily_counts.get(today, 0)
                if daily_used >= max_daily_count:
                    rem = max(0, max_count - record.used_count) if max_count > 0 else -1
                    return ConsumeResult(
                        allowed=False, reason="daily_limit_exceeded",
                        used_count=record.used_count, max_count=max_count,
                        scope_key=scope_key, remaining=rem,
                    )
            record.used_count += 1
            record.last_used_ts = self._now_ts()
            if max_daily_count > 0:
                today = self._today_str()
                record.daily_counts[today] = record.daily_counts.get(today, 0) + 1
            self._save_principal(principal_id)
            rem = max(0, max_count - record.used_count) if max_count > 0 else -1
            return ConsumeResult(
                allowed=True, used_count=record.used_count,
                max_count=max_count, scope_key=scope_key, remaining=rem,
            )

    def get_usage(self, principal_id, permission_id, scope_key):
        with self._lock:
            records = self._load_principal(principal_id)
            return records.get(self._record_key(permission_id, scope_key))

    def get_all_usage(self, principal_id: str) -> Dict[str, UsageRecord]:
        with self._lock:
            return dict(self._load_principal(principal_id))

    def reset_usage(self, principal_id, permission_id, scope_key) -> bool:
        with self._lock:
            records = self._load_principal(principal_id)
            key = self._record_key(permission_id, scope_key)
            if key in records:
                del records[key]
                return self._save_principal(principal_id)
            return False

    def reset_all(self, principal_id: str) -> bool:
        with self._lock:
            self._cache[principal_id] = {}
            return self._save_principal(principal_id)


_global_usage_store: Optional[CapabilityUsageStore] = None
_usage_lock = threading.Lock()


def get_capability_usage_store() -> CapabilityUsageStore:
    global _global_usage_store
    if _global_usage_store is None:
        with _usage_lock:
            if _global_usage_store is None:
                _global_usage_store = CapabilityUsageStore()
    return _global_usage_store


def reset_capability_usage_store(
    usage_dir: Optional[str] = None,
) -> CapabilityUsageStore:
    global _global_usage_store
    with _usage_lock:
        _global_usage_store = CapabilityUsageStore(usage_dir)
    return _global_usage_store
