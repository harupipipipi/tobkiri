from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.frontend.invocation_events import (  # noqa: E402
    InvocationEventError,
    InvocationEventStore,
)


def test_events_are_monotonic_resumable_and_secret_redacted(tmp_path: Path) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3")

    first = store.append(
        "inv-1",
        "accepted",
        {"request": {"authorization": "Bearer secret", "name": "safe"}},
    )
    second = store.append("inv-1", "progress", {"completed": 1, "total": 2})
    third = store.append(
        "inv-1",
        "partial_result",
        {"message": "provider said Bearer abc.def-123 and sk-abcdefghijklmnop"},
    )

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert first["payload"]["request"]["authorization"] == "[REDACTED]"
    assert first["payload"]["request"]["name"] == "safe"
    assert third["payload"]["message"] == (
        "provider said [REDACTED] and [REDACTED]"
    )
    assert store.resume("inv-1", after_sequence=1) == [second, third]


def test_terminal_event_closes_invocation_and_snapshot_is_authoritative(
    tmp_path: Path,
) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3")
    store.append("inv-2", "started")
    terminal = store.append("inv-2", "completed", {"result": "ok"})

    snapshot = store.snapshot("inv-2")

    assert snapshot["last_sequence"] == terminal["sequence"]
    assert snapshot["status"] == "completed"
    assert snapshot["terminal"] is True
    with pytest.raises(InvocationEventError, match="already terminated"):
        store.append("inv-2", "progress")


def test_invocation_claim_is_atomic(tmp_path: Path) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3")

    assert store.claim("inv-claim", {"request_fingerprint": "one"}) is True
    assert store.claim("inv-claim", {"request_fingerprint": "one"}) is False
    assert store.resume("inv-claim")[0]["type"] == "accepted"


def test_event_contract_rejects_unknown_types_and_oversized_payloads(
    tmp_path: Path,
) -> None:
    store = InvocationEventStore(
        tmp_path / "events.sqlite3",
        max_payload_bytes=32,
    )

    with pytest.raises(InvocationEventError, match="unsupported event type"):
        store.append("inv-3", "made_up")
    with pytest.raises(InvocationEventError, match="size limit"):
        store.append("inv-3", "progress", {"message": "x" * 100})


def test_event_retention_prunes_expired_rows_on_open(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    store = InvocationEventStore(path)
    store.append(
        "inv-expired",
        "completed",
        timestamp="2020-01-01T00:00:00+00:00",
    )

    reopened = InvocationEventStore(path, retention_days=1)

    assert reopened.resume("inv-expired") == []


def test_snapshot_reads_actual_latest_event_beyond_first_thousand(
    tmp_path: Path,
) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3")
    store.claim(
        "inv-long",
        {"request_fingerprint": "long"},
        owner_key="alice:default:chat",
        request_fingerprint="long",
    )
    for index in range(1001):
        store.append("inv-long", "progress", {"index": index})
    terminal = store.append("inv-long", "completed")

    snapshot = store.snapshot(
        "inv-long",
        owner_key="alice:default:chat",
    )
    assert snapshot["last_sequence"] == terminal["sequence"]
    assert snapshot["terminal"] is True
    assert store.resume(
        "inv-long",
        owner_key="bob:default:chat",
    ) == []


def test_invocation_identity_is_owner_scoped(tmp_path: Path) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3")

    assert store.claim(
        "same-id",
        {"owner": "alice"},
        owner_key="alice:default",
        request_fingerprint="alice",
    )
    assert store.claim(
        "same-id",
        {"owner": "bob"},
        owner_key="bob:default",
        request_fingerprint="bob",
    )
    assert store.resume("same-id", owner_key="alice:default")[0]["payload"] == {
        "owner": "alice"
    }
    assert store.resume("same-id", owner_key="bob:default")[0]["payload"] == {
        "owner": "bob"
    }


def test_executing_state_rejects_late_cancel_settlement(tmp_path: Path) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3")
    assert store.claim(
        "inv-cas",
        {},
        owner_key="alice:default",
        request_fingerprint="cas",
    )
    assert store.mark_executing(
        "inv-cas",
        owner_key="alice:default",
        expected_state="accepted",
    )

    with pytest.raises(InvocationEventError, match="settle_terminal"):
        store.set_state(
            "inv-cas",
            "cancelled",
            owner_key="alice:default",
            expected_states={"accepted"},
        )
    store.settle_terminal(
        "inv-cas",
        "succeeded",
        owner_key="alice:default",
        result={"status": "succeeded"},
        event_type="completed",
        expected_states={"executing"},
    )
    assert store.stored("inv-cas", owner_key="alice:default")["state"] == "succeeded"


def test_legacy_global_identity_schema_migrates_without_data_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE command_invocations (
                invocation_id TEXT PRIMARY KEY,
                owner_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                approval_request_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE invocation_events (
                invocation_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (invocation_id, sequence)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO command_invocations
            VALUES ('legacy', 'alice:default', 'fingerprint', 'accepted',
                    NULL, NULL, '2026-01-01', '2026-01-01')
            """
        )
        connection.execute(
            """
            INSERT INTO invocation_events
            VALUES ('legacy', 1, 'accepted', '2026-01-01', '{}')
            """
        )
    store = InvocationEventStore(path)

    assert store.stored("legacy", owner_key="alice:default")["state"] == "accepted"
    assert store.resume("legacy", owner_key="alice:default")[0]["type"] == "accepted"
    assert store.claim(
        "legacy",
        {},
        owner_key="bob:default",
        request_fingerprint="other",
    )


def test_result_size_is_bounded(tmp_path: Path) -> None:
    store = InvocationEventStore(
        tmp_path / "events.sqlite3",
        max_result_bytes=1024,
    )
    assert store.claim(
        "large",
        {},
        owner_key="alice:default",
        request_fingerprint="large",
    )
    with pytest.raises(InvocationEventError, match="result exceeds"):
        store.settle_terminal(
            "large",
            "succeeded",
            owner_key="alice:default",
            result={"value": "x" * 2048},
            event_type="completed",
            expected_states={"accepted"},
        )


@pytest.mark.parametrize(
    "terminal_state",
    ["succeeded", "failed", "cancelled", "conflicted", "expired"],
)
def test_set_state_rejects_terminal_states(
    tmp_path: Path,
    terminal_state: str,
) -> None:
    store = InvocationEventStore(tmp_path / f"{terminal_state}.sqlite3")
    assert store.claim(
        "terminal-bypass",
        {},
        owner_key="alice:default",
        request_fingerprint=terminal_state,
    )

    with pytest.raises(InvocationEventError, match="settle_terminal"):
        store.set_state(
            "terminal-bypass",
            terminal_state,
            owner_key="alice:default",
            expected_states={"accepted"},
        )

    assert store.stored("terminal-bypass", owner_key="alice:default")["state"] == (
        "accepted"
    )


def test_recover_stale_atomically_persists_failed_state_and_event(
    tmp_path: Path,
) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3", lease_seconds=10)
    owner_key = "alice:default"
    assert store.claim(
        "stale-execution",
        {},
        owner_key=owner_key,
        request_fingerprint="stale-execution",
    )
    assert store.mark_executing(
        "stale-execution",
        owner_key=owner_key,
        expected_state="accepted",
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE command_invocations
            SET updated_at = '2020-01-01T00:00:00+00:00'
            WHERE owner_key = ? AND invocation_id = ?
            """,
            (owner_key, "stale-execution"),
        )

    assert store.recover_stale("stale-execution", owner_key=owner_key) == "failed"

    stored = store.stored("stale-execution", owner_key=owner_key)
    events = store.resume("stale-execution", owner_key=owner_key)
    assert stored is not None
    assert stored["state"] == "failed"
    assert stored["result"]["error"]["code"] == "EXECUTION_OUTCOME_UNKNOWN"
    assert [event["type"] for event in events] == ["accepted", "failed"]
    assert events[-1]["payload"]["error"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN"
    )
    assert store.snapshot("stale-execution", owner_key=owner_key)["terminal"] is True

    assert store.recover_stale("stale-execution", owner_key=owner_key) is None
    assert [
        event["type"]
        for event in store.resume("stale-execution", owner_key=owner_key)
    ] == ["accepted", "failed"]


def test_recover_stale_rolls_back_state_when_terminal_event_insert_fails(
    tmp_path: Path,
) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3", lease_seconds=10)
    owner_key = "alice:default"
    assert store.claim(
        "stale-rollback",
        {},
        owner_key=owner_key,
        request_fingerprint="stale-rollback",
    )
    assert store.mark_executing(
        "stale-rollback",
        owner_key=owner_key,
        expected_state="accepted",
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE command_invocations
            SET updated_at = '2020-01-01T00:00:00+00:00'
            WHERE owner_key = ? AND invocation_id = ?
            """,
            (owner_key, "stale-rollback"),
        )
        connection.execute(
            """
            CREATE TRIGGER fail_stale_terminal_event_insert
            BEFORE INSERT ON invocation_events
            WHEN NEW.event_type = 'failed'
            BEGIN
                SELECT RAISE(ABORT, 'injected stale terminal event failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected stale terminal"):
        store.recover_stale("stale-rollback", owner_key=owner_key)

    assert store.stored("stale-rollback", owner_key=owner_key)["state"] == (
        "executing"
    )
    assert [
        event["type"]
        for event in store.resume("stale-rollback", owner_key=owner_key)
    ] == ["accepted"]

    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER fail_stale_terminal_event_insert")

    assert store.recover_stale("stale-rollback", owner_key=owner_key) == "failed"
    assert store.recover_stale("stale-rollback", owner_key=owner_key) is None
    assert [
        event["type"]
        for event in store.resume("stale-rollback", owner_key=owner_key)
    ] == ["accepted", "failed"]


def test_terminal_settlement_commits_state_result_and_event_together(
    tmp_path: Path,
) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3")
    owner_key = "alice:default"
    assert store.claim(
        "terminal-success",
        {"request": "safe"},
        owner_key=owner_key,
        request_fingerprint="terminal-success",
    )
    assert store.mark_executing(
        "terminal-success",
        owner_key=owner_key,
        expected_state="accepted",
    )

    event = store.settle_terminal(
        "terminal-success",
        "succeeded",
        owner_key=owner_key,
        result={"status": "succeeded", "token": "sk-abcdefghijklmnop"},
        event_type="completed",
        event_payload={"authorization": "Bearer this-should-not-be-stored"},
        expected_states={"executing"},
    )

    stored = store.stored("terminal-success", owner_key=owner_key)
    snapshot = store.snapshot("terminal-success", owner_key=owner_key)
    assert stored == {
        "request_fingerprint": "terminal-success",
        "state": "succeeded",
        "result": {"status": "succeeded", "token": "[REDACTED]"},
        "approval_request_id": None,
    }
    assert event["sequence"] == 2
    assert event["payload"] == {"authorization": "[REDACTED]"}
    assert snapshot["status"] == "completed"
    assert snapshot["terminal"] is True
    assert snapshot["last_sequence"] == event["sequence"]
    assert snapshot["latest"] == event
    with pytest.raises(InvocationEventError, match="already terminated"):
        store.settle_terminal(
            "terminal-success",
            "failed",
            owner_key=owner_key,
            result={"status": "failed"},
            event_type="failed",
            expected_states={"succeeded"},
        )


def test_terminal_settlement_rolls_back_state_when_event_insert_fails(
    tmp_path: Path,
) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3")
    owner_key = "alice:default"
    assert store.claim(
        "terminal-rollback",
        {},
        owner_key=owner_key,
        request_fingerprint="terminal-rollback",
    )
    assert store.mark_executing(
        "terminal-rollback",
        owner_key=owner_key,
        expected_state="accepted",
    )
    before_events = store.resume("terminal-rollback", owner_key=owner_key)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_terminal_event_insert
            BEFORE INSERT ON invocation_events
            WHEN NEW.event_type = 'completed'
            BEGIN
                SELECT RAISE(ABORT, 'injected terminal event insert failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected terminal event"):
        store.settle_terminal(
            "terminal-rollback",
            "succeeded",
            owner_key=owner_key,
            result={"status": "succeeded"},
            event_type="completed",
            expected_states={"executing"},
        )

    assert store.stored("terminal-rollback", owner_key=owner_key) == {
        "request_fingerprint": "terminal-rollback",
        "state": "executing",
        "result": None,
        "approval_request_id": None,
    }
    assert store.resume("terminal-rollback", owner_key=owner_key) == before_events
    assert store.snapshot("terminal-rollback", owner_key=owner_key)["terminal"] is False


def test_terminal_settlement_rejects_conflicts_without_partial_write(
    tmp_path: Path,
) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3")
    owner_key = "alice:default"
    assert store.claim(
        "terminal-conflict",
        {},
        owner_key=owner_key,
        request_fingerprint="terminal-conflict",
    )
    assert store.mark_executing(
        "terminal-conflict",
        owner_key=owner_key,
        expected_state="accepted",
    )
    before_events = store.resume("terminal-conflict", owner_key=owner_key)

    with pytest.raises(InvocationEventError, match="transition conflict"):
        store.settle_terminal(
            "terminal-conflict",
            "succeeded",
            owner_key=owner_key,
            result={"status": "succeeded"},
            event_type="completed",
            expected_states={"accepted"},
        )
    with pytest.raises(InvocationEventError, match="transition conflict"):
        store.settle_terminal(
            "terminal-conflict",
            "succeeded",
            owner_key="bob:default",
            result={"status": "succeeded"},
            event_type="completed",
            expected_states={"executing"},
        )

    assert store.stored("terminal-conflict", owner_key=owner_key) == {
        "request_fingerprint": "terminal-conflict",
        "state": "executing",
        "result": None,
        "approval_request_id": None,
    }
    assert store.resume("terminal-conflict", owner_key=owner_key) == before_events


def test_terminal_settlement_checks_lease_and_payload_contracts(tmp_path: Path) -> None:
    store = InvocationEventStore(
        tmp_path / "events.sqlite3",
        max_payload_bytes=64,
        max_result_bytes=1024,
    )
    owner_key = "alice:default"
    assert store.claim(
        "terminal-lease",
        {},
        owner_key=owner_key,
        request_fingerprint="terminal-lease",
    )
    store.set_state(
        "terminal-lease",
        "approval_required",
        owner_key=owner_key,
        result={"status": "approval_required"},
        expected_states={"accepted"},
    )
    assert store.claim_resume(
        "terminal-lease",
        owner_key=owner_key,
        request_fingerprint="terminal-lease",
        lease_id="correct-lease",
    )
    assert store.mark_executing(
        "terminal-lease",
        owner_key=owner_key,
        expected_state="resuming",
        lease_id="correct-lease",
    )

    with pytest.raises(InvocationEventError, match="transition conflict"):
        store.settle_terminal(
            "terminal-lease",
            "succeeded",
            owner_key=owner_key,
            result={"status": "succeeded"},
            event_type="completed",
            expected_states={"executing"},
            lease_id="wrong-lease",
        )
    with pytest.raises(InvocationEventError, match="size limit"):
        store.settle_terminal(
            "terminal-lease",
            "succeeded",
            owner_key=owner_key,
            result={"status": "succeeded"},
            event_type="completed",
            event_payload={"message": "x" * 128},
            expected_states={"executing"},
            lease_id="correct-lease",
        )
    with pytest.raises(InvocationEventError, match="result exceeds"):
        store.settle_terminal(
            "terminal-lease",
            "succeeded",
            owner_key=owner_key,
            result={"message": "x" * 2048},
            event_type="completed",
            expected_states={"executing"},
            lease_id="correct-lease",
        )
    with pytest.raises(InvocationEventError, match="state/event mismatch"):
        store.settle_terminal(
            "terminal-lease",
            "succeeded",
            owner_key=owner_key,
            result={"status": "succeeded"},
            event_type="failed",
            expected_states={"executing"},
            lease_id="correct-lease",
        )

    assert store.stored("terminal-lease", owner_key=owner_key) == {
        "request_fingerprint": "terminal-lease",
        "state": "executing",
        "result": {"status": "approval_required"},
        "approval_request_id": None,
    }
    assert [event["type"] for event in store.resume(
        "terminal-lease", owner_key=owner_key
    )] == ["accepted"]
