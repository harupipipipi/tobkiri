"""
hmac_key_manager.py - HMAC鍵のローテーション管理 + 署名ユーティリティ (DI Container 対応)

鍵の生成・保存・ローテーション・グレースピリオド検証を提供する。

鍵の保存先: user_data/hmac_keys.json (.gitignore 登録済み)
グレースピリオド: デフォルト24時間（ローテーション後も旧鍵で検証可能）
ローテーショントリガー:
  - 環境変数 RUMI_HMAC_ROTATE=true で起動時にローテーション
  - プログラムから rotate() / rotate_key() を呼び出し

署名ユーティリティ (#65):
  - generate_or_load_signing_key(key_path) → bytes
  - compute_data_hmac(key, data_dict) → str
  - verify_data_hmac(key, data_dict, expected_hmac) → bool
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .compat import safe_chmod

logger = logging.getLogger(__name__)

# Fernet 暗号化の可用性 (W21-B)
_FERNET_AVAILABLE = False
try:
    from cryptography.fernet import Fernet as _Fernet
    _FERNET_AVAILABLE = True
except ImportError:
    _Fernet = None

# 暗号化鍵ファイル名 (W21-B)
_ENC_KEY_FILENAME = "hmac_keys.key"

# デフォルトグレースピリオド（秒）: 24時間
DEFAULT_GRACE_PERIOD_SECONDS = 86400

# デフォルト鍵保存パス
_DEFAULT_KEYS_FILENAME = "hmac_keys.json"
_DEFAULT_KEYS_SUBDIR = "user_data"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_ts() -> str:
    return _now_utc().isoformat().replace("+00:00", "Z")


def _parse_ts(ts: str) -> datetime:
    """ISO 8601 タイムスタンプをパース"""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


# ======================================================================
# 署名ユーティリティ関数 (#65)
# ======================================================================

def generate_or_load_signing_key(
    key_path: Path,
    env_var: Optional[str] = None,
) -> bytes:
    """
    署名用秘密鍵をロードまたは生成する。

    優先順位:
    1. env_var が指定されていれば環境変数から取得
    2. key_path ファイルから読み込み
    3. 新規生成して key_path に atomic write (0o600)

    Args:
        key_path: 鍵ファイルのパス
        env_var:  環境変数名（省略可）

    Returns:
        鍵データ (bytes)
    """
    # 1. 環境変数
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val and len(env_val) >= 32:
            return env_val.encode("utf-8")

    # 2. ファイルから読み込み
    if key_path.exists():
        try:
            key_data = key_path.read_text(encoding="utf-8").strip()
            if key_data and len(key_data) >= 32:
                return key_data.encode("utf-8")
            elif key_data:
                logger.warning(
                    "鍵ファイルの鍵長が不十分です（%d文字）。再生成します。",
                    len(key_data),
                )
        except Exception:
            pass

    # 3. 新規生成 + atomic write
    key_str = hashlib.sha256(os.urandom(32)).hexdigest()
    key_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(key_path.parent), prefix=".signing_key_tmp_"
    )
    try:
        os.write(fd, key_str.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(key_path))
        try:
            safe_chmod(str(key_path), 0o600)
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

    return key_str.encode("utf-8")


def compute_data_hmac(key: bytes, data_dict: Dict[str, Any]) -> str:
    """
    data_dict の HMAC-SHA256 署名を計算する。

    '_hmac' で始まるキーは署名対象から除外する。

    Args:
        key:       署名鍵 (bytes)
        data_dict: 署名対象のデータ

    Returns:
        hex ダイジェスト文字列
    """
    filtered = {k: v for k, v in data_dict.items() if not k.startswith("_hmac")}
    payload = json.dumps(filtered, sort_keys=True, ensure_ascii=False)
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_data_hmac(
    key: bytes,
    data_dict: Dict[str, Any],
    expected_hmac: str,
) -> bool:
    """
    data_dict の HMAC-SHA256 署名を検証する。

    Args:
        key:           署名鍵 (bytes)
        data_dict:     検証対象のデータ
        expected_hmac: 期待される hex ダイジェスト

    Returns:
        True: 署名一致 / False: 不一致
    """
    computed = compute_data_hmac(key, data_dict)
    return hmac.compare_digest(computed, expected_hmac)


# ======================================================================
# HMACKey データクラス
# ======================================================================

@dataclass
class HMACKey:
    """HMAC鍵情報"""
    key: str
    created_at: str
    rotated_at: Optional[str] = None  # この鍵がローテーションで退役した時刻
    is_active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "created_at": self.created_at,
            "rotated_at": self.rotated_at,
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HMACKey":
        return cls(
            key=data["key"],
            created_at=data["created_at"],
            rotated_at=data.get("rotated_at"),
            is_active=data.get("is_active", True),
        )


# ======================================================================
# HMACKeyManager
# ======================================================================

class HMACKeyManager:
    """
    HMAC鍵のライフサイクル管理

    機能:
    - 初回起動時に鍵を自動生成
    - ローテーション（新鍵生成 + 旧鍵をグレースピリオド付きで保持）
    - グレースピリオド内は旧鍵でも検証可能
    - グレースピリオド超過した旧鍵は自動削除

    使い方:
        manager = HMACKeyManager()
        current_token = manager.get_active_key()
        is_valid = manager.verify_token(token_from_request)
    """

    def __init__(
        self,
        keys_path: Optional[str] = None,
        grace_period_seconds: int = DEFAULT_GRACE_PERIOD_SECONDS,
    ):
        """
        Args:
            keys_path: 鍵ファイルのパス。None の場合は RUMI_USER_DATA
                （未設定時のみ bundled runtime の user_data）
            grace_period_seconds: グレースピリオド（秒）
        """
        if keys_path is None:
            configured_user_data = os.environ.get("RUMI_USER_DATA", "").strip()
            if configured_user_data:
                keys_path = str(Path(configured_user_data) / _DEFAULT_KEYS_FILENAME)
            else:
                try:
                    from .paths import USER_DATA_DIR
                    keys_path = str(USER_DATA_DIR / _DEFAULT_KEYS_FILENAME)
                except ImportError:
                    keys_path = os.path.join(
                        _DEFAULT_KEYS_SUBDIR,
                        _DEFAULT_KEYS_FILENAME,
                    )

        self._keys_path = Path(keys_path)
        self._grace_period = timedelta(seconds=grace_period_seconds)
        self._keys: List[HMACKey] = []
        self._lock = threading.Lock()

        # 鍵をロードまたは初回生成
        self._load_or_initialize()

        # 環境変数によるローテーショントリガー
        if os.environ.get("RUMI_HMAC_ROTATE", "").lower() == "true":
            self.rotate()
            # 一度ローテーションしたらフラグをクリア（同一プロセス内での再トリガー防止）
            os.environ.pop("RUMI_HMAC_ROTATE", None)


    # ==================================================================
    # 暗号化関連メソッド (W21-B)
    # ==================================================================

    def _get_enc_key_path(self) -> Path:
        """暗号化鍵ファイルのパスを返す"""
        return self._keys_path.with_suffix(".key")

    @staticmethod
    def _is_encrypted_format(data: dict) -> bool:
        """ファイル内容が暗号化形式かどうかを判定する"""
        return (
            isinstance(data, dict)
            and data.get("encryption") == "fernet"
            and "payload" in data
        )

    def _check_security_mode(self) -> str:
        """RUMI_SECURITY_MODE を返す。デフォルトは strict。"""
        return os.environ.get("RUMI_SECURITY_MODE", "strict").lower()

    def _get_encryption_key(self) -> bytes:
        """
        暗号化鍵をロードまたは新規生成する。

        Returns:
            Fernet 鍵 (bytes, URL-safe base64)

        Raises:
            RuntimeError: cryptography が利用不可の場合
        """
        if not _FERNET_AVAILABLE:
            raise RuntimeError(
                "cryptography パッケージが必要です。"
                " pip install cryptography でインストールしてください。"
            )
        enc_key_path = self._get_enc_key_path()
        if enc_key_path.exists():
            try:
                key_data = enc_key_path.read_bytes().strip()
                # 有効な Fernet 鍵か検証
                _Fernet(key_data)
                return key_data
            except Exception:
                pass
        # 新規生成
        new_key = _Fernet.generate_key()
        enc_key_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(enc_key_path.parent),
            prefix=".hmac_enc_key_tmp_",
        )
        try:
            os.write(fd, new_key)
            os.close(fd)
            fd = -1
            os.replace(tmp_path, str(enc_key_path))
            try:
                safe_chmod(str(enc_key_path), 0o600)
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
        # 監査ログ (best-effort)
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_security_event(
                event_type="hmac_encryption_key_generated",
                severity="info",
                description="HMAC暗号化鍵を新規生成しました",
            )
        except Exception:
            pass
        logger.info("HMAC暗号化鍵を新規生成しました: %s", enc_key_path)
        return new_key

    def _encrypt_data(self, data_str: str) -> str:
        """
        Fernet で文字列を暗号化し、base64 トークンを返す。

        Args:
            data_str: 暗号化する JSON 文字列

        Returns:
            Base64 エンコードされた Fernet トークン文字列
        """
        enc_key = self._get_encryption_key()
        f = _Fernet(enc_key)
        token = f.encrypt(data_str.encode("utf-8"))
        return token.decode("ascii")

    def _decrypt_data(self, payload: str) -> str:
        """
        Fernet トークンを復号して元の文字列を返す。

        Args:
            payload: Base64 エンコードされた Fernet トークン

        Returns:
            復号された JSON 文字列

        Raises:
            Exception: 復号に失敗した場合
        """
        enc_key = self._get_encryption_key()
        f = _Fernet(enc_key)
        return f.decrypt(payload.encode("ascii")).decode("utf-8")

    def _load_or_initialize(self) -> None:
        """鍵ファイルをロード。存在しなければ新規生成。(W21-B: 暗号化対応)"""
        with self._lock:
            if self._keys_path.exists():
                try:
                    with open(self._keys_path, "r", encoding="utf-8") as f:
                        raw_data = json.load(f)

                    # --- W21-B: 暗号化ファイル判定 ---
                    keys_data = None
                    if self._is_encrypted_format(raw_data):
                        # Fernet 暗号化ファイル → 復号を試みる
                        try:
                            decrypted_str = self._decrypt_data(raw_data["payload"])
                            keys_data = json.loads(decrypted_str)
                        except Exception as dec_err:
                            logger.warning(
                                "暗号化鍵ファイルの復号に失敗しました。"
                                "バックアップ後に新規生成します: %s", dec_err
                            )
                            try:
                                from .audit_logger import get_audit_logger
                                get_audit_logger().log_security_event(
                                    event_type="hmac_key_decryption_failed",
                                    severity="warning",
                                    description="HMAC鍵ファイルの復号に失敗しました",
                                    details={"error": str(dec_err)},
                                )
                            except Exception:
                                pass
                            try:
                                bak_path = self._keys_path.with_suffix(".json.bak")
                                shutil.copy2(str(self._keys_path), str(bak_path))
                            except Exception:
                                pass
                            self._keys = []
                            self._generate_new_key_internal()
                            return
                    else:
                        # 平文 JSON (レガシー or permissive フォールバック)
                        keys_data = raw_data
                        if keys_data.get("keys"):
                            # レガシーファイルからの読み込み → 次回保存で暗号化
                            try:
                                from .audit_logger import get_audit_logger
                                get_audit_logger().log_security_event(
                                    event_type="hmac_key_legacy_migration",
                                    severity="info",
                                    description="レガシー平文鍵ファイルを読み込みました。次回保存時に暗号化されます。",
                                )
                            except Exception:
                                pass
                            logger.info(
                                "レガシー平文鍵ファイルを読み込みました。"
                                "次回保存時に暗号化形式にマイグレーションされます。"
                            )

                    self._keys = [HMACKey.from_dict(k) for k in keys_data.get("keys", [])]
                    # 期限切れの旧鍵を削除
                    self._cleanup_expired_keys_internal()
                    # アクティブ鍵がなければ新規生成
                    if not any(k.is_active for k in self._keys):
                        self._generate_new_key_internal()
                    return
                except (json.JSONDecodeError, KeyError, IOError, OSError) as e:
                    logger.warning(
                        "鍵ファイル読み込みエラー、バックアップ後に新規生成します: %s", e
                    )
                    # 破損ファイルをバックアップ
                    try:
                        bak_path = self._keys_path.with_suffix(".json.bak")
                        shutil.copy2(str(self._keys_path), str(bak_path))
                    except Exception:
                        pass

            # 初回 or リカバリー: 新規生成
            self._keys = []
            self._generate_new_key_internal()

    def _generate_new_key_internal(self) -> HMACKey:
        """新しいアクティブ鍵を生成（ロック保持状態で呼び出す内部用）"""
        new_key = HMACKey(
            key=secrets.token_urlsafe(32),
            created_at=_now_ts(),
            is_active=True,
        )
        self._keys.append(new_key)
        self._save_internal()
        return new_key

    def _save_internal(self) -> None:
        """鍵ファイルを atomic write で保存（ロック保持状態で呼び出す内部用）(W21-B: 暗号化対応)"""
        self._keys_path.parent.mkdir(parents=True, exist_ok=True)
        inner_data = {
            "version": "1.0",
            "updated_at": _now_ts(),
            "grace_period_seconds": int(self._grace_period.total_seconds()),
            "keys": [k.to_dict() for k in self._keys],
        }
        inner_json = json.dumps(inner_data, ensure_ascii=False, indent=2)

        # --- W21-B: 暗号化ラッパー ---
        security_mode = self._check_security_mode()
        use_encryption = False

        if _FERNET_AVAILABLE:
            use_encryption = True
        elif security_mode == "strict":
            raise RuntimeError(
                "RUMI_SECURITY_MODE=strict ですが cryptography パッケージが"
                "インストールされていません。暗号化保存ができないため中断します。"
                " pip install cryptography でインストールしてください。"
            )
        else:
            # permissive: 平文フォールバック
            logger.warning(
                "cryptography パッケージが利用不可のため、平文で保存します。"
                "セキュリティ向上のために pip install cryptography を推奨します。"
            )
            try:
                from .audit_logger import get_audit_logger
                get_audit_logger().log_security_event(
                    event_type="hmac_key_plaintext_fallback",
                    severity="warning",
                    description="cryptography未インストールのため平文保存にフォールバックしました",
                )
            except Exception:
                pass

        if use_encryption:
            try:
                encrypted_payload = self._encrypt_data(inner_json)
                outer_data = {
                    "version": "1.0",
                    "encryption": "fernet",
                    "payload": encrypted_payload,
                }
                content = json.dumps(outer_data, ensure_ascii=False, indent=2)
            except Exception as enc_err:
                if security_mode == "strict":
                    raise
                logger.warning("暗号化に失敗しました。平文で保存します: %s", enc_err)
                content = inner_json
        else:
            content = inner_json

        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._keys_path.parent),
            prefix=".hmac_keys_tmp_",
            suffix=".json",
        )
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            fd = -1
            os.replace(tmp_path, str(self._keys_path))
            try:
                safe_chmod(str(self._keys_path), 0o600)
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

    def _cleanup_expired_keys_internal(self) -> None:
        """グレースピリオド超過した旧鍵を削除（ロック保持状態）"""
        now = _now_utc()
        surviving: List[HMACKey] = []
        for key in self._keys:
            if key.is_active:
                surviving.append(key)
                continue
            # 退役済み鍵: グレースピリオド内なら保持
            if key.rotated_at:
                try:
                    rotated_time = _parse_ts(key.rotated_at)
                    if now - rotated_time < self._grace_period:
                        surviving.append(key)
                        continue
                except (ValueError, TypeError):
                    pass
            # グレースピリオド超過または rotated_at なし → 削除
        if len(surviving) != len(self._keys):
            self._keys = surviving
            self._save_internal()
        else:
            self._keys = surviving

    def get_active_key(self) -> str:
        """現在のアクティブ鍵を取得"""
        with self._lock:
            for key in self._keys:
                if key.is_active:
                    return key.key
            # アクティブ鍵がない（理論上ないはず）→ 生成
            new_key = self._generate_new_key_internal()
            return new_key.key

    def verify_token(self, token: str) -> bool:
        """
        トークンを検証

        アクティブ鍵 + グレースピリオド内の旧鍵すべてで照合する。
        1つでも一致すれば True。

        Args:
            token: 検証するトークン文字列

        Returns:
            True: 有効な鍵で署名されている
            False: どの鍵にも一致しない
        """
        if not token:
            return False
        with self._lock:
            # Enforce grace-period expiry at authentication time.  Retired
            # keys may remain in memory after rotate(), so cleanup cannot be
            # limited to load/rotation paths.
            self._cleanup_expired_keys_internal()
            for key in self._keys:
                if hmac.compare_digest(token, key.key):
                    return True
            return False

    def rotate(self) -> str:
        """
        鍵をローテーション

        現在のアクティブ鍵を退役させ（グレースピリオド付き）、
        新しいアクティブ鍵を生成する。

        Returns:
            新しいアクティブ鍵
        """
        with self._lock:
            now_str = _now_ts()
            # 現在のアクティブ鍵を退役
            for key in self._keys:
                if key.is_active:
                    key.is_active = False
                    key.rotated_at = now_str
            # 期限切れの旧鍵を削除
            self._cleanup_expired_keys_internal()
            # 新しいアクティブ鍵を生成
            new_key = self._generate_new_key_internal()

            # 監査ログ（best-effort）
            try:
                from .audit_logger import get_audit_logger
                audit = get_audit_logger()
                audit.log_security_event(
                    event_type="hmac_key_rotated",
                    severity="info",
                    description="HMAC API key rotated",
                    details={
                        "new_key_created_at": new_key.created_at,
                        "grace_period_seconds": int(self._grace_period.total_seconds()),
                        "total_keys": len(self._keys),
                    },
                )
            except Exception:
                pass

            logger.info(
                "鍵ローテーション完了。アクティブ鍵数: %d, グレースピリオド中の旧鍵数: %d",
                sum(1 for k in self._keys if k.is_active),
                sum(1 for k in self._keys if not k.is_active),
            )

            return new_key.key

    # エイリアス（T-018 指示による追加）
    rotate_key = rotate

    def get_key_info(self) -> Dict[str, Any]:
        """鍵の状態情報を取得（デバッグ/管理用、鍵の値は含めない）"""
        with self._lock:
            return {
                "total_keys": len(self._keys),
                "active_keys": sum(1 for k in self._keys if k.is_active),
                "grace_period_keys": sum(1 for k in self._keys if not k.is_active),
                "grace_period_seconds": int(self._grace_period.total_seconds()),
                "keys_path": str(self._keys_path),
                "keys": [
                    {
                        "created_at": k.created_at,
                        "rotated_at": k.rotated_at,
                        "is_active": k.is_active,
                        "key_prefix": k.key[:8] + "...",  # 先頭8文字のみ
                    }
                    for k in self._keys
                ],
            }


def get_hmac_key_manager() -> HMACKeyManager:
    """
    グローバルな HMACKeyManager を取得する（遅延初期化）。

    DI コンテナ経由でキャッシュされる。

    Returns:
        HMACKeyManager インスタンス
    """
    from .di_container import get_container
    return get_container().get("hmac_key_manager")


def initialize_hmac_key_manager(
    keys_path: Optional[str] = None,
    grace_period_seconds: int = DEFAULT_GRACE_PERIOD_SECONDS,
) -> HMACKeyManager:
    """
    HMACKeyManager を明示的に初期化する。

    指定パラメータで新しいインスタンスを生成し、DI コンテナに設定する。

    Args:
        keys_path:            鍵ファイルのパス
        grace_period_seconds: グレースピリオド（秒）

    Returns:
        新しい HMACKeyManager インスタンス
    """
    from .di_container import get_container
    instance = HMACKeyManager(
        keys_path=keys_path,
        grace_period_seconds=grace_period_seconds,
    )
    get_container().set_instance("hmac_key_manager", instance)
    return instance


def reset_hmac_key_manager() -> None:
    """
    HMACKeyManager をリセットする（テスト用）。

    DI コンテナのキャッシュを破棄する。
    次回 get_hmac_key_manager() で再生成される。
    """
    from .di_container import get_container
    get_container().reset("hmac_key_manager")
