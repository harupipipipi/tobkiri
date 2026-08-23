"""Transactional profile-scoped Team state storage.

The public Company contracts remain compatibility adapters.  This module is the
only persistence owner and deliberately stores each Team entity independently.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from core_runtime.runtime_state import sqlite_wal_connection

VERSION = "tobkiri.team-state.v1"
LEGACY_VERSION = "rumi.company-state.v1"
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_HISTORY_LIMIT = 1_000
DEFAULT_HISTORY_LIMIT = 10_000

ENTITY_TABLES = {
    "roles": "roles",
    "members": "members",
    "departments": "departments",
    "member_pools": "member_pools",
    "channels": "channels",
    "team_conversation_bindings": "team_conversation_bindings",
    "tasks": "work_items",
    "assignments": "assignments",
    "execution_attempts": "execution_attempts",
    "execution_leases": "execution_leases",
    "inbox_items": "inbox_items",
    "routes": "inbound_routes",
    "connections": "connections",
    "summaries": "summaries",
}


class TeamStateConflict(RuntimeError):
    """A typed retryable optimistic-concurrency failure."""

    code = "TEAM_STATE_CONFLICT"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        team_id: str = "",
        entity_id: str = "",
        expected_revision: int | None = None,
        current_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.team_id = team_id
        self.entity_id = entity_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision

    def diagnostic(self) -> dict[str, Any]:
        """Return a metadata-redacted retry diagnostic."""

        return {
            "code": self.code,
            "retryable": True,
            "team_id": self.team_id,
            "entity_id": self.entity_id,
            "expected_revision": self.expected_revision,
            "current_revision": self.current_revision,
        }


class TeamStateTransitionError(RuntimeError):
    """A non-retryable invalid lifecycle transition."""

    code = "TEAM_STATE_TRANSITION_INVALID"
    retryable = False


class TeamMigrationConflict(RuntimeError):
    """A typed diagnostic for divergent or duplicate legacy sources."""

    code = "TEAM_MIGRATION_CONFLICT"
    retryable = False


class TeamStateQuarantined(RuntimeError):
    """Malformed legacy state was quarantined instead of treated as empty."""

    code = "TEAM_STATE_QUARANTINED"
    retryable = False

    def __init__(self, quarantine_path: Path) -> None:
        super().__init__("Legacy Team state is malformed and was quarantined")
        self.quarantine_path = quarantine_path


class TransactionalTeamStore:
    """SQLite WAL-backed canonical Team store for one validated profile."""

    def __init__(self, profile_id: str, root: Path) -> None:
        self.profile_id = profile_id
        self.root = root
        self.path = root / "team-state.sqlite3"
        self.legacy_path = root / "companies.json"
        self.root.mkdir(parents=True, exist_ok=True)
        _private(self.root, 0o700)
        self._initialize()
        self._migrate_legacy_json()
        for candidate in (root / "company_runtime.db", root / "company_runtime.sqlite3"):
            if candidate.is_file():
                self.migrate_legacy_sqlite(candidate)

    def connection(self) -> sqlite3.Connection:
        """Open a configured WAL connection owned by this store."""

        connection = sqlite_wal_connection(self.path)
        connection.execute("PRAGMA trusted_schema=OFF")
        return connection

    def snapshot(self, *, limit: int = 1_000, cursor: str = "") -> dict[str, Any]:
        """Return a consistent, stable Team page and its database cursor."""

        limit = _bounded_limit(limit, maximum=1_000)
        with closing(self.connection()) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT team_id FROM teams WHERE team_id > ? ORDER BY team_id LIMIT ?",
                (cursor, limit + 1),
            ).fetchall()
            visible = rows[:limit]
            companies = [self._project(connection, row["team_id"]) for row in visible]
            revision = int(
                connection.execute(
                    "SELECT value FROM store_metadata WHERE key='commit_sequence'"
                ).fetchone()["value"]
            )
            connection.commit()
        next_cursor = str(visible[-1]["team_id"]) if len(rows) > limit else ""
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": revision,
            "source_revision": revision,
            "companies": companies,
            "next_cursor": next_cursor,
        }

    def get(self, team_id: str) -> dict[str, Any] | None:
        """Return one exact compatibility projection without mutating it."""

        with closing(self.connection()) as connection:
            connection.execute("BEGIN")
            value = self._project(connection, team_id)
            connection.commit()
        return value

    def lookup(self, team_id: str) -> dict[str, Any]:
        """Distinguish a missing, archived, deleted, or active Team."""

        with closing(self.connection()) as connection:
            row = connection.execute(
                "SELECT status, deleted_at_ms, revision FROM teams WHERE team_id=?",
                (team_id,),
            ).fetchone()
        if row is None:
            return {"state": "missing", "team_id": team_id}
        state = "deleted" if row["deleted_at_ms"] is not None else str(row["status"])
        return {"state": state, "team_id": team_id, "revision": row["revision"]}

    def apply(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one Team-scoped CAS mutation atomically."""

        team_id = str(arguments["company_id"])
        expected = int(arguments["expected_revision"])
        with closing(self.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._team_revision(connection, team_id)
                if name == "company.create":
                    if current is not None:
                        raise TeamStateConflict(
                            "Team already exists",
                            team_id=team_id,
                            expected_revision=expected,
                            current_revision=current,
                        )
                    if expected != 0:
                        self._stale(team_id, expected, 0)
                    result = self._create(connection, team_id, arguments)
                elif name == "migration.operations.import":
                    result = self._import_operations(connection, team_id, arguments)
                    if result.get("deduplicated"):
                        connection.commit()
                        return result
                else:
                    if current is None:
                        raise KeyError("Team is unknown")
                    if current != expected:
                        self._stale(team_id, expected, current)
                    result = self._mutate(connection, team_id, name, arguments)
                revision = self._advance(connection, team_id, expected)
                connection.commit()
                return {**result, "revision": revision, "source_revision": revision}
            except Exception:
                connection.rollback()
                raise

    def list_timeline(
        self,
        team_id: str,
        *,
        kind: str = "message",
        limit: int = 100,
        after_sequence: int = 0,
    ) -> dict[str, Any]:
        """Return a stable append-only timeline page without loading Team graphs."""

        limit = _bounded_limit(limit, maximum=MAX_HISTORY_LIMIT)
        with closing(self.connection()) as connection:
            rows = connection.execute(
                "SELECT sequence, payload_json FROM timeline_events "
                "WHERE team_id=? AND kind=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (team_id, kind, max(0, after_sequence), limit + 1),
            ).fetchall()
        visible = rows[:limit]
        return {
            "records": [_decode(row["payload_json"]) for row in visible],
            "next_cursor": int(visible[-1]["sequence"]) if len(rows) > limit else None,
        }

    def claim_work(
        self,
        team_id: str,
        work_item_id: str,
        member_id: str,
        *,
        expected_revision: int,
        lease_duration_ms: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Atomically assign, start an attempt, and acquire a fenced lease."""

        now_ms = _now_ms()
        with closing(self.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = connection.execute(
                    "SELECT result_json FROM idempotency_records "
                    "WHERE team_id=? AND idempotency_key=?",
                    (team_id, idempotency_key),
                ).fetchone()
                if prior:
                    connection.commit()
                    return _decode(prior["result_json"])
                current = self._team_revision(connection, team_id)
                if current != expected_revision:
                    self._stale(team_id, expected_revision, current or 0)
                work = self._entity(connection, "work_items", team_id, work_item_id)
                if work is None or work.get("status") not in {"queued", "assigned"}:
                    raise TeamStateTransitionError("Work Item cannot be claimed")
                if self._entity(connection, "members", team_id, member_id) is None:
                    raise KeyError("Team member is unknown")
                assignment_id = str(uuid.uuid4())
                attempt_id = str(uuid.uuid4())
                lease_id = str(uuid.uuid4())
                fence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(fencing_token), 0) + 1 AS token "
                        "FROM execution_leases WHERE team_id=? AND entity_id=?",
                        (team_id, work_item_id),
                    ).fetchone()["token"]
                )
                work.update(
                    {
                        "status": "running",
                        "assignee_member_id": member_id,
                        "updated_at_ms": now_ms,
                    }
                )
                self._put_entity(connection, "work_items", team_id, work_item_id, work)
                assignment = {
                    "id": assignment_id,
                    "work_item_id": work_item_id,
                    "member_id": member_id,
                    "created_at_ms": now_ms,
                }
                attempt = {
                    "id": attempt_id,
                    "work_item_id": work_item_id,
                    "assignment_id": assignment_id,
                    "status": "running",
                    "created_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
                lease = {
                    "id": work_item_id,
                    "lease_id": lease_id,
                    "work_item_id": work_item_id,
                    "attempt_id": attempt_id,
                    "holder_member_id": member_id,
                    "fencing_token": fence,
                    "expires_at_ms": now_ms + max(1, lease_duration_ms),
                    "created_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
                self._put_entity(connection, "assignments", team_id, assignment_id, assignment)
                self._put_entity(connection, "execution_attempts", team_id, attempt_id, attempt)
                self._put_entity(
                    connection,
                    "execution_leases",
                    team_id,
                    work_item_id,
                    lease,
                    fencing_token=fence,
                    expires_at_ms=lease["expires_at_ms"],
                )
                revision = self._advance(connection, team_id, expected_revision)
                result = {
                    "work_item": work,
                    "assignment": assignment,
                    "attempt": attempt,
                    "lease": lease,
                    "revision": revision,
                    "source_revision": revision,
                }
                connection.execute(
                    "INSERT INTO idempotency_records"
                    "(team_id,idempotency_key,result_json,created_at_ms) VALUES(?,?,?,?)",
                    (team_id, idempotency_key, _encode(result), now_ms),
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def migrate_legacy_sqlite(self, source_path: Path) -> dict[str, Any]:
        """Import a supported legacy Company runtime database exactly once."""

        source_path = source_path.resolve()
        raw_digest = _file_digest(source_path)
        migration_id = f"company-runtime-sqlite:{raw_digest}"
        with closing(self.connection()) as connection:
            prior = connection.execute(
                "SELECT status FROM migration_records WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
            if prior and prior["status"] == "activated":
                return {"migration_id": migration_id, "deduplicated": True}
        backup_dir = self.root / "migration-backups"
        backup_dir.mkdir(exist_ok=True)
        _private(backup_dir, 0o700)
        backup = backup_dir / f"company-runtime-{raw_digest}.db"
        if not backup.exists():
            shutil.copy2(source_path, backup)
            _private(backup, 0o600)
        try:
            legacy = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
            legacy.row_factory = sqlite3.Row
            legacy.execute("PRAGMA query_only=ON")
            tables = {
                str(row["name"])
                for row in legacy.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            supported = {
                "company_threads",
                "company_messages",
                "company_tasks",
                "company_task_assignments",
                "company_agent_runs",
                "company_agent_inbox",
                "company_summaries",
            }
            if not tables & supported:
                raise sqlite3.DatabaseError("unsupported legacy Company database")
            extracted = {
                table: [dict(row) for row in legacy.execute(f"SELECT * FROM {table}")]
                for table in sorted(tables & supported)
            }
            legacy.close()
        except sqlite3.Error:
            quarantine = self.root / "quarantine"
            quarantine.mkdir(exist_ok=True)
            _private(quarantine, 0o700)
            target = quarantine / f"company-runtime-{raw_digest[:16]}.db"
            if not target.exists():
                shutil.copy2(source_path, target)
                _private(target, 0o600)
            raise TeamStateQuarantined(target)
        now_ms = _now_ms()
        with closing(self.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                team_ids = sorted(
                    {
                        str(row.get("company_id") or "")
                        for rows in extracted.values()
                        for row in rows
                        if row.get("company_id")
                    }
                )
                for team_id in team_ids:
                    if self._team_revision(connection, team_id) is None:
                        self._create(
                            connection,
                            team_id,
                            {
                                "name": team_id,
                                "description": "Migrated legacy Company runtime.",
                                "settings": {},
                                "metadata": {"migration_source": "company-runtime-sqlite"},
                                "conversation_group_id": f"company:{team_id}",
                            },
                        )
                    self._import_legacy_runtime_rows(connection, team_id, extracted, now_ms)
                connection.execute(
                    "INSERT INTO migration_records"
                    "(migration_id,team_id,source_kind,source_digest,status,backup_path,"
                    "started_at_ms,activated_at_ms) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        migration_id,
                        "",
                        "company-runtime-sqlite",
                        raw_digest,
                        "activated",
                        str(backup),
                        now_ms,
                        now_ms,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "migration_id": migration_id,
            "deduplicated": False,
            "team_ids": team_ids,
        }

    def _initialize(self) -> None:
        with closing(self.connection()) as connection:
            connection.executescript(_SCHEMA)
        _private(self.path, 0o600)

    def _team_revision(self, connection: sqlite3.Connection, team_id: str) -> int | None:
        row = connection.execute(
            "SELECT revision FROM teams WHERE team_id=? AND deleted_at_ms IS NULL",
            (team_id,),
        ).fetchone()
        return int(row["revision"]) if row else None

    @staticmethod
    def _stale(team_id: str, expected: int, current: int) -> None:
        raise TeamStateConflict(
            "Team revision is stale",
            team_id=team_id,
            expected_revision=expected,
            current_revision=current,
        )

    def _advance(self, connection: sqlite3.Connection, team_id: str, expected: int) -> int:
        updated = connection.execute(
            "UPDATE teams SET revision=revision+1 WHERE team_id=? AND revision=?",
            (team_id, expected),
        )
        if updated.rowcount != 1:
            current = self._team_revision(connection, team_id) or 0
            self._stale(team_id, expected, current)
        connection.execute(
            "UPDATE store_metadata SET value=CAST(value AS INTEGER)+1 WHERE key='commit_sequence'"
        )
        return expected + 1

    def _create(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        now_ms = _now_ms()
        team = {
            "id": team_id,
            "name": str(arguments["name"])[:200],
            "description": str(arguments["description"])[:4_000],
            "status": "active",
            "settings": _json_object(arguments["settings"]),
            "metadata": _json_object(arguments["metadata"]),
            "conversation_group_id": str(arguments["conversation_group_id"])[:255],
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
        }
        connection.execute(
            "INSERT INTO teams(team_id,revision,status,payload_json,created_at_ms,"
            "updated_at_ms) VALUES(?,0,'active',?,?,?)",
            (team_id, _encode(team), now_ms, now_ms),
        )
        return {"company": {**team, **_empty_collections(), "revision": 1}}

    def _mutate(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        now_ms = _now_ms()
        if name == "company.delete":
            active = connection.execute(
                "SELECT 1 FROM work_items WHERE team_id=? "
                "AND json_extract(payload_json,'$.status') IN "
                "('assigned','running','waiting') LIMIT 1",
                (team_id,),
            ).fetchone()
            if active:
                raise TeamStateTransitionError("Team has active Work Items")
            connection.execute(
                "UPDATE teams SET status='deleted',deleted_at_ms=?,updated_at_ms=? WHERE team_id=?",
                (now_ms, now_ms, team_id),
            )
            return {"deleted_company_id": team_id}
        if name == "company.update":
            team = self._team_payload(connection, team_id)
            updates = _json_object(arguments["updates"])
            for key in ("name", "status", "description", "conversation_group_id"):
                if key in updates:
                    team[key] = updates[key]
            for key in ("settings", "metadata"):
                if key in updates:
                    if key == "settings" and arguments.get("replace_settings"):
                        team[key] = _json_object(updates[key])
                    else:
                        team[key] = {**_json_object(team.get(key)), **_json_object(updates[key])}
            team["updated_at_ms"] = now_ms
            connection.execute(
                "UPDATE teams SET status=?,payload_json=?,updated_at_ms=? WHERE team_id=?",
                (str(team.get("status") or "active"), _encode(team), now_ms, team_id),
            )
            return {"company": self._project(connection, team_id)}
        if name == "agent.upsert":
            role = dict(arguments["role"])
            role_id = str(role["id"])
            role = self._normalized_named(connection, "roles", team_id, role_id, role, now_ms)
            self._put_entity(connection, "roles", team_id, role_id, role, arguments)
            member = self._normalized_member(
                connection,
                team_id,
                str(arguments["member"]["id"]),
                arguments["member"],
                now_ms,
            )
            self._put_entity(connection, "members", team_id, str(member["id"]), member, arguments)
            self._sync_default_channels(connection, team_id, now_ms)
            self._touch_team(connection, team_id, now_ms)
            return {"agent": member, "role": role}
        prefix = name.split(".", 1)[0]
        table_for_prefix = {
            "role": "roles",
            "member": "members",
            "channel": "channels",
            "route": "inbound_routes",
            "task": "work_items",
        }
        if prefix in table_for_prefix and name.endswith(".upsert"):
            table = table_for_prefix[prefix]
            entity_id = str(arguments["record_id"])
            record = dict(arguments["record"])
            if table == "members":
                record = self._normalized_member(connection, team_id, entity_id, record, now_ms)
            elif table == "work_items":
                record = self._normalized_work(connection, team_id, entity_id, record, now_ms)
            else:
                record = self._normalized_named(
                    connection, table, team_id, entity_id, record, now_ms
                )
            self._put_entity(connection, table, team_id, entity_id, record, arguments)
            if table in {"members", "channels"}:
                self._sync_default_channels(connection, team_id, now_ms)
            self._touch_team(connection, team_id, now_ms)
            return {prefix if prefix != "task" else "task": record}
        if prefix in table_for_prefix and name.endswith(".delete"):
            table = table_for_prefix[prefix]
            entity_id = str(arguments["record_id"])
            if table == "members":
                self._assert_member_idle(connection, team_id, entity_id)
            deleted = connection.execute(
                f"DELETE FROM {table} WHERE team_id=? AND entity_id=?",
                (team_id, entity_id),
            )
            if deleted.rowcount != 1:
                raise KeyError(f"Team {prefix} is unknown")
            if table == "members":
                self._sync_default_channels(connection, team_id, now_ms)
            self._touch_team(connection, team_id, now_ms)
            return {f"deleted_{prefix}_id": entity_id}
        if name == "agent.delete":
            entity_id = str(arguments["record_id"])
            self._assert_member_idle(connection, team_id, entity_id)
            deleted = connection.execute(
                "DELETE FROM members WHERE team_id=? AND entity_id=?",
                (team_id, entity_id),
            )
            if deleted.rowcount != 1:
                raise KeyError("Team member is unknown")
            self._sync_default_channels(connection, team_id, now_ms)
            self._touch_team(connection, team_id, now_ms)
            return {"deleted_agent_id": entity_id}
        if name == "task.transition":
            task_id = str(arguments["record_id"])
            task = self._entity(connection, "work_items", team_id, task_id)
            if task is None:
                raise KeyError("Team Work Item is unknown")
            target = str(arguments["status"])
            transitions = {
                "queued": {"assigned", "cancelled", "blocked"},
                "assigned": {"running", "cancelled", "blocked"},
                "running": {"waiting", "completed", "failed", "cancelled", "blocked"},
                "waiting": {"running", "cancelled", "blocked"},
                "blocked": {"queued", "assigned", "cancelled"},
                "failed": {"queued", "cancelled"},
            }
            if target not in transitions.get(str(task["status"]), set()):
                raise TeamStateTransitionError("Work Item transition is invalid")
            details = _json_object(arguments["details"])
            assignee = details.get("assignee_member_id")
            if assignee and self._entity(connection, "members", team_id, str(assignee)) is None:
                raise KeyError("Team Work Item assignee is unknown")
            task["status"] = target
            if "assignee_member_id" in details:
                task["assignee_member_id"] = str(assignee or "")
            if target == "completed":
                task["result_reference"] = details.get("result_reference")
            if target in {"failed", "blocked"}:
                task["error"] = str(details.get("error") or "")[:1_000]
            task["updated_at_ms"] = now_ms
            self._put_entity(connection, "work_items", team_id, task_id, task, arguments)
            self._touch_team(connection, team_id, now_ms)
            return {"task": task}
        if name in {"inbound.append", "message.append"}:
            kind = "inbound" if name.startswith("inbound") else "message"
            record = _timeline(arguments["record"], now_ms)
            encoded = _encode(record)
            _assert_payload(encoded)
            try:
                connection.execute(
                    "INSERT INTO timeline_events"
                    "(team_id,event_id,kind,payload_json,created_at_ms) VALUES(?,?,?,?,?)",
                    (team_id, record["id"], kind, encoded, now_ms),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    "SELECT payload_json FROM timeline_events WHERE team_id=? AND event_id=?",
                    (team_id, record["id"]),
                ).fetchone()
                prior = _decode(existing["payload_json"])
                if prior != record:
                    raise TeamStateConflict(
                        "Timeline event identifier already has different content",
                        team_id=team_id,
                        entity_id=str(record["id"]),
                    )
                return {kind: prior, "deduplicated": True}
            self._touch_team(connection, team_id, now_ms)
            return {kind: record, "deduplicated": False}
        raise ValueError(f"unknown Team action: {name}")

    def _project(self, connection: sqlite3.Connection, team_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT revision,status,payload_json FROM teams "
            "WHERE team_id=? AND deleted_at_ms IS NULL",
            (team_id,),
        ).fetchone()
        if row is None:
            return None
        team = _decode(row["payload_json"])
        for key, table in ENTITY_TABLES.items():
            records = connection.execute(
                f"SELECT payload_json FROM {table} WHERE team_id=? ORDER BY entity_id",
                (team_id,),
            ).fetchall()
            team[key] = {
                str(record["id"]): record
                for record in (_decode(item["payload_json"]) for item in records)
            }
        for kind, key in (("inbound", "inbound"), ("message", "messages")):
            rows = connection.execute(
                "SELECT payload_json FROM timeline_events WHERE team_id=? AND kind=? "
                "ORDER BY sequence DESC LIMIT ?",
                (team_id, kind, DEFAULT_HISTORY_LIMIT),
            ).fetchall()
            team[key] = [_decode(item["payload_json"]) for item in reversed(rows)]
        team["revision"] = int(row["revision"])
        team["source_revision"] = int(row["revision"])
        team["counts"] = self._counts(connection, team_id)
        return team

    @staticmethod
    def _counts(connection: sqlite3.Connection, team_id: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for key, table in {
            "members": "members",
            "channels": "channels",
            "tasks": "work_items",
            "messages": "timeline_events",
        }.items():
            where = "team_id=?"
            parameters: tuple[Any, ...] = (team_id,)
            if key == "messages":
                where += " AND kind='message'"
            result[key] = int(
                connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE {where}", parameters
                ).fetchone()["count"]
            )
        return result

    @staticmethod
    def _team_payload(connection: sqlite3.Connection, team_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload_json FROM teams WHERE team_id=?", (team_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Team is unknown")
        return _decode(row["payload_json"])

    def _entity(
        self, connection: sqlite3.Connection, table: str, team_id: str, entity_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            f"SELECT payload_json FROM {table} WHERE team_id=? AND entity_id=?",
            (team_id, entity_id),
        ).fetchone()
        return _decode(row["payload_json"]) if row else None

    def _put_entity(
        self,
        connection: sqlite3.Connection,
        table: str,
        team_id: str,
        entity_id: str,
        record: Mapping[str, Any],
        arguments: Mapping[str, Any] | None = None,
        *,
        fencing_token: int = 0,
        expires_at_ms: int = 0,
    ) -> int:
        encoded = _encode(record)
        _assert_payload(encoded)
        row = connection.execute(
            f"SELECT revision FROM {table} WHERE team_id=? AND entity_id=?",
            (team_id, entity_id),
        ).fetchone()
        current = int(row["revision"]) if row else 0
        expected = (arguments or {}).get("expected_entity_revision")
        if expected is not None and int(expected) != current:
            raise TeamStateConflict(
                "Entity revision is stale",
                team_id=team_id,
                entity_id=entity_id,
                expected_revision=int(expected),
                current_revision=current,
            )
        now_ms = _now_ms()
        connection.execute(
            f"INSERT INTO {table}"
            "(team_id,entity_id,revision,payload_json,created_at_ms,updated_at_ms,"
            "fencing_token,expires_at_ms) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(team_id,entity_id) DO UPDATE SET "
            "revision=excluded.revision,payload_json=excluded.payload_json,"
            "updated_at_ms=excluded.updated_at_ms,fencing_token=excluded.fencing_token,"
            "expires_at_ms=excluded.expires_at_ms",
            (
                team_id,
                entity_id,
                current + 1,
                encoded,
                int(record.get("created_at_ms") or now_ms),
                int(record.get("updated_at_ms") or now_ms),
                fencing_token,
                expires_at_ms,
            ),
        )
        return current + 1

    def _normalized_named(
        self,
        connection: sqlite3.Connection,
        table: str,
        team_id: str,
        entity_id: str,
        record: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        current = self._entity(connection, table, team_id, entity_id)
        result = dict(record)
        result["id"] = entity_id
        result["name"] = str(result.get("name") or entity_id)[:200]
        result["created_at_ms"] = current["created_at_ms"] if current else now_ms
        result["updated_at_ms"] = now_ms
        return result

    def _normalized_member(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        entity_id: str,
        record: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        role_id = str(record.get("role_id") or "")
        if self._entity(connection, "roles", team_id, role_id) is None:
            raise KeyError("Team member role is unknown")
        current = self._entity(connection, "members", team_id, entity_id)
        return {
            "id": entity_id,
            "display_name": str(record.get("display_name") or entity_id)[:200],
            "role_id": role_id,
            "agent_profile_id": str(record.get("agent_profile_id") or "default")[:255],
            "mentions": sorted(
                {str(item).casefold()[:100] for item in record.get("mentions") or []}
            )[:100],
            "enabled": bool(record.get("enabled", True)),
            "metadata": _json_object(record.get("metadata")),
            "created_at_ms": current["created_at_ms"] if current else now_ms,
            "updated_at_ms": now_ms,
        }

    def _normalized_work(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        entity_id: str,
        record: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        assignee = str(record.get("assignee_member_id") or "")
        if assignee and self._entity(connection, "members", team_id, assignee) is None:
            raise KeyError("Team Work Item assignee is unknown")
        status = str(record.get("status") or "queued")
        if status not in {
            "queued",
            "assigned",
            "running",
            "waiting",
            "blocked",
            "failed",
            "completed",
            "cancelled",
        }:
            raise ValueError("Team Work Item status is invalid")
        current = self._entity(connection, "work_items", team_id, entity_id)
        return {
            "id": entity_id,
            "title": str(record.get("title") or "Task")[:500],
            "description": str(record.get("description") or "")[:100_000],
            "status": status,
            "assignee_member_id": assignee,
            "channel_id": str(record.get("channel_id") or "")[:255],
            "priority": max(0, min(100, int(record.get("priority") or 50))),
            "idempotency_key": str(record.get("idempotency_key") or entity_id)[:255],
            "result_reference": record.get("result_reference"),
            "error": str(record.get("error") or "")[:1_000],
            "metadata": _json_object(record.get("metadata")),
            "created_at_ms": current["created_at_ms"] if current else now_ms,
            "updated_at_ms": now_ms,
        }

    @staticmethod
    def _touch_team(connection: sqlite3.Connection, team_id: str, now_ms: int) -> None:
        team = TransactionalTeamStore._team_payload(connection, team_id)
        team["updated_at_ms"] = now_ms
        connection.execute(
            "UPDATE teams SET payload_json=?,updated_at_ms=? WHERE team_id=?",
            (_encode(team), now_ms, team_id),
        )

    def _assert_member_idle(
        self, connection: sqlite3.Connection, team_id: str, member_id: str
    ) -> None:
        active = connection.execute(
            "SELECT 1 FROM work_items WHERE team_id=? "
            "AND json_extract(payload_json,'$.assignee_member_id')=? "
            "AND json_extract(payload_json,'$.status') IN "
            "('assigned','running','waiting') LIMIT 1",
            (team_id, member_id),
        ).fetchone()
        if active:
            raise TeamStateTransitionError("Team member has active Work Items")

    def _sync_default_channels(
        self, connection: sqlite3.Connection, team_id: str, now_ms: int
    ) -> None:
        member_ids = [
            str(row["entity_id"])
            for row in connection.execute(
                "SELECT entity_id FROM members WHERE team_id=? ORDER BY entity_id",
                (team_id,),
            ).fetchall()
        ]
        rows = connection.execute(
            "SELECT entity_id,payload_json FROM channels WHERE team_id=?",
            (team_id,),
        ).fetchall()
        for row in rows:
            channel = _decode(row["payload_json"])
            if channel.get("is_default") or str(row["entity_id"]) in {"general", "ops-company"}:
                channel["member_ids"] = member_ids
                channel["updated_at_ms"] = now_ms
                self._put_entity(connection, "channels", team_id, str(row["entity_id"]), channel)

    def _import_legacy_runtime_rows(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        rows: Mapping[str, list[dict[str, Any]]],
        now_ms: int,
    ) -> None:
        """Import normalized runtime rows for one Team in the activation txn."""

        for row in rows.get("company_messages", []):
            if str(row.get("company_id")) != team_id:
                continue
            record = {
                "id": str(row["message_id"]),
                "type": "message",
                "actor_id": str(row.get("sender_id") or ""),
                "channel_id": str(row.get("channel_id") or ""),
                "text": str(row.get("content") or "")[:100_000],
                "metadata": _json_column(row.get("metadata_json")),
                "created_at_ms": _timestamp_ms(row.get("created_at"), now_ms),
            }
            self._insert_migrated_timeline(connection, team_id, record)
        for row in rows.get("company_tasks", []):
            if str(row.get("company_id")) != team_id:
                continue
            task_id = str(row["task_id"])
            record = {
                "id": task_id,
                "title": str(row.get("title") or "Task")[:500],
                "description": str(row.get("description") or "")[:100_000],
                "status": str(row.get("status") or "queued"),
                "assignee_member_id": "",
                "channel_id": str(row.get("channel_id") or "")[:255],
                "priority": _legacy_priority(row.get("priority")),
                "idempotency_key": task_id,
                "result_reference": None,
                "error": "",
                "metadata": _json_column(row.get("metadata_json")),
                "created_at_ms": _timestamp_ms(row.get("created_at"), now_ms),
                "updated_at_ms": _timestamp_ms(row.get("updated_at"), now_ms),
            }
            self._insert_migrated_entity(connection, "work_items", team_id, task_id, record)
        mappings = (
            (
                "company_task_assignments",
                "assignments",
                lambda row: f"{row['task_id']}:{row['agent_id']}",
            ),
            ("company_agent_runs", "execution_attempts", lambda row: row["link_id"]),
            ("company_agent_inbox", "inbox_items", lambda row: row["inbox_id"]),
        )
        for source, target, identifier in mappings:
            for row in rows.get(source, []):
                if str(row.get("company_id")) != team_id:
                    continue
                entity_id = str(identifier(row))
                record = {
                    **{key: value for key, value in row.items() if key != "company_id"},
                    "id": entity_id,
                    "created_at_ms": _timestamp_ms(row.get("created_at"), now_ms),
                    "updated_at_ms": _timestamp_ms(row.get("updated_at"), now_ms),
                }
                self._insert_migrated_entity(connection, target, team_id, entity_id, record)
        for row in rows.get("company_summaries", []):
            if str(row.get("company_id")) != team_id:
                continue
            entity_id = f"{row['scope_type']}:{row['scope_id']}"
            record = {
                **{key: value for key, value in row.items() if key != "company_id"},
                "id": entity_id,
                "created_at_ms": _timestamp_ms(row.get("created_at"), now_ms),
                "updated_at_ms": _timestamp_ms(row.get("updated_at"), now_ms),
            }
            self._insert_migrated_entity(connection, "summaries", team_id, entity_id, record)

    def _insert_migrated_entity(
        self,
        connection: sqlite3.Connection,
        table: str,
        team_id: str,
        entity_id: str,
        record: Mapping[str, Any],
    ) -> None:
        current = self._entity(connection, table, team_id, entity_id)
        if current is not None:
            if _digest(current) != _digest(record):
                raise TeamMigrationConflict(f"Divergent legacy {table} identifier: {entity_id}")
            return
        self._put_entity(connection, table, team_id, entity_id, record)

    @staticmethod
    def _insert_migrated_timeline(
        connection: sqlite3.Connection,
        team_id: str,
        record: Mapping[str, Any],
    ) -> None:
        current = connection.execute(
            "SELECT payload_json FROM timeline_events WHERE team_id=? AND event_id=?",
            (team_id, str(record["id"])),
        ).fetchone()
        if current:
            if _decode(current["payload_json"]) != record:
                raise TeamMigrationConflict(f"Divergent legacy timeline identifier: {record['id']}")
            return
        connection.execute(
            "INSERT INTO timeline_events"
            "(team_id,event_id,kind,payload_json,created_at_ms) VALUES(?,?,?,?,?)",
            (
                team_id,
                str(record["id"]),
                "message",
                _encode(record),
                int(record["created_at_ms"]),
            ),
        )

    def _import_operations(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        legacy = _json_object(arguments["legacy_state"])
        digest = _digest(legacy)
        migration_id = f"operations-company-v1:{team_id}"
        row = connection.execute(
            "SELECT source_digest,status FROM migration_records WHERE migration_id=?",
            (migration_id,),
        ).fetchone()
        if row:
            if row["source_digest"] != digest or row["status"] != "activated":
                raise TeamMigrationConflict("Operations migration source differs")
            company = self._project(connection, team_id)
            return {
                "company": company,
                "migration_id": migration_id,
                "deduplicated": True,
                "revision": company["revision"] if company else 0,
            }
        if self._team_revision(connection, team_id) is not None:
            raise TeamMigrationConflict("Team exists before Operations migration")
        now_ms = _now_ms()
        create = {
            "name": "Rumi Operations Company",
            "description": "Migrated legacy Operations Company.",
            "settings": {"legacy_operations": {**legacy, "source_hash": digest}},
            "metadata": {"migration_source": "operations-company-v1"},
            "conversation_group_id": str(
                legacy.get("conversation_group_id") or f"company:{team_id}"
            ),
        }
        self._create(connection, team_id, create)
        for role_id, name in (
            ("legacy-client-manager", "Client Manager"),
            ("legacy-operations-monitor", "Operations Monitor"),
        ):
            role = {
                "id": role_id,
                "name": name,
                "work_type": "agent",
                "created_at_ms": now_ms,
                "updated_at_ms": now_ms,
            }
            self._put_entity(connection, "roles", team_id, role_id, role)
        for member_id, name, role_id in (
            ("client_manager", "Client Manager", "legacy-client-manager"),
            ("operations_monitor", "Operations Monitor", "legacy-operations-monitor"),
        ):
            member = self._normalized_member(
                connection,
                team_id,
                member_id,
                {
                    "display_name": name,
                    "role_id": role_id,
                    "mentions": [member_id],
                    "metadata": {"migration_source": "operations-company-v1"},
                },
                now_ms,
            )
            self._put_entity(connection, "members", team_id, member_id, member)
        channel = {
            "id": "ops-company",
            "name": "Operations",
            "is_default": True,
            "member_ids": ["client_manager", "operations_monitor"],
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
        }
        self._put_entity(connection, "channels", team_id, "ops-company", channel)
        connection.execute(
            "INSERT INTO migration_records(migration_id,team_id,source_kind,"
            "source_digest,status,backup_path,started_at_ms,activated_at_ms) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (migration_id, team_id, "operations", digest, "activated", "", now_ms, now_ms),
        )
        return {
            "company": self._project(connection, team_id),
            "migration_id": migration_id,
            "deduplicated": False,
        }

    def _migrate_legacy_json(self) -> None:
        if not self.legacy_path.is_file():
            return
        raw = self.legacy_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        migration_id = f"companies-json:{digest}"
        with closing(self.connection()) as connection:
            existing = connection.execute(
                "SELECT status FROM migration_records WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
            if existing and existing["status"] == "activated":
                return
        try:
            value = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(value, Mapping)
                or value.get("version") != LEGACY_VERSION
                or value.get("profile_id") != self.profile_id
                or not isinstance(value.get("companies"), Mapping)
            ):
                raise ValueError("invalid legacy Team state")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            quarantine = self.root / "quarantine"
            quarantine.mkdir(exist_ok=True)
            _private(quarantine, 0o700)
            target = quarantine / f"companies-{digest[:16]}.json"
            if not target.exists():
                shutil.copy2(self.legacy_path, target)
                _private(target, 0o600)
            raise TeamStateQuarantined(target)
        backup_dir = self.root / "migration-backups"
        backup_dir.mkdir(exist_ok=True)
        _private(backup_dir, 0o700)
        backup = backup_dir / f"companies-{digest}.json"
        if not backup.exists():
            shutil.copy2(self.legacy_path, backup)
            _private(backup, 0o600)
        now_ms = _now_ms()
        with closing(self.connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for team_id in sorted(value["companies"]):
                    self._import_legacy_team(connection, str(team_id), value["companies"][team_id])
                connection.execute(
                    "INSERT OR REPLACE INTO migration_records"
                    "(migration_id,team_id,source_kind,source_digest,status,backup_path,"
                    "started_at_ms,activated_at_ms) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        migration_id,
                        "",
                        "companies-json",
                        digest,
                        "activated",
                        str(backup),
                        now_ms,
                        now_ms,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _import_legacy_team(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        value: Any,
    ) -> None:
        if not isinstance(value, Mapping) or str(value.get("id") or "") != team_id:
            raise TeamMigrationConflict("Legacy Team record is malformed")
        existing = self._team_revision(connection, team_id)
        if existing is not None:
            projected = self._project(connection, team_id)
            if _digest(_strip_projection(projected or {})) != _digest(_strip_projection(value)):
                raise TeamMigrationConflict("Legacy Team duplicates canonical Team")
            return
        team = {
            key: value.get(key)
            for key in (
                "id",
                "name",
                "description",
                "status",
                "settings",
                "metadata",
                "conversation_group_id",
                "created_at_ms",
                "updated_at_ms",
            )
        }
        created = int(team.get("created_at_ms") or _now_ms())
        updated = int(team.get("updated_at_ms") or created)
        connection.execute(
            "INSERT INTO teams(team_id,revision,status,payload_json,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?)",
            (
                team_id,
                max(0, int(value.get("revision") or 0)),
                str(team.get("status") or "active"),
                _encode(team),
                created,
                updated,
            ),
        )
        for key, table in ENTITY_TABLES.items():
            records = value.get(key, {})
            if isinstance(records, Mapping):
                iterable = records.items()
            elif isinstance(records, list):
                iterable = (
                    (str(item.get("id") or uuid.uuid4()), item)
                    for item in records
                    if isinstance(item, Mapping)
                )
            else:
                iterable = ()
            for entity_id, record in iterable:
                self._put_entity(connection, table, team_id, str(entity_id), record)
        for key, kind in (("inbound", "inbound"), ("messages", "message")):
            records = value.get(key, [])
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, Mapping):
                    continue
                normalized = dict(record)
                event_id = str(normalized.get("id") or uuid.uuid4())
                normalized["id"] = event_id
                connection.execute(
                    "INSERT INTO timeline_events(team_id,event_id,kind,payload_json,created_at_ms) VALUES(?,?,?,?,?)",
                    (
                        team_id,
                        event_id,
                        kind,
                        _encode(normalized),
                        int(normalized.get("created_at_ms") or created),
                    ),
                )


def _empty_collections() -> dict[str, Any]:
    return {
        "roles": {},
        "members": {},
        "departments": {},
        "member_pools": {},
        "channels": {},
        "team_conversation_bindings": {},
        "tasks": {},
        "assignments": {},
        "execution_attempts": {},
        "execution_leases": {},
        "inbox_items": {},
        "routes": {},
        "connections": {},
        "summaries": {},
        "inbound": [],
        "messages": [],
    }


def _timeline(value: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
    return {
        "id": str(value.get("id") or uuid.uuid4()),
        "type": str(value.get("type") or "message")[:120],
        "actor_id": str(value.get("actor_id") or "")[:255],
        "channel_id": str(value.get("channel_id") or "")[:255],
        "text": str(value.get("text") or "")[:100_000],
        "metadata": _json_object(value.get("metadata")),
        "created_at_ms": int(value.get("created_at_ms") or now_ms),
    }


def _strip_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    for key in ("revision", "source_revision", "counts"):
        result.pop(key, None)
    return result


def _bounded_limit(value: int, *, maximum: int) -> int:
    if value < 1 or value > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def _assert_payload(encoded: str) -> None:
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("Team entity payload is too large")


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    encoded = _encode(value)
    _assert_payload(encoded)
    return _decode(encoded)


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _decode(value: str) -> Any:
    return json.loads(value)


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _json_column(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _timestamp_ms(value: Any, fallback: int) -> int:
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1_000)
    except ValueError:
        return fallback


def _legacy_priority(value: Any) -> int:
    text = str(value or "normal").casefold()
    return {"low": 25, "normal": 50, "high": 75, "urgent": 100}.get(text, 50)


def _private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _now_ms() -> int:
    return int(time.time() * 1_000)


_ENTITY_SCHEMA = """
    team_id TEXT NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    expires_at_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(team_id, entity_id)
"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS store_metadata(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO store_metadata(key,value) VALUES('commit_sequence','0');
CREATE TABLE IF NOT EXISTS teams(
    team_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    deleted_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS roles({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS members({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS departments({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS member_pools({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS channels({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS team_conversation_bindings({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS work_items({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS assignments({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS execution_attempts({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS execution_leases({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS inbox_items({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS connections({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS inbound_routes({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS summaries({_ENTITY_SCHEMA});
CREATE TABLE IF NOT EXISTS timeline_events(
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    created_at_ms INTEGER NOT NULL,
    UNIQUE(team_id,event_id)
);
CREATE INDEX IF NOT EXISTS timeline_page_idx
    ON timeline_events(team_id,kind,sequence);
CREATE TABLE IF NOT EXISTS inbound_receipts(
    team_id TEXT NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    receipt_id TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY(team_id,receipt_id)
);
CREATE TABLE IF NOT EXISTS migration_records(
    migration_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    backup_path TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    activated_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS idempotency_records(
    team_id TEXT NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK(json_valid(result_json)),
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY(team_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS work_status_idx
    ON work_items(team_id,json_extract(payload_json,'$.status'));
CREATE INDEX IF NOT EXISTS lease_expiry_idx
    ON execution_leases(team_id,expires_at_ms,fencing_token);
"""
