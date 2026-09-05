from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.frontend.command_protocol import CommandProtocolRegistry  # noqa: E402
from domain.frontend.offline_queue import (  # noqa: E402
    OfflineOperationQueue,
    OfflineQueueConflict,
    OfflineQueueError,
)


def _commands(tmp_path: Path, monkeypatch) -> dict[str, dict]:
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
        str(tmp_path / "settings.json"),
    )
    catalog = CommandProtocolRegistry(DEFAULTSPACK_ROOT).catalog()
    return {command["canonical_id"]: command for command in catalog["commands"]}


def test_only_explicit_desired_state_is_queueable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands = _commands(tmp_path, monkeypatch)
    queue = OfflineOperationQueue(tmp_path / "offline.sqlite3")

    record = queue.enqueue(
        command=commands["defaultspack:deepthink"],
        args={"enabled": True},
        idempotency_key="offline-deepthink-1",
        expected_revision=4,
    )

    assert record["state"] == "queued"
    assert record["request"]["args"] == {"enabled": True}
    assert record["request"]["expected_revision"] == 4
    assert queue.pending() == [record]


def test_host_pack_approval_and_secret_operations_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands = _commands(tmp_path, monkeypatch)
    queue = OfflineOperationQueue(tmp_path / "offline.sqlite3")

    with pytest.raises(OfflineQueueError, match="backend-authoritative"):
        queue.enqueue(
            command=commands["defaultspack:home_title"],
            args={"value": "Offline"},
            idempotency_key="offline-host-1",
            expected_revision=0,
        )
    with pytest.raises(OfflineQueueError, match="registered schema"):
        queue.enqueue(
            command=commands["defaultspack:deepthink"],
            args={"enabled": True, "api_key": "do-not-store"},
            idempotency_key="offline-secret-1",
            expected_revision=0,
        )


def test_idempotency_conflict_and_explicit_conflict_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands = _commands(tmp_path, monkeypatch)
    queue = OfflineOperationQueue(tmp_path / "offline.sqlite3")
    record = queue.enqueue(
        command=commands["defaultspack:deepthink"],
        args={"enabled": True},
        idempotency_key="offline-conflict-1",
        expected_revision=2,
    )
    duplicate = queue.enqueue(
        command=commands["defaultspack:deepthink"],
        args={"enabled": True},
        idempotency_key="offline-conflict-1",
        expected_revision=2,
    )

    assert duplicate["queue_id"] == record["queue_id"]
    with pytest.raises(OfflineQueueConflict, match="different operation"):
        queue.enqueue(
            command=commands["defaultspack:deepthink"],
            args={"enabled": False},
            idempotency_key="offline-conflict-1",
            expected_revision=2,
        )
    claimed = queue.claim_pending(owner_key="local", worker_id="test-worker")
    assert claimed[0]["queue_id"] == record["queue_id"]
    result = queue.record_result(
        record["queue_id"],
        state="conflicted",
        result={
            "current": {"enabled": False, "revision": 3},
            "queued": {"enabled": True, "revision": 2},
        },
        lease_id=claimed[0]["lease_id"],
    )
    assert result["state"] == "conflicted"
    assert queue.pending() == []


def test_protocol_replays_offline_desired_state_through_normal_invocation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
        str(tmp_path / "settings.json"),
    )
    protocol = CommandProtocolRegistry(DEFAULTSPACK_ROOT)

    queued = protocol.enqueue_offline(
        {
            "command_ref": "defaultspack:deepthink",
            "args": {"enabled": True},
            "idempotency_key": "offline-replay-1",
            "expected_revision": 0,
        }
    )
    replayed = protocol.replay_offline()

    assert queued["status"] == "queued"
    assert replayed["results"][0]["state"] == "completed"
    assert protocol.query_states(
        ["defaultspack:models.deepthink_enabled"]
    )["states"][0]["value"] is True


def test_replay_lease_is_atomic_owner_scoped_and_cancellable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands = _commands(tmp_path, monkeypatch)
    queue = OfflineOperationQueue(tmp_path / "offline.sqlite3")
    first = queue.enqueue(
        command=commands["defaultspack:deepthink"],
        args={"enabled": True},
        idempotency_key="offline-lease-1",
        expected_revision=0,
        owner_key="alice",
    )
    queue.enqueue(
        command=commands["defaultspack:deepthink"],
        args={"enabled": False},
        idempotency_key="offline-lease-2",
        expected_revision=0,
        owner_key="bob",
    )

    claimed = queue.claim_pending(owner_key="alice", worker_id="worker-a")
    assert [item["queue_id"] for item in claimed] == [first["queue_id"]]
    assert queue.claim_pending(owner_key="alice", worker_id="worker-b") == []
    assert queue.pending(owner_key="bob")
    assert queue.cancel(first["queue_id"], owner_key="bob")["status"] == "not_found"
    cancelled_outcome = queue.cancel(first["queue_id"], owner_key="alice")
    assert cancelled_outcome["status"] == "cancelled"
    assert queue.cancellation_requested(
        first["queue_id"],
        owner_key="alice",
        lease_id=claimed[0]["lease_id"],
    )
    cancelled = queue.record_result(
        first["queue_id"],
        state="cancelled",
        result={"status": "cancelled"},
        owner_key="alice",
        lease_id=claimed[0]["lease_id"],
    )
    assert cancelled["state"] == "cancelled"


def test_cancel_before_effect_barrier_prevents_replay_effect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands = _commands(tmp_path, monkeypatch)
    queue = OfflineOperationQueue(tmp_path / "offline.sqlite3")
    record = queue.enqueue(
        command=commands["defaultspack:deepthink"],
        args={"enabled": True},
        idempotency_key="offline-barrier-cancel-1",
        expected_revision=0,
        owner_key="alice",
    )
    claimed = queue.claim_pending(owner_key="alice", worker_id="worker-a")
    lease_id = claimed[0]["lease_id"]

    cancelled = queue.cancel(record["queue_id"], owner_key="alice")
    barrier = queue.begin_effect_commit(
        record["queue_id"],
        owner_key="alice",
        lease_id=lease_id,
    )

    assert cancelled["status"] == "cancelled"
    assert cancelled["too_late"] is False
    assert barrier["status"] == "cancelled"
    assert barrier["queue"]["state"] == "cancelled"
    assert barrier["queue"]["result"]["status"] == "cancelled"


def test_cancel_after_effect_barrier_is_durably_too_late(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands = _commands(tmp_path, monkeypatch)
    queue = OfflineOperationQueue(tmp_path / "offline.sqlite3")
    record = queue.enqueue(
        command=commands["defaultspack:deepthink"],
        args={"enabled": True},
        idempotency_key="offline-barrier-late-1",
        expected_revision=0,
        owner_key="alice",
    )
    claimed = queue.claim_pending(owner_key="alice", worker_id="worker-a")
    lease_id = claimed[0]["lease_id"]

    barrier = queue.begin_effect_commit(
        record["queue_id"],
        owner_key="alice",
        lease_id=lease_id,
    )
    too_late = queue.cancel(record["queue_id"], owner_key="alice")
    completed = queue.record_result(
        record["queue_id"],
        state="completed",
        result={"status": "succeeded", "state_changes": [{"value": True}]},
        owner_key="alice",
        lease_id=lease_id,
    )
    after_completion = queue.cancel(record["queue_id"], owner_key="alice")

    assert barrier["status"] == "effect_committing"
    assert too_late["status"] == "too_late"
    assert too_late["too_late"] is True
    assert queue.cancellation_requested(
        record["queue_id"],
        owner_key="alice",
        lease_id=lease_id,
    )
    assert completed["state"] == "completed"
    assert after_completion["status"] == "too_late"
    assert after_completion["too_late"] is True


def test_effect_barrier_and_cancel_race_allow_exactly_one_outcome(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands = _commands(tmp_path, monkeypatch)
    path = tmp_path / "offline.sqlite3"
    queue = OfflineOperationQueue(path)
    record = queue.enqueue(
        command=commands["defaultspack:deepthink"],
        args={"enabled": True},
        idempotency_key="offline-barrier-race-1",
        expected_revision=0,
        owner_key="alice",
    )
    claimed = queue.claim_pending(owner_key="alice", worker_id="worker-a")
    lease_id = claimed[0]["lease_id"]
    replay_queue = OfflineOperationQueue(path)
    cancel_queue = OfflineOperationQueue(path)
    start = threading.Barrier(2)
    outcomes: dict[str, dict] = {}

    def cross_barrier() -> None:
        start.wait()
        outcomes["barrier"] = replay_queue.begin_effect_commit(
            record["queue_id"],
            owner_key="alice",
            lease_id=lease_id,
        )

    def cancel() -> None:
        start.wait()
        outcomes["cancel"] = cancel_queue.cancel(
            record["queue_id"],
            owner_key="alice",
        )

    barrier_thread = threading.Thread(target=cross_barrier)
    cancel_thread = threading.Thread(target=cancel)
    barrier_thread.start()
    cancel_thread.start()
    barrier_thread.join()
    cancel_thread.join()

    if outcomes["barrier"]["status"] == "effect_committing":
        assert outcomes["cancel"]["status"] == "too_late"
        completed = queue.record_result(
            record["queue_id"],
            state="completed",
            result={"status": "succeeded", "state_changes": [{"value": True}]},
            owner_key="alice",
            lease_id=lease_id,
        )
        assert completed["state"] == "completed"
    else:
        assert outcomes["barrier"]["status"] == "cancelled"
        assert outcomes["cancel"]["status"] == "cancelled"
        assert outcomes["barrier"]["queue"]["state"] == "cancelled"


def test_effect_owner_can_record_result_after_lease_expiry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands = _commands(tmp_path, monkeypatch)
    queue = OfflineOperationQueue(tmp_path / "offline.sqlite3")
    record = queue.enqueue(
        command=commands["defaultspack:deepthink"],
        args={"enabled": True},
        idempotency_key="offline-expired-result-1",
        expected_revision=0,
        owner_key="alice",
    )
    claimed = queue.claim_pending(owner_key="alice", worker_id="worker-a")
    lease_id = claimed[0]["lease_id"]
    queue.begin_effect_commit(
        record["queue_id"],
        owner_key="alice",
        lease_id=lease_id,
    )
    with sqlite3.connect(queue.path) as connection:
        connection.execute(
            "UPDATE offline_operations SET lease_expires_at = ? WHERE queue_id = ?",
            ("2000-01-01T00:00:00+00:00", record["queue_id"]),
        )

    completed = queue.record_result(
        record["queue_id"],
        state="completed",
        result={"status": "succeeded", "state_changes": [{"value": True}]},
        owner_key="alice",
        lease_id=lease_id,
    )

    assert completed["state"] == "completed"


def test_expired_effect_requires_reconciliation_without_auto_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands = _commands(tmp_path, monkeypatch)
    queue = OfflineOperationQueue(tmp_path / "offline.sqlite3")
    record = queue.enqueue(
        command=commands["defaultspack:deepthink"],
        args={"enabled": True},
        idempotency_key="offline-reconcile-1",
        expected_revision=0,
        owner_key="alice",
    )
    claimed = queue.claim_pending(owner_key="alice", worker_id="worker-a")
    lease_id = claimed[0]["lease_id"]
    queue.begin_effect_commit(
        record["queue_id"],
        owner_key="alice",
        lease_id=lease_id,
    )
    with sqlite3.connect(queue.path) as connection:
        connection.execute(
            "UPDATE offline_operations SET lease_expires_at = ? WHERE queue_id = ?",
            ("2000-01-01T00:00:00+00:00", record["queue_id"]),
        )

    reconciled = queue.reconcile_expired_effect_commits(owner_key="alice")

    assert reconciled[0]["state"] == "reconciliation_required"
    assert reconciled[0]["result"]["status"] == "reconciliation_required"
    assert reconciled[0]["result"]["error"]["code"] == "EFFECT_OUTCOME_UNKNOWN"
    assert queue.claim_pending(owner_key="alice", worker_id="worker-b") == []
    assert queue.cancel(record["queue_id"], owner_key="alice")["status"] == "too_late"
    with pytest.raises(OfflineQueueError, match="terminal"):
        queue.record_result(
            record["queue_id"],
            state="completed",
            result={"status": "succeeded"},
            owner_key="alice",
            lease_id=lease_id,
        )
