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

    with pytest.raises(InvocationEventError, match="transition conflict"):
        store.set_state(
            "inv-cas",
            "cancelled",
            owner_key="alice:default",
            expected_states={"accepted"},
        )
    store.set_state(
        "inv-cas",
        "succeeded",
        owner_key="alice:default",
        result={"status": "succeeded"},
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
        store.set_state(
            "large",
            "succeeded",
            owner_key="alice:default",
            result={"value": "x" * 2048},
            expected_states={"accepted"},
        )
