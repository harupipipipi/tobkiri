"""Durable Host-owned idempotency ledger for repository context runs."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from .paths import USER_DATA_DIR
from .runtime_state import sqlite_wal_connection

_RUNNING_TTL_SECONDS = 15 * 60
_COMPLETED_TTL_SECONDS = 7 * 24 * 60 * 60
_MAX_ROWS_PER_PROFILE = 4096


class RepositoryContextLedgerError(RuntimeError):
    """Base error for durable repository-context reservations."""


class RepositoryContextLedgerConflict(RepositoryContextLedgerError):
    """An idempotency key was reused with different bound content."""


class RepositoryContextLedgerInProgress(RepositoryContextLedgerError):
    """An equivalent invocation is already running."""


class RepositoryContextBudgetExceeded(RepositoryContextLedgerError):
    """A Host-bound repository-context budget was exhausted."""


class RepositoryContextLedger:
    """Reserve and complete bounded profile-scoped repository invocations."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else (
            Path(USER_DATA_DIR)
            / "database"
            / "repository_context_idempotency.sqlite3"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def reserve(
        self,
        *,
        profile_id: str,
        key: str,
        digest: str,
    ) -> dict[str, Any] | None:
        now = time.time()
        with sqlite_wal_connection(self.path) as connection:
            self._migrate(connection)
            connection.execute("BEGIN IMMEDIATE")
            self._prune(connection, profile_id, now)
            row = connection.execute(
                """
                SELECT digest, status, result_json, updated_at
                FROM repository_context_invocations
                WHERE profile_id = ? AND invocation_key = ?
                """,
                (profile_id, key),
            ).fetchone()
            if row is not None:
                if str(row["digest"]) != digest:
                    connection.rollback()
                    raise RepositoryContextLedgerConflict(
                        "idempotency key conflicts with different content"
                    )
                if str(row["status"]) == "completed":
                    result = json.loads(str(row["result_json"] or "{}"))
                    connection.commit()
                    return result if isinstance(result, dict) else {}
                if now - float(row["updated_at"] or 0) <= _RUNNING_TTL_SECONDS:
                    connection.rollback()
                    raise RepositoryContextLedgerInProgress(
                        "repository context invocation is already in progress"
                    )
                connection.execute(
                    """
                    UPDATE repository_context_invocations
                    SET updated_at = ?, result_json = NULL
                    WHERE profile_id = ? AND invocation_key = ?
                    """,
                    (now, profile_id, key),
                )
                connection.commit()
                return None
            connection.execute(
                """
                INSERT INTO repository_context_invocations(
                    profile_id, invocation_key, digest, status,
                    result_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', NULL, ?, ?)
                """,
                (profile_id, key, digest, now, now),
            )
            connection.commit()
        return None

    def complete(
        self,
        *,
        profile_id: str,
        key: str,
        digest: str,
        result: Mapping[str, Any],
    ) -> None:
        encoded = json.dumps(
            dict(result),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with sqlite_wal_connection(self.path) as connection:
            self._migrate(connection)
            cursor = connection.execute(
                """
                UPDATE repository_context_invocations
                SET status = 'completed', result_json = ?, updated_at = ?
                WHERE profile_id = ? AND invocation_key = ? AND digest = ?
                """,
                (encoded, time.time(), profile_id, key, digest),
            )
            if cursor.rowcount != 1:
                raise RepositoryContextLedgerConflict(
                    "idempotency reservation changed before completion"
                )

    def abandon(
        self,
        *,
        profile_id: str,
        key: str,
        digest: str,
    ) -> None:
        with sqlite_wal_connection(self.path) as connection:
            self._migrate(connection)
            connection.execute(
                """
                DELETE FROM repository_context_invocations
                WHERE profile_id = ? AND invocation_key = ?
                  AND digest = ? AND status = 'running'
                """,
                (profile_id, key, digest),
            )

    def reserve_budget(
        self,
        *,
        profile_id: str,
        workspace_id: str,
        key: str,
        digest: str,
        limits: Mapping[str, Any],
    ) -> None:
        """Create the durable, process-global cost ledger for one run."""

        normalized = {
            "maximum_tool_calls": int(limits["maximum_tool_calls"]),
            "maximum_steps": int(limits["maximum_steps"]),
            "maximum_cost": float(limits["maximum_cost"]),
            "context_token_budget": int(limits["context_token_budget"]),
            "deadline_epoch_ms": int(limits["deadline_epoch_ms"]),
        }
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = time.time()
        with sqlite_wal_connection(self.path) as connection:
            self._migrate(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT digest, limits_json, status
                FROM repository_context_budget_usage
                WHERE profile_id = ? AND workspace_id = ?
                  AND invocation_key = ?
                """,
                (profile_id, workspace_id, key),
            ).fetchone()
            if row is not None:
                if (
                    str(row["digest"]) != digest
                    or str(row["limits_json"]) != encoded
                ):
                    connection.rollback()
                    raise RepositoryContextLedgerConflict(
                        "budget ledger identity conflicts with different content"
                    )
                if str(row["status"]) == "running":
                    connection.rollback()
                    raise RepositoryContextLedgerInProgress(
                        "repository context budget is already in progress"
                    )
                connection.execute(
                    """
                    UPDATE repository_context_budget_usage
                    SET tool_calls = 0, steps = 0, input_tokens = 0,
                        output_tokens = 0, cost = 0, status = 'running',
                        updated_at = ?
                    WHERE profile_id = ? AND workspace_id = ?
                      AND invocation_key = ?
                    """,
                    (now, profile_id, workspace_id, key),
                )
                connection.commit()
                return
            connection.execute(
                """
                INSERT INTO repository_context_budget_usage(
                    profile_id, workspace_id, invocation_key, digest,
                    limits_json, tool_calls, steps, input_tokens,
                    output_tokens, cost, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 'running', ?, ?)
                """,
                (
                    profile_id,
                    workspace_id,
                    key,
                    digest,
                    encoded,
                    now,
                    now,
                ),
            )
            connection.commit()

    def consume_budget(
        self,
        *,
        profile_id: str,
        workspace_id: str,
        key: str,
        digest: str,
        tool_calls: int = 0,
        steps: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0.0,
    ) -> dict[str, int | float]:
        """Atomically consume global run budget or fail without updating it."""

        increments = {
            "tool_calls": int(tool_calls),
            "steps": int(steps),
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cost": float(cost),
        }
        if any(value < 0 for value in increments.values()):
            raise ValueError("budget increments must be non-negative")
        with sqlite_wal_connection(self.path) as connection:
            self._migrate(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM repository_context_budget_usage
                WHERE profile_id = ? AND workspace_id = ?
                  AND invocation_key = ? AND digest = ?
                """,
                (profile_id, workspace_id, key, digest),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RepositoryContextLedgerConflict(
                    "repository context budget was not reserved"
                )
            limits = json.loads(str(row["limits_json"]))
            usage = {
                name: row[name] + increments[name]
                for name in (
                    "tool_calls",
                    "steps",
                    "input_tokens",
                    "output_tokens",
                    "cost",
                )
            }
            if int(time.time() * 1000) >= int(
                limits["deadline_epoch_ms"]
            ):
                connection.rollback()
                raise RepositoryContextBudgetExceeded(
                    "repository context timeout budget exceeded"
                )
            if usage["tool_calls"] > limits["maximum_tool_calls"]:
                connection.rollback()
                raise RepositoryContextBudgetExceeded(
                    "repository context Tool-call budget exceeded"
                )
            if usage["steps"] > limits["maximum_steps"]:
                connection.rollback()
                raise RepositoryContextBudgetExceeded(
                    "repository context step budget exceeded"
                )
            if (
                usage["input_tokens"] + usage["output_tokens"]
                > limits["context_token_budget"]
            ):
                connection.rollback()
                raise RepositoryContextBudgetExceeded(
                    "repository context token budget exceeded"
                )
            if usage["cost"] > limits["maximum_cost"]:
                connection.rollback()
                raise RepositoryContextBudgetExceeded(
                    "repository context cost budget exceeded"
                )
            connection.execute(
                """
                UPDATE repository_context_budget_usage
                SET tool_calls = ?, steps = ?, input_tokens = ?,
                    output_tokens = ?, cost = ?, updated_at = ?
                WHERE profile_id = ? AND workspace_id = ?
                  AND invocation_key = ? AND digest = ?
                """,
                (
                    usage["tool_calls"],
                    usage["steps"],
                    usage["input_tokens"],
                    usage["output_tokens"],
                    usage["cost"],
                    time.time(),
                    profile_id,
                    workspace_id,
                    key,
                    digest,
                ),
            )
            connection.commit()
            return usage

    def complete_budget(
        self,
        *,
        profile_id: str,
        workspace_id: str,
        key: str,
        digest: str,
    ) -> None:
        """Seal the durable cost record after successful handoff."""

        with sqlite_wal_connection(self.path) as connection:
            self._migrate(connection)
            cursor = connection.execute(
                """
                UPDATE repository_context_budget_usage
                SET status = 'completed', updated_at = ?
                WHERE profile_id = ? AND workspace_id = ?
                  AND invocation_key = ? AND digest = ?
                """,
                (time.time(), profile_id, workspace_id, key, digest),
            )
            if cursor.rowcount != 1:
                raise RepositoryContextLedgerConflict(
                    "repository context budget record changed"
                )

    def abandon_budget(
        self,
        *,
        profile_id: str,
        workspace_id: str,
        key: str,
        digest: str,
    ) -> None:
        """Mark a failed attempt reusable without deleting its audit usage."""

        with sqlite_wal_connection(self.path) as connection:
            self._migrate(connection)
            connection.execute(
                """
                UPDATE repository_context_budget_usage
                SET status = 'failed', updated_at = ?
                WHERE profile_id = ? AND workspace_id = ?
                  AND invocation_key = ? AND digest = ?
                  AND status = 'running'
                """,
                (
                    time.time(),
                    profile_id,
                    workspace_id,
                    key,
                    digest,
                ),
            )

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS repository_context_invocations(
                profile_id TEXT NOT NULL,
                invocation_key TEXT NOT NULL,
                digest TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(profile_id, invocation_key)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS repository_context_budget_usage(
                profile_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                invocation_key TEXT NOT NULL,
                digest TEXT NOT NULL,
                limits_json TEXT NOT NULL,
                tool_calls INTEGER NOT NULL,
                steps INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost REAL NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(profile_id, workspace_id, invocation_key)
            )
            """
        )

    @staticmethod
    def _prune(
        connection: sqlite3.Connection,
        profile_id: str,
        now: float,
    ) -> None:
        connection.execute(
            """
            DELETE FROM repository_context_invocations
            WHERE profile_id = ? AND (
                (status = 'running' AND updated_at < ?)
                OR (status = 'completed' AND updated_at < ?)
            )
            """,
            (
                profile_id,
                now - _RUNNING_TTL_SECONDS,
                now - _COMPLETED_TTL_SECONDS,
            ),
        )
        connection.execute(
            """
            DELETE FROM repository_context_invocations
            WHERE profile_id = ? AND rowid NOT IN (
                SELECT rowid FROM repository_context_invocations
                WHERE profile_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
            )
            """,
            (profile_id, profile_id, _MAX_ROWS_PER_PROFILE),
        )
