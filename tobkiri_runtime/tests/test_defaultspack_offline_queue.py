from __future__ import annotations

import sys
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
    assert queue.cancel(first["queue_id"], owner_key="bob") is False
    assert queue.cancel(first["queue_id"], owner_key="alice") is True
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
