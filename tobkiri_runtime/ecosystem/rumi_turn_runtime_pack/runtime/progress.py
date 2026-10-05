"""Bounded owner journal for live provisional chunks, never an execution queue."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterator, Mapping

from tobkiri_protocol.canonical import canonical_digest, canonical_json
from tobkiri_protocol.turn_progress_v1 import (
    MAX_BYTES,
    MAX_EVENTS,
    TTL_SECONDS,
    VERSION,
    validate_begin,
    validate_event,
)


class TurnProgressJournal:
    """Persist only bounded nonsecret display data in the Turn owner's database."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = path, clock

    def begin(self, binding: Mapping[str, Any], *, owner: str, capture: str) -> str:
        """Reserve one saved AI stage once, with its authenticated owner/capture."""
        binding = validate_begin(binding)
        identity = canonical_digest(dict(binding)).removeprefix("sha256:")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._purge(connection)
            if connection.execute("SELECT count(*) FROM progress").fetchone()[0] >= 64:
                raise ValueError("progress capacity is exhausted")
            try:
                connection.execute(
                    "INSERT INTO progress VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        owner,
                        capture,
                        json.dumps(binding),
                        "",
                        "",
                        0,
                        self.clock() + TTL_SECONDS,
                        0,
                        "",
                    ),
                )
            except sqlite3.IntegrityError:
                raise PermissionError("saved AI stage already reserved") from None
        return identity

    def bind(
        self,
        identity: str,
        *,
        owner: str,
        capture: str,
        ai_input_digest: str,
        producer: str,
        producer_input_digest: str,
    ) -> None:
        """Seal one exact producer before its transport begins; no retry binding."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._record(connection, identity, owner, capture)
            if row[4] or json.loads(row[3])["ai_input_digest"] != ai_input_digest:
                raise PermissionError("progress producer binding changed")
            connection.execute(
                "UPDATE progress SET producer=?, producer_input=? WHERE id=?",
                (producer, producer_input_digest, identity),
            )

    def publish(
        self,
        identity: str,
        *,
        owner: str,
        capture: str,
        producer: str,
        producer_input_digest: str,
        cursor: int,
        event: Mapping[str, Any],
        execution: str,
    ) -> None:
        """Append one received event in strict order under the sealed producer."""
        event = validate_event(event)
        encoded = canonical_json(event)
        if type(cursor) is not int or not 1 <= cursor <= MAX_EVENTS:
            raise ValueError("progress cursor is invalid")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._record(connection, identity, owner, capture)
            if row[4:6] != (producer, producer_input_digest) or row[8] or row[9] != execution:
                raise PermissionError("progress producer is unavailable")
            count, size = connection.execute(
                "SELECT count(*), coalesce(sum(size), 0) FROM chunks WHERE id=?",
                (identity,),
            ).fetchone()
            if cursor != count + 1 or size + len(encoded) > MAX_BYTES:
                raise PermissionError("progress cursor or byte limit is invalid")
            connection.execute(
                "INSERT INTO chunks VALUES(?,?,?,?)",
                (identity, cursor, encoded.decode(), len(encoded)),
            )
            connection.execute(
                "UPDATE progress SET cursor=?, terminal=? WHERE id=?",
                (cursor, int(event["type"] == "finish"), identity),
            )

    def claim(
        self,
        identity: str,
        *,
        owner: str,
        capture: str,
        producer: str,
        producer_input_digest: str,
        execution: str,
    ) -> None:
        """Consume the one producer reservation before it opens any transport."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._record(connection, identity, owner, capture)
            if row[4:6] != (producer, producer_input_digest) or row[9] or row[8]:
                raise PermissionError("progress producer execution already claimed")
            connection.execute("UPDATE progress SET execution=? WHERE id=?", (execution, identity))

    def read(
        self,
        identity: str,
        *,
        owner: str,
        capture: str,
        cursor: int,
    ) -> dict[str, Any]:
        """Return a bounded provisional page without publishing or starting AI."""
        if type(cursor) is not int or not 0 <= cursor <= MAX_EVENTS:
            raise ValueError("progress cursor is invalid")
        with self._connection() as connection:
            row = self._record(connection, identity, owner, capture)
            if cursor > row[6]:
                raise ValueError("progress cursor is ahead of producer")
            events, size = [], 0
            for sequence, body in connection.execute(
                "SELECT cursor, body FROM chunks WHERE id=? AND cursor>? ORDER BY cursor LIMIT 128",
                (identity, cursor),
            ):
                size += len(body.encode())
                if size > 64 * 1024:
                    break
                events.append({"cursor": sequence, "event": json.loads(body)})
            return {
                "version": VERSION,
                "provisional": True,
                "binding": json.loads(row[3]),
                "events": events,
                "cursor": events[-1]["cursor"] if events else cursor,
                "provider_complete": bool(row[8]),
                "expires_at_ms": int(row[7] * 1000),
            }

    def find(self, turn_id: str, *, owner: str, capture: str) -> str:
        """Find only this authenticated owner's latest unexpired saved AI stage."""
        with self._connection() as connection:
            self._purge(connection)
            rows = connection.execute(
                "SELECT id, binding FROM progress WHERE owner=? AND capture=? "
                "ORDER BY expires DESC",
                (owner, capture),
            )
            for identity, binding in rows:
                if json.loads(binding)["turn_id"] == turn_id:
                    return str(identity)
        raise KeyError("live progress is unavailable")

    def _record(
        self, connection: sqlite3.Connection, identity: str, owner: str, capture: str
    ) -> tuple[Any, ...]:
        self._purge(connection)
        row = connection.execute("SELECT * FROM progress WHERE id=?", (identity,)).fetchone()
        if row is None or row[1:3] != (owner, capture):
            raise PermissionError("progress owner or capture does not match")
        return tuple(row)

    def _purge(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "DELETE FROM chunks WHERE id IN (SELECT id FROM progress WHERE expires<=?)",
            (self.clock(),),
        )
        connection.execute("DELETE FROM progress WHERE expires<=?", (self.clock(),))

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        # The Turn owner has already validated/created its durable root.
        if self.path.is_symlink() or not self.path.parent.is_dir():
            raise PermissionError("progress owner path is unavailable")
        connection = sqlite3.connect(self.path, timeout=1)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS progress (id TEXT PRIMARY KEY, "
            "owner TEXT, capture TEXT, binding TEXT, producer TEXT, "
            "producer_input TEXT, cursor INTEGER, expires REAL, terminal INTEGER, execution TEXT)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS chunks (id TEXT, cursor INTEGER, "
            "body TEXT, size INTEGER, PRIMARY KEY(id,cursor))"
        )
        connection.commit()
        try:
            with connection:
                yield connection
        finally:
            connection.close()
