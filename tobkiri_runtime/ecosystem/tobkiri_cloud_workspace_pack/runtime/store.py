"""Pack-owned SQLite checkpoints with CAS, replay and local fenced writers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping

from ecosystem.tobkiri_cloud_workspace_pack.runtime.capsule import (
    canonical,
    identifier,
    integer,
    parse_json,
    validate_capsule,
)

MAX_RETAINED_BYTES = 32 * 1024 * 1024
MAX_WORKSPACES = 64
WRITER_TTL_MS = 120_000


class Conflict(RuntimeError):
    """Reject stale revisions, replay rebinding and a second local writer."""


class WorkspaceStore:
    """Own only this Pack's Profile directory; never open another Pack's state."""

    def __init__(self, root: Path, profile_id: str) -> None:
        self.profile_id = identifier(profile_id)
        if not root.is_absolute():
            raise PermissionError("workspace state root must be Host-owned")
        self.root = root / "packs" / "tobkiri_cloud_workspace_pack" / "profiles" / self.profile_id
        self.path = self.root / "workspace.sqlite3"

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Serialize mutations and publish their replay result in one transaction."""
        for part in [self.path, *self.path.parents]:
            if part.is_symlink():
                raise PermissionError("workspace state contains a symbolic link")
        if write:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
        connection = sqlite3.connect(
            self.path if write else f"file:{self.path}?mode=ro",
            uri=not write,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        try:
            if write:
                os.chmod(self.path, 0o600)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS heads (id TEXT PRIMARY KEY, "
                    "revision INTEGER NOT NULL, head TEXT NOT NULL, owner TEXT NOT NULL, "
                    "fence INTEGER NOT NULL, expiry INTEGER NOT NULL, value BLOB NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS snapshots "
                    "(digest TEXT PRIMARY KEY, manifest BLOB NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS blobs (digest TEXT PRIMARY KEY, data BLOB NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS replay "
                    "(id TEXT PRIMARY KEY, digest TEXT NOT NULL, result BLOB NOT NULL)"
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

    def get(self, workspace_id: str) -> dict[str, Any] | None:
        """Read one public checkpoint without returning writer identity or lease."""
        identifier(workspace_id)
        if not self.path.exists():
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value FROM heads WHERE id=?", (workspace_id,)
            ).fetchone()
        return parse_json(row[0]) if row else None

    def list(self) -> list[dict[str, Any]]:
        """Return bounded public checkpoint summaries without creating state."""
        if not self.path.exists():
            return []
        with self.connection() as connection:
            rows = connection.execute("SELECT value FROM heads ORDER BY id").fetchall()
        return [parse_json(row[0]) for row in rows]

    def read_capsule(self, manifest_digest: str) -> tuple[dict[str, Any], dict[str, bytes]]:
        """Load and reverify immutable bytes, detecting corrupt retained content."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT manifest FROM snapshots WHERE digest=?",
                (manifest_digest,),
            ).fetchone()
            if row is None:
                raise LookupError("workspace checkpoint is unknown")
            manifest = parse_json(row[0])
            blobs: dict[str, bytes] = {}
            for entry in manifest["files"]:
                value = connection.execute(
                    "SELECT data FROM blobs WHERE digest=?",
                    (entry["digest"],),
                ).fetchone()
                if value is None:
                    raise ValueError("workspace checkpoint content is missing")
                blobs[entry["digest"]] = bytes(value[0])
        validate_capsule(manifest, blobs)
        return manifest, blobs

    def replay(self, request_id: str, fingerprint: str) -> dict[str, Any] | None:
        """Return the exact prior result before repeating an expensive read."""
        if not self.path.exists():
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT digest,result FROM replay WHERE id=?", (request_id,)
            ).fetchone()
        return _replay(row, fingerprint)

    def publish(
        self,
        manifest: Mapping[str, Any],
        blobs: Mapping[str, bytes],
        *,
        expected_revision: int,
        actor: str,
        request_id: str,
        fingerprint: str,
        guard: Callable[[], None],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """CAS a checkpoint with a locally serialized, owner-fenced writer."""
        validate_capsule(manifest, blobs)
        expected = integer(expected_revision)
        now = int(time.time() * 1000) if now_ms is None else integer(now_ms)
        workspace_id = manifest["workspace_id"]
        guard()
        with self.connection(write=True) as connection:
            prior = connection.execute(
                "SELECT digest,result FROM replay WHERE id=?", (request_id,)
            ).fetchone()
            replayed = _replay(prior, fingerprint)
            if replayed is not None:
                return replayed
            row = connection.execute("SELECT * FROM heads WHERE id=?", (workspace_id,)).fetchone()
            if (row["revision"] if row else 0) != expected:
                raise Conflict("workspace checkpoint revision is stale")
            if row and row["owner"] != actor and row["expiry"] > now:
                raise Conflict("workspace has another active local writer")
            if manifest["revision"] != expected + 1:
                raise Conflict("workspace capsule revision is stale")
            if manifest["parent_digest"] != (row["head"] if row else ""):
                raise Conflict("workspace capsule parent differs")
            fence = (row["fence"] if row else 0) + (
                1 if not row or row["owner"] != actor or row["expiry"] <= now else 0
            )
            if (
                row is None
                and connection.execute("SELECT count(*) FROM heads").fetchone()[0] >= MAX_WORKSPACES
            ):
                raise ValueError("workspace retention capacity exceeded")
            retained = connection.execute(
                "SELECT coalesce(sum(length(data)),0) FROM blobs"
            ).fetchone()[0]
            for key, data in blobs.items():
                existing = connection.execute(
                    "SELECT data FROM blobs WHERE digest=?", (key,)
                ).fetchone()
                if existing and bytes(existing[0]) != data:
                    raise ValueError("retained workspace content is corrupt")
                if existing is None:
                    retained += len(data)
                    if retained > MAX_RETAINED_BYTES:
                        raise ValueError("workspace retention capacity exceeded")
                    connection.execute("INSERT INTO blobs VALUES (?,?)", (key, data))
            connection.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?,?)",
                (manifest["manifest_digest"], canonical(manifest)),
            )
            result = {
                "id": workspace_id,
                "revision": expected + 1,
                "checkpoint_digest": manifest["manifest_digest"],
                "source_profile_id": manifest["source"]["profile_id"],
                "source_plan_digest": manifest["source"]["plan_digest"],
                "file_count": len(manifest["files"]),
                "total_bytes": manifest["total_bytes"],
                "status": "checkpointed_locally",
                "container_status": "not_started",
            }
            guard()
            connection.execute(
                "INSERT OR REPLACE INTO heads VALUES (?,?,?,?,?,?,?)",
                (
                    workspace_id,
                    expected + 1,
                    manifest["manifest_digest"],
                    actor,
                    fence,
                    now + WRITER_TTL_MS,
                    canonical(result),
                ),
            )
            connection.execute(
                "INSERT INTO replay VALUES (?,?,?)", (request_id, fingerprint, canonical(result))
            )
        return result

    def release_writer(
        self,
        workspace_id: str,
        *,
        expected_revision: int,
        actor: str,
        guard: Callable[[], None],
    ) -> None:
        """Fence the current local writer before preparing a future handoff."""
        with self.connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM heads WHERE id=?", (identifier(workspace_id),)
            ).fetchone()
            if row is None or row["revision"] != integer(expected_revision):
                raise Conflict("workspace checkpoint revision is stale")
            if row["owner"] != actor:
                raise Conflict("only the current local writer can release")
            guard()
            connection.execute(
                "UPDATE heads SET expiry=0,fence=fence+1 WHERE id=?",
                (workspace_id,),
            )

    def restore_local(self, checkpoint: str, *, guard: Callable[[], None]) -> dict[str, Any]:
        """Restore verified files into a new Pack-owned directory, never a Host path."""
        manifest, blobs = self.read_capsule(checkpoint)
        guard()
        destination = self.root / "restored" / checkpoint[7:]
        for part in [destination, *destination.parents]:
            if part.is_symlink():
                raise PermissionError("workspace restore contains a symbolic link")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
        try:
            for entry in manifest["files"]:
                path = temporary / entry["path"]
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with path.open("xb") as stream:
                    stream.write(blobs[entry["digest"]])
                path.chmod(0o600)
            (temporary / "capsule-manifest.json").write_bytes(canonical(manifest))
            guard()
            # Retained restores are immutable. Repeating restoration creates
            # no write into an existing user/Host workspace.
            if destination.exists():
                raise Conflict("workspace checkpoint was already restored")
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return {
            "status": "restored_locally",
            "checkpoint_digest": checkpoint,
            "file_count": len(manifest["files"]),
            "total_bytes": manifest["total_bytes"],
            "container_status": "not_started",
        }


def _replay(row: sqlite3.Row | None, fingerprint: str) -> dict[str, Any] | None:
    if row is None:
        return None
    if row["digest"] != fingerprint:
        raise Conflict("workspace request identity was rebound")
    return parse_json(row["result"])
