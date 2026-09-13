"""
host_privilege_manager.py - ホスト特権操作管理

Dockerコンテナ外で実行が必要な特権操作を管理する。

W21-A 変更:
  VULN-C02: 永続化 + HMAC 署名 + caller 認証 + 監査ログ + 入力バリデーション
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    Set,
)

if TYPE_CHECKING:
    from .audit_logger import AuditLogger

from .compat import safe_chmod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
_ID_MAX_LEN = 256

_TRUSTED_CALLERS: frozenset = frozenset({"system", "kernel", "approval_manager"})

_PERSIST_FILENAME = "host_privileges.json"

# ---------------------------------------------------------------------------
# オプショナル依存 (try-except で保護)
# ---------------------------------------------------------------------------

_hmac_available = False
class _SigningKeyLoader(Protocol):
    def __call__(
        self,
        key_path: Path,
        env_var: Optional[str] = None,
    ) -> bytes: ...


_hmac_generate_or_load_signing_key: Optional[_SigningKeyLoader] = None
_hmac_compute_data_hmac: Optional[Callable[[bytes, Dict[str, Any]], str]] = None
_hmac_verify_data_hmac: Optional[
    Callable[[bytes, Dict[str, Any], str], bool]
] = None

try:
    from .hmac_key_manager import (
        generate_or_load_signing_key as _hmac_generate_or_load_signing_key,
        compute_data_hmac as _hmac_compute_data_hmac,
        verify_data_hmac as _hmac_verify_data_hmac,
    )
    _hmac_available = True
except Exception:
    logger.warning("hmac_key_manager が利用不可。HMAC 署名なしで動作します。")

_audit_available = False
_get_audit_logger: Optional[Callable[[], "AuditLogger"]] = None

try:
    from .audit_logger import get_audit_logger as _get_audit_logger
    _audit_available = True
except Exception:
    logger.warning("audit_logger が利用不可。監査ログなしで動作します。")


def _resolve_base_dir() -> Path:
    """BASE_DIR を解決する (paths.py が利用不可なら __file__ から推定)。"""
    try:
        from .paths import BASE_DIR
        return BASE_DIR
    except Exception:
        return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# PrivilegeResult
# ---------------------------------------------------------------------------

@dataclass
class PrivilegeResult:
    """特権操作結果"""
    success: bool
    data: Any = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# HostPrivilegeManager
# ---------------------------------------------------------------------------

class HostPrivilegeManager:
    """ホスト特権操作管理 (W21-A hardened)"""

    def __init__(self, data_dir: Optional[str] = None):
        if data_dir is not None:
            self._data_dir = Path(data_dir)
        else:
            self._data_dir = _resolve_base_dir() / "user_data" / "permissions"

        self._persist_path = self._data_dir / _PERSIST_FILENAME
        self._granted: Dict[str, Set[str]] = {}
        self._lock = threading.Lock()

        # HMAC 署名鍵 (lazy)
        self._signing_key: Optional[bytes] = None

        # 起動時ロード
        self._load()

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _validate_id(value: str, name: str) -> Optional[str]:
        """ID バリデーション。問題があればエラーメッセージを返す。"""
        if not value:
            return f"Invalid {name}: empty string"
        if len(value) > _ID_MAX_LEN:
            return f"Invalid {name}: exceeds {_ID_MAX_LEN} characters"
        if not _ID_PATTERN.match(value):
            return f"Invalid {name}: contains invalid characters (allowed: a-zA-Z0-9._-)"
        return None

    @staticmethod
    def _get_trusted_callers() -> frozenset:
        """信頼済み caller のセットを返す (静的 + 環境変数)。"""
        extra_raw = os.environ.get("RUMI_PRIVILEGE_TRUSTED_CALLERS", "")
        if extra_raw.strip():
            extra = frozenset(c.strip() for c in extra_raw.split(",") if c.strip())
            return _TRUSTED_CALLERS | extra
        return _TRUSTED_CALLERS

    def _check_caller(self, caller_id: Optional[str], action: str) -> Optional[PrivilegeResult]:
        """caller 認証。不正なら PrivilegeResult を返す。正常なら None。"""
        effective = caller_id if caller_id is not None else "system"
        trusted = self._get_trusted_callers()
        if effective not in trusted:
            self._audit("host_privilege_unauthorized", "warning",
                        f"Unauthorized caller: {effective}",
                        details={"caller_id": effective, "action": action})
            return PrivilegeResult(success=False,
                                   error=f"Unauthorized caller: {effective}")
        return None

    # ------------------------------------------------------------------
    # 監査ログ
    # ------------------------------------------------------------------

    def _audit(
        self,
        event_type: str,
        severity: Literal["info", "warning", "error", "critical"],
        description: str,
               pack_id: Optional[str] = None,
               details: Optional[Dict[str, Any]] = None) -> None:
        """監査ログを記録 (best-effort)。"""
        if not _audit_available or _get_audit_logger is None:
            return
        try:
            audit = _get_audit_logger()
            audit.log_security_event(
                event_type=event_type,
                severity=severity,
                description=description,
                pack_id=pack_id,
                details=details or {},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # HMAC 署名
    # ------------------------------------------------------------------

    def _get_signing_key(self) -> Optional[bytes]:
        """署名鍵を取得 (lazy init)。"""
        if self._signing_key is not None:
            return self._signing_key
        if not _hmac_available or _hmac_generate_or_load_signing_key is None:
            return None
        try:
            key_path = self._data_dir / ".signing_key"
            self._signing_key = _hmac_generate_or_load_signing_key(key_path)
            return self._signing_key
        except Exception:
            return None

    def _compute_signature(self, data: Dict[str, Any]) -> Optional[str]:
        """data dict (hmac_signature 除外済み) の HMAC 署名を計算。"""
        key = self._get_signing_key()
        if key is None or _hmac_compute_data_hmac is None:
            return None
        try:
            return _hmac_compute_data_hmac(key, data)
        except Exception:
            return None

    def _verify_signature(self, data: Dict[str, Any], expected: str) -> bool:
        """HMAC 署名を検証。"""
        key = self._get_signing_key()
        if key is None or _hmac_verify_data_hmac is None:
            return False
        try:
            return _hmac_verify_data_hmac(key, data, expected)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 永続化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """ファイルからロード。ファイルなしなら空で初期化。"""
        if not self._persist_path.exists():
            return
        try:
            raw = self._persist_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as e:
            logger.warning("host_privileges.json 読み込みエラー: %s", e)
            self._audit("host_privilege_load_error", "warning",
                        f"Load error: {e}",
                        details={"error": str(e)})
            return

        # HMAC 検証
        stored_sig = data.get("hmac_signature")
        signable = {k: v for k, v in data.items() if k != "hmac_signature"}

        if stored_sig:
            if not self._verify_signature(signable, stored_sig):
                logger.warning("host_privileges.json HMAC 署名検証失敗。内容は受け入れます。")
                self._audit("host_privilege_hmac_fail", "warning",
                            "HMAC verification failed",
                            details={"path": str(self._persist_path)})
        else:
            if _hmac_available:
                logger.warning(
                    "host_privileges.json に HMAC 署名がありません (レガシー)。"
                    "次回保存で署名付与します。"
                )

        # grants を復元
        grants_raw = data.get("grants", {})
        for pack_id, privs in grants_raw.items():
            if isinstance(privs, list):
                self._granted[pack_id] = set(privs)

    def _save(self) -> None:
        """ファイルに永続化 (atomic write, HMAC 署名付き)。ロック保持状態で呼び出す。"""
        self._data_dir.mkdir(parents=True, exist_ok=True)

        grants_serializable = {
            pid: sorted(list(privs))
            for pid, privs in self._granted.items()
            if privs
        }

        data: Dict[str, Any] = {
            "version": "1.0",
            "updated_at": self._now_ts(),
            "grants": grants_serializable,
        }

        # HMAC 署名
        sig = self._compute_signature(data)
        if sig is not None:
            data["hmac_signature"] = sig

        content = json.dumps(data, ensure_ascii=False, indent=2)

        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._data_dir),
            prefix=".host_priv_tmp_",
            suffix=".json",
        )
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            fd = -1
            os.replace(tmp_path, str(self._persist_path))
            try:
                safe_chmod(str(self._persist_path), 0o600)
            except (OSError, AttributeError):
                pass
        except Exception:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def grant(self, pack_id: str, privilege_id: str,
              caller_id: Optional[str] = None) -> PrivilegeResult:
        """特権を付与"""
        err = self._validate_id(pack_id, "pack_id")
        if err:
            return PrivilegeResult(success=False, error=err)
        err = self._validate_id(privilege_id, "privilege_id")
        if err:
            return PrivilegeResult(success=False, error=err)

        effective_caller = caller_id if caller_id is not None else "system"
        denied = self._check_caller(caller_id, "grant")
        if denied is not None:
            return denied

        with self._lock:
            if pack_id not in self._granted:
                self._granted[pack_id] = set()
            self._granted[pack_id].add(privilege_id)
            self._save()

        self._audit("host_privilege_grant", "info",
                    f"Granted {privilege_id} to {pack_id}",
                    pack_id=pack_id,
                    details={"pack_id": pack_id, "privilege_id": privilege_id,
                             "caller_id": effective_caller})
        return PrivilegeResult(success=True)

    def revoke(self, pack_id: str, privilege_id: str,
               caller_id: Optional[str] = None) -> PrivilegeResult:
        """特権を取り消し"""
        err = self._validate_id(pack_id, "pack_id")
        if err:
            return PrivilegeResult(success=False, error=err)
        err = self._validate_id(privilege_id, "privilege_id")
        if err:
            return PrivilegeResult(success=False, error=err)

        effective_caller = caller_id if caller_id is not None else "system"
        denied = self._check_caller(caller_id, "revoke")
        if denied is not None:
            return denied

        with self._lock:
            if pack_id in self._granted:
                self._granted[pack_id].discard(privilege_id)
            self._save()

        self._audit("host_privilege_revoke", "info",
                    f"Revoked {privilege_id} from {pack_id}",
                    pack_id=pack_id,
                    details={"pack_id": pack_id, "privilege_id": privilege_id,
                             "caller_id": effective_caller})
        return PrivilegeResult(success=True)

    def revoke_all(self, pack_id: str,
                   caller_id: Optional[str] = None) -> PrivilegeResult:
        """全特権を取り消し"""
        err = self._validate_id(pack_id, "pack_id")
        if err:
            return PrivilegeResult(success=False, error=err)

        effective_caller = caller_id if caller_id is not None else "system"
        denied = self._check_caller(caller_id, "revoke_all")
        if denied is not None:
            return denied

        with self._lock:
            self._granted.pop(pack_id, None)
            self._save()

        self._audit("host_privilege_revoke_all", "info",
                    f"Revoked all privileges from {pack_id}",
                    pack_id=pack_id,
                    details={"pack_id": pack_id, "caller_id": effective_caller})
        return PrivilegeResult(success=True)

    def has_privilege(self, pack_id: str, privilege_id: str) -> bool:
        """特権があるかチェック"""
        with self._lock:
            return privilege_id in self._granted.get(pack_id, set())

    def execute(self, pack_id: str, privilege_id: str,
                params: Dict[str, Any]) -> PrivilegeResult:
        """特権操作を実行"""
        if not self.has_privilege(pack_id, privilege_id):
            return PrivilegeResult(success=False,
                                   error=f"Privilege not granted: {privilege_id}")
        return PrivilegeResult(success=True,
                               data={"privilege_id": privilege_id, "pack_id": pack_id})

    def list_privileges(self) -> List[Dict[str, Any]]:
        """付与済み特権一覧"""
        with self._lock:
            return [
                {"pack_id": pack_id, "privileges": sorted(list(privs))}
                for pack_id, privs in self._granted.items()
                if privs
            ]


# ---------------------------------------------------------------------------
# グローバル変数（後方互換のため残存。DI コンテナ優先）
# ---------------------------------------------------------------------------
_global_privilege_manager: Optional[HostPrivilegeManager] = None
_hpm_lock = threading.Lock()


def get_host_privilege_manager() -> HostPrivilegeManager:
    """
    グローバルな HostPrivilegeManager を取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。

    Returns:
        HostPrivilegeManager インスタンス
    """
    from .di_container import get_container
    return get_container().get("host_privilege_manager")


def initialize_host_privilege_manager(
    data_dir: Optional[str] = None,
) -> HostPrivilegeManager:
    """
    HostPrivilegeManager を初期化する。

    新しいインスタンスを生成し、DI コンテナのキャッシュを置き換える。

    Returns:
        初期化済み HostPrivilegeManager インスタンス
    """
    global _global_privilege_manager
    with _hpm_lock:
        _global_privilege_manager = HostPrivilegeManager(data_dir=data_dir)
    from .di_container import get_container
    get_container().set_instance("host_privilege_manager", _global_privilege_manager)
    return _global_privilege_manager


def reset_host_privilege_manager(
    data_dir: Optional[str] = None,
) -> HostPrivilegeManager:
    """
    HostPrivilegeManager をリセットする（テスト用）。

    新しいインスタンスを生成し、DI コンテナのキャッシュを置き換える。

    Returns:
        新しい HostPrivilegeManager インスタンス
    """
    global _global_privilege_manager
    from .di_container import get_container
    container = get_container()
    new = HostPrivilegeManager(data_dir=data_dir)
    with _hpm_lock:
        _global_privilege_manager = new
    container.set_instance("host_privilege_manager", new)
    return new
