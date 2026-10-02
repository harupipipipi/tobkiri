"""Profile-owned, bounded task plans and one-shot execution journal."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping

from tobkiri_protocol.workspace_capsule_v1 import canonical, identifier, parse_json

MAX_TASKS = 16
MAX_RETAINED_BYTES = 32 * 1024 * 1024


class TaskState:
    """Persist only this provider's prepared inputs, receipts and output capsules."""

    def __init__(self, root: Path, profile_id: str) -> None:
        """Bind a private state directory to the captured immutable Profile."""
        if not root.is_absolute():
            raise PermissionError("task state root is not Host-owned")
        self.root = (
            root
            / "packs/rumi_coding_sandbox_service_pack/tasks"
            / identifier(profile_id)
        )
        self.path = self.root / "tasks.sqlite3"

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Serialize plan claims and enforce a finite retained byte budget."""
        self.ensure_root()
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        self.path.chmod(0o600)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, owner TEXT, "
                "plan BLOB, archive BLOB, executable BLOB, status TEXT, receipt BLOB, output BLOB)"
            )
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def ensure_root(self) -> None:
        """Reject symlink state ancestors before any staging or process call."""
        for part in (self.path, *self.path.parents):
            if part.is_symlink():
                raise PermissionError("task state contains a symbolic link")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def prepare(
        self,
        plan: Mapping[str, Any],
        archive: bytes,
        executable: Mapping[str, Any],
        owner: str,
    ) -> dict[str, Any]:
        """Seal one request identity without replacing an earlier plan."""
        with self.connection() as db:
            prior = db.execute(
                "SELECT * FROM tasks WHERE id=?", (plan["task_id"],)
            ).fetchone()
            if prior:
                sealed = parse_json(prior["plan"])
                comparable = {
                    key: value
                    for key, value in sealed.items()
                    if key != "expires_at_ms"
                }
                proposed = {
                    key: value for key, value in plan.items() if key != "expires_at_ms"
                }
                if (
                    prior["owner"] != owner
                    or comparable != proposed
                    or bytes(prior["archive"]) != archive
                    or bytes(prior["executable"]) != canonical(executable)
                ):
                    raise PermissionError("task request identity was rebound")
                return sealed
            count, retained = db.execute(
                "SELECT count(*),coalesce(sum(length(archive)+coalesce(length(output),0)"
                "+ CASE WHEN status IN ('prepared','running') THEN 5242880 ELSE 0 END),0) FROM tasks"
            ).fetchone()
            if (
                count >= MAX_TASKS
                or retained + len(archive) + 5 * 1024 * 1024 > MAX_RETAINED_BYTES
            ):
                raise ValueError("task retention capacity exceeded")
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,'prepared',NULL,NULL)",
                (
                    plan["task_id"],
                    owner,
                    canonical(plan),
                    archive,
                    canonical(executable),
                ),
            )
        return dict(plan)

    def read(self, task_id: str, owner: str) -> dict[str, Any]:
        """Read only one owner's task without disclosing private identities."""
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE id=?", (identifier(task_id),)
            ).fetchone()
        if row is None or row["owner"] != owner:
            raise LookupError("workspace task is unavailable")
        result = dict(row)
        for key in ("plan", "executable", "receipt"):
            if result[key] is not None:
                result[key] = parse_json(result[key])
        return result

    def claim(self, task_id: str, owner: str, plan: Mapping[str, Any]) -> None:
        """Consume a prepared task once; an interrupted run is never rerun."""
        with self.connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if (
                row is None
                or row["owner"] != owner
                or bytes(row["plan"]) != canonical(plan)
                or row["status"] != "prepared"
            ):
                raise PermissionError("workspace task was already claimed or differs")
            db.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))

    def finish(
        self,
        task_id: str,
        receipt: Mapping[str, Any],
        output: bytes | None,
    ) -> None:
        """Retain bounded verified output, or preserve an ambiguous failure."""
        with self.connection() as db:
            retained = db.execute(
                "SELECT coalesce(sum(length(archive)+coalesce(length(output),0)),0) FROM tasks"
            ).fetchone()[0]
            if output is not None and retained + len(output) > MAX_RETAINED_BYTES:
                raise ValueError("task output retention capacity exceeded")
            db.execute(
                "UPDATE tasks SET status=?,receipt=?,output=? WHERE id=? AND status='running'",
                (receipt["status"], canonical(receipt), output, task_id),
            )
