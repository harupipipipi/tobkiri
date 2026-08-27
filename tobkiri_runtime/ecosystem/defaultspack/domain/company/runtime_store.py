from __future__ import annotations

import os
import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from core_runtime.runtime_events import utc_now
from core_runtime.runtime_state import run_migrations, sqlite_wal_connection
from domain.agent_runtime.models import json_dumps, json_loads

from .models import DEFAULT_CHANNEL_ID, gen_id


ACTIVE_RUN_STATUSES = {"created", "queued", "running", "waiting_approval", "waiting_user_input", "paused", "resumable"}
TERMINAL_RUN_STATUSES = {"completed", "done", "error", "failed", "cancelled", "canceled", "planned", "stale", "missing"}
OPEN_TASK_STATUSES = {"queued", "assigned", "running", "waiting_approval", "blocked", "stale"}
DONE_TASK_STATUSES = {"completed", "cancelled", "failed"}
BLOCKER_SIGNAL_TOKENS = ("blocker", "failed", "timeout", "unanswered", "not_executed")
NON_BLOCKER_SIGNALS = {"subagent_repaired"}


def default_runtime_db_path() -> Path:
    override = os.environ.get("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH", "").strip()
    if override:
        return Path(override)
    runtime_dir = os.environ.get("RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DIR", "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "company_runtime.db"
    json_store = os.environ.get("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", "").strip()
    if json_store:
        path = Path(json_store)
        directory = path.parent if path.suffix == ".json" else path
        return directory / "company_runtime.db"
    user_data = os.environ.get("RUMI_USER_DATA", "").strip()
    if user_data:
        return (
            Path(user_data).expanduser()
            / "defaultspack"
            / "shared"
            / "companies"
            / "company_runtime.db"
        )
    return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "companies" / "company_runtime.db"


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _decode_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    data = dict(row) if row is not None else None
    if data is None:
        return None
    for key, fallback in (
        ("mentions_json", []),
        ("task_ids_json", []),
        ("target_agent_ids_json", []),
        ("metadata_json", {}),
        ("actions_json", []),
    ):
        if key in data:
            public_key = key[:-5] if key.endswith("_json") else key
            data[public_key] = json_loads(data.get(key), fallback)
            data.pop(key, None)
    for key in ("summary_dirty", "dirty"):
        if key in data:
            data[key] = bool(data[key])
    return data


def _clean_agent_ids(agent_ids: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    values = agent_ids if isinstance(agent_ids, list) else []
    for value in values:
        agent_id = str(value).strip()
        if agent_id and agent_id not in seen:
            seen.add(agent_id)
            result.append(agent_id)
    return result


def _stable_sync_id(prefix: str, metadata: dict[str, Any] | None) -> str | None:
    sync_key = str((metadata or {}).get("sync_key") or "").strip()
    if not sync_key:
        return None
    digest = hashlib.sha256(sync_key.encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"{prefix}{digest}"


def _metadata_blocker_signal(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    signal = str(metadata.get("signal") or "").strip()
    normalized = signal.lower()
    if not normalized or normalized in NON_BLOCKER_SIGNALS:
        return None
    if metadata.get("external_blocker") is True:
        return signal
    if any(token in normalized for token in BLOCKER_SIGNAL_TOKENS):
        return signal
    return None


def _blocker_signal_item(message: dict[str, Any]) -> dict[str, Any] | None:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    signal = _metadata_blocker_signal(metadata)
    if signal is None:
        return None
    item = {
        "signal": signal,
        "message_id": message.get("message_id") or message.get("id"),
        "channel_id": message.get("channel_id"),
        "thread_id": message.get("thread_id"),
        "sender_id": message.get("sender_id"),
        "created_at": message.get("created_at"),
        "sync_source": metadata.get("sync_source"),
    }
    for key in (
        "schedule_id",
        "execution_id",
        "child_conversation_id",
        "parent_conversation_id",
        "external_blocker",
        "external_issue_policy",
        "provider_health",
    ):
        if key in metadata:
            item[key] = metadata.get(key)
    return item


class CompanyRuntimeStore:
    """SQLite WAL store for Slack-like team workspace runtime state."""

    _instance: Optional["CompanyRuntimeStore"] = None
    _class_lock = threading.RLock()

    def __new__(cls, db_path: str | Path | None = None):
        if db_path is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                return cls._instance
        inst = super().__new__(cls)
        inst._initialized = False
        return inst

    def __init__(self, db_path: str | Path | None = None) -> None:
        target = Path(db_path) if db_path is not None else default_runtime_db_path()
        if getattr(self, "_initialized", False) and getattr(self, "db_path", None) == target:
            return
        self.db_path = target
        self.runtime_dir = target.parent
        self._local = threading.local()
        self._migrate_lock = threading.RLock()
        _ = self.conn
        self._initialized = True

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite_wal_connection(self.db_path)
            with self._migrate_lock:
                self._migrate(conn)
            self._local.conn = conn
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        run_migrations(conn, [(1, self._migration_1), (2, self._migration_2)], table_name="company_runtime_migrations")

    @staticmethod
    def _migration_1(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS company_threads(
              thread_id TEXT PRIMARY KEY,
              company_id TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              parent_message_id TEXT,
              title TEXT,
              status TEXT NOT NULL DEFAULT 'open',
              summary_dirty INTEGER NOT NULL DEFAULT 1,
              metadata_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS company_messages(
              message_id TEXT PRIMARY KEY,
              company_id TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              thread_id TEXT,
              sender_id TEXT NOT NULL,
              content TEXT NOT NULL,
              mentions_json TEXT,
              task_ids_json TEXT,
              metadata_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS company_tasks(
              task_id TEXT PRIMARY KEY,
              company_id TEXT NOT NULL,
              channel_id TEXT,
              thread_id TEXT,
              message_id TEXT,
              title TEXT NOT NULL,
              description TEXT,
              target_agent_ids_json TEXT,
              source TEXT NOT NULL DEFAULT 'manual',
              status TEXT NOT NULL DEFAULT 'queued',
              priority TEXT NOT NULL DEFAULT 'normal',
              metadata_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              assigned_at TEXT,
              completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS company_task_assignments(
              company_id TEXT NOT NULL,
              task_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(company_id, task_id, agent_id)
            );

            CREATE TABLE IF NOT EXISTS company_agent_runs(
              link_id TEXT PRIMARY KEY,
              company_id TEXT NOT NULL,
              task_id TEXT,
              thread_id TEXT,
              message_id TEXT,
              agent_id TEXT NOT NULL,
              run_id TEXT NOT NULL,
              status TEXT NOT NULL,
              lease_until TEXT,
              heartbeat_at TEXT,
              metadata_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS company_agent_inbox(
              inbox_id TEXT PRIMARY KEY,
              company_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              message_id TEXT,
              task_id TEXT,
              run_id TEXT,
              kind TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open',
              priority TEXT NOT NULL DEFAULT 'normal',
              content TEXT NOT NULL,
              metadata_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS company_summaries(
              company_id TEXT NOT NULL,
              scope_type TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              summary TEXT NOT NULL DEFAULT '',
              dirty INTEGER NOT NULL DEFAULT 1,
              generated_by TEXT,
              metadata_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(company_id, scope_type, scope_id)
            );

            CREATE INDEX IF NOT EXISTS idx_company_threads_company_channel ON company_threads(company_id, channel_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_company_messages_thread ON company_messages(company_id, thread_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_company_messages_channel ON company_messages(company_id, channel_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_company_tasks_status ON company_tasks(company_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_company_tasks_thread ON company_tasks(company_id, thread_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_company_task_assignments_agent ON company_task_assignments(company_id, agent_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_company_task_assignments_task ON company_task_assignments(company_id, task_id);
            CREATE INDEX IF NOT EXISTS idx_company_runs_agent_status ON company_agent_runs(company_id, agent_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_company_runs_run_id ON company_agent_runs(run_id);
            CREATE INDEX IF NOT EXISTS idx_company_inbox_agent_status ON company_agent_inbox(company_id, agent_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_company_summaries_dirty ON company_summaries(company_id, dirty, updated_at);
            """
        )

    @staticmethod
    def _migration_2(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS company_task_assignments(
              company_id TEXT NOT NULL,
              task_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(company_id, task_id, agent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_company_task_assignments_agent ON company_task_assignments(company_id, agent_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_company_task_assignments_task ON company_task_assignments(company_id, task_id);
            """
        )
        rows = conn.execute(
            """
            SELECT company_id, task_id, target_agent_ids_json, created_at, updated_at
            FROM company_tasks
            """
        ).fetchall()
        for row in rows:
            agent_ids = _clean_agent_ids(json_loads(row["target_agent_ids_json"], []))
            for agent_id in agent_ids:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO company_task_assignments(company_id, task_id, agent_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (row["company_id"], row["task_id"], agent_id, row["created_at"], row["updated_at"]),
                )

    def _replace_task_assignments_locked(self, company_id: str, task_id: str, agent_ids: Any, now: str) -> None:
        clean_ids = _clean_agent_ids(agent_ids)
        self.conn.execute(
            "DELETE FROM company_task_assignments WHERE company_id = ? AND task_id = ?",
            (str(company_id), str(task_id)),
        )
        for agent_id in clean_ids:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO company_task_assignments(company_id, task_id, agent_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(company_id), str(task_id), agent_id, now, now),
            )

    def ensure_thread(
        self,
        company_id: str,
        *,
        channel_id: str = DEFAULT_CHANNEL_ID,
        thread_id: str | None = None,
        parent_message_id: str | None = None,
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        tid = str(thread_id or gen_id("thread_"))
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO company_threads(
                  thread_id, company_id, channel_id, parent_message_id, title, status,
                  summary_dirty, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'open', 1, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                  channel_id=excluded.channel_id,
                  parent_message_id=COALESCE(excluded.parent_message_id, company_threads.parent_message_id),
                  title=COALESCE(NULLIF(excluded.title, ''), company_threads.title),
                  summary_dirty=1,
                  metadata_json=excluded.metadata_json,
                  updated_at=excluded.updated_at
                """,
                (
                    tid,
                    str(company_id),
                    str(channel_id or DEFAULT_CHANNEL_ID),
                    parent_message_id,
                    str(title or ""),
                    json_dumps(metadata or {}),
                    now,
                    now,
                ),
            )
        return self.get_thread(tid) or {"thread_id": tid, "company_id": str(company_id), "channel_id": str(channel_id)}

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM company_threads WHERE thread_id = ?", (str(thread_id),)).fetchone()
        return _decode_row(row)

    def list_threads(self, company_id: str, *, channel_id: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        sql = "SELECT * FROM company_threads WHERE company_id = ?"
        params: list[Any] = [str(company_id)]
        if channel_id:
            sql += " AND channel_id = ?"
            params.append(str(channel_id))
        total = self.conn.execute("SELECT COUNT(*) AS count FROM (" + sql + ")", params).fetchone()["count"]
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        rows = self.conn.execute(sql, [*params, int(limit), int(offset)]).fetchall()
        return [_decode_row(row) or {} for row in rows], int(total)

    def add_message(
        self,
        company_id: str,
        *,
        channel_id: str = DEFAULT_CHANNEL_ID,
        sender_id: str,
        content: str,
        thread_id: str | None = None,
        mentions: list[str] | None = None,
        task_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata_dict = metadata or {}
        message_id = _stable_sync_id("msg_sync_", metadata_dict) or gen_id("msg_")
        resolved_thread_id = thread_id or _stable_sync_id("thread_sync_", metadata_dict)
        title = str(content or "").strip().splitlines()[0][:120] if str(content or "").strip() else "Company message"
        thread = self.ensure_thread(
            company_id,
            channel_id=channel_id,
            thread_id=resolved_thread_id,
            title=title,
            metadata={"source": "message", **metadata_dict},
        )
        tid = str(thread["thread_id"])
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO company_messages(
                  message_id, company_id, channel_id, thread_id, sender_id, content,
                  mentions_json, task_ids_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    str(company_id),
                    str(channel_id or DEFAULT_CHANNEL_ID),
                    tid,
                    str(sender_id),
                    str(content),
                    json_dumps(list(mentions or [])),
                    json_dumps(list(task_ids or [])),
                    json_dumps(metadata_dict),
                    now,
                    now,
                ),
            )
            self.conn.execute(
                """
                UPDATE company_threads
                SET parent_message_id = COALESCE(parent_message_id, ?), summary_dirty = 1, updated_at = ?
                WHERE thread_id = ?
                """,
                (message_id, now, tid),
            )
        return self.get_message(message_id) or {"message_id": message_id, "id": message_id}

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM company_messages WHERE message_id = ?", (str(message_id),)).fetchone()
        data = _decode_row(row)
        if data is not None:
            data["id"] = data["message_id"]
        return data

    def get_message_by_sync_key(
        self, company_id: str, sync_key: str
    ) -> dict[str, Any] | None:
        """Return the company message identified by a stable sync key."""

        message_id = _stable_sync_id("msg_sync_", {"sync_key": sync_key})
        if message_id is None:
            return None
        message = self.get_message(message_id)
        if message is None or str(message.get("company_id") or "") != str(company_id):
            return None
        return message

    def update_message_tasks(self, message_id: str, task_ids: list[str]) -> dict[str, Any] | None:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "UPDATE company_messages SET task_ids_json = ?, updated_at = ? WHERE message_id = ?",
                (json_dumps(list(task_ids or [])), now, str(message_id)),
            )
        return self.get_message(message_id)

    def update_message(
        self,
        message_id: str,
        updates: dict[str, Any],
        *,
        company_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(updates, dict):
            return None
        message = self.get_message(message_id)
        if message is None:
            return None
        if company_id and str(message.get("company_id") or "") != str(company_id):
            return None
        assignments = []
        params: list[Any] = []
        for key, value in updates.items():
            if key == "metadata" and isinstance(value, dict):
                metadata = {**(message.get("metadata") if isinstance(message.get("metadata"), dict) else {}), **value}
                assignments.append("metadata_json = ?")
                params.append(json_dumps(metadata))
            elif key == "mentions":
                assignments.append("mentions_json = ?")
                params.append(json_dumps(list(value or [])))
            elif key == "task_ids":
                assignments.append("task_ids_json = ?")
                params.append(json_dumps(list(value or [])))
            elif key in {"content", "sender_id", "channel_id", "thread_id"}:
                assignments.append(key + " = ?")
                params.append(str(value))
        if not assignments:
            return message
        now = utc_now()
        assignments.append("updated_at = ?")
        params.extend([now, str(message_id)])
        with self.conn:
            self.conn.execute("UPDATE company_messages SET " + ", ".join(assignments) + " WHERE message_id = ?", params)
        updated = self.get_message(message_id)
        if updated is not None:
            self.mark_summary_dirty(str(updated.get("company_id") or ""), "thread", str(updated.get("thread_id") or ""))
            self.mark_summary_dirty(str(updated.get("company_id") or ""), "channel", str(updated.get("channel_id") or DEFAULT_CHANNEL_ID))
        return updated

    def list_messages(
        self,
        company_id: str,
        *,
        channel_id: str | None = None,
        thread_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        order: str = "asc",
    ) -> tuple[list[dict[str, Any]], int]:
        sql = "SELECT * FROM company_messages WHERE company_id = ?"
        params: list[Any] = [str(company_id)]
        if channel_id:
            sql += " AND channel_id = ?"
            params.append(str(channel_id))
        if thread_id:
            sql += " AND thread_id = ?"
            params.append(str(thread_id))
        total = self.conn.execute("SELECT COUNT(*) AS count FROM (" + sql + ")", params).fetchone()["count"]
        direction = "DESC" if str(order or "").strip().lower() in {"desc", "descending", "latest", "newest"} else "ASC"
        sql += f" ORDER BY created_at {direction}, rowid {direction} LIMIT ? OFFSET ?"
        rows = self.conn.execute(sql, [*params, int(limit), int(offset)]).fetchall()
        messages = []
        for row in rows:
            item = _decode_row(row) or {}
            item["id"] = item.get("message_id")
            messages.append(item)
        return messages, int(total)

    def list_channel_ids(self, company_id: str) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT channel_id
            FROM (
              SELECT DISTINCT channel_id FROM company_messages WHERE company_id = ?
              UNION
              SELECT DISTINCT channel_id FROM company_threads WHERE company_id = ?
              UNION
              SELECT DISTINCT channel_id FROM company_tasks WHERE company_id = ? AND channel_id IS NOT NULL
            )
            WHERE channel_id IS NOT NULL AND channel_id != ''
            ORDER BY channel_id
            """,
            (str(company_id), str(company_id), str(company_id)),
        ).fetchall()
        return [str(row["channel_id"]) for row in rows if str(row["channel_id"] or "").strip()]

    def create_task(
        self,
        company_id: str,
        *,
        title: str,
        description: str = "",
        target_agent_ids: list[str] | None = None,
        source: str = "manual",
        status: str = "queued",
        priority: str = "normal",
        channel_id: str | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        task_id = gen_id("task_")
        clean_target_agent_ids = _clean_agent_ids(target_agent_ids or [])
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO company_tasks(
                  task_id, company_id, channel_id, thread_id, message_id, title, description,
                  target_agent_ids_json, source, status, priority, metadata_json,
                  created_at, updated_at, assigned_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    str(company_id),
                    channel_id,
                    thread_id,
                    message_id,
                    str(title),
                    str(description or ""),
                    json_dumps(clean_target_agent_ids),
                    str(source or "manual"),
                    str(status or "queued"),
                    str(priority or "normal"),
                    json_dumps(metadata or {}),
                    now,
                    now,
                    now if status in {"assigned", "running", "waiting_approval"} else None,
                    now if status in DONE_TASK_STATUSES else None,
                ),
            )
            self._replace_task_assignments_locked(str(company_id), task_id, clean_target_agent_ids, now)
        self.mark_summary_dirty(str(company_id), "task", task_id)
        return self.get_task(task_id) or {"task_id": task_id, "id": task_id}

    def get_task(self, task_id: str, company_id: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM company_tasks WHERE task_id = ?"
        params: list[Any] = [str(task_id)]
        if company_id:
            sql += " AND company_id = ?"
            params.append(str(company_id))
        row = self.conn.execute(sql, params).fetchone()
        data = _decode_row(row)
        if data is not None:
            data["id"] = data["task_id"]
        return data

    def update_task(self, task_id: str, updates: dict[str, Any], *, company_id: str | None = None) -> dict[str, Any] | None:
        if not isinstance(updates, dict):
            return None
        task = self.get_task(task_id, company_id)
        if task is None:
            return None
        writable = {"title", "description", "source", "status", "priority", "channel_id", "thread_id", "message_id"}
        assignments = []
        params: list[Any] = []
        new_target_agent_ids: list[str] | None = None
        for key, value in updates.items():
            if key == "target_agent_ids":
                new_target_agent_ids = _clean_agent_ids(value)
                assignments.append("target_agent_ids_json = ?")
                params.append(json_dumps(new_target_agent_ids))
            elif key == "metadata" and isinstance(value, dict):
                metadata = {**(task.get("metadata") if isinstance(task.get("metadata"), dict) else {}), **value}
                assignments.append("metadata_json = ?")
                params.append(json_dumps(metadata))
            elif key in writable:
                assignments.append(key + " = ?")
                params.append(value)
        status = updates.get("status")
        now = utc_now()
        if status in {"assigned", "running", "waiting_approval"} and not task.get("assigned_at"):
            assignments.append("assigned_at = ?")
            params.append(now)
        if status in DONE_TASK_STATUSES:
            assignments.append("completed_at = ?")
            params.append(now)
        assignments.append("updated_at = ?")
        params.append(now)
        where = "task_id = ?"
        params.append(str(task_id))
        if company_id:
            where += " AND company_id = ?"
            params.append(str(company_id))
        with self.conn:
            self.conn.execute("UPDATE company_tasks SET " + ", ".join(assignments) + " WHERE " + where, params)
            if new_target_agent_ids is not None:
                self._replace_task_assignments_locked(str(task["company_id"]), str(task_id), new_target_agent_ids, now)
        updated = self.get_task(task_id, company_id)
        if updated is not None:
            self.mark_summary_dirty(str(updated["company_id"]), "task", str(task_id))
        return updated

    def delete_task(self, task_id: str, *, company_id: str | None = None) -> bool:
        task = self.get_task(task_id, company_id)
        if task is None:
            return False
        resolved_company_id = str(task["company_id"])
        with self.conn:
            self.conn.execute(
                "DELETE FROM company_task_assignments WHERE company_id = ? AND task_id = ?",
                (resolved_company_id, str(task_id)),
            )
            self.conn.execute(
                "DELETE FROM company_summaries WHERE company_id = ? AND scope_type = 'task' AND scope_id = ?",
                (resolved_company_id, str(task_id)),
            )
            cursor = self.conn.execute(
                "DELETE FROM company_tasks WHERE company_id = ? AND task_id = ?",
                (resolved_company_id, str(task_id)),
            )
        return cursor.rowcount > 0

    def list_tasks(
        self,
        company_id: str,
        *,
        status: str | None = None,
        target_agent_id: str | None = None,
        thread_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        sql = "SELECT * FROM company_tasks WHERE company_id = ?"
        params: list[Any] = [str(company_id)]
        if status:
            sql += " AND status = ?"
            params.append(str(status))
        if thread_id:
            sql += " AND thread_id = ?"
            params.append(str(thread_id))
        if target_agent_id:
            sql += """
                AND EXISTS (
                  SELECT 1 FROM company_task_assignments a
                  WHERE a.company_id = company_tasks.company_id
                    AND a.task_id = company_tasks.task_id
                    AND a.agent_id = ?
                )
            """
            params.append(str(target_agent_id))
        total = int(self.conn.execute("SELECT COUNT(*) AS count FROM (" + sql + ")", params).fetchone()["count"])
        rows = self.conn.execute(sql + " ORDER BY updated_at DESC LIMIT ? OFFSET ?", [*params, int(limit), int(offset)]).fetchall()
        tasks = [_decode_row(row) or {} for row in rows]
        for task in tasks:
            task["id"] = task.get("task_id")
        return tasks, total

    def list_open_tasks(self, company_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        tasks, _ = self.list_tasks(company_id, limit=limit * 2)
        return [task for task in tasks if str(task.get("status")) in OPEN_TASK_STATUSES][:limit]

    def record_agent_run(
        self,
        company_id: str,
        *,
        agent_id: str,
        run_id: str,
        task_id: str | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
        status: str = "running",
        lease_until: str | None = None,
        heartbeat_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        link_id = gen_id("link_")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO company_agent_runs(
                  link_id, company_id, task_id, thread_id, message_id, agent_id, run_id, status,
                  lease_until, heartbeat_at, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    str(company_id),
                    task_id,
                    thread_id,
                    message_id,
                    str(agent_id),
                    str(run_id),
                    str(status or "running"),
                    lease_until,
                    heartbeat_at or now,
                    json_dumps(metadata or {}),
                    now,
                    now,
                ),
            )
        self.mark_summary_dirty(str(company_id), "run", str(run_id))
        return self.get_run_link(link_id) or {"link_id": link_id}

    def get_run_link(self, link_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM company_agent_runs WHERE link_id = ?", (str(link_id),)).fetchone()
        return _decode_row(row)

    def list_run_links(
        self,
        company_id: str,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_total: bool = False,
    ) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
        sql = "SELECT * FROM company_agent_runs WHERE company_id = ?"
        params: list[Any] = [str(company_id)]
        if agent_id:
            sql += " AND agent_id = ?"
            params.append(str(agent_id))
        if task_id:
            sql += " AND task_id = ?"
            params.append(str(task_id))
        if status:
            sql += " AND status = ?"
            params.append(str(status))
        total = int(self.conn.execute("SELECT COUNT(*) AS count FROM (" + sql + ")", params).fetchone()["count"])
        rows = self.conn.execute(sql + " ORDER BY updated_at DESC LIMIT ? OFFSET ?", [*params, int(limit), int(offset)]).fetchall()
        runs = [_decode_row(row) or {} for row in rows]
        if include_total:
            return runs, total
        return runs

    def update_run_link_status(self, run_id: str, status: str, *, heartbeat_at: str | None = None) -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                UPDATE company_agent_runs
                SET status = ?, heartbeat_at = COALESCE(?, heartbeat_at), updated_at = ?
                WHERE run_id = ?
                """,
                (str(status), heartbeat_at, now, str(run_id)),
            )

    def find_active_run_for_agent(self, company_id: str, agent_id: str) -> dict[str, Any] | None:
        rows = self.conn.execute(
            """
            SELECT * FROM company_agent_runs
            WHERE company_id = ? AND agent_id = ?
            ORDER BY updated_at DESC
            LIMIT 20
            """,
            (str(company_id), str(agent_id)),
        ).fetchall()
        for row in rows:
            link = _decode_row(row) or {}
            status = str(link.get("status") or "").lower()
            run_id = str(link.get("run_id") or "")
            try:
                from domain.agent_runtime.run_store import AgentRunStore

                run = AgentRunStore().get_run(run_id)
                if not isinstance(run, dict):
                    self.update_run_link_status(run_id, "missing")
                    continue
                run_status = str(run.get("status") or "").lower()
                if run_status:
                    status = run_status
                    if status != str(link.get("status") or "").lower():
                        self.update_run_link_status(run_id, status, heartbeat_at=run.get("heartbeat_at"))
            except Exception:
                self.update_run_link_status(run_id, "stale")
                continue
            if status in TERMINAL_RUN_STATUSES:
                continue
            if status in ACTIVE_RUN_STATUSES:
                link["status"] = status
                return link
        return None

    def add_inbox_item(
        self,
        company_id: str,
        *,
        agent_id: str,
        kind: str,
        content: str,
        message_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        status: str = "open",
        priority: str = "normal",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        inbox_id = gen_id("inbox_")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO company_agent_inbox(
                  inbox_id, company_id, agent_id, message_id, task_id, run_id, kind, status,
                  priority, content, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inbox_id,
                    str(company_id),
                    str(agent_id),
                    message_id,
                    task_id,
                    run_id,
                    str(kind),
                    str(status),
                    str(priority),
                    str(content),
                    json_dumps(metadata or {}),
                    now,
                    now,
                ),
            )
        return self.get_inbox_item(inbox_id) or {"inbox_id": inbox_id}

    def get_inbox_item(self, inbox_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM company_agent_inbox WHERE inbox_id = ?", (str(inbox_id),)).fetchone()
        return _decode_row(row)

    def update_inbox_item(self, inbox_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_inbox_item(inbox_id)
        if current is None:
            return None
        assignments = []
        params: list[Any] = []
        for key in ("status", "priority", "content", "task_id", "run_id"):
            if key in updates:
                assignments.append(key + " = ?")
                params.append(updates[key])
        if isinstance(updates.get("metadata"), dict):
            assignments.append("metadata_json = ?")
            params.append(json_dumps({**(current.get("metadata") or {}), **updates["metadata"]}))
        assignments.append("updated_at = ?")
        params.append(utc_now())
        params.append(str(inbox_id))
        with self.conn:
            self.conn.execute("UPDATE company_agent_inbox SET " + ", ".join(assignments) + " WHERE inbox_id = ?", params)
        return self.get_inbox_item(inbox_id)

    def list_inbox(
        self,
        company_id: str,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM company_agent_inbox WHERE company_id = ?"
        params: list[Any] = [str(company_id)]
        if agent_id:
            sql += " AND agent_id = ?"
            params.append(str(agent_id))
        if status:
            sql += " AND status = ?"
            params.append(str(status))
        if kind:
            sql += " AND kind = ?"
            params.append(str(kind))
        sql += " ORDER BY updated_at DESC LIMIT ?"
        rows = self.conn.execute(sql, [*params, int(limit)]).fetchall()
        return [_decode_row(row) or {} for row in rows]

    def list_unassigned_mentions(self, company_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_inbox(company_id, agent_id="operations_manager", status="open", kind="unassigned_mention", limit=limit)

    def mark_summary_dirty(self, company_id: str, scope_type: str, scope_id: str) -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO company_summaries(
                  company_id, scope_type, scope_id, summary, dirty, generated_by, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, '', 1, NULL, '{}', ?, ?)
                ON CONFLICT(company_id, scope_type, scope_id) DO UPDATE SET
                  dirty=1,
                  updated_at=excluded.updated_at
                """,
                (str(company_id), str(scope_type), str(scope_id), now, now),
            )
            if scope_type == "thread":
                self.conn.execute(
                    "UPDATE company_threads SET summary_dirty = 1, updated_at = ? WHERE thread_id = ?",
                    (now, str(scope_id)),
                )

    def upsert_summary(
        self,
        company_id: str,
        *,
        scope_type: str,
        scope_id: str,
        summary: str,
        generated_by: str = "scribe",
        dirty: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO company_summaries(
                  company_id, scope_type, scope_id, summary, dirty, generated_by, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_id, scope_type, scope_id) DO UPDATE SET
                  summary=excluded.summary,
                  dirty=excluded.dirty,
                  generated_by=excluded.generated_by,
                  metadata_json=excluded.metadata_json,
                  updated_at=excluded.updated_at
                """,
                (
                    str(company_id),
                    str(scope_type),
                    str(scope_id),
                    str(summary),
                    1 if dirty else 0,
                    str(generated_by),
                    json_dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            if scope_type == "thread":
                self.conn.execute(
                    "UPDATE company_threads SET summary_dirty = ? WHERE thread_id = ?",
                    (1 if dirty else 0, str(scope_id)),
                )
        return self.get_summary(company_id, scope_type, scope_id) or {}

    def get_summary(self, company_id: str, scope_type: str, scope_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM company_summaries
            WHERE company_id = ? AND scope_type = ? AND scope_id = ?
            """,
            (str(company_id), str(scope_type), str(scope_id)),
        ).fetchone()
        return _decode_row(row)

    def list_summaries(
        self,
        company_id: str,
        *,
        scope_type: str | None = None,
        dirty: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        sql = "SELECT * FROM company_summaries WHERE company_id = ?"
        params: list[Any] = [str(company_id)]
        if scope_type:
            sql += " AND scope_type = ?"
            params.append(str(scope_type))
        if dirty is not None:
            sql += " AND dirty = ?"
            params.append(1 if dirty else 0)
        total = self.conn.execute("SELECT COUNT(*) AS count FROM (" + sql + ")", params).fetchone()["count"]
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        rows = self.conn.execute(sql, [*params, int(limit), int(offset)]).fetchall()
        return [_decode_row(row) or {} for row in rows], int(total)

    def list_dirty_summaries(self, company_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM company_summaries
            WHERE company_id = ? AND dirty = 1
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (str(company_id), int(limit)),
        ).fetchall()
        return [_decode_row(row) or {} for row in rows]

    def stats(self, company_id: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for key, table in (
            ("threads", "company_threads"),
            ("messages", "company_messages"),
            ("tasks", "company_tasks"),
            ("runs", "company_agent_runs"),
            ("inbox", "company_agent_inbox"),
            ("summaries", "company_summaries"),
        ):
            row = self.conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE company_id = ?", (str(company_id),)).fetchone()
            result[key] = int(row["count"] if row else 0)
        return result

    def blocker_signal_summary(self, company_id: str, *, limit: int = 20) -> dict[str, Any]:
        """Return latest scheduler/subagent blocker signals for visible status panes."""
        messages, _total = self.list_messages(
            company_id,
            limit=max(int(limit), 1),
            offset=0,
            order="desc",
        )
        signals: list[dict[str, Any]] = []
        for message in messages:
            item = _blocker_signal_item(message)
            if item is not None:
                signals.append(item)
        return {
            "blocker_count": len(signals),
            "latest_signal": signals[0] if signals else None,
            "signals": signals,
        }
