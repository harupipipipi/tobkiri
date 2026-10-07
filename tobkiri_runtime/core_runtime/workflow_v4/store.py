"""Durable, local-first Workflow v4 state store."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.platform_paths import canonical_platform_path

from .models import (
    ATTEMPT_TRANSITIONS,
    RUN_TRANSITIONS,
    DefinitionState,
    RunState,
    StepAttemptState,
    WorkflowConflict,
    WorkflowDenied,
    WorkflowNotFound,
    digest,
    etag,
)

_SCHEMA_VERSION = 1


class WorkflowStoreV4:
    """SQLite store with authenticated records and optimistic transitions."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        seal_key: bytes | None = None,
    ) -> None:
        self.path = canonical_platform_path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or self.path.parent.is_symlink():
            raise WorkflowDenied("workflow state paths cannot be symbolic links")
        self._seal_key = seal_key or self._load_or_create_key(
            self.path.with_suffix(self.path.suffix + ".key")
        )
        self._connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def _load_or_create_key(self, path: Path) -> bytes:
        from .seal_key import load_or_create_workflow_key

        return load_or_create_workflow_key(path, database_path=self.path)

    def _initialize(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_definitions (
                    definition_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    seal TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_revisions (
                    definition_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    revision_digest TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    seal TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (definition_id, revision)
                );
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    definition_id TEXT NOT NULL,
                    revision_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    seal TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    seal TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (run_id, step_id, attempt_number)
                );
                CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    seal TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE (attempt_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS workflow_occurrences (
                    claim_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            row = self._connection.execute(
                "SELECT value FROM workflow_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO workflow_meta(key, value) VALUES('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            elif int(row["value"]) != _SCHEMA_VERSION:
                raise WorkflowDenied("workflow store schema version is unsupported")

    def close(self) -> None:
        """Close the local database."""

        self._connection.close()

    def _encode(self, value: Mapping[str, Any]) -> tuple[str, str]:
        raw = canonical_json(value)
        return raw.decode("utf-8"), hmac.new(self._seal_key, raw, hashlib.sha256).hexdigest()

    def _now_ms(self) -> int:
        """Return an exact integer timestamp suitable for canonical JSON."""

        return int(self._clock() * 1000)

    def _decode(self, payload: str, seal: str) -> dict[str, Any]:
        raw = payload.encode("utf-8")
        expected = hmac.new(self._seal_key, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, seal):
            raise WorkflowDenied("workflow state authentication failed")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise WorkflowDenied("workflow state is not an object")
        return value

    def create_definition(self, definition_id: str, document: Mapping[str, Any]) -> dict[str, Any]:
        """Create revision 1 of a draft Definition."""

        now = self._now_ms()
        revision_digest = digest(
            {"definition_id": definition_id, "revision": 1, "document": document}
        )
        record = {
            "definition_id": definition_id,
            "revision": 1,
            "revision_digest": revision_digest,
            "etag": etag(definition_id, 1, revision_digest),
            "state": DefinitionState.DRAFT.value,
            "document": dict(document),
            "created_at": now,
            "updated_at": now,
        }
        payload, seal = self._encode(record)
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    "INSERT INTO workflow_definitions VALUES(?,?,?,?,?,?)",
                    (definition_id, 1, record["state"], payload, seal, now),
                )
                self._connection.execute(
                    "INSERT INTO workflow_revisions VALUES(?,?,?,?,?,?,?)",
                    (
                        definition_id,
                        1,
                        revision_digest,
                        record["state"],
                        payload,
                        seal,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflict("workflow definition already exists") from exc
        return record

    def get_definition(self, definition_id: str) -> dict[str, Any]:
        """Load and authenticate the current Definition revision."""

        with self._lock:
            row = self._connection.execute(
                "SELECT payload, seal FROM workflow_definitions WHERE definition_id=?",
                (definition_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound("workflow definition is unavailable")
        return self._decode(row["payload"], row["seal"])

    def get_revision(self, revision_digest: str) -> dict[str, Any]:
        """Load one immutable Definition revision by digest."""

        with self._lock:
            row = self._connection.execute(
                "SELECT payload, seal FROM workflow_revisions WHERE revision_digest=?",
                (revision_digest,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound("workflow revision is unavailable")
        return self._decode(row["payload"], row["seal"])

    def list_definitions(self) -> list[dict[str, Any]]:
        """List authenticated current Definition revisions."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT payload, seal FROM workflow_definitions ORDER BY definition_id"
            ).fetchall()
        return [self._decode(row["payload"], row["seal"]) for row in rows]

    def update_definition(
        self,
        definition_id: str,
        document: Mapping[str, Any],
        *,
        if_match: str,
    ) -> dict[str, Any]:
        """Append a draft revision under strong ETag locking."""

        with self._lock, self._connection:
            current = self.get_definition(definition_id)
            if current["etag"] != if_match:
                raise WorkflowConflict("workflow definition ETag is stale")
            if current["state"] != DefinitionState.DRAFT.value:
                raise WorkflowConflict("only a draft definition can be updated")
            revision = int(current["revision"]) + 1
            now = self._now_ms()
            revision_digest = digest(
                {
                    "definition_id": definition_id,
                    "revision": revision,
                    "document": document,
                }
            )
            record = {
                **current,
                "revision": revision,
                "revision_digest": revision_digest,
                "etag": etag(definition_id, revision, revision_digest),
                "document": dict(document),
                "updated_at": now,
            }
            payload, seal = self._encode(record)
            changed = self._connection.execute(
                "UPDATE workflow_definitions SET revision=?, payload=?, seal=?, updated_at=?"
                " WHERE definition_id=? AND revision=? AND state='draft'",
                (revision, payload, seal, now, definition_id, current["revision"]),
            ).rowcount
            if changed != 1:
                raise WorkflowConflict("workflow definition update lost a race")
            self._connection.execute(
                "INSERT INTO workflow_revisions VALUES(?,?,?,?,?,?,?)",
                (
                    definition_id,
                    revision,
                    revision_digest,
                    DefinitionState.DRAFT.value,
                    payload,
                    seal,
                    now,
                ),
            )
        return record

    def transition_definition(
        self,
        definition_id: str,
        *,
        if_match: str,
        expected: DefinitionState,
        target: DefinitionState,
        compiled: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish or archive a Definition under a state fence."""

        with self._lock, self._connection:
            current = self.get_definition(definition_id)
            if current["etag"] != if_match or current["state"] != expected.value:
                raise WorkflowConflict("workflow definition transition is stale")
            now = self._now_ms()
            record = {**current, "state": target.value, "updated_at": now}
            if compiled is not None:
                record["compiled"] = dict(compiled)
            record["etag"] = etag(
                definition_id, int(record["revision"]), record["revision_digest"] + target.value
            )
            payload, seal = self._encode(record)
            changed = self._connection.execute(
                "UPDATE workflow_definitions SET state=?, payload=?, seal=?, updated_at=?"
                " WHERE definition_id=? AND revision=? AND state=?",
                (
                    target.value,
                    payload,
                    seal,
                    now,
                    definition_id,
                    current["revision"],
                    expected.value,
                ),
            ).rowcount
            if changed != 1:
                raise WorkflowConflict("workflow definition transition lost a race")
            self._connection.execute(
                "UPDATE workflow_revisions SET state=?, payload=?, seal=?"
                " WHERE definition_id=? AND revision=?",
                (target.value, payload, seal, definition_id, current["revision"]),
            )
        return record

    def delete_draft(self, definition_id: str, *, if_match: str) -> None:
        """Delete only an unpublished draft under an ETag fence."""

        with self._lock, self._connection:
            current = self.get_definition(definition_id)
            if current["etag"] != if_match or current["state"] != "draft":
                raise WorkflowConflict("only the matching draft can be deleted")
            self._connection.execute(
                "DELETE FROM workflow_definitions WHERE definition_id=?",
                (definition_id,),
            )
            self._connection.execute(
                "DELETE FROM workflow_revisions WHERE definition_id=?",
                (definition_id,),
            )

    def create_run(
        self,
        *,
        run_id: str,
        definition: Mapping[str, Any],
        activation: Mapping[str, Any],
        inputs: Mapping[str, Any],
        occurrence_id: str | None,
    ) -> dict[str, Any]:
        """Create a Run pinned to published revision and ActivationRecord."""

        if definition.get("state") != DefinitionState.PUBLISHED.value:
            raise WorkflowConflict("only a published definition can start")
        claim_key = None
        if occurrence_id:
            claim_key = digest(
                {
                    "occurrence_id": occurrence_id,
                    "revision_digest": definition["revision_digest"],
                }
            )
        now = self._now_ms()
        record = {
            "run_id": run_id,
            "definition_id": definition["definition_id"],
            "revision_digest": definition["revision_digest"],
            "activation_id": str(activation["activation_id"]),
            "activation_digest": str(activation["activation_digest"]),
            "catalog_digest": str(activation["catalog_digest"]),
            "security_epoch": int(activation["security_epoch"]),
            "state": RunState.QUEUED.value,
            "version": 1,
            "inputs": dict(inputs),
            "occurrence_id": occurrence_id,
            "created_at": now,
            "updated_at": now,
        }
        payload, seal = self._encode(record)
        try:
            with self._lock:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    # Recheck the published owner record under the same write
                    # transaction as the run/occurrence claim. An archived or
                    # replaced snapshot cannot start after a stale preflight.
                    current = self.get_definition(str(definition["definition_id"]))
                    if (current.get("state") != DefinitionState.PUBLISHED.value
                            or digest(current) != digest(definition)):
                        raise WorkflowConflict("published Workflow changed before run creation")
                    if claim_key:
                        self._connection.execute(
                            "INSERT INTO workflow_occurrences VALUES(?,?,?)",
                            (claim_key, run_id, now),
                        )
                    self._connection.execute(
                        "INSERT INTO workflow_runs VALUES(?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            record["definition_id"],
                            record["revision_digest"],
                            record["state"],
                            1,
                            payload,
                            seal,
                            now,
                        ),
                    )
                    self._connection.execute("COMMIT")
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflict("workflow run or occurrence was already claimed") from exc
        return record

    def get_run(self, run_id: str) -> dict[str, Any]:
        """Load and authenticate one Run."""

        with self._lock:
            row = self._connection.execute(
                "SELECT payload, seal FROM workflow_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise WorkflowNotFound("workflow run is unavailable")
        return self._decode(row["payload"], row["seal"])

    def request_cancel(self, run_id: str) -> dict[str, Any]:
        """Record the durable stop intent on the Run, before any drain.

        ``cancel_requested`` is a sealed payload flag committed by a
        compare-and-swap update inside ``BEGIN IMMEDIATE``, so it is atomic
        against other connections: any attempt committed before it lands is
        guaranteed to appear in the cancel path's drain list, and every
        later ``create_attempt`` is refused by the admission predicate.
        Terminal or already-requested runs return their record unchanged.
        """

        terminal = {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.TIMED_OUT,
            RunState.NEEDS_RECONCILIATION,
        }
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self.get_run(run_id)
                if (
                    RunState(current["state"]) in terminal
                    or current.get("cancel_requested")
                ):
                    self._connection.execute("COMMIT")
                    return current
                record = {
                    **current,
                    "cancel_requested": True,
                    "version": int(current["version"]) + 1,
                    "updated_at": self._now_ms(),
                }
                payload, seal = self._encode(record)
                changed = self._connection.execute(
                    "UPDATE workflow_runs SET version=?, payload=?, seal=?, updated_at=?"
                    " WHERE run_id=? AND state=? AND version=?",
                    (
                        record["version"],
                        payload,
                        seal,
                        record["updated_at"],
                        run_id,
                        current["state"],
                        current["version"],
                    ),
                ).rowcount
                if changed != 1:
                    raise WorkflowConflict(
                        "workflow run cancel intent lost a race"
                    )
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        return record

    def transition_run(
        self,
        run_id: str,
        *,
        expected: set[RunState],
        target: RunState,
        updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Transition a Run with state and version compare-and-swap."""

        with self._lock, self._connection:
            current = self.get_run(run_id)
            current_state = RunState(current["state"])
            if current_state not in expected:
                raise WorkflowConflict("workflow run transition is invalid or stale")
            if target not in RUN_TRANSITIONS.get(current_state, frozenset()):
                raise WorkflowConflict("workflow run state transition is not allowed")
            record = {
                **current,
                **dict(updates or {}),
                "state": target.value,
                "version": int(current["version"]) + 1,
                "updated_at": self._now_ms(),
            }
            payload, seal = self._encode(record)
            changed = self._connection.execute(
                "UPDATE workflow_runs SET state=?, version=?, payload=?, seal=?, updated_at=?"
                " WHERE run_id=? AND state=? AND version=?",
                (
                    target.value,
                    record["version"],
                    payload,
                    seal,
                    record["updated_at"],
                    run_id,
                    current["state"],
                    current["version"],
                ),
            ).rowcount
            if changed != 1:
                raise WorkflowConflict("workflow run transition lost a race")
        return record

    def create_attempt(
        self,
        *,
        run_id: str,
        step_id: str,
        attempt_number: int,
        request: Mapping[str, Any],
        output_bindings: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create an attempt with its exact Contract Request digest.

        ``output_bindings`` records which same-run committed step outputs the
        resolved request input bound to; it is provenance only and never joins
        the request digest.
        """

        attempt_id = f"{run_id}:{step_id}:{attempt_number}"
        now = self._now_ms()
        record = {
            "attempt_id": attempt_id,
            "run_id": run_id,
            "step_id": step_id,
            "attempt_number": attempt_number,
            "dispatch_admission": "not_admitted",
            "state": StepAttemptState.PENDING.value,
            "version": 1,
            "request": dict(request),
            "request_digest": digest(request),
            "created_at": now,
            "updated_at": now,
        }
        if output_bindings:
            record["output_bindings"] = [dict(item) for item in output_bindings]
        payload, seal = self._encode(record)
        try:
            with self._lock:
                # BEGIN IMMEDIATE so the run-state predicate and the insert
                # are atomic against other connections: under WAL a deferred
                # reader would not stop a concurrent cancel committing the
                # stop intent between the check and the write.
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    run = self.get_run(run_id)
                    if run.get("cancel_requested") or RunState(
                        run["state"]
                    ) is RunState.CANCELLED:
                        raise WorkflowDenied(
                            "workflow run is cancelling; new attempts are fenced"
                        )
                    previous = [
                        item for item in self.list_attempts(run_id)
                        if item["step_id"] == step_id
                    ]
                    if attempt_number != len(previous) + 1 or (
                        previous
                        and previous[-1]["state"] != StepAttemptState.FAILED.value
                    ):
                        raise WorkflowConflict(
                            "workflow attempt is not the next failed-step retry"
                        )
                    self._connection.execute(
                        "INSERT INTO workflow_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            attempt_id,
                            run_id,
                            step_id,
                            attempt_number,
                            record["state"],
                            1,
                            payload,
                            seal,
                            now,
                        ),
                    )
                    self._connection.execute("COMMIT")
                except BaseException:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    raise
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflict("workflow attempt is a replay") from exc
        return record

    def admit_dispatch(self, attempt_id: str, request_id: str) -> None:
        """Atomically fence Stop and record admission before the Broker call.

        Only new attempts carry not_admitted proof. Legacy absence and a
        repeated admission are uncertain, never interpreted as undispatched.
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self.get_attempt(attempt_id)
                run = self.get_run(str(current["run_id"]))
                if (
                    current["request"].get("request_id") != request_id
                    or current["state"] != StepAttemptState.RUNNING.value
                    or current.get("dispatch_admission") != "not_admitted"
                    or run.get("cancel_requested")
                    or RunState(run["state"]) not in {
                        RunState.RUNNING, RunState.WAITING_APPROVAL,
                    }
                ):
                    raise WorkflowDenied("durable dispatch admission is fenced")
                record = {
                    **current, "dispatch_admission": "admitted",
                    "version": int(current["version"]) + 1,
                    "updated_at": self._now_ms(),
                }
                payload, seal = self._encode(record)
                changed = self._connection.execute(
                    "UPDATE workflow_attempts SET version=?, payload=?, seal=?, updated_at=?"
                    " WHERE attempt_id=? AND state=? AND version=?",
                    (record["version"], payload, seal, record["updated_at"],
                     attempt_id, current["state"], current["version"]),
                ).rowcount
                if changed != 1:
                    raise WorkflowConflict("dispatch admission lost a race")
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        """Load and authenticate one StepAttempt."""

        with self._lock:
            row = self._connection.execute(
                "SELECT payload, seal FROM workflow_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound("workflow attempt is unavailable")
        return self._decode(row["payload"], row["seal"])

    def get_attempt_for_request(self, request_id: str) -> dict[str, Any]:
        """Resolve one exact durable request, then authenticate its full record."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload, seal FROM workflow_attempts "
                "WHERE json_extract(payload, '$.request.request_id')=? LIMIT 2",
                (request_id,),
            ).fetchall()
        if len(rows) != 1:
            raise WorkflowNotFound("workflow request has no unique attempt")
        record = self._decode(rows[0]["payload"], rows[0]["seal"])
        if record.get("request", {}).get("request_id") != request_id:
            raise WorkflowConflict("workflow request identity differs")
        return record

    def list_attempts(self, run_id: str) -> list[dict[str, Any]]:
        """List a Run's authenticated attempt history."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT payload, seal FROM workflow_attempts WHERE run_id=?"
                " ORDER BY step_id, attempt_number",
                (run_id,),
            ).fetchall()
        return [self._decode(row["payload"], row["seal"]) for row in rows]

    def transition_attempt(
        self,
        attempt_id: str,
        *,
        expected: set[StepAttemptState],
        target: StepAttemptState,
        updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Transition an attempt with a state/version fence."""

        with self._lock, self._connection:
            current = self.get_attempt(attempt_id)
            current_state = StepAttemptState(current["state"])
            if current_state not in expected:
                raise WorkflowConflict("workflow attempt transition is invalid or stale")
            if target not in ATTEMPT_TRANSITIONS.get(current_state, frozenset()):
                raise WorkflowConflict("workflow attempt state transition is not allowed")
            record = {
                **current,
                **dict(updates or {}),
                "state": target.value,
                "version": int(current["version"]) + 1,
                "updated_at": self._now_ms(),
            }
            if (current_state is StepAttemptState.PENDING
                    and target is StepAttemptState.DISPATCHING
                    and "dispatch_admission" not in current):
                # Legacy pending/waiting attempts have never reached dispatch.
                # Initialize only at their state/version-fenced boundary;
                # never backfill a legacy RUNNING or DISPATCHING record.
                record["dispatch_admission"] = "not_admitted"
            payload, seal = self._encode(record)
            changed = self._connection.execute(
                "UPDATE workflow_attempts SET state=?, version=?, payload=?, seal=?,"
                " updated_at=? WHERE attempt_id=? AND state=? AND version=?",
                (
                    target.value,
                    record["version"],
                    payload,
                    seal,
                    record["updated_at"],
                    attempt_id,
                    current["state"],
                    current["version"],
                ),
            ).rowcount
            if changed != 1:
                raise WorkflowConflict("workflow attempt transition lost a race")
        return record

    def checkpoint(self, attempt_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Append a safe checkpoint without leases, credentials, or Host handles."""

        forbidden = {"dispatch_token", "invocation_lease", "credential", "host_handle"}
        if forbidden.intersection(payload):
            raise WorkflowDenied("checkpoint contains ephemeral authority or Host state")
        with self._lock, self._connection:
            attempt = self.get_attempt(attempt_id)
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS sequence"
                " FROM workflow_checkpoints WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            sequence = int(row["sequence"]) + 1
            record = {
                "checkpoint_id": f"{attempt_id}:checkpoint:{sequence}",
                "run_id": attempt["run_id"],
                "attempt_id": attempt_id,
                "sequence": sequence,
                **dict(payload),
                "created_at": self._now_ms(),
            }
            encoded, seal = self._encode(record)
            self._connection.execute(
                "INSERT INTO workflow_checkpoints VALUES(?,?,?,?,?,?,?)",
                (
                    record["checkpoint_id"],
                    record["run_id"],
                    attempt_id,
                    sequence,
                    encoded,
                    seal,
                    record["created_at"],
                ),
            )
        return record

    def latest_checkpoint(self, attempt_id: str) -> dict[str, Any] | None:
        """Return the latest authenticated checkpoint."""

        with self._lock:
            row = self._connection.execute(
                "SELECT payload, seal FROM workflow_checkpoints WHERE attempt_id=?"
                " ORDER BY sequence DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
        return self._decode(row["payload"], row["seal"]) if row else None
