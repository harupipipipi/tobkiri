"""Deterministic resource bounds for PackAPI replay and control journals."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading
from typing import Mapping

import pytest

from core_runtime.control_reconciliation_v4 import (
    ControlReconciliationCapacityError,
    ControlReconciliationStore,
)
from core_runtime.pack_api_server import (
    _RequestReplayCapacityError,
    _RequestReplayGuard,
)
from tobkiri_protocol.canonical import canonical_digest


class _Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds


def _request_id(index: int) -> str:
    return f"00000000-0000-4000-8000-{index % 1_000_000_000_000:012d}"


def _begin(
    store: ControlReconciliationStore,
    index: int,
    *,
    session_id: str = "session-a",
    session_expires_at: float | None = None,
) -> tuple[Mapping[str, object], bool]:
    return store.begin_operation(
        request_id=_request_id(index),
        session_id=session_id,
        operation_id="profile.change.activate",
        contract_id="tobkiri.host.control-presentation.v4",
        request_digest=canonical_digest({"request": index}),
        session_expires_at=session_expires_at,
    )


def test_replay_guard_million_identity_churn_stays_bounded_by_session_ttl() -> None:
    clock = _Clock()
    guard = _RequestReplayGuard(capacity=8, clock=clock, max_session_ttl_seconds=1.0)

    for index in range(1_000_000):
        assert guard.consume(
            "session-a",
            _request_id(index),
            session_ttl_seconds=1.0,
        )
        clock.advance(1.01)

    assert guard.snapshot() == {"capacity": 8, "entries": 0, "sessions": 0}


def test_replay_guard_same_session_different_session_and_capacity_fail_closed() -> None:
    guard = _RequestReplayGuard(capacity=2)
    request_id = _request_id(1)

    assert guard.consume("session-a", request_id)
    assert not guard.consume("session-a", request_id)
    assert guard.consume("session-b", request_id)
    with pytest.raises(_RequestReplayCapacityError, match="capacity"):
        guard.consume("session-a", _request_id(2))
    assert guard.snapshot()["entries"] == 2


def test_replay_guard_authentication_renewal_does_not_extend_identity_horizon() -> None:
    clock = _Clock()
    guard = _RequestReplayGuard(capacity=2, clock=clock, max_session_ttl_seconds=10.0)
    request_id = _request_id(1)
    assert guard.consume("session-a", request_id, session_ttl_seconds=5.0)
    clock.advance(4.0)
    guard.renew_session("session-a", session_ttl_seconds=5.0)
    clock.advance(2.0)

    assert guard.consume("session-a", request_id, session_ttl_seconds=5.0)


def test_replay_guard_rejects_reuse_within_window_then_recovers_capacity() -> None:
    clock = _Clock()
    guard = _RequestReplayGuard(capacity=2, clock=clock, max_session_ttl_seconds=5.0)
    first = _request_id(1)

    assert guard.consume("session-a", first, session_ttl_seconds=5.0)
    assert not guard.consume("session-a", first, session_ttl_seconds=5.0)
    assert guard.consume("session-a", _request_id(2), session_ttl_seconds=5.0)
    with pytest.raises(_RequestReplayCapacityError, match="capacity"):
        guard.consume("session-a", _request_id(3), session_ttl_seconds=5.0)

    clock.advance(5.01)
    assert guard.consume("session-a", first, session_ttl_seconds=5.0)
    assert guard.snapshot()["entries"] == 1


def test_replay_guard_concurrent_admission_never_exceeds_capacity() -> None:
    guard = _RequestReplayGuard(capacity=7)

    def consume(index: int) -> str:
        try:
            return "fresh" if guard.consume("session-a", _request_id(index)) else "replay"
        except _RequestReplayCapacityError:
            return "full"

    with ThreadPoolExecutor(max_workers=32) as executor:
        outcomes = list(executor.map(consume, range(100)))

    assert outcomes.count("fresh") == 7
    assert outcomes.count("full") == 93
    assert guard.snapshot()["entries"] == 7


def test_session_renewal_debounces_sliding_expiry_writes(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / "reconciliation.sqlite3"
    store = ControlReconciliationStore(
        path,
        clock=clock,
        session_renewal_debounce_seconds=30.0,
    )
    _begin(store, 1, session_expires_at=2_000.0)

    store.renew_session("session-a", expires_at=3_000.0)
    store.renew_session("session-a", expires_at=3_020.0)
    with sqlite3.connect(path) as connection:
        first_expiry = float(
            connection.execute("SELECT expires_at FROM control_replay_sessions").fetchone()[0]
        )
    assert first_expiry == 3_000.0

    store.renew_session("session-a", expires_at=3_031.0)
    with sqlite3.connect(path) as connection:
        renewed_expiry = float(
            connection.execute("SELECT expires_at FROM control_replay_sessions").fetchone()[0]
        )
    assert renewed_expiry == 3_031.0
    store.close()


def test_near_capacity_status_reads_do_not_copy_journal_during_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ControlReconciliationStore(
        tmp_path / "reconciliation.sqlite3",
        max_operation_records=32,
    )
    for index in range(32):
        _begin(store, index)

    def reject_snapshot_copy(_snapshot: Path) -> None:
        raise AssertionError("operation status copied the journal")

    monkeypatch.setattr(store, "_copy_stable_snapshot", reject_snapshot_copy)

    def finish(index: int) -> str:
        record = store.finish_operation(
            _request_id(index),
            session_id="session-a",
            state="succeeded",
            result={"state": "succeeded", "index": index},
        )
        return str(record["state"])

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(finish, range(32)))

    assert outcomes == ["succeeded"] * 32
    assert all(
        store.operation_status(_request_id(index), session_id="session-a")["state"]
        == "succeeded"
        for index in range(32)
    )
    store.close()


def test_operation_capacity_preserves_pending_and_terminal_replay(tmp_path: Path) -> None:
    store = ControlReconciliationStore(
        tmp_path / "reconciliation.sqlite3",
        max_operation_records=2,
        terminal_retention_seconds=10.0,
        clock=lambda: 1_000.0,
    )
    first, created = _begin(store, 1, session_expires_at=2_000.0)
    assert created and first["state"] == "pending"
    store.finish_operation(
        _request_id(1),
        session_id="session-a",
        state="succeeded",
        result={"state": "active"},
    )
    _begin(store, 2, session_expires_at=2_000.0)

    replay, created = _begin(store, 1, session_expires_at=2_000.0)
    assert not created and replay["result"] == {"state": "active"}
    with pytest.raises(ControlReconciliationCapacityError, match="capacity"):
        _begin(store, 3, session_expires_at=2_000.0)
    assert store.journal_snapshot()["records"] == 2


def test_terminal_compaction_requires_both_retention_and_session_expiry(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    path = tmp_path / "reconciliation.sqlite3"
    store = ControlReconciliationStore(
        path,
        max_operation_records=2,
        terminal_retention_seconds=10.0,
        clock=clock,
    )
    _begin(store, 1, session_expires_at=1_005.0)
    store.finish_operation(
        _request_id(1),
        session_id="session-a",
        state="failed",
        result={"state": "error", "code": "UNAPPROVED"},
        safe_error_code="UNAPPROVED",
    )
    clock.advance(11.0)

    replay, created = _begin(store, 1, session_expires_at=1_100.0)
    assert not created and replay["state"] == "failed"
    assert store.journal_snapshot()["records"] == 1

    clock.advance(100.0)
    _begin(store, 2, session_id="session-b", session_expires_at=1_200.0)
    assert store.journal_snapshot()["records"] == 1
    with sqlite3.connect(path) as connection:
        audit = connection.execute(
            """
            SELECT compacted_operations, compacted_failed
            FROM control_journal_audit WHERE singleton_id=1
            """
        ).fetchone()
    assert audit == (1, 1)


def test_restart_replays_terminal_record_until_session_window_ends(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / "reconciliation.sqlite3"
    original = ControlReconciliationStore(path, clock=clock)
    _begin(original, 1, session_expires_at=2_000.0)
    expected = original.finish_operation(
        _request_id(1),
        session_id="session-a",
        state="succeeded",
        result={"state": "active", "revision": 1},
    )
    original.close()

    restarted = ControlReconciliationStore(path, clock=clock)
    replay, created = _begin(restarted, 1, session_expires_at=2_000.0)
    assert not created
    assert replay == expected


def test_database_page_limit_and_oversized_result_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "reconciliation.sqlite3"
    store = ControlReconciliationStore(
        path,
        max_database_bytes=1024 * 1024,
        max_operation_result_bytes=64,
    )
    _begin(store, 1)
    outcome = store.finish_operation(
        _request_id(1),
        session_id="session-a",
        state="succeeded",
        result={"state": "active", "value": "x" * 1_000},
    )

    assert outcome["state"] == "indeterminate"
    assert outcome["result"] is None
    assert outcome["safe_error_code"] == "RESULT_TOO_LARGE"
    with store._connect() as connection:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        max_pages = int(connection.execute("PRAGMA max_page_count").fetchone()[0])
    assert page_size * max_pages <= 1024 * 1024
