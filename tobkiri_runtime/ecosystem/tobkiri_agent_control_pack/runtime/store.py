"""Pack-owned SQLite CAS and replay journal; no other Pack data is opened."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterator, Mapping

from tobkiri_protocol.work_plan_v1 import PLAN_VERSION

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class Conflict(RuntimeError):
    """An expected revision or retained operation identity no longer matches."""


def identifier(value: Any) -> str:
    """Reject empty, oversized and path-like identifiers."""
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("identifier is invalid")
    return value


def integer(value: Any, *, minimum: int = 0) -> int:
    """Require an exact integer instead of accepting booleans or coercions."""
    if type(value) is not int or value < minimum:
        raise ValueError("integer is invalid")
    return value


def digest(value: Any) -> str:
    """Hash canonical JSON for operation identity and immutable references."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PlanStore:
    """Persist profile-scoped plans and exact idempotency results atomically."""

    def __init__(self, root: Path, profile_id: str) -> None:
        self.profile_id = identifier(profile_id)
        self.path = (
            Path(root)
            / "packs"
            / "tobkiri_agent_control_pack"
            / "profiles"
            / self.profile_id
            / "plans.sqlite3"
        )

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            if write:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS plans "
                    "(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, value TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS operations "
                    "(id TEXT PRIMARY KEY, digest TEXT NOT NULL, value TEXT NOT NULL)"
                )
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, plan_id: str) -> dict[str, Any] | None:
        """Read a plan without creating a directory on an absent read."""
        identifier(plan_id)
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM plans WHERE id=?", (plan_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self) -> list[dict[str, Any]]:
        """Return this Profile's plans in deterministic ID order."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return []
        with self._connection() as connection:
            rows = connection.execute("SELECT value FROM plans ORDER BY id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def replay(
        self,
        plan_id: str,
        operation_id: str,
        arguments: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any] | None:
        """Read a retained result before repeating an external computation."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT digest,value FROM operations WHERE id=?",
                (f"{identifier(plan_id)}:{identifier(operation_id)}",),
            ).fetchone()
        if not row:
            return None
        if row[0] != digest({"plan": plan_id, "arguments": arguments, "actor": actor}):
            raise Conflict("operation ID was rebound")
        return json.loads(row[1])

    def mutate(
        self,
        plan_id: str,
        expected_revision: int,
        operation_id: str,
        arguments: Mapping[str, Any],
        actor: str,
        change: Callable[[dict[str, Any]], Mapping[str, Any]],
        *,
        guard: Callable[[], None] = lambda: None,
    ) -> dict[str, Any]:
        """CAS one plan and its replay result, including create at revision zero."""
        identifier(plan_id)
        identifier(operation_id)
        integer(expected_revision)
        fingerprint = digest({"plan": plan_id, "arguments": arguments, "actor": actor})
        key = f"{plan_id}:{operation_id}"
        guard()
        with self._connection(write=True) as connection:
            replay = connection.execute(
                "SELECT digest,value FROM operations WHERE id=?", (key,)
            ).fetchone()
            if replay:
                if replay[0] != fingerprint:
                    raise Conflict("operation ID was rebound")
                return json.loads(replay[1])
            row = connection.execute(
                "SELECT revision,value FROM plans WHERE id=?", (plan_id,)
            ).fetchone()
            if (row[0] if row else 0) != expected_revision:
                raise Conflict("plan revision is stale")
            plan = json.loads(row[1]) if row else new_plan(plan_id, self.profile_id)
            result = dict(change(plan))
            plan["revision"] = expected_revision + 1
            plan["last_actor"] = actor
            if len(json.dumps(plan)) > 8 * 1024 * 1024:
                raise ValueError("plan retention capacity exceeded")
            guard()
            connection.execute(
                "INSERT INTO plans VALUES(?,?,?) ON CONFLICT(id) DO UPDATE "
                "SET revision=excluded.revision,value=excluded.value",
                (plan_id, plan["revision"], json.dumps(plan)),
            )
            value = {**result, "plan": deepcopy(plan)}
            connection.execute(
                "INSERT INTO operations VALUES(?,?,?)",
                (key, fingerprint, json.dumps(value)),
            )
            return value


def new_plan(plan_id: str, profile_id: str) -> dict[str, Any]:
    """Construct an empty plan; reads do not persist or schedule it."""
    return {
        "version": PLAN_VERSION,
        "id": plan_id,
        "profile_id": profile_id,
        "conversation_id": None,
        "revision": 0,
        "generation": 0,
        "status": "active",
        "goal": None,
        "todos": [],
        "instructions": [],
        "inbox": [],
        "history": [],
        "previews": {},
        "effects": {},
        "context": None,
        "review": None,
        "review_occurrences": {},
        "settings": {
            "enabled": True,
            "interval_seconds": 600,
            "executor": None,
            "reviewer": None,
            "model_policy": {"mode": "inherit_conversation"},
            "thinking_policy": {"mode": "inherit_conversation"},
        },
        "schedule": {
            "id": f"work-plan-{digest(plan_id)[:24]}",
            "status": "unconfigured",
            "next_run_at_ms": None,
        },
    }
