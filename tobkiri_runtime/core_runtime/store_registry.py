"""
store_registry.py - ストア定義・作成・列挙・削除 (SQLite / DI Container 対応)

Store（共有領域）を管理する。
公式は "tool/chat/asset" の意味を一切解釈しない。

永続化: SQLite (user_data/stores/stores.db)  WAL モード

追加機能:
- #62 create_store_for_pack: 宣言的Store作成
- #6  cas: Compare-And-Swap (BEGIN IMMEDIATE + value_hash)
- I-1  audit_store_usage: ストアキーサイズ集計
- #18 list_keys: ページネーション付きキー列挙
- #19 batch_get: 複数キー一括取得

T-041 改善:
- 接続ヘルスチェック (60秒間隔 SELECT 1)
- store_data.store_id インデックス追加
- close 時 WAL チェックポイント (PASSIVE)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

STORES_INDEX_PATH = "user_data/stores/index.json"
STORES_BASE_DIR = Path("user_data/stores")
STORES_DB_PATH = "user_data/stores/stores.db"
MAX_STORES_PER_PACK = 10
MAX_VALUE_BYTES_CAS = 1 * 1024 * 1024  # 1MB
CAS_LOCK_TIMEOUT = 5  # seconds (互換用に残す)

# 接続ヘルスチェック間隔 (秒)
_HEALTH_CHECK_INTERVAL = 60.0

# Sentinel: CAS で「キーが存在しないことを期待する」ことを示す。
# 従来は expected_value=None がこの意味だったが、JSON null を期待する
# ユースケースに対応するため sentinel に変更。
# **破壊的変更**: 既存の expected_value=None 呼び出しは
# 「JSON null を期待」に意味が変わる。「キー不存在を期待」する場合は
# expected_value を省略する（デフォルト _EXPECT_MISSING が適用される）。
_EXPECT_MISSING = object()

# key バリデーション用パターン（スラッシュ許可 — キー階層で使用中）
_KEY_PATTERN = re.compile(r'^[a-zA-Z0-9_/.:\-]{1,512}$')


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _validate_store_path(root_path: str) -> Optional[str]:
    """
    root_path が STORES_BASE_DIR 配下であることを検証する。

    Returns:
        エラーメッセージ (問題がなければ None)
    """
    if ".." in str(root_path):
        return "root_path must not contain '..'"
    resolved = Path(root_path).resolve()
    base = STORES_BASE_DIR.resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        return f"root_path must be under {STORES_BASE_DIR}/"
    return None


def _validate_key(key: str) -> Optional[str]:
    """
    Store key (または prefix) の文字種・長さを検証する。

    許可パターン: ^[a-zA-Z0-9_/.:-]{1,512}$

    Args:
        key: 検証対象のキー文字列

    Returns:
        エラーメッセージ (問題がなければ None)
    """
    if not isinstance(key, str) or not _KEY_PATTERN.match(key):
        return f"Invalid key: must match {_KEY_PATTERN.pattern} (got {repr(key)[:80]})"
    return None


def _normalize_value_hash(value: Any) -> str:
    """
    値の正規化ハッシュ (SHA-256) を計算する。

    json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    の SHA-256 hex digest を返す。CAS 比較に使用する。

    Args:
        value: ハッシュ対象の Python オブジェクト

    Returns:
        SHA-256 hex digest 文字列
    """
    canonical = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class StoreDefinition:
    store_id: str
    root_path: str
    created_at: str
    created_by: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "store_id": self.store_id,
            "root_path": self.root_path,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StoreDefinition":
        return cls(
            store_id=data.get("store_id", ""),
            root_path=data.get("root_path", ""),
            created_at=data.get("created_at", ""),
            created_by=data.get("created_by", ""),
        )


@dataclass
class StoreResult:
    success: bool
    store_id: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "store_id": self.store_id,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# StoreRegistry
# ---------------------------------------------------------------------------

class StoreRegistry:
    MAX_BATCH_KEYS = 100
    MAX_BATCH_RESPONSE_BYTES = 900 * 1024  # 900KB

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = Path(db_path or STORES_DB_PATH)
        self._local = threading.local()

        # 起動時: stale tmp を削除
        from .store_migration import cleanup_stale_tmp
        cleanup_stale_tmp(self._db_path)

        # マイグレーション: DB が無く index.json があれば自動移行
        index_path = Path(STORES_INDEX_PATH)
        if not self._db_path.exists() and index_path.exists():
            from .store_migration import migrate_json_to_sqlite
            migrate_json_to_sqlite(self._db_path, index_path)

        # DB 初期化（テーブル作成 + PRAGMA）
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------ #
    # Connection 管理
    # ------------------------------------------------------------------ #

    def _get_conn(self) -> sqlite3.Connection:
        """
        現スレッド用の SQLite connection を返す（遅延初期化）。

        per-thread connection で WAL モードと組み合わせ、
        スレッドセーフな並行アクセスを実現する。

        60 秒に 1 回 ``SELECT 1`` でヘルスチェックし、
        壊れていれば自動的に再接続する。
        """
        conn: Optional[sqlite3.Connection] = getattr(
            self._local, "conn", None
        )
        if conn is None:
            conn = self._create_conn()
        else:
            # ヘルスチェック（頻度制限付き）
            now = time.monotonic()
            last_check: float = getattr(
                self._local, "last_health_check", 0.0
            )
            if now - last_check >= _HEALTH_CHECK_INTERVAL:
                try:
                    conn.execute("SELECT 1")
                    self._local.last_health_check = now
                except Exception:
                    # 接続が壊れている → クローズして再作成
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = self._create_conn()
        return conn

    def _create_conn(self) -> sqlite3.Connection:
        """新しい SQLite connection を作成し threading.local に保存する。"""
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=10.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        self._apply_pragmas(conn)
        self._local.conn = conn
        self._local.last_health_check = time.monotonic()
        return conn

    @staticmethod
    def _apply_pragmas(conn: sqlite3.Connection) -> None:
        """PRAGMA を設定する。"""
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA cache_size = -8000")

    def _init_db(self) -> None:
        """テーブル・インデックスを作成し user_version を設定する。"""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stores (
                store_id   TEXT PRIMARY KEY,
                root_path  TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS store_data (
                store_id   TEXT NOT NULL
                    REFERENCES stores(store_id) ON DELETE CASCADE,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                value_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (store_id, key)
            );
            CREATE INDEX IF NOT EXISTS idx_store_data_store_id
                ON store_data(store_id);
        """)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    def close(self) -> None:
        """
        現スレッドの SQLite connection をクローズする。

        マルチスレッド環境では各スレッドが自身の connection を
        close する必要がある。

        WAL チェックポイント (PASSIVE) を実行してから接続を閉じ、
        WAL ファイルの肥大化を防止する。
        """
        conn: Optional[sqlite3.Connection] = getattr(
            self._local, "conn", None
        )
        if conn is not None:
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    # ------------------------------------------------------------------ #
    # タイムスタンプ
    # ------------------------------------------------------------------ #

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # ------------------------------------------------------------------ #
    # Store CRUD
    # ------------------------------------------------------------------ #

    def create_store(
        self,
        store_id: str,
        root_path: str,
        created_by: str = "api_user",
    ) -> StoreResult:
        if not store_id or not re.match(r'^[a-zA-Z0-9_-]{1,128}$', store_id):
            return StoreResult(
                success=False, store_id=store_id,
                error="store_id must match ^[a-zA-Z0-9_-]{1,128}$",
            )
        if not root_path:
            return StoreResult(
                success=False, store_id=store_id, error="root_path is required",
            )

        # パストラバーサル防止
        path_err = _validate_store_path(root_path)
        if path_err:
            return StoreResult(
                success=False, store_id=store_id, error=path_err,
            )

        rp = Path(root_path)
        try:
            rp.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return StoreResult(
                success=False, store_id=store_id,
                error=f"Failed to create root_path: {e}",
            )

        resolved_root = str(rp.resolve())
        now = self._now_ts()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO stores "
                "(store_id, root_path, created_at, created_by) "
                "VALUES (?, ?, ?, ?)",
                (store_id, resolved_root, now, created_by),
            )
            conn.commit()
        except sqlite3.Error as e:
            return StoreResult(
                success=False, store_id=store_id,
                error=f"Database error: {e}",
            )

        if cur.rowcount == 0:
            return StoreResult(
                success=False, store_id=store_id,
                error=f"Store already exists: {store_id}",
            )

        self._audit("store_created", True, {
            "store_id": store_id, "root_path": resolved_root,
        })
        return StoreResult(success=True, store_id=store_id)

    def get_store(self, store_id: str) -> Optional[StoreDefinition]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT store_id, root_path, created_at, created_by "
            "FROM stores WHERE store_id = ?",
            (store_id,),
        ).fetchone()
        if row is None:
            return None
        return StoreDefinition(
            store_id=row["store_id"],
            root_path=row["root_path"],
            created_at=row["created_at"],
            created_by=row["created_by"],
        )

    def list_stores(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT store_id, root_path, created_at, created_by FROM stores"
        ).fetchall()
        return [
            {
                "store_id": r["store_id"],
                "root_path": r["root_path"],
                "created_at": r["created_at"],
                "created_by": r["created_by"],
            }
            for r in rows
        ]

    def delete_store(
        self,
        store_id: str,
        delete_files: bool = False,
    ) -> StoreResult:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT root_path FROM stores WHERE store_id = ?",
            (store_id,),
        ).fetchone()

        if row is None:
            return StoreResult(
                success=False, store_id=store_id,
                error=f"Store not found: {store_id}",
            )

        root_path = row["root_path"]

        # パストラバーサル防止（登録済みパスも再検証）
        path_err = _validate_store_path(root_path)
        if path_err:
            return StoreResult(
                success=False, store_id=store_id, error=path_err,
            )

        # I-6: DB 削除を先に実行し、ファイル削除は後に行う。
        # DB 削除失敗時にファイルだけ消える事故を防ぐ。
        try:
            conn.execute(
                "DELETE FROM stores WHERE store_id = ?", (store_id,)
            )
            conn.commit()
        except sqlite3.Error as e:
            return StoreResult(
                success=False, store_id=store_id,
                error=f"Database error: {e}",
            )

        if delete_files:
            try:
                rp = Path(root_path)
                if rp.exists():
                    shutil.rmtree(rp)
            except Exception as e:
                # DB 上は削除済みなのでファイル削除失敗は警告のみ
                logging.getLogger(__name__).warning(
                    "Store %s: DB deleted but file cleanup failed: %s",
                    store_id, e,
                )

        self._audit("store_deleted", True, {
            "store_id": store_id, "delete_files": delete_files,
        })
        return StoreResult(success=True, store_id=store_id)

    # ------------------------------------------------------------------ #
    # is_store_accessible
    # ------------------------------------------------------------------ #

    def is_store_accessible(
        self,
        store_id: str,
        pack_id: str,
        allowed_store_ids: "Optional[List[str]]" = None,
    ) -> bool:
        """
        pack_id が store_id にアクセスできるか判定する。

        チェック順:
        1. allowed_store_ids (grant の config 由来) に含まれれば許可
        2. SharedStoreManager.is_sharing_approved() が True なら許可
        3. それ以外は拒否
        """
        if allowed_store_ids is not None and store_id in allowed_store_ids:
            return True

        try:
            from .store_sharing_manager import get_shared_store_manager
            ssm = get_shared_store_manager()
            return ssm.is_sharing_approved(pack_id, store_id)
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # #62  Declarative store creation (called from approval_manager)
    # ------------------------------------------------------------------ #

    def create_store_for_pack(
        self,
        pack_id: str,
        stores_decl: List[Dict[str, Any]],
    ) -> List[StoreResult]:
        """
        ecosystem.json の stores 宣言に基づき Store を一括作成する。

        - store_id には "{pack_id}__" プレフィックスを強制付与
        - 1 Pack あたり最大 MAX_STORES_PER_PACK 個
        - 既に存在する場合はスキップ（成功扱い）
        """
        results: List[StoreResult] = []

        if not stores_decl or not isinstance(stores_decl, list):
            return results

        if len(stores_decl) > MAX_STORES_PER_PACK:
            results.append(StoreResult(
                success=False,
                error=f"Too many stores declared ({len(stores_decl)}). "
                      f"Maximum is {MAX_STORES_PER_PACK} per Pack.",
            ))
            return results

        for entry in stores_decl:
            if not isinstance(entry, dict):
                results.append(StoreResult(
                    success=False,
                    error="Invalid store declaration (not a dict)",
                ))
                continue

            raw_store_id = entry.get("store_id", "")
            if not raw_store_id or not isinstance(raw_store_id, str):
                results.append(StoreResult(
                    success=False,
                    error="Missing or invalid store_id in declaration",
                ))
                continue

            # プレフィックス強制
            prefix = f"{pack_id}__"
            if raw_store_id.startswith(prefix):
                qualified_id = raw_store_id
            else:
                qualified_id = f"{prefix}{raw_store_id}"

            # store_id バリデーション
            if not re.match(r'^[a-zA-Z0-9_-]{1,128}$', qualified_id):
                results.append(StoreResult(
                    success=False,
                    store_id=qualified_id,
                    error="Qualified store_id must match ^[a-zA-Z0-9_-]{1,128}$",
                ))
                continue

            root_path = str(STORES_BASE_DIR / qualified_id)

            # 既存チェック — 存在すれば成功扱い
            existing = self.get_store(qualified_id)
            if existing is not None:
                results.append(StoreResult(success=True, store_id=qualified_id))
                continue

            result = self.create_store(
                store_id=qualified_id,
                root_path=root_path,
                created_by=f"pack:{pack_id}",
            )
            results.append(result)

        return results

    # ------------------------------------------------------------------ #
    # #6  Compare-And-Swap (SQLite CAS)
    # ------------------------------------------------------------------ #

    def cas(
        self,
        store_id: str,
        key: str,
        expected_value: Any = _EXPECT_MISSING,
        new_value: Any = _EXPECT_MISSING,
    ) -> Dict[str, Any]:
        """
        Compare-And-Swap: expected_value が現在値と一致する場合のみ
        new_value で上書きする。

        BEGIN IMMEDIATE + value_hash 比較で CAS を実現。
        fcntl/signal を使用しないため全プラットフォーム対応。

        **セマンティクス (I-5 改善)**:

        ``expected_value`` の 3 つの状態と対応する動作:

        1. **省略 (デフォルト ``_EXPECT_MISSING``)**:
           キーがストア内に **存在しない** ことを期待する。
           - キーが存在しない → ``new_value`` で新規作成 (成功)
           - キーが既に存在する → ``conflict`` エラーを返す

        2. **``expected_value=None``** (Python の ``None`` / JSON ``null``):
           キーが存在し、その値が **JSON null** であることを期待する。
           - キーが存在し値が ``null`` → ``new_value`` で上書き (成功)
           - キーが存在し値が ``null`` 以外 → ``conflict`` (ハッシュ不一致)
           - キーが存在しない → ``conflict`` (キー不存在)

        3. **``expected_value=<任意の値>``**:
           キーが存在し、その値が ``expected_value`` と一致することを期待する。
           比較は value_hash (SHA-256) で行われる。
           - 一致 → ``new_value`` で上書き (成功)
           - 不一致 → ``conflict``
           - キー不存在 → ``conflict``

        .. note::
           Wave 5-B 以前は ``expected_value=None`` が「キー不存在を期待」の
           意味だったが、sentinel ``_EXPECT_MISSING`` の導入により
           ``None`` は純粋に JSON null を指すようになった。
           「キー不存在を期待」する場合は ``expected_value`` を省略すること。
        """
        # new_value は必須（デフォルトはシグネチャ制約のためのダミー）
        if new_value is _EXPECT_MISSING:
            return {
                "success": False,
                "error": "new_value is required",
                "error_type": "validation_error",
            }

        # key バリデーション
        key_err = _validate_key(key)
        if key_err:
            return {
                "success": False,
                "error": key_err,
                "error_type": "validation_error",
            }

        store_def = self.get_store(store_id)
        if store_def is None:
            return {
                "success": False,
                "error": f"Store not found: {store_id}",
                "error_type": "store_not_found",
            }

        # value size check
        try:
            new_value_json = json.dumps(
                new_value,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as e:
            return {
                "success": False,
                "error": f"new_value is not JSON serializable: {e}",
                "error_type": "validation_error",
            }

        if len(new_value_json.encode("utf-8")) > MAX_VALUE_BYTES_CAS:
            return {
                "success": False,
                "error": f"Value too large (max {MAX_VALUE_BYTES_CAS} bytes)",
                "error_type": "payload_too_large",
            }

        new_hash = hashlib.sha256(
            new_value_json.encode("utf-8")
        ).hexdigest()
        now = self._now_ts()

        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            return {
                "success": False,
                "error": f"CAS lock timeout: {e}",
                "error_type": "timeout",
            }

        try:
            row = conn.execute(
                "SELECT value, value_hash FROM store_data "
                "WHERE store_id = ? AND key = ?",
                (store_id, key),
            ).fetchone()

            exists = row is not None

            if not exists and expected_value is _EXPECT_MISSING:
                # create: キーが存在せず expected_value が _EXPECT_MISSING → 新規作成
                conn.execute(
                    "INSERT INTO store_data "
                    "(store_id, key, value, value_hash, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (store_id, key, new_value_json, new_hash, now),
                )
                conn.commit()
                return {"success": True, "store_id": store_id, "key": key}

            if not exists and expected_value is not _EXPECT_MISSING:
                conn.rollback()
                return {
                    "success": False,
                    "error": "Key does not exist but expected_value was provided",
                    "error_type": "conflict",
                    "current_value": None,
                }

            # exists is True from here
            current_value_json = row["value"]
            current_hash = row["value_hash"]

            try:
                current_value = json.loads(current_value_json)
            except (json.JSONDecodeError, TypeError):
                current_value = None

            if expected_value is _EXPECT_MISSING:
                conn.rollback()
                return {
                    "success": False,
                    "error": "Key already exists but expected it to be missing",
                    "error_type": "conflict",
                    "current_value": current_value,
                }

            # compare via hash
            expected_hash = _normalize_value_hash(expected_value)
            if current_hash != expected_hash:
                conn.rollback()
                return {
                    "success": False,
                    "error": "Value mismatch (conflict)",
                    "error_type": "conflict",
                    "current_value": current_value,
                }

            # swap: UPDATE
            conn.execute(
                "UPDATE store_data "
                "SET value = ?, value_hash = ?, updated_at = ? "
                "WHERE store_id = ? AND key = ?",
                (new_value_json, new_hash, now, store_id, key),
            )
            conn.commit()
            return {"success": True, "store_id": store_id, "key": key}

        except sqlite3.Error as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return {
                "success": False,
                "error": f"CAS I/O error: {e}",
                "error_type": "io_error",
            }

    # ------------------------------------------------------------------ #
    # #18  List with pagination
    # ------------------------------------------------------------------ #

    def list_keys(
        self,
        store_id: str,
        prefix: str = "",
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Store 内のキーを列挙する（ページネーション対応）。

        prefix フィルタ: WHERE key >= :prefix AND key < :prefix_upper
        cursor (keyset pagination): WHERE key > :cursor
        limit/cursor 両方 None なら全件返却（後方互換）。

        idx_store_data_store_id インデックスにより store_id フィルタが高速化。
        """
        # prefix バリデーション（空文字列は許可）
        if prefix:
            prefix_err = _validate_key(prefix)
            if prefix_err:
                return {
                    "success": False,
                    "error": prefix_err,
                    "error_type": "validation_error",
                }

        store_def = self.get_store(store_id)
        if store_def is None:
            return {
                "success": False,
                "error": f"Store not found: {store_id}",
                "error_type": "store_not_found",
            }

        conn = self._get_conn()

        # ---- Build WHERE clause ----
        conditions: List[str] = ["store_id = ?"]
        params: List[Any] = [store_id]

        if prefix:
            prefix_upper = prefix + "\uffff"
            conditions.append("key >= ?")
            params.append(prefix)
            conditions.append("key < ?")
            params.append(prefix_upper)

        where = " AND ".join(conditions)

        # Total estimate (prefix-filtered count)
        total_estimate: int = conn.execute(
            f"SELECT COUNT(*) FROM store_data WHERE {where}",
            params,
        ).fetchone()[0]

        # No pagination → return all (backward compatible)
        if limit is None and cursor is None:
            rows = conn.execute(
                f"SELECT key FROM store_data WHERE {where} ORDER BY key",
                params,
            ).fetchall()
            return {
                "success": True,
                "keys": [r["key"] for r in rows],
                "next_cursor": None,
                "has_more": False,
                "total_estimate": total_estimate,
            }

        # Pagination
        if limit is None:
            limit = 100
        if not isinstance(limit, int) or limit < 1:
            limit = 1
        if limit > 1000:
            limit = 1000

        page_conditions = list(conditions)
        page_params = list(params)

        if cursor:
            page_conditions.append("key > ?")
            page_params.append(cursor)

        page_where = " AND ".join(page_conditions)

        # Fetch limit + 1 to detect has_more
        rows = conn.execute(
            f"SELECT key FROM store_data WHERE {page_where} "
            f"ORDER BY key LIMIT ?",
            page_params + [limit + 1],
        ).fetchall()

        keys = [r["key"] for r in rows]
        has_more = len(keys) > limit
        if has_more:
            keys = keys[:limit]

        next_cursor: Optional[str] = None
        if has_more and keys:
            next_cursor = keys[-1]

        return {
            "success": True,
            "keys": keys,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total_estimate": total_estimate,
        }

    # ------------------------------------------------------------------ #
    # #19  Batch get (single-query optimized)
    # ------------------------------------------------------------------ #

    def batch_get(
        self,
        store_id: str,
        keys: List[str],
    ) -> Dict[str, Any]:
        """
        複数キーを一度に取得する。最大100キー、累計900KB超で残りはnull。

        単一の ``WHERE store_id = ? AND key IN (...)`` クエリで一括取得し、
        N+1 問題を回避する。プレースホルダはキー数に応じて動的生成。
        """
        if not keys or not isinstance(keys, list):
            return {
                "success": False,
                "error": "Missing or invalid keys",
                "error_type": "validation_error",
            }

        if len(keys) > self.MAX_BATCH_KEYS:
            return {
                "success": False,
                "error": f"Too many keys ({len(keys)}). "
                         f"Maximum is {self.MAX_BATCH_KEYS}.",
                "error_type": "validation_error",
            }

        store_def = self.get_store(store_id)
        if store_def is None:
            return {
                "success": False,
                "error": f"Store not found: {store_id}",
                "error_type": "store_not_found",
            }

        conn = self._get_conn()

        # 一括取得: 有効なキーだけ SQL で取得 (単一クエリ)
        valid_keys = [k for k in keys if k and isinstance(k, str) and _validate_key(k) is None]
        fetched: Dict[str, str] = {}
        if valid_keys:
            placeholders = ",".join("?" for _ in valid_keys)
            rows = conn.execute(
                f"SELECT key, value FROM store_data "
                f"WHERE store_id = ? AND key IN ({placeholders})",
                [store_id] + valid_keys,
            ).fetchall()
            for r in rows:
                fetched[r["key"]] = r["value"]

        results: Dict[str, Any] = {}
        found = 0
        not_found = 0
        truncated = 0
        warnings: List[str] = []
        cumulative_size = 0
        size_exceeded = False

        for key in keys:
            if size_exceeded:
                results[key] = None
                truncated += 1
                continue

            if not key or not isinstance(key, str):
                results[key if key else ""] = None
                not_found += 1
                continue

            if _validate_key(key) is not None:
                results[key] = None
                not_found += 1
                continue

            raw = fetched.get(key)
            if raw is None:
                results[key] = None
                not_found += 1
                continue

            entry_size = len(raw.encode("utf-8"))
            if cumulative_size + entry_size > self.MAX_BATCH_RESPONSE_BYTES:
                size_exceeded = True
                results[key] = None
                truncated += 1
                remaining_count = len(keys) - len(results)
                warnings.append(
                    f"Response size limit (900KB) exceeded at key '{key}'. "
                    f"Remaining {remaining_count} keys returned as null."
                )
                continue

            cumulative_size += entry_size
            try:
                results[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                results[key] = None
                not_found += 1
                continue
            found += 1

        resp: Dict[str, Any] = {
            "success": True,
            "results": results,
            "found": found,
            "not_found": not_found,
            "truncated": truncated,
            "warnings": warnings,
        }
        return resp

    # ------------------------------------------------------------------ #
    # I-1  Store usage audit
    # ------------------------------------------------------------------ #

    def audit_store_usage(self, store_id: str) -> Dict[str, Any]:
        """
        ストア内の全キーサイズを集計する。

        SQLite の集約関数のみで処理するためキー数に依存せず高速。
        スキーマ変更は不要（SELECT のみ）。

        Args:
            store_id: 集計対象のストア ID

        Returns:
            {
                "store_id": str,
                "key_count": int,
                "total_size_bytes": int,
                "largest_key": str,
                "largest_size_bytes": int,
            }
            ストアが存在しない場合は error キーを含む dict を返す。
        """
        store_def = self.get_store(store_id)
        if store_def is None:
            return {
                "success": False,
                "error": f"Store not found: {store_id}",
                "error_type": "store_not_found",
            }

        conn = self._get_conn()

        # 集計: キー数 + 合計サイズ (LENGTH(CAST(... AS BLOB)) でバイト数)
        agg_row = conn.execute(
            "SELECT COUNT(*) AS key_count, "
            "COALESCE(SUM(LENGTH(CAST(value AS BLOB))), 0) AS total_size_bytes "
            "FROM store_data WHERE store_id = ?",
            (store_id,),
        ).fetchone()

        key_count: int = agg_row["key_count"]
        total_size_bytes: int = agg_row["total_size_bytes"]

        # 最大キー
        largest_key = ""
        largest_size_bytes = 0
        if key_count > 0:
            largest_row = conn.execute(
                "SELECT key, LENGTH(CAST(value AS BLOB)) AS size_bytes "
                "FROM store_data WHERE store_id = ? "
                "ORDER BY LENGTH(CAST(value AS BLOB)) DESC LIMIT 1",
                (store_id,),
            ).fetchone()
            if largest_row is not None:
                largest_key = largest_row["key"]
                largest_size_bytes = largest_row["size_bytes"]

        result = {
            "store_id": store_id,
            "key_count": key_count,
            "total_size_bytes": total_size_bytes,
            "largest_key": largest_key,
            "largest_size_bytes": largest_size_bytes,
        }

        self._audit("store_usage_audit", True, result)

        return result

    # ------------------------------------------------------------------ #
    # 監査ログ
    # ------------------------------------------------------------------ #

    @staticmethod
    def _audit(event_type: str, success: bool, details: Dict[str, Any]) -> None:
        try:
            from .audit_logger import get_audit_logger
            get_audit_logger().log_system_event(
                event_type=event_type, success=success, details=details,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# グローバルアクセサ
# ---------------------------------------------------------------------------

def get_store_registry() -> StoreRegistry:
    """
    グローバルな StoreRegistry を取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。

    Returns:
        StoreRegistry インスタンス
    """
    from .di_container import get_container
    return get_container().get("store_registry")


def reset_store_registry(db_path: str | None = None) -> StoreRegistry:
    """
    StoreRegistry をリセットする（テスト用）。

    新しいインスタンスを生成し、DI コンテナのキャッシュを置き換える。

    Args:
        db_path: SQLite DB ファイルパス（省略時はデフォルト）

    Returns:
        新しい StoreRegistry インスタンス
    """
    from .di_container import get_container
    new_instance = StoreRegistry(db_path)
    container = get_container()
    container.set_instance("store_registry", new_instance)
    return new_instance
