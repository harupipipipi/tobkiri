from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from core_runtime.runtime_audit_helpers import redact_sensitive
from core_runtime.runtime_events import RuntimeEvent, utc_now
from core_runtime.runtime_state import run_migrations, sqlite_wal_connection

from .models import AgentRun, RunStatus, json_dumps, json_loads


def _normalize_run_ids(run_ids: Iterable[str] | str | None) -> list[str] | None:
    if run_ids is None:
        return None
    values = [run_ids] if isinstance(run_ids, str) else list(run_ids)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        run_id = str(value).strip()
        if run_id and run_id not in seen:
            seen.add(run_id)
            result.append(run_id)
    return result


def _error_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        message = str(value.get("message") or "").strip()
        code = str(value.get("code") or "").strip()
        if message:
            return f"{code}: {message}" if code else message
    return json_dumps(redact_sensitive(value))


def default_runtime_dir() -> Path:
    override = os.environ.get("RUMI_DEFAULTSPACK_AGENT_RUNTIME_DIR")
    if override:
        return Path(override)
    user_data = os.environ.get("RUMI_USER_DATA", "").strip()
    if user_data:
        return Path(user_data) / "defaultspack" / "shared" / "agent_runtime"
    return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "agent_runtime"


class AgentRunStore:
    """SQLite WAL store for durable defaultspack agent runs."""

    _instance: Optional["AgentRunStore"] = None

    def __new__(cls, db_path: str | Path | None = None):
        if db_path is None:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
        inst = super().__new__(cls)
        inst._initialized = False
        return inst

    def __init__(self, db_path: str | Path | None = None) -> None:
        if getattr(self, "_initialized", False):
            return
        self.runtime_dir = Path(db_path).parent if db_path is not None else default_runtime_dir()
        self.db_path = Path(db_path) if db_path is not None else self.runtime_dir / "state.db"
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
        run_migrations(
            conn,
            [
                (1, self._migration_1),
                (2, self._migration_2),
                (3, self._migration_3),
            ],
            table_name="agent_runtime_migrations",
        )

    @staticmethod
    def _migration_1(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_runs(
              run_id TEXT PRIMARY KEY,
              session_key TEXT,
              conversation_id TEXT,
              agent_id TEXT,
              task TEXT,
              status TEXT,
              model TEXT,
              system_prompt_id TEXT,
              system_prompt_hash TEXT,
              runtime_profile_key TEXT,
              runtime_profile_json TEXT,
              capability_graph_json TEXT,
              created_at TEXT,
              updated_at TEXT,
              started_at TEXT,
              completed_at TEXT,
              parent_run_id TEXT,
              root_run_id TEXT,
              current_transcript_id TEXT,
              compaction_count INTEGER DEFAULT 0,
              heartbeat_at TEXT,
              error TEXT,
              result_json TEXT,
              execution_json TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_steps(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT,
              step_no INTEGER,
              step_type TEXT,
              status TEXT,
              content_json TEXT,
              created_at TEXT,
              updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_messages(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT,
              transcript_id TEXT,
              role TEXT,
              content_json TEXT,
              tool_call_id TEXT,
              tool_name TEXT,
              token_estimate INTEGER,
              created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_tool_calls(
              tool_call_id TEXT PRIMARY KEY,
              run_id TEXT,
              tool_name TEXT,
              arguments_json TEXT,
              status TEXT,
              approval_id TEXT,
              result_json TEXT,
              started_at TEXT,
              completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_approvals(
              approval_id TEXT PRIMARY KEY,
              run_id TEXT,
              tool_call_id TEXT,
              reviewer TEXT,
              status TEXT,
              reason TEXT,
              requested_at TEXT,
              decided_at TEXT,
              decision_json TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_compactions(
              compact_id TEXT PRIMARY KEY,
              run_id TEXT,
              transcript_id TEXT,
              reason TEXT,
              summary TEXT,
              packet_json TEXT,
              replacement_transcript_id TEXT,
              tokens_before INTEGER,
              tokens_after INTEGER,
              created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT,
              event_type TEXT,
              payload_json TEXT,
              created_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status);
            CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_key, updated_at);
            CREATE INDEX IF NOT EXISTS idx_agent_messages_run ON agent_messages(run_id, id);
            CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps(run_id, step_no);
            """
        )
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS agent_messages_fts USING fts5(
                  content,
                  tool_name,
                  content='agent_messages',
                  content_rowid='id'
                )
                """
            )
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _migration_2(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_runs_heartbeat ON agent_runs(status, heartbeat_at);
            CREATE INDEX IF NOT EXISTS idx_agent_approvals_status ON agent_approvals(status, requested_at);
            """
        )

    @staticmethod
    def _migration_3(conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()
        }
        additions = {
            "root_scope_id": "TEXT",
            "agent_kind": "TEXT DEFAULT 'subagent'",
            "runtime_kind": "TEXT DEFAULT 'agent_run'",
            "subagent_role": "TEXT",
            "placement_id": "TEXT",
            "placement_revision": "TEXT",
            "placement_map_id": "TEXT",
            "effective_plan_hash": "TEXT",
            "protocol_membership_json": "TEXT DEFAULT '[]'",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE agent_runs ADD COLUMN {name} {sql_type}"
                )
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_runs_placement
              ON agent_runs(placement_map_id, placement_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_agent_runs_root_scope
              ON agent_runs(root_scope_id, updated_at);
            """
        )

    def upsert_run(self, run: AgentRun) -> None:
        now = utc_now()
        created_at = run.created_at or now
        updated_at = run.updated_at or now
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO agent_runs(
                  run_id, session_key, conversation_id, agent_id, task, status, model,
                  system_prompt_id, system_prompt_hash, runtime_profile_key,
                  runtime_profile_json, capability_graph_json, created_at, updated_at,
                  started_at, completed_at, parent_run_id, root_run_id,
                  root_scope_id, agent_kind, runtime_kind, subagent_role,
                  placement_id, placement_revision, placement_map_id,
                  effective_plan_hash, protocol_membership_json,
                  current_transcript_id, compaction_count, heartbeat_at, error,
                  result_json, execution_json
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(run_id) DO UPDATE SET
                  session_key=excluded.session_key,
                  conversation_id=excluded.conversation_id,
                  agent_id=excluded.agent_id,
                  task=excluded.task,
                  status=excluded.status,
                  model=excluded.model,
                  system_prompt_id=excluded.system_prompt_id,
                  system_prompt_hash=excluded.system_prompt_hash,
                  runtime_profile_key=excluded.runtime_profile_key,
                  runtime_profile_json=excluded.runtime_profile_json,
                  capability_graph_json=excluded.capability_graph_json,
                  updated_at=excluded.updated_at,
                  started_at=COALESCE(excluded.started_at, agent_runs.started_at),
                  completed_at=excluded.completed_at,
                  parent_run_id=excluded.parent_run_id,
                  root_run_id=excluded.root_run_id,
                  root_scope_id=excluded.root_scope_id,
                  agent_kind=excluded.agent_kind,
                  runtime_kind=excluded.runtime_kind,
                  subagent_role=excluded.subagent_role,
                  placement_id=excluded.placement_id,
                  placement_revision=excluded.placement_revision,
                  placement_map_id=excluded.placement_map_id,
                  effective_plan_hash=excluded.effective_plan_hash,
                  protocol_membership_json=excluded.protocol_membership_json,
                  current_transcript_id=excluded.current_transcript_id,
                  compaction_count=excluded.compaction_count,
                  heartbeat_at=excluded.heartbeat_at,
                  error=excluded.error,
                  result_json=excluded.result_json,
                  execution_json=excluded.execution_json
                """,
                (
                    run.run_id,
                    run.session_key,
                    run.conversation_id,
                    run.agent_id,
                    run.task,
                    run.status,
                    run.model,
                    run.system_prompt_id,
                    run.system_prompt_hash,
                    run.runtime_profile_key,
                    json_dumps(redact_sensitive(run.runtime_profile_json)),
                    json_dumps(redact_sensitive(run.capability_graph_json)),
                    created_at,
                    updated_at,
                    run.started_at,
                    run.completed_at,
                    run.parent_run_id,
                    run.root_run_id or run.run_id,
                    run.root_scope_id or run.root_run_id or run.run_id,
                    run.agent_kind,
                    run.runtime_kind,
                    run.subagent_role,
                    run.placement_id,
                    run.placement_revision,
                    run.placement_map_id,
                    run.effective_plan_hash,
                    json_dumps(run.protocol_membership_json),
                    run.current_transcript_id,
                    run.compaction_count,
                    run.heartbeat_at,
                    _error_text(run.error),
                    json_dumps(redact_sensitive(run.result_json)),
                    json_dumps(redact_sensitive(run.execution_json)),
                ),
            )

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        for key in (
            "runtime_profile_json",
            "capability_graph_json",
            "result_json",
            "execution_json",
            "protocol_membership_json",
        ):
            fallback: Any = [] if key == "protocol_membership_json" else {}
            data[key] = json_loads(data.get(key), fallback)
        return data

    def list_runs(self, *, status: str | None = None, run_ids: Iterable[str] | str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clean_run_ids = _normalize_run_ids(run_ids)
        if clean_run_ids == []:
            return []
        sql = "SELECT * FROM agent_runs"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        if clean_run_ids is not None:
            sql += " AND " if status else " WHERE "
            sql += "run_id IN (" + ",".join("?" for _ in clean_run_ids) + ")"
            params.extend(clean_run_ids)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self.get_run(row["run_id"]) for row in rows if row["run_id"]]

    def list_active(self, *, agent_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        statuses = ("created", "queued", "running", "waiting_approval", "waiting_user_input", "paused", "resumable")
        placeholders = ",".join("?" for _ in statuses)
        sql = f"SELECT run_id FROM agent_runs WHERE status IN ({placeholders})"
        params: list[Any] = list(statuses)
        if agent_id:
            sql += " AND agent_id = ?"
            params.append(str(agent_id))
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [self.get_run(row["run_id"]) for row in rows if row["run_id"]]

    def touch(
        self,
        run_id: str,
        *,
        status: str | None = None,
        heartbeat_at: str | None = None,
        event_type: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        heartbeat = heartbeat_at or now
        assignments = ["heartbeat_at = ?", "updated_at = ?"]
        params: list[Any] = [heartbeat, now]
        if status:
            assignments.insert(0, "status = ?")
            params.insert(0, str(status))
        params.append(str(run_id))
        with self.conn:
            self.conn.execute(
                "UPDATE agent_runs SET " + ", ".join(assignments) + " WHERE run_id = ?",
                params,
            )
        if event_type:
            self.add_event(run_id, event_type, payload or {"status": status, "heartbeat_at": heartbeat})

    def touch_heartbeat(self, run_id: str, *, event_type: str = "heartbeat", payload: dict[str, Any] | None = None) -> None:
        self.touch(run_id, event_type=event_type, payload=payload)

    def list_stale(
        self,
        *,
        stale_after_seconds: int = 600,
        run_ids: Iterable[str] | str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clean_run_ids = _normalize_run_ids(run_ids)
        if clean_run_ids == []:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(1, int(stale_after_seconds)))).isoformat().replace("+00:00", "Z")
        statuses = ("running", "queued", "waiting_approval", "waiting_user_input", "paused", "resumable")
        placeholders = ",".join("?" for _ in statuses)
        run_filter = ""
        params: list[Any] = [*statuses, cutoff]
        if clean_run_ids is not None:
            run_filter = " AND run_id IN (" + ",".join("?" for _ in clean_run_ids) + ")"
            params.extend(clean_run_ids)
        params.append(int(limit))
        rows = self.conn.execute(
            f"""
            SELECT run_id FROM agent_runs
            WHERE status IN ({placeholders})
              AND COALESCE(heartbeat_at, updated_at, created_at) < ?
              {run_filter}
            ORDER BY COALESCE(heartbeat_at, updated_at, created_at) ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self.get_run(row["run_id"]) for row in rows if row["run_id"]]

    def list_waiting_approval(self, *, run_ids: Iterable[str] | str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clean_run_ids = _normalize_run_ids(run_ids)
        if clean_run_ids == []:
            return []
        run_filter = ""
        params: list[Any] = []
        if clean_run_ids is not None:
            run_filter = " AND r.run_id IN (" + ",".join("?" for _ in clean_run_ids) + ")"
            params.extend(clean_run_ids)
        params.append(int(limit))
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT r.run_id
            FROM agent_runs r
            LEFT JOIN agent_approvals a ON a.run_id = r.run_id
            WHERE (r.status = 'waiting_approval' OR a.status = 'pending')
            {run_filter}
            ORDER BY r.updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self.get_run(row["run_id"]) for row in rows if row["run_id"]]

    def update_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
        result: Any = None,
        completed: bool = False,
    ) -> None:
        completed_at = utc_now() if completed else None
        with self.conn:
            self.conn.execute(
                """
                UPDATE agent_runs
                SET status = ?, error = ?, result_json = ?, completed_at = COALESCE(?, completed_at), updated_at = ?, heartbeat_at = ?
                WHERE run_id = ?
                """,
                (status, _error_text(error), json_dumps(result), completed_at, utc_now(), utc_now(), run_id),
            )

    def replace_steps(self, run_id: str, steps: Iterable[Any]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM agent_steps WHERE run_id = ?", (run_id,))
            for index, step in enumerate(steps, start=1):
                data = step.to_dict() if hasattr(step, "to_dict") else dict(step)
                self.conn.execute(
                    """
                    INSERT INTO agent_steps(run_id, step_no, step_type, status, content_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        int(data.get("step_number") or data.get("step_no") or index),
                        str(data.get("step_type") or ""),
                        str(data.get("status") or "completed"),
                        json_dumps(redact_sensitive(data.get("content", data.get("content_json", {})))),
                        str(data.get("created_at") or utc_now()),
                        str(data.get("updated_at") or utc_now()),
                    ),
                )

    def replace_messages(self, run_id: str, transcript_id: str, messages: Iterable[dict[str, Any]]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM agent_messages WHERE run_id = ?", (run_id,))
            for message in messages:
                role = str(message.get("role") or "")
                tool_name = message.get("name") or message.get("tool_name")
                token_estimate = estimate_tokens(message)
                self.conn.execute(
                    """
                    INSERT INTO agent_messages(
                      run_id, transcript_id, role, content_json, tool_call_id, tool_name, token_estimate, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        transcript_id,
                        role,
                        json_dumps(redact_sensitive(message)),
                        message.get("tool_call_id"),
                        tool_name,
                        token_estimate,
                        utc_now(),
                    ),
                )

    def list_messages(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        clean_limit = max(1, min(int(limit or 100), 1000))
        rows = self.conn.execute(
            """
            SELECT * FROM agent_messages
            WHERE run_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (str(run_id), clean_limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            data = dict(row)
            data["content_json"] = json_loads(data.get("content_json"), {})
            result.append(data)
        return result

    def record_tool_call(
        self,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        status: str = "pending",
        approval_id: str | None = None,
        result: Any = None,
    ) -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO agent_tool_calls(
                  tool_call_id, run_id, tool_name, arguments_json, status, approval_id, result_json, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_call_id) DO UPDATE SET
                  status=excluded.status,
                      approval_id=COALESCE(excluded.approval_id, agent_tool_calls.approval_id),
                  result_json=excluded.result_json,
                  completed_at=excluded.completed_at
                """,
                (
                    tool_call_id,
                    run_id,
                    tool_name,
                    json_dumps(redact_sensitive(arguments or {})),
                    status,
                    approval_id,
                    json_dumps(redact_sensitive(result)),
                    now,
                    now if status in {"completed", "failed", "rejected"} else None,
                ),
            )

    def record_approval(
        self,
        approval_id: str,
        run_id: str,
        tool_call_id: str,
        *,
        reviewer: str = "user",
        status: str = "pending",
        reason: str = "",
        decision: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO agent_approvals(
                  approval_id, run_id, tool_call_id, reviewer, status, reason, requested_at, decided_at, decision_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                  status=excluded.status,
                  reason=excluded.reason,
                  decided_at=excluded.decided_at,
                  decision_json=excluded.decision_json
                """,
                (
                    approval_id,
                    run_id,
                    tool_call_id,
                    reviewer,
                    status,
                    reason,
                    now,
                    now if status != "pending" else None,
                    json_dumps(redact_sensitive(decision or {})),
                ),
            )

    def is_approval_granted(self, run_id: str, tool_call_id: str, approval_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT status FROM agent_approvals
            WHERE run_id = ? AND tool_call_id = ? AND approval_id = ?
            """,
            (run_id, tool_call_id, approval_id),
        ).fetchone()
        if row is None:
            return False
        return str(row["status"]).lower() in {"approved", "allow", "allowed", "granted"}

    def record_compaction(
        self,
        compact_id: str,
        run_id: str,
        transcript_id: str,
        *,
        reason: str,
        summary: str,
        packet: dict[str, Any],
        replacement_transcript_id: str,
        tokens_before: int = 0,
        tokens_after: int = 0,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO agent_compactions(
                  compact_id, run_id, transcript_id, reason, summary, packet_json,
                  replacement_transcript_id, tokens_before, tokens_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    compact_id,
                    run_id,
                    transcript_id,
                    reason,
                    summary,
                    json_dumps(redact_sensitive(packet)),
                    replacement_transcript_id,
                    tokens_before,
                    tokens_after,
                    utc_now(),
                ),
            )
            self.conn.execute(
                """
                UPDATE agent_runs
                SET current_transcript_id = ?, compaction_count = compaction_count + 1, updated_at = ?
                WHERE run_id = ?
                """,
                (replacement_transcript_id, utc_now(), run_id),
            )

    def add_event(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        event = RuntimeEvent(event_type=event_type, run_id=run_id, payload=redact_sensitive(payload or {}))
        with self.conn:
            self.conn.execute(
                "INSERT INTO agent_events(run_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (run_id, event.event_type, json_dumps(event.payload), event.created_at),
            )

    def events(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_events WHERE run_id = ? ORDER BY id DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            data = dict(row)
            data["payload_json"] = json_loads(data.get("payload_json"), {})
            result.append(data)
        return result

    def save_execution(
        self,
        execution: Any,
        *,
        session_key: str | None = None,
        transcript_id: str | None = None,
    ) -> None:
        run_id = str(getattr(execution, "execution_id"))
        context = getattr(execution, "context", {}) or {}
        execution_json = execution.to_dict() if hasattr(execution, "to_dict") else dict(execution)
        execution_json["context"] = context
        execution_json["messages"] = list(getattr(execution, "messages", []) or [])
        execution_json = redact_sensitive(execution_json)
        status = str(getattr(execution, "status", RunStatus.CREATED.value))
        completed = status in {"completed", "planned", "cancelled", "error", "failed"}
        run = AgentRun(
            run_id=run_id,
            session_key=session_key or context.get("session_key") or f"agent:{context.get('agent_id', 'main')}:main",
            conversation_id=context.get("conversation_id"),
            agent_id=context.get("agent_id"),
            task=str(getattr(execution, "task", "")),
            status=status,
            model=str(getattr(execution, "model", "default")),
            runtime_profile_key=context.get("runtime_profile_key") or context.get("_runtime_profile_key"),
            runtime_profile_json=context.get("runtime_profile") if isinstance(context.get("runtime_profile"), dict) else {},
            capability_graph_json=context.get("capability_graph") if isinstance(context.get("capability_graph"), dict) else {},
            created_at=str(getattr(execution, "created_at", "") or utc_now()),
            updated_at=str(getattr(execution, "updated_at", "") or utc_now()),
            started_at=str(getattr(execution, "created_at", "") or utc_now()),
            completed_at=utc_now() if completed else None,
            parent_run_id=context.get("parent_run_id"),
            root_run_id=context.get("root_run_id"),
            root_scope_id=context.get("root_scope_id"),
            agent_kind=str(context.get("agent_kind") or "subagent"),
            runtime_kind=str(context.get("runtime_kind") or "agent_run"),
            subagent_role=context.get("subagent_role"),
            placement_id=context.get("placement_id"),
            placement_revision=context.get("placement_revision"),
            placement_map_id=context.get("placement_map_id"),
            effective_plan_hash=context.get("effective_plan_hash"),
            protocol_membership_json=list(
                context.get("protocol_membership")
                if isinstance(context.get("protocol_membership"), list)
                else []
            ),
            current_transcript_id=transcript_id or context.get("transcript_id") or f"tr_{run_id}",
            heartbeat_at=utc_now() if status == "running" else None,
            error=getattr(execution, "error", None),
            result_json=getattr(execution, "result", None),
            execution_json=execution_json,
        )
        self.upsert_run(run)
        self.replace_steps(run_id, getattr(execution, "steps", []))
        self.replace_messages(run_id, run.current_transcript_id or f"tr_{run_id}", getattr(execution, "messages", []))
        pending = getattr(execution, "pending_tool_call", None)
        if isinstance(pending, dict):
            call_id = str(pending.get("id") or pending.get("tool_call_id") or f"call_{run_id}_{len(getattr(execution, 'steps', []))}")
            approval_id = str(pending.get("approval_id") or f"approval_{call_id}")
            self.record_tool_call(
                run_id,
                call_id,
                str(pending.get("tool_name") or "unknown"),
                pending.get("tool_args") or {},
                status="pending",
                approval_id=approval_id,
            )
            self.record_approval(approval_id, run_id, call_id)
        self.add_event(run_id, "run_step", {"status": status})

    def load_execution_dict(self, run_id: str) -> Optional[dict[str, Any]]:
        run = self.get_run(run_id)
        if not run:
            return None
        execution = run.get("execution_json")
        if isinstance(execution, dict) and execution:
            return execution
        return {
            "execution_id": run_id,
            "task": run.get("task"),
            "model": run.get("model"),
            "status": run.get("status"),
            "result": run.get("result_json"),
            "error": run.get("error"),
        }


def estimate_tokens(message: Any) -> int:
    raw = json_dumps(message)
    return max(1, len(raw) // 4)
