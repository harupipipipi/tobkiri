"""
secrets_store.py - Secrets 管理（at-rest 暗号化対応）

user_data/secrets/ に秘密を暗号化して保存する。
運用API は list(mask) / set / delete のみ。get（再表示）は絶対に提供しない。
値はログ・監査・例外に絶対出さない。

保存: 1 key 1 file (user_data/secrets/<KEY>.json)
KEY制約: ^[A-Z0-9_]{1,64}$
削除: tombstone (deleted_at を入れ、value は空にする)
journal: user_data/secrets/journal.jsonl (値/長さ/ハッシュは入れない)

暗号化:
- Fernet (cryptography パッケージ) を使用（必須依存）
- 暗号化キー: 環境変数 RUMI_SECRETS_KEY → user_data/.secrets_key → 自動生成
- 後方互換性: 平文データ読み込み時に自動暗号化マイグレーション

平文フォールバックポリシー:
- 環境変数 RUMI_SECRETS_ALLOW_PLAINTEXT (デフォルト "auto")
  - "auto": 未暗号化シークレットが存在する間は平文を許可。全て暗号化済みになったら自動で禁止
  - "true": 常に平文を許可（マイグレーション中の一時使用）
  - "false": 常に平文を禁止
- マイグレーション完了マーカー: user_data/secrets/.migration_complete

セキュリティモード:
- 環境変数 RUMI_SECURITY_MODE (デフォルト "strict")
  - "strict": auto モードでもマーカーに関係なく平文フォールバックを禁止
  - "permissive": 従来のマーカーベースの判定を使用
- 平文フォールバック発生時に severity=critical の監査ログを記録
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
from typing import Any, Dict, List, Optional

from .compat import safe_chmod
from cryptography.fernet import Fernet


logger = logging.getLogger(__name__)

SECRETS_DIR = "user_data/secrets"
KEY_PATTERN = re.compile(r"^[A-Z0-9_]{1,64}$")
SECRETS_KEY_FILE_NAME = ".secrets_key"

# Fernet トークンは常に "gAAAAA" で始まる
_FERNET_PREFIX = "gAAAAA"

PLAINTEXT_POLICY_ENV = "RUMI_SECRETS_ALLOW_PLAINTEXT"
MIGRATION_MARKER_FILE = ".migration_complete"


# ------------------------------------------------------------------
# 暗号化バックエンド
# ------------------------------------------------------------------

class _CryptoBackend:
    """Fernet 暗号化バックエンド"""

    def __init__(self, key_path: Path) -> None:
        self._key_path = Path(key_path)
        self._fernet: Optional[Fernet] = None
        self._initialized = False
        self._init_lock = threading.Lock()

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self._setup()
            self._initialized = True

    def _setup(self) -> None:
        key_bytes = self._load_or_generate_key()
        self._fernet = Fernet(key_bytes)
        logger.info("Secrets encryption: Fernet (cryptography) enabled.")

    def _load_or_generate_key(self) -> bytes:
        """暗号化キーをロード、なければ生成して保存"""
        # 1. Explicit host contract (never ambient process environment)
        from .host_contract import host_contract_value

        env_key = host_contract_value("secrets_store_key")
        if env_key:
            logger.debug("Using encryption key from the host credential contract.")
            return env_key.encode("utf-8")

        # 2. ファイルから取得
        if self._key_path.exists():
            try:
                key_data = self._key_path.read_text(encoding="utf-8").strip()
                if key_data:
                    logger.debug("Using encryption key from %s.", self._key_path)
                    return key_data.encode("utf-8")
            except Exception as e:
                logger.warning("Failed to read key file %s: %s", self._key_path, e)

        # 3. 自動生成
        new_key = Fernet.generate_key()

        # ファイルに保存 (atomic write)
        try:
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._key_path.parent), prefix=".secrets_key_tmp_"
            )
            try:
                try:
                    safe_chmod(tmp_path, 0o600)
                except (OSError, AttributeError):
                    pass
                os.write(fd, new_key if isinstance(new_key, bytes) else new_key.encode("utf-8"))
                os.close(fd)
                fd = -1
                os.replace(tmp_path, str(self._key_path))
                try:
                    safe_chmod(str(self._key_path), 0o600)
                except (OSError, AttributeError):
                    pass
                logger.info("Generated new encryption key → %s", self._key_path)
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
        except Exception as e:
            logger.warning("Failed to save key to %s: %s", self._key_path, e)

        return new_key if isinstance(new_key, bytes) else new_key.encode("utf-8")

    def encrypt(self, plaintext: str) -> str:
        """平文を暗号化して文字列を返す"""
        self._ensure_initialized()
        fernet = self._fernet
        if fernet is None:
            raise RuntimeError("secrets encryption backend was not initialized")
        return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str, *, allow_plaintext: bool = False) -> str:
        """暗号文を復号して平文を返す

        Args:
            ciphertext: 暗号化された文字列（または平文）
            allow_plaintext: True の場合、非 Fernet 値を平文として返す。
                             False の場合、非 Fernet 値は ValueError を発生。
        """
        self._ensure_initialized()
        fernet = self._fernet
        if fernet is None:
            raise RuntimeError("secrets encryption backend was not initialized")
        if ciphertext.startswith(_FERNET_PREFIX):
            return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        # 非 Fernet 値 — ポリシーに従う
        if allow_plaintext:
            logger.warning(
                "Plaintext fallback used during decryption. "
                "This secret should be migrated to encrypted storage."
            )
            return ciphertext
        raise ValueError(
            "Decryption failed: value is not a valid Fernet token and "
            "plaintext fallback is disabled by policy."
        )

    def is_encrypted(self, value: str) -> bool:
        """値が暗号化済みかどうか判定"""
        if not isinstance(value, str):
            return False
        return value.startswith(_FERNET_PREFIX)


# ------------------------------------------------------------------
# データクラス（公開API互換 — 変更なし）
# ------------------------------------------------------------------

@dataclass
class SecretMeta:
    key: str
    exists: bool
    deleted: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    deleted_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "key": self.key,
            "exists": self.exists,
            "deleted": self.deleted,
        }
        if self.created_at:
            d["created_at"] = self.created_at
        if self.updated_at:
            d["updated_at"] = self.updated_at
        if self.deleted_at:
            d["deleted_at"] = self.deleted_at
        return d


@dataclass
class SecretSetResult:
    success: bool
    key: str = ""
    created: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "key": self.key,
            "created": self.created,
            "error": self.error,
        }


@dataclass
class SecretDeleteResult:
    success: bool
    key: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "key": self.key,
            "error": self.error,
        }


# ------------------------------------------------------------------
# Atomic write ヘルパー
# ------------------------------------------------------------------

def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """JSON データを atomic に書き込む（tempfile → os.replace）"""
    content = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}_tmp_", suffix=".json"
    )
    try:
        try:
            safe_chmod(tmp_path, 0o600)
        except (OSError, AttributeError):
            pass
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(path))
        try:
            safe_chmod(str(path), 0o600)
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
# SecretsStore
# ------------------------------------------------------------------

class SecretsStore:
    """
    Secrets 管理（at-rest 暗号化対応）

    API:
    - list_keys() -> mask list
    - set_secret(key, value) -> set
    - delete_secret(key) -> tombstone delete
    - has_secret(key) -> exists check（値は返さない）
    - _read_value(key) -> 内部専用（外部APIには絶対公開しない）
    """

    def __init__(self, secrets_dir: Optional[str] = None):
        self._secrets_dir = Path(secrets_dir or SECRETS_DIR)
        self._secrets_key_path = self._secrets_dir.parent / SECRETS_KEY_FILE_NAME
        self._crypto = _CryptoBackend(self._secrets_key_path)
        self._lock = threading.RLock()
        self._secrets_dir.mkdir(parents=True, exist_ok=True)
        # auto モードの初期化: マーカーがなければ全スキャンして判定
        self._init_migration_marker()

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def validate_key(key: str) -> Optional[str]:
        if not key:
            return "Key is empty"
        if not KEY_PATTERN.match(key):
            return "Invalid key: must match ^[A-Z0-9_]{1,64}$"
        return None

    def _key_path(self, key: str) -> Path:
        return self._secrets_dir / f"{key}.json"

    # ----------------------------------------------------------
    # 平文フォールバック ポリシー
    # ----------------------------------------------------------

    @staticmethod
    def _get_plaintext_policy() -> str:
        """環境変数から平文ポリシーを取得する。

        Returns:
            "auto" | "true" | "false"
        """
        raw = os.environ.get(PLAINTEXT_POLICY_ENV, "auto").strip().lower()
        if raw in ("auto", "true", "false"):
            return raw
        logger.warning(
            "Invalid %s value '%s'; falling back to 'auto'.",
            PLAINTEXT_POLICY_ENV, raw,
        )
        return "auto"

    def _migration_marker_path(self) -> Path:
        return self._secrets_dir / MIGRATION_MARKER_FILE

    def _has_migration_marker(self) -> bool:
        """マイグレーション完了マーカーが存在するか"""
        return self._migration_marker_path().exists()

    def _write_migration_marker(self) -> None:
        """マイグレーション完了マーカーを書き込む"""
        try:
            marker_path = self._migration_marker_path()
            marker_path.write_text(
                json.dumps({
                    "completed_at": self._now_ts(),
                    "note": "All secrets migrated to encrypted storage.",
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("Migration complete marker written: %s", marker_path)
        except Exception as e:
            logger.warning("Failed to write migration marker: %s", e)

    def _check_all_encrypted(self) -> bool:
        """全ての既存シークレットが暗号化済みか確認する。

        削除済み(tombstone)のシークレットは空文字なのでスキップ。
        """
        if not self._secrets_dir.exists():
            return True
        for f in self._secrets_dir.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                # tombstone は value が空文字 → 暗号化不要
                if data.get("deleted_at"):
                    continue
                raw_value = data.get("value", "")
                if raw_value and not self._crypto.is_encrypted(raw_value):
                    return False
            except Exception:
                continue
        return True

    def _init_migration_marker(self) -> None:
        """起動時にマイグレーション状態を確認し、必要に応じてマーカーを書き込む。

        auto モードでマーカーが未作成の場合のみ全スキャンを実行する。
        全シークレットが暗号化済みであればマーカーを書き込み、
        以降のポリシー判定を O(1) にする。
        """
        if self._get_plaintext_policy() != "auto":
            return
        if self._has_migration_marker():
            return
        if self._check_all_encrypted():
            self._write_migration_marker()

    def _is_plaintext_allowed(self) -> bool:
        """現在のポリシーに基づき平文フォールバックが許可されるか判定する。

        Returns:
            True: 平文フォールバックを許可
            False: 平文フォールバックを拒否
        """
        policy = self._get_plaintext_policy()

        if policy == "true":
            return True
        if policy == "false":
            return False

        # policy == "auto"
        security_mode = os.environ.get("RUMI_SECURITY_MODE", "strict").lower()
        if security_mode == "strict":
            return False  # strict モードでは auto でも平文禁止
        # permissive: マーカーベースの従来判定
        # マーカーが存在する → 全暗号化済み → 平文禁止
        # マーカーが存在しない → まだ未暗号化シークレットあり → 平文許可
        return not self._has_migration_marker()

    # ----------------------------------------------------------

    def set_secret(
        self,
        key: str,
        value: str,
        actor: str = "api_user",
        reason: str = "",
    ) -> SecretSetResult:
        err = self.validate_key(key)
        if err:
            return SecretSetResult(success=False, key=key, error=err)

        with self._lock:
            path = self._key_path(key)
            created = not path.exists()

            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    if existing.get("deleted_at"):
                        created = True
                except Exception:
                    created = True

            now = self._now_ts()
            encrypted_value = self._crypto.encrypt(value)
            data = {
                "key": key,
                "value": encrypted_value,
                "created_at": now if created else self._read_meta_field(key, "created_at", now),
                "updated_at": now,
                "deleted_at": None,
            }

            try:
                _atomic_write_json(path, data)
            except Exception:
                return SecretSetResult(
                    success=False, key=key,
                    error="Failed to write secret file",
                )

            self._append_journal("set", key, actor, reason)
            self._audit("secret_set", True, {"key": key, "created": created, "actor": actor})
            return SecretSetResult(success=True, key=key, created=created)

    def delete_secret(
        self,
        key: str,
        actor: str = "api_user",
        reason: str = "",
    ) -> SecretDeleteResult:
        err = self.validate_key(key)
        if err:
            return SecretDeleteResult(success=False, key=key, error=err)

        with self._lock:
            path = self._key_path(key)
            if not path.exists():
                return SecretDeleteResult(
                    success=False, key=key, error=f"Secret not found: {key}",
                )

            now = self._now_ts()
            data = {
                "key": key,
                "value": "",
                "created_at": self._read_meta_field(key, "created_at", now),
                "updated_at": now,
                "deleted_at": now,
            }

            try:
                _atomic_write_json(path, data)
            except Exception:
                return SecretDeleteResult(
                    success=False, key=key, error="Failed to write tombstone",
                )

            self._append_journal("deleted", key, actor, reason)
            self._audit("secret_deleted", True, {"key": key, "actor": actor})
            return SecretDeleteResult(success=True, key=key)

    def list_keys(self) -> List[SecretMeta]:
        results: List[SecretMeta] = []
        with self._lock:
            if not self._secrets_dir.exists():
                return results
            for f in sorted(self._secrets_dir.glob("*.json")):
                try:
                    with open(f, "r", encoding="utf-8") as fp:
                        data = json.load(fp)
                    deleted_at = data.get("deleted_at")
                    results.append(SecretMeta(
                        key=data.get("key", f.stem),
                        exists=not bool(deleted_at),
                        deleted=bool(deleted_at),
                        created_at=data.get("created_at"),
                        updated_at=data.get("updated_at"),
                        deleted_at=deleted_at,
                    ))
                except Exception:
                    continue
        return results

    def has_secret(self, key: str) -> bool:
        if self.validate_key(key):
            return False
        with self._lock:
            path = self._key_path(key)
            if not path.exists():
                return False
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return not bool(data.get("deleted_at"))
            except Exception:
                return False

    def _read_value(self, key: str) -> Optional[str]:
        """内部専用。API からは絶対に呼ばない。"""
        if self.validate_key(key):
            return None
        with self._lock:
            path = self._key_path(key)
            if not path.exists():
                return None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("deleted_at"):
                    return None
                raw_value = data.get("value")
                if raw_value is None:
                    return None

                # 平文ポリシー判定
                allow_plaintext = self._is_plaintext_allowed()

                # 復号
                try:
                    plaintext = self._crypto.decrypt(
                        raw_value, allow_plaintext=allow_plaintext
                    )
                except Exception as e:
                    logger.error("Failed to decrypt secret '%s': %s", key, e)
                    return None

                # 平文データの自動マイグレーション
                if not self._crypto.is_encrypted(raw_value):
                    # CRITICAL 監査ログ: 平文フォールバックが発生
                    self._audit("plaintext_fallback", True, {
                        "key": key,
                        "severity": "critical",
                        "message": "Plaintext fallback used for secret read. Migration required.",
                    })
                    self._migrate_to_encrypted(key, data, plaintext)

                return plaintext
            except Exception:
                return None

    def _internal_read_value(self, key: str, caller_id: str = "") -> Optional[str]:
        """内部サービス専用の値読み取り。

        SecretsGrantManager からのみ呼ばれる。
        呼び出し元を監査ログに記録する。

        Args:
            key: Secret キー名
            caller_id: 呼び出し元の識別子（監査ログ用）

        Returns:
            復号済みの Secret 値、または None
        """
        self._audit("secret_internal_read", True, {
            "key": key, "caller": caller_id,
        })
        return self._read_value(key)

    def _migrate_to_encrypted(
        self, key: str, data: Dict[str, Any], plaintext: str
    ) -> None:
        """平文の secret を暗号化して書き直す（自動マイグレーション）"""
        try:
            encrypted_value = self._crypto.encrypt(plaintext)
            migrated_data = dict(data)
            migrated_data["value"] = encrypted_value
            path = self._key_path(key)
            _atomic_write_json(path, migrated_data)
            logger.info("Migrated secret '%s' from plaintext to encrypted storage.", key)

            # マイグレーション後に全暗号化チェック → マーカー書き込み
            if self._get_plaintext_policy() == "auto" and not self._has_migration_marker():
                if self._check_all_encrypted():
                    self._write_migration_marker()

        except Exception as e:
            # マイグレーション失敗でも元データは atomic write により保持
            logger.warning(
                "Failed to migrate secret '%s' to encrypted storage: %s. "
                "Plaintext data is preserved.", key, e
            )

    def _read_meta_field(self, key: str, field: str, default: str = "") -> str:
        try:
            path = self._key_path(key)
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f).get(field, default)
        except Exception:
            pass
        return default

    def _append_journal(
        self, action: str, key: str, actor: str, reason: str = "",
    ) -> None:
        with self._lock:
            entry: Dict[str, Any] = {
                "ts": self._now_ts(),
                "action": action,
                "key": key,
                "actor": actor,
            }
            if reason:
                entry["reason"] = reason
            try:
                with open(self._secrets_dir / "journal.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass

    @staticmethod
    def _audit(event_type: str, success: bool, details: Dict[str, Any]) -> None:
        try:
            from .audit_logger import get_audit_logger
            get_audit_logger().log_system_event(
                event_type=event_type, success=success, details=details,
            )
        except Exception:
            pass


# グローバル変数（後方互換のため残存。DI コンテナ優先）
_global_secrets_store: Optional[SecretsStore] = None
_secrets_lock = threading.Lock()


def get_secrets_store() -> SecretsStore:
    """
    グローバルな SecretsStore を取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。

    Returns:
        SecretsStore インスタンス
    """
    from .di_container import get_container
    return get_container().get("secrets_store")


def reset_secrets_store(secrets_dir: str | None = None) -> SecretsStore:
    """
    SecretsStore をリセットする（テスト用）。

    新しいインスタンスを生成し、DI コンテナのキャッシュを置き換える。

    Args:
        secrets_dir: Secrets ディレクトリ（省略時はデフォルト）

    Returns:
        新しい SecretsStore インスタンス
    """
    global _global_secrets_store
    with _secrets_lock:
        _global_secrets_store = SecretsStore(secrets_dir)
    # DI コンテナのキャッシュも更新（_secrets_lock の外で実行してデッドロック回避）
    from .di_container import get_container
    get_container().set_instance("secrets_store", _global_secrets_store)
    return _global_secrets_store
