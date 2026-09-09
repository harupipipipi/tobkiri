"""Durable turn identities survive process exit without authorizing execution."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict

PAYLOAD = {
    "profile_id": "defaults",
    "request_id": "request",
    "turn_id": "turn",
    "conversation_id": "conversation",
    "conversation_revision": 1,
}

SAVED_PAYLOAD = {
    "request": {
        "turn_id": "turn",
        "conversation_id": "conversation",
        "conversation_revision": 1,
        "content": "Hello",
    }
}


def test_saved_claim_has_one_winner_across_independent_stores(tmp_path: Path) -> None:
    barrier = Barrier(2)

    def claim() -> dict:
        store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
        barrier.wait(timeout=10)
        return store.claim_saved(SAVED_PAYLOAD)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sorted(result["claimed"] for result in results) == [False, True]
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    record = store.get("turn")
    assert record["status"] == "running"
    assert record["revision"] == 2
    assert len(record["events"]) == 2
    assert store.claim_saved(SAVED_PAYLOAD) == {"claimed": False, "turn": record}


@pytest.mark.parametrize("status", ["running", "waiting", "completed", "failed", "cancelled"])
def test_saved_claim_never_reclaims_started_or_terminal_turns(
    tmp_path: Path, status: str,
) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    claimed = store.claim_saved(SAVED_PAYLOAD)
    assert claimed["claimed"] is True
    record = claimed["turn"]
    if status != "running":
        record = store.mutate("transition", "turn", expected_revision=2, status=status)
    reopened = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    assert reopened.claim_saved(SAVED_PAYLOAD) == {"claimed": False, "turn": record}
    with pytest.raises(TurnConflict, match="input identity"):
        reopened.claim_saved({"request": {**SAVED_PAYLOAD["request"], "content": "Changed"}})
    assert reopened.get("turn") == record


def test_saved_claim_lost_reply_is_not_reissued_after_process_exit(tmp_path: Path) -> None:
    script = (
        "import json,sys; from pathlib import Path; "
        "from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime; "
        "s=DurableTurnRuntime('defaults', user_data_root=Path(sys.argv[1])); "
        "assert s.claim_saved(json.loads(sys.argv[2]))['claimed']"
    )
    subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path), json.dumps(SAVED_PAYLOAD)],
        check=True, capture_output=True, timeout=20,
    )
    reopened = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    before = reopened.get("turn")
    assert reopened.claim_saved(SAVED_PAYLOAD) == {"claimed": False, "turn": before}


def test_invalid_saved_claim_does_not_create_storage(tmp_path: Path) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    with pytest.raises(ValueError):
        store.claim_saved({**SAVED_PAYLOAD, "approved": True})
    assert not store.path.exists()


def test_saved_claim_does_not_retry_after_ambiguous_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    mutate = store.mutate
    calls = []

    def lose_reply(*args, **kwargs):
        calls.append(args)
        mutate(*args, **kwargs)
        raise sqlite3.OperationalError("reply lost after commit")

    monkeypatch.setattr(store, "mutate", lose_reply)
    with pytest.raises(sqlite3.OperationalError, match="reply lost"):
        store.claim_saved(SAVED_PAYLOAD)
    assert len(calls) == 1
    reopened = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    assert reopened.claim_saved(SAVED_PAYLOAD)["claimed"] is False


def test_guidance_race_does_not_cause_an_implicit_claim_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    mutate = store.mutate

    def race(*args, **kwargs):
        mutate("steer", "turn", expected_revision=1, guidance={"text": "wait"})
        return mutate(*args, **kwargs)

    monkeypatch.setattr(store, "mutate", race)
    result = store.claim_saved(SAVED_PAYLOAD)
    assert result["claimed"] is False
    assert result["turn"]["status"] == "queued"
    assert result["turn"]["revision"] == 2
    assert len(result["turn"]["events"]) == 2


def test_absent_reads_and_invalid_begin_do_not_create_storage(tmp_path: Path) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    assert store.get("turn") is None
    assert store.list() == []
    for patch in (
        {"request_id": None},
        {"conversation_revision": True},
        {"turn_id": " turn "},
        {"conversation_id": "x" * 257},
    ):
        with pytest.raises(ValueError):
            store.begin({**PAYLOAD, **patch})
    with pytest.raises(PermissionError):
        store.begin({**PAYLOAD, "profile_id": "other"})
    with pytest.raises(PermissionError):
        store.begin({**PAYLOAD, "approved": True})
    assert not store.path.parent.exists()


@pytest.mark.parametrize("input_digest", [None, "sha256:" + "a" * 64])
def test_restart_returns_running_snapshot_without_new_event(
    tmp_path: Path, input_digest: str | None,
) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    payload = {**PAYLOAD, **({"input_digest": input_digest} if input_digest else {})}
    store.begin(payload)
    running = store.mutate("transition", "turn", expected_revision=1, status="running")
    script = (
        "import json,sys; from pathlib import Path; "
        "from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime; "
        "s=DurableTurnRuntime('defaults', user_data_root=Path(sys.argv[1])); "
        "print(json.dumps(s.begin(json.loads(sys.argv[2]))))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path), json.dumps(payload)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert json.loads(result.stdout) == running
    assert store.get("turn") == running
    assert len(running["events"]) == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"turn_id": "other"},
        {"conversation_id": "other"},
        {"conversation_revision": 2},
        {"request_id": "other"},
    ],
)
def test_rebinding_remains_denied_after_reopen(tmp_path: Path, patch: dict) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    before = store.begin(PAYLOAD)
    reopened = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    with pytest.raises(TurnConflict):
        reopened.begin({**PAYLOAD, **patch})
    assert reopened.list() == [before]


@pytest.mark.parametrize("initial,retry", [
    (None, "sha256:" + "a" * 64),
    ("sha256:" + "a" * 64, None),
    ("sha256:" + "a" * 64, "sha256:" + "b" * 64),
])
def test_input_binding_cannot_be_added_removed_or_replaced_after_begin(
    tmp_path: Path, initial: str | None, retry: str | None,
) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    before = store.begin({**PAYLOAD, **({"input_digest": initial} if initial else {})})
    reopened = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    with pytest.raises(TurnConflict, match="input identity"):
        reopened.begin({**PAYLOAD, **({"input_digest": retry} if retry else {})})
    assert reopened.get("turn") == before


@pytest.mark.parametrize("digest", [None, True, "", "a" * 64, "sha256:" + "A" * 64,
                                     "sha256:" + "a" * 64 + "\n"])
def test_invalid_input_digest_does_not_create_storage(tmp_path: Path, digest: object) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    with pytest.raises(ValueError, match="input digest"):
        store.begin({**PAYLOAD, "input_digest": digest})
    assert not store.path.parent.exists()


def test_two_independent_connections_have_one_revision_winner(tmp_path: Path) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    store.begin(PAYLOAD)
    barrier = Barrier(2)

    def mutate() -> str:
        independent = DurableTurnRuntime("defaults", user_data_root=tmp_path)
        barrier.wait(timeout=10)
        try:
            independent.mutate("transition", "turn", expected_revision=1, status="running")
            return "committed"
        except TurnConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: mutate(), range(2))) == ["committed", "conflict"]
    assert store.get("turn")["revision"] == 2
    assert len(store.get("turn")["events"]) == 2


def test_terminal_identity_is_not_pruned_at_capacity(tmp_path: Path) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path, max_turns=1)
    store.begin(PAYLOAD)
    terminal = store.mutate("transition", "turn", expected_revision=1, status="cancelled")
    with pytest.raises(TurnConflict, match="capacity"):
        store.begin({**PAYLOAD, "turn_id": "second", "request_id": "second"})
    assert store.begin(PAYLOAD) == terminal
    assert store.list() == [terminal]


def test_processes_cannot_commit_the_same_revision(tmp_path: Path) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    store.begin(PAYLOAD)
    script = """
import sys
from pathlib import Path
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict
store = DurableTurnRuntime('defaults', user_data_root=Path(sys.argv[1]))
sys.stdin.readline()
try:
    store.mutate('transition', 'turn', expected_revision=1, status='running')
    print('committed')
except TurnConflict:
    print('conflict')
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.stdin.write("start\n")
            process.stdin.flush()
        results = [process.communicate(timeout=20) for process in processes]
        assert all(process.returncode == 0 for process in processes), results
        assert sorted(out.strip() for out, _ in results) == ["committed", "conflict"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    assert store.get("turn")["revision"] == 2


def test_oversize_mutation_rolls_back_all_state(tmp_path: Path) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    before = store.begin(PAYLOAD)
    with pytest.raises(ValueError, match="size limit"):
        store.mutate("steer", "turn", expected_revision=1, guidance={"text": "x" * 1048576})
    assert store.get("turn") == before
    assert (
        store.mutate("transition", "turn", expected_revision=1, status="running")["revision"] == 2
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"id": "other"},
        {"request_id": "other"},
        {"profile_id": "other"},
        {"revision": True},
        {"status": "invented"},
        {"events": None},
    ],
)
def test_corrupt_record_is_not_rehydrated(tmp_path: Path, patch: dict) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    before = store.begin(PAYLOAD)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE turns SET body = ?", (json.dumps({**before, **patch}),))
    for action in (
        lambda: store.get("turn"),
        lambda: store.list(),
        lambda: store.begin(PAYLOAD),
        lambda: store.mutate("transition", "turn", expected_revision=1, status="running"),
    ):
        with pytest.raises(ValueError):
            action()


def test_guidance_handoff_and_consume_preserve_owner_semantics(tmp_path: Path) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    store.begin(PAYLOAD)
    guided = store.mutate("steer", "turn", expected_revision=1, guidance={"text": "continue"})
    handed = store.mutate("handoff", "turn", expected_revision=2, target={"id": "worker"})
    consumed = store.mutate("consume_guidance", "turn", expected_revision=3)
    assert consumed["items"][0]["id"] == guided["guidance"][0]["id"]
    assert consumed["turn"]["handoff"] == handed["handoff"]
    assert store.get("turn") == consumed["turn"]


def test_relative_root_and_symlink_database_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        DurableTurnRuntime("defaults", user_data_root=Path("relative"))
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    store.begin(PAYLOAD)
    original = store.path.with_name("original.sqlite3")
    store.path.rename(original)
    store.path.symlink_to(original)
    with pytest.raises(PermissionError):
        store.get("turn")
    with pytest.raises(PermissionError):
        store.begin(PAYLOAD)


@pytest.mark.parametrize("limit", [True, 0, 201, "1", 1.0])
def test_list_rejects_invalid_limits(tmp_path: Path, limit: object) -> None:
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    with pytest.raises(ValueError):
        store.list(limit=limit)
    assert not store.path.exists()
