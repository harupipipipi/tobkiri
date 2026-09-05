"""Durability and fail-closed tests for v4 control reconciliation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import threading
import time

import pytest

import core_runtime.control_reconciliation_v4 as reconciliation
import core_runtime.process_identity as process_identity
from core_runtime.control_reconciliation_v4 import (
    ControlReconciliationError,
    ControlReconciliationStore,
    ControlReconciliationUnavailableError,
    ProcessIdentityEvidence,
)
from tobkiri_protocol.canonical import canonical_digest


class _Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _process_operation(
    path_value: str,
    request_id: str,
    ready: object,
    release: object,
    results: object,
) -> None:
    store = ControlReconciliationStore(
        Path(path_value),
        instance_id="child-process",
        lease_timeout_seconds=0.5,
        heartbeat_interval_seconds=0.05,
    )
    _begin(store, request_id)
    ready.set()  # type: ignore[attr-defined]
    release.wait(10.0)  # type: ignore[attr-defined]
    status = store.finish_operation(
        request_id,
        session_id="session-a",
        state="succeeded",
        result={"state": "approved"},
    )
    results.put(status["state"])  # type: ignore[attr-defined]
    store.close()


def _process_prepare(path_value: str, results: object) -> None:
    try:
        store = ControlReconciliationStore(Path(path_value))
        store.prepare_for_operation()
        results.put("ok")  # type: ignore[attr-defined]
    except Exception as error:  # pragma: no cover - reported to parent
        results.put(  # type: ignore[attr-defined]
            f"{type(error).__name__}: {error}; cause={error.__cause__!r}"
        )


def _fork_inherited_store(
    inherited: ControlReconciliationStore,
    path_value: str,
    request_id: str,
    connection: object,
    crash: bool,
) -> None:
    inherited_rejected = False
    try:
        _begin(inherited, request_id)
    except ControlReconciliationUnavailableError:
        inherited_rejected = True
    stale_heartbeat = bool(
        inherited._heartbeat_thread is not None  # noqa: SLF001
        and inherited._heartbeat_thread.is_alive()  # noqa: SLF001
    )
    fresh = ControlReconciliationStore(
        Path(path_value),
        instance_id="fork-child",
        lease_timeout_seconds=0.5,
        heartbeat_interval_seconds=0.05,
    )
    _begin(fresh, request_id)
    connection.send((inherited_rejected, stale_heartbeat))  # type: ignore[attr-defined]
    if crash:
        connection.close()  # type: ignore[attr-defined]
        os._exit(17)
    fresh.finish_operation(
        request_id,
        session_id="session-a",
        state="succeeded",
        result={"state": "approved"},
    )
    fresh.close()
    connection.close()  # type: ignore[attr-defined]


def _fork_reject_inherited_only(
    inherited: ControlReconciliationStore,
    request_id: str,
    connection: object,
) -> None:
    try:
        _begin(inherited, request_id)
    except ControlReconciliationUnavailableError:
        connection.send("rejected")  # type: ignore[attr-defined]
    else:
        connection.send("accepted")  # type: ignore[attr-defined]
    connection.close()  # type: ignore[attr-defined]


def _begin(
    store: ControlReconciliationStore,
    request_id: str,
    *,
    session_id: str = "session-a",
    request_digest: str | None = None,
):
    return store.begin_operation(
        request_id=request_id,
        session_id=session_id,
        operation_id="profile.change.approve",
        contract_id="tobkiri.host.control-presentation.v4",
        request_digest=request_digest or canonical_digest({"request_id": request_id}),
    )


def test_constructor_is_filesystem_immutable_until_first_operation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "absent" / "control" / "reconciliation-v4.sqlite3"

    store = ControlReconciliationStore(path, instance_id="process-a")

    assert store.path == path
    assert not (tmp_path / "absent").exists()


def test_read_only_status_on_missing_journal_is_filesystem_immutable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "absent"
    store = ControlReconciliationStore(root / "control" / "reconciliation-v4.sqlite3")

    with pytest.raises(ControlReconciliationError, match="unavailable"):
        store.operation_status(
            "00000000-0000-4000-8000-000000000000",
            session_id="session-a",
        )

    assert not root.exists()


def test_first_authorized_operation_initializes_and_recovers_durable_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    store = ControlReconciliationStore(path, instance_id="process-a")

    store.prepare_for_operation()
    pending, created = _begin(
        store,
        "00000000-0000-4000-8000-000000000001",
    )

    assert created is True
    assert pending["state"] == "pending"
    assert path.is_file()
    assert (
        ControlReconciliationStore(path).operation_status(
            "00000000-0000-4000-8000-000000000001",
            session_id="session-a",
        )
        == pending
    )
    store.close()


def test_concurrent_first_operation_initialization_is_serialized(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    stores = [
        ControlReconciliationStore(path, instance_id=f"process-{index}") for index in range(8)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda store: store.prepare_for_operation(), stores))

    assert path.is_file()
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables == {
        "control_journal_audit",
        "control_operations",
        "control_replay_sessions",
        "control_recovery_audit",
        "profile_ceremonies",
        "sqlite_sequence",
    }


def test_legacy_schema_migration_is_atomic_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE control_operations (
                request_id TEXT PRIMARY KEY,
                session_digest TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                contract_id TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                owner_instance TEXT NOT NULL,
                result_json TEXT,
                result_digest TEXT,
                record_refs_json TEXT NOT NULL,
                safe_error_code TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(target=_process_prepare, args=(str(path), results)) for _index in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10.0)
        assert process.exitcode == 0
    assert [results.get(timeout=5.0) for _process in processes] == ["ok"] * 6
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(control_operations)")}
    assert {
        "owner_pid",
        "owner_process_token",
        "owner_boot_id",
        "owner_process_start",
        "owner_proof_version",
        "lease_expires_at",
    } <= columns


def test_live_foreign_operation_is_not_reclaimed_during_slow_mutation(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    first = ControlReconciliationStore(
        path,
        instance_id="process-a",
        lease_timeout_seconds=10.0,
        heartbeat_interval_seconds=0.01,
        clock=clock,
    )
    second = ControlReconciliationStore(
        path,
        instance_id="process-b",
        lease_timeout_seconds=10.0,
        heartbeat_interval_seconds=0.01,
        clock=clock,
    )
    request_id = "01010101-0101-4101-8101-010101010101"
    _begin(first, request_id)

    clock.value += 11.0
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with sqlite3.connect(path) as connection:
            lease = connection.execute(
                "SELECT lease_expires_at FROM control_operations WHERE request_id=?",
                (request_id,),
            ).fetchone()[0]
        if lease > clock.value:
            break
        time.sleep(0.01)
    assert lease > clock.value
    second.prepare_for_operation()

    succeeded = first.finish_operation(
        request_id,
        session_id="session-a",
        state="succeeded",
        result={"state": "approved"},
    )
    assert succeeded["state"] == "succeeded"
    first.close()
    second.close()


def test_finish_and_concurrent_begin_cannot_stop_the_only_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    store = ControlReconciliationStore(
        path,
        instance_id="process-a",
        lease_timeout_seconds=1.0,
        heartbeat_interval_seconds=0.05,
    )
    first_id = "10101010-1010-4010-8010-101010101010"
    second_id = "11101010-1010-4010-8010-101010101010"
    _begin(store, first_id)
    entered = threading.Event()
    release = threading.Event()
    original = store._has_pending_owned_operation

    def blocked_check() -> bool:
        entered.set()
        assert release.wait(5.0)
        return original()

    monkeypatch.setattr(store, "_has_pending_owned_operation", blocked_check)
    finish = threading.Thread(
        target=lambda: store.finish_operation(
            first_id,
            session_id="session-a",
            state="succeeded",
            result={"state": "approved"},
        )
    )
    finish.start()
    assert entered.wait(5.0)
    begin = threading.Thread(target=lambda: _begin(store, second_id))
    begin.start()
    time.sleep(0.05)
    release.set()
    finish.join(5.0)
    begin.join(5.0)
    assert not finish.is_alive() and not begin.is_alive()
    assert store._heartbeat_thread is not None
    assert store._heartbeat_thread.is_alive()
    assert store.operation_status(second_id, session_id="session-a")["state"] == "pending"
    store.finish_operation(
        second_id,
        session_id="session-a",
        state="failed",
        result=None,
    )
    store.close()


def test_live_operation_is_not_reclaimed_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    results = context.Queue()
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    request_id = "06060606-0606-4606-8606-060606060606"
    process = context.Process(
        target=_process_operation,
        args=(str(path), request_id, ready, release, results),
    )
    process.start()
    try:
        assert ready.wait(10.0)
        time.sleep(0.6)
        contender = ControlReconciliationStore(
            path,
            instance_id="parent-process",
            lease_timeout_seconds=0.5,
            heartbeat_interval_seconds=0.05,
        )
        contender.prepare_for_operation()
        assert contender.operation_status(request_id, session_id="session-a")["state"] == "pending"
        release.set()
        assert results.get(timeout=10.0) == "succeeded"
        contender.close()
    finally:
        release.set()
        process.join(10.0)
        if process.is_alive():
            process.terminate()
            process.join(5.0)
    assert process.exitcode == 0


def test_crashed_process_is_reclaimed_after_lease_timeout(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    results = context.Queue()
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    request_id = "07070707-0707-4707-8707-070707070707"
    process = context.Process(
        target=_process_operation,
        args=(str(path), request_id, ready, release, results),
    )
    process.start()
    assert ready.wait(10.0)
    process.terminate()
    process.join(5.0)
    assert process.exitcode is not None
    time.sleep(0.6)

    restarted = ControlReconciliationStore(
        path,
        instance_id="restarted-process",
        lease_timeout_seconds=0.5,
        heartbeat_interval_seconds=0.05,
    )
    assert restarted.recover_abandoned_operations() == 1
    status = restarted.operation_status(request_id, session_id="session-a")
    assert status["state"] == "indeterminate"


def test_expired_crashed_operation_is_reclaimed_with_pid_reuse_audit(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    crashed = ControlReconciliationStore(
        path,
        instance_id="reused-instance",
        lease_timeout_seconds=10.0,
        heartbeat_interval_seconds=1.0,
        clock=clock,
        process_start_reader=lambda _pid: ProcessIdentityEvidence("live", "start-a"),
    )
    request_id = "02020202-0202-4202-8202-020202020202"
    _begin(crashed, request_id)
    with sqlite3.connect(path) as connection:
        crashed_token = connection.execute(
            "SELECT owner_process_token FROM control_operations WHERE request_id=?",
            (request_id,),
        ).fetchone()[0]
    crashed.close()
    clock.value += 11.0

    restarted = ControlReconciliationStore(
        path,
        instance_id="reused-instance",
        lease_timeout_seconds=10.0,
        heartbeat_interval_seconds=1.0,
        clock=clock,
        process_start_reader=lambda _pid: ProcessIdentityEvidence("dead"),
    )
    assert restarted.recover_abandoned_operations() == 1
    status = restarted.operation_status(request_id, session_id="session-a")
    assert status["state"] == "indeterminate"
    with sqlite3.connect(path) as connection:
        audit = connection.execute(
            """
            SELECT abandoned_owner_process_token, recovered_by_process_token,
                   recovered_count, reason
            FROM control_recovery_audit
            """
        ).fetchone()
    assert audit[0] == crashed_token
    assert audit[1] != crashed_token
    assert audit[2:] == (1, "process_dead")


@pytest.mark.parametrize("jump", [1_000_000.0, -1_000_000.0])
def test_wall_clock_jump_never_reclaims_demonstrably_live_owner(
    tmp_path: Path,
    jump: float,
) -> None:
    clock = _Clock()
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"

    def process_reader(_pid: int) -> ProcessIdentityEvidence:
        return ProcessIdentityEvidence("live", "same-process-start")

    owner = ControlReconciliationStore(
        path,
        instance_id="owner",
        clock=clock,
        boot_id="boot-a",
        process_start_reader=process_reader,
    )
    request_id = "08080808-0808-4808-8808-080808080808"
    _begin(owner, request_id)
    owner.close()
    clock.value += jump
    contender = ControlReconciliationStore(
        path,
        instance_id="contender",
        clock=clock,
        boot_id="boot-a",
        process_start_reader=process_reader,
    )

    assert contender.recover_abandoned_operations() == 0
    assert contender.operation_status(request_id, session_id="session-a")["state"] == "pending"


@pytest.mark.parametrize(
    ("boot_id", "observed_start", "reason"),
    [
        ("boot-b", "same-process-start", "host_rebooted"),
        ("boot-a", "reused-process-start", "pid_reused"),
    ],
)
def test_reboot_and_pid_reuse_are_proven_recovery_boundaries(
    tmp_path: Path,
    boot_id: str,
    observed_start: str,
    reason: str,
) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    owner = ControlReconciliationStore(
        path,
        instance_id="owner",
        boot_id="boot-a",
        process_start_reader=lambda _pid: ProcessIdentityEvidence("live", "same-process-start"),
    )
    request_id = "09090909-0909-4909-8909-090909090909"
    _begin(owner, request_id)
    owner.close()
    restarted = ControlReconciliationStore(
        path,
        instance_id="restarted",
        boot_id=boot_id,
        process_start_reader=lambda _pid: ProcessIdentityEvidence("live", observed_start),
    )

    assert restarted.recover_abandoned_operations() == 1
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT reason FROM control_recovery_audit ORDER BY recovery_id DESC"
            ).fetchone()[0]
            == reason
        )


@pytest.mark.parametrize(
    "probe_error",
    [FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired("ps", 1.0)],
)
def test_process_probe_errors_are_unknown_not_dead(
    monkeypatch: pytest.MonkeyPatch,
    probe_error: BaseException,
) -> None:
    monkeypatch.setattr(process_identity, "_is_darwin", lambda: False)
    monkeypatch.setattr(
        reconciliation.os, "kill", lambda _pid, _signal: (_ for _ in ()).throw(PermissionError())
    )
    monkeypatch.setattr(reconciliation.Path, "exists", lambda _path: False)
    monkeypatch.setattr(
        reconciliation.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(probe_error),
    )

    assert reconciliation._process_start_identity(424242).state == "unknown"


def test_windows_process_identity_probes_do_not_spawn_posix_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows store construction must not launch POSIX process utilities."""

    monkeypatch.setattr(reconciliation.os, "name", "nt")

    def unexpected_probe(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("POSIX process probe was launched on Windows")

    monkeypatch.setattr(reconciliation.subprocess, "run", unexpected_probe)
    monkeypatch.setattr(reconciliation.os, "kill", unexpected_probe)

    assert reconciliation._current_boot_id() is None
    assert reconciliation._process_start_identity(424242) == ProcessIdentityEvidence("unknown")


def test_unavailable_identity_never_uses_wall_clock_lease_for_new_rows(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"

    def unavailable(_pid: int) -> ProcessIdentityEvidence:
        return ProcessIdentityEvidence("unknown")

    owner = ControlReconciliationStore(
        path,
        instance_id="owner",
        clock=clock,
        boot_id="boot-a",
        process_start_reader=unavailable,
    )
    request_id = "10101010-1010-4010-8010-101010101010"
    _begin(owner, request_id)
    owner.close()

    for jumped_time in (-1_000_000.0, 1_000_000.0):
        clock.value = jumped_time
        contender = ControlReconciliationStore(
            path,
            instance_id=f"contender-{jumped_time}",
            clock=clock,
            boot_id="boot-a",
            process_start_reader=unavailable,
        )
        assert contender.recover_abandoned_operations() == 0

    rebooted = ControlReconciliationStore(
        path,
        instance_id="rebooted",
        clock=clock,
        boot_id="boot-b",
        process_start_reader=unavailable,
    )
    assert rebooted.recover_abandoned_operations() == 1
    assert rebooted.operation_status(request_id, session_id="session-a")["state"] == (
        "indeterminate"
    )


def test_abandonment_reason_requires_explicit_evidence(tmp_path: Path) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    owner = ControlReconciliationStore(
        path,
        instance_id="owner",
        boot_id="boot-a",
        process_start_reader=lambda _pid: ProcessIdentityEvidence("live", "start-a"),
    )
    request_id = "11111111-1010-4010-8010-101010101010"
    _begin(owner, request_id)
    owner.close()
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM control_operations WHERE request_id=?", (request_id,)
        ).fetchone()
    assert row is not None

    unknown = ControlReconciliationStore(
        path,
        instance_id="unknown",
        boot_id="boot-a",
        process_start_reader=lambda _pid: ProcessIdentityEvidence("unknown"),
    )
    assert unknown._abandonment_reason(row, now=10**12) is None  # noqa: SLF001

    dead = ControlReconciliationStore(
        path,
        instance_id="dead",
        boot_id="boot-a",
        process_start_reader=lambda _pid: ProcessIdentityEvidence("dead"),
    )
    assert dead._abandonment_reason(row, now=0) == "process_dead"  # noqa: SLF001


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
@pytest.mark.parametrize("crash", [False, True])
def test_store_inherited_across_fork_is_rejected_and_reconstructed(
    tmp_path: Path,
    crash: bool,
) -> None:
    context = multiprocessing.get_context("fork")
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    parent = ControlReconciliationStore(
        path,
        instance_id="fork-parent",
        lease_timeout_seconds=0.5,
        heartbeat_interval_seconds=0.05,
    )
    parent_request = "12121212-1212-4212-8212-121212121212"
    child_request = "13131313-1313-4313-8313-131313131313"
    _begin(parent, parent_request)
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_fork_inherited_store,
        args=(parent, str(path), child_request, send, crash),
    )
    process.start()
    send.close()
    assert receive.poll(10.0)
    assert receive.recv() == (True, False)
    process.join(10.0)
    assert process.exitcode == (17 if crash else 0)

    if crash:
        assert parent.recover_abandoned_operations() == 1
        expected = "indeterminate"
    else:
        expected = "succeeded"
    assert parent.operation_status(child_request, session_id="session-a")["state"] == expected
    assert parent.operation_status(parent_request, session_id="session-a")["state"] == "pending"
    assert parent._heartbeat_thread is not None  # noqa: SLF001
    assert parent._heartbeat_thread.is_alive()  # noqa: SLF001
    parent.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_uninitialized_inherited_store_rejects_without_filesystem_mutation(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    path = tmp_path / "absent" / "control" / "reconciliation-v4.sqlite3"
    inherited = ControlReconciliationStore(path, instance_id="fork-parent")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_fork_reject_inherited_only,
        args=(inherited, "14141414-1414-4414-8414-141414141414", send),
    )
    process.start()
    send.close()
    assert receive.poll(10.0)
    assert receive.recv() == "rejected"
    process.join(10.0)
    assert process.exitcode == 0
    assert not path.parent.parent.exists()


def test_hardlinked_database_and_ancestor_symlink_are_rejected_without_alias_mutation(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim.sqlite3"
    victim.write_bytes(b"outside victim")
    victim.chmod(0o640)
    victim_before = (victim.read_bytes(), victim.stat().st_mode)
    control = tmp_path / "control"
    control.mkdir()
    os.link(victim, control / "reconciliation-v4.sqlite3")

    with pytest.raises(ControlReconciliationUnavailableError, match="unsafe"):
        ControlReconciliationStore(control / "reconciliation-v4.sqlite3").prepare_for_operation()
    assert (victim.read_bytes(), victim.stat().st_mode) == victim_before

    alias_root = tmp_path / "alias"
    alias_root.symlink_to(control, target_is_directory=True)
    with pytest.raises(ControlReconciliationUnavailableError, match="unsafe"):
        ControlReconciliationStore(alias_root / "reconciliation-v4.sqlite3").operation_status(
            "missing", session_id="session-a"
        )
    assert (victim.read_bytes(), victim.stat().st_mode) == victim_before


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_hardlinked_sidecars_are_rejected_without_victim_mutation(
    tmp_path: Path,
    suffix: str,
) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    request_id = "13131313-1313-4313-8313-131313131313"
    store = ControlReconciliationStore(path)
    _begin(store, request_id)
    store.finish_operation(
        request_id,
        session_id="session-a",
        state="succeeded",
        result={"state": "approved"},
    )
    store.close()
    sidecar = Path(f"{path}{suffix}")
    sidecar.unlink(missing_ok=True)
    victim = tmp_path / f"victim{suffix}"
    victim.write_bytes(b"outside sidecar victim")
    victim.chmod(0o640)
    before = (victim.read_bytes(), victim.stat().st_mode)
    os.link(victim, sidecar)

    with pytest.raises(ControlReconciliationUnavailableError, match="unsafe"):
        ControlReconciliationStore(path).operation_status(
            request_id,
            session_id="session-a",
        )

    assert (victim.read_bytes(), victim.stat().st_mode) == before


def test_bounded_open_retry_uses_one_small_total_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic = _Clock(0.0)
    attempts = 0

    def locked_connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("database is locked")

    def advance(duration: float) -> None:
        monotonic.value += duration

    monkeypatch.setattr(sqlite3, "connect", locked_connect)
    store = ControlReconciliationStore(
        tmp_path / "control" / "reconciliation-v4.sqlite3",
        monotonic_clock=monotonic,
        retry_sleep=advance,
        open_retry_seconds=0.05,
    )

    with pytest.raises(ControlReconciliationUnavailableError):
        store.prepare_for_operation()
    assert monotonic.value <= 0.05
    assert attempts <= 6


def test_valid_wal_live_read_preserves_sources_and_secures_created_sidecar(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    path.parent.mkdir()
    request_id = "03030303-0303-4303-8303-030303030303"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        """
        CREATE TABLE control_operations (
            request_id TEXT PRIMARY KEY,
            session_digest TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            contract_id TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            state TEXT NOT NULL,
            owner_instance TEXT NOT NULL,
            result_json TEXT,
            result_digest TEXT,
            record_refs_json TEXT NOT NULL,
            safe_error_code TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO control_operations VALUES (
            ?, ?, 'profile.change.approve',
            'tobkiri.host.control-presentation.v4', ?, 'pending',
            'process-a', NULL, NULL, '[]', NULL, 1.0, 1.0
        )
        """,
        (
            request_id,
            ControlReconciliationStore.session_digest("session-a"),
            canonical_digest({"request_id": request_id}),
        ),
    )
    connection.commit()
    database_bytes = path.read_bytes()
    wal_bytes = Path(f"{path}-wal").read_bytes()
    connection.close()
    path.write_bytes(database_bytes)
    Path(f"{path}-wal").write_bytes(wal_bytes)
    Path(f"{path}-shm").unlink(missing_ok=True)
    before_database = path.read_bytes()
    before_wal = Path(f"{path}-wal").read_bytes()

    status = ControlReconciliationStore(path).operation_status(
        request_id,
        session_id="session-a",
    )

    assert status["state"] == "pending"
    assert path.read_bytes() == before_database
    assert Path(f"{path}-wal").read_bytes() == before_wal
    shm_metadata = Path(f"{path}-shm").stat()
    assert stat.S_ISREG(shm_metadata.st_mode)
    assert shm_metadata.st_nlink == 1
    assert shm_metadata.st_uid == os.getuid()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_immutable_status_read_in_read_only_directory_preserves_tree(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    request_id = "04040404-0404-4404-8404-040404040404"
    writer = ControlReconciliationStore(path, instance_id="process-a")
    _begin(writer, request_id)
    writer.finish_operation(
        request_id,
        session_id="session-a",
        state="succeeded",
        result={"state": "approved"},
    )
    writer.close()
    before = _tree_bytes(tmp_path)
    path.parent.chmod(0o500)
    try:
        status = ControlReconciliationStore(path).operation_status(
            request_id,
            session_id="session-a",
        )
    finally:
        path.parent.chmod(0o700)

    assert status["state"] == "succeeded"
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("kind", ["corrupt", "symlink"])
def test_lazy_initialization_fails_closed_for_unsafe_journal_path(
    tmp_path: Path,
    kind: str,
) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    path.parent.mkdir()
    if kind == "corrupt":
        path.write_bytes(b"not a sqlite database")
    else:
        target = tmp_path / "outside.sqlite3"
        target.write_bytes(b"")
        path.symlink_to(target)
    before = path.read_bytes()
    store = ControlReconciliationStore(path, instance_id="process-a")

    with pytest.raises(ControlReconciliationError, match="journal"):
        store.prepare_for_operation()

    assert path.read_bytes() == before


def test_blocked_parent_path_is_normalized_to_reconciliation_error(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_bytes(b"not a directory")
    store = ControlReconciliationStore(blocked / "reconciliation-v4.sqlite3")

    with pytest.raises(ControlReconciliationError, match="path"):
        store.prepare_for_operation()


@pytest.mark.parametrize("kind", ["corrupt", "symlink", "permission"])
def test_existing_read_failures_are_normalized_and_immutable(
    tmp_path: Path,
    kind: str,
) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    path.parent.mkdir()
    if kind == "corrupt":
        path.write_bytes(b"not a sqlite database")
    elif kind == "symlink":
        target = tmp_path / "outside.sqlite3"
        target.write_bytes(b"outside")
        path.symlink_to(target)
    else:
        path.write_bytes(b"unreadable")
        path.chmod(0)
    before = None if kind == "permission" else _tree_bytes(tmp_path)
    try:
        with pytest.raises(ControlReconciliationError, match="journal"):
            ControlReconciliationStore(path).operation_status(
                "05050505-0505-4505-8505-050505050505",
                session_id="session-a",
            )
    finally:
        if kind == "permission":
            path.chmod(0o600)

    if before is not None:
        assert _tree_bytes(tmp_path) == before
    else:
        assert path.read_bytes() == b"unreadable"


def test_operation_journal_reconciles_terminal_results_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control" / "reconciliation-v4.sqlite3"
    first = ControlReconciliationStore(path, instance_id="process-a")
    request_id = "11111111-1111-4111-8111-111111111111"
    pending, created = _begin(first, request_id)
    assert created is True
    assert pending["state"] == "pending"

    result = {
        "state": "approved",
        "approval_id": "approval.profile-change.test",
        "approval_digest": "sha256:" + "1" * 64,
    }
    succeeded = first.finish_operation(
        request_id,
        session_id="session-a",
        state="succeeded",
        result=result,
        record_refs=[
            {
                "record_id": "approval.profile-change.test",
                "record_digest": "sha256:" + "1" * 64,
            }
        ],
    )
    restarted = ControlReconciliationStore(path, instance_id="process-b")
    status = restarted.operation_status(request_id, session_id="session-a")
    replay, created = _begin(restarted, request_id)

    assert created is False
    assert replay == status == succeeded
    assert status["state"] == "succeeded"
    assert status["result"] == result
    assert status["result_digest"] == canonical_digest(result)
    assert status["record_refs"][0]["record_id"] == result["approval_id"]


def test_operation_journal_marks_only_abandoned_pending_work_indeterminate(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    path = tmp_path / "reconciliation-v4.sqlite3"
    original = ControlReconciliationStore(
        path,
        instance_id="process-a",
        lease_timeout_seconds=10.0,
        heartbeat_interval_seconds=1.0,
        clock=clock,
    )
    pending_id = "22222222-2222-4222-8222-222222222222"
    failed_id = "33333333-3333-4333-8333-333333333333"
    _begin(original, pending_id)
    _begin(original, failed_id)
    original.finish_operation(
        failed_id,
        session_id="session-a",
        state="failed",
        result={"state": "error", "code": "UNAPPROVED"},
        safe_error_code="UNAPPROVED",
    )
    original.close()
    clock.value += 11.0

    restarted = ControlReconciliationStore(
        path,
        instance_id="process-b",
        lease_timeout_seconds=10.0,
        heartbeat_interval_seconds=1.0,
        clock=clock,
        process_start_reader=lambda _pid: ProcessIdentityEvidence("dead"),
    )
    assert restarted.recover_abandoned_operations() == 1
    pending = restarted.operation_status(pending_id, session_id="session-a")
    failed = restarted.operation_status(failed_id, session_id="session-a")

    assert pending["state"] == "indeterminate"
    assert pending["safe_error_code"] == "PROCESS_RESTART"
    assert failed["state"] == "failed"
    assert failed["safe_error_code"] == "UNAPPROVED"
    assert (
        ControlReconciliationStore(path).operation_status(pending_id, session_id="session-a")
        == pending
    )


def test_operation_status_rejects_unknown_cross_session_and_tampered_replay(
    tmp_path: Path,
) -> None:
    store = ControlReconciliationStore(tmp_path / "reconciliation-v4.sqlite3")
    request_id = "44444444-4444-4444-8444-444444444444"
    digest = canonical_digest({"payload": "exact"})
    _begin(store, request_id, request_digest=digest)

    with pytest.raises(ControlReconciliationError, match="unknown"):
        store.operation_status(
            "55555555-5555-4555-8555-555555555555",
            session_id="session-a",
        )
    with pytest.raises(ControlReconciliationError, match="another session"):
        store.operation_status(request_id, session_id="session-b")
    with pytest.raises(ControlReconciliationError, match="binding changed"):
        _begin(
            store,
            request_id,
            request_digest=canonical_digest({"payload": "tampered"}),
        )

    store.finish_operation(
        request_id,
        session_id="session-a",
        state="succeeded",
        result={"state": "approved"},
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE control_operations SET result_json=? WHERE request_id=?",
            ('{"state":"forged"}', request_id),
        )
    with pytest.raises(ControlReconciliationError, match="digest changed"):
        store.operation_status(request_id, session_id="session-a")
