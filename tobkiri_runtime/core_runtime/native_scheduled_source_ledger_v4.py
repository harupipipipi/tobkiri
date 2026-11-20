"""Use the Launcher owner for Calendar source and receipt persistence."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from tobkiri_protocol.canonical import canonical_digest, strict_loads

from .native_provider_prepare_ipc import ScheduledSourceLedgerOperation

_SOURCE_LIMIT = 128 * 1024


class NativeScheduledSourceLedger:
    """Expose the existing Calendar adapter's storage methods on the managed port."""

    def __init__(
        self,
        call: Callable[[ScheduledSourceLedgerOperation, Mapping[str, Any]], dict[str, Any]],
    ) -> None:
        self._call = call

    def admit(self, key: str, envelope: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return self._call("admit", {"key": key, "envelope": dict(envelope)})["value"]

    def recover_source(self, key: str, turn_id: str) -> Mapping[str, Any] | None:
        return self._call("recover-source", {"key": key, "turn_id": turn_id})["value"]

    def bind_source(self, key: str, source: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._call("bind-source", {"key": key, "source": dict(source)})["value"]

    def has_source(self, key: str) -> bool:
        return self._call("has-source", {"key": key})["value"]

    def bind_destination(self, key: str, intent: Mapping[str, Any], target: str) -> str:
        return self._call("bind-destination", {
            "key": key, "intent": dict(intent), "target": target,
        })["value"]

    def finish(self, key: str, receipt: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._call("finish", {"key": key, "receipt": dict(receipt)})["value"]


def observe_scheduled_source_digest(
    root: Path, profile_id: str, key: str, turn_id: str,
) -> str | None:
    """Recheck an immutable source during another request's live socket exchange.

    The Launcher serves that exchange inline, so a new socket request cannot run
    until it finishes. This view reads one bounded row and never waits for write
    access, initializes a database, or supplies the source used for dispatch.
    """
    root = root.resolve(strict=True)
    database = root / "packs/rumi_turn_runtime_pack/profiles" / profile_id / "scheduled-chat-jobs.sqlite3"

    def identity() -> tuple[int, int]:
        if not database.is_relative_to(root) or database.resolve(strict=True) != database:
            raise PermissionError("scheduled source ledger path changed")
        metadata = database.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PermissionError("scheduled source ledger identity changed")
        return metadata.st_dev, metadata.st_ino

    try:
        opened = identity()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(database, flags), "rb") as header_file:
            metadata = os.fstat(header_file.fileno())
            header = header_file.read(20)
            if (
                (metadata.st_dev, metadata.st_ino) != opened
                or not header.startswith(b"SQLite format 3\x00")
                or header[18:20] != b"\x01\x01"
            ):
                # The original ledger uses rollback journaling. SQLite may set
                # up a shared-memory sidecar while reading a WAL database.
                raise PermissionError("scheduled source ledger is outside the read-only format")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = database.with_name(database.name + suffix)
            try:
                metadata = sidecar.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PermissionError("scheduled source ledger sidecar changed")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _SOURCE_LIMIT + 4096)
            db.execute("PRAGMA trusted_schema=OFF")
            rows = db.execute(
                "SELECT source FROM main.sources WHERE job_key=? AND turn_id=? LIMIT 2",
                (key, turn_id),
            ).fetchall()
        if identity() != opened:
            raise PermissionError("scheduled source ledger was replaced")
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0][0], str):
            raise ValueError("scheduled source record is invalid")
        source = strict_loads(rows[0][0], max_bytes=_SOURCE_LIMIT)
        if not isinstance(source, dict):
            raise ValueError("scheduled source record is invalid")
        return canonical_digest(source)
    except (OSError, sqlite3.Error, ValueError) as error:
        raise PermissionError("scheduled source ledger could not be checked") from error
