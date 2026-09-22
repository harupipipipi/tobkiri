"""Process ownership and filesystem identity tests for AuthorityStore."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import threading
from typing import Any

import pytest

import core_runtime.process_identity as process_identity
import core_runtime.secure_sqlite_path as secure_paths
from core_runtime.authority.v4_store import AuthorityStore, AuthorityStoreError
from core_runtime.process_identity import ProcessIdentityEvidence


class _StaticRows:
    def __init__(self, rows: list[tuple[int, str, str]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[int, str, str]]:
        return self._rows


class _MisreportedPathConnection(sqlite3.Connection):
    reported_path: Path

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql.strip().upper() == "PRAGMA DATABASE_LIST":
            return _StaticRows([(0, "main", str(self.reported_path))])
        return super().execute(sql, *args, **kwargs)


class _FakeWindowsProcessAPI:
    def __init__(self, creation_time: int | None = 0x123456789ABCDEF0) -> None:
        self.creation_time = creation_time
        self.opened: list[int] = []
        self.closed: list[int] = []
        self.open_failure = False
        self.open_error: BaseException | None = None
        self.close_failure = False

    def open_process(self, process_id: int) -> int | None:
        self.opened.append(process_id)
        if self.open_error is not None:
            raise self.open_error
        return None if self.open_failure else 73

    def process_creation_time(self, handle: int) -> int | None:
        assert handle == 73
        return self.creation_time

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)
        if self.close_failure:
            raise OSError("simulated CloseHandle failure")


def _exercise_inherited_store(
    inherited: AuthorityStore,
    path_value: str,
    connection: object,
) -> None:
    rejected: list[str] = []
    operations = {
        "read": lambda: inherited.security_epoch,
        "mutation": lambda: inherited.advance_security_epoch("fork-child"),
        "validation": lambda: inherited.put_records_atomically([]),
        "lease": lambda: inherited.get_lease("missing"),
    }
    for name, operation in operations.items():
        try:
            operation()
        except AuthorityStoreError:
            rejected.append(name)
    fresh = AuthorityStore(Path(path_value))
    fresh_epoch = fresh.security_epoch
    fresh.close()
    connection.send((rejected, fresh_epoch))  # type: ignore[attr-defined]
    connection.close()  # type: ignore[attr-defined]


def _race_authority_construction(
    path_value: str,
    barrier: object,
    results: object,
) -> None:
    try:
        barrier.wait(timeout=15)  # type: ignore[attr-defined]
        for _ in range(10):
            with AuthorityStore(Path(path_value)) as store:
                assert store.security_epoch == 1
        results.put(("ok", ""))  # type: ignore[attr-defined]
    except BaseException as exc:
        results.put(("error", repr(exc)))  # type: ignore[attr-defined]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_child_rejects_all_inherited_authority_and_reconstructs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority" / "authority.sqlite3"
    parent = AuthorityStore(path)
    original_epoch = parent.security_epoch
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_exercise_inherited_store,
        args=(parent, str(path), send),
    )
    process.start()
    send.close()
    assert receive.poll(10.0)
    rejected, fresh_epoch = receive.recv()
    process.join(10.0)

    assert process.exitcode == 0
    assert rejected == ["read", "mutation", "validation", "lease"]
    assert fresh_epoch == original_epoch
    assert parent.security_epoch == original_epoch
    assert parent.advance_security_epoch("parent-continues") == original_epoch + 1
    parent.close()


def test_process_identity_unavailable_fails_closed_without_marking_dead(
    tmp_path: Path,
) -> None:
    state = {"available": True}

    def identity_reader(_process_id: int) -> ProcessIdentityEvidence:
        if state["available"]:
            return ProcessIdentityEvidence("live", "stable-start")
        return ProcessIdentityEvidence("unknown")

    path = tmp_path / "authority.sqlite3"
    store = AuthorityStore(path, process_start_reader=identity_reader)
    state["available"] = False
    with pytest.raises(AuthorityStoreError, match="identity"):
        _ = store.security_epoch
    state["available"] = True
    assert store.security_epoch == 1
    store.close()

    with pytest.raises(AuthorityStoreError, match="identity"):
        AuthorityStore(
            tmp_path / "unavailable.sqlite3",
            process_start_reader=lambda _pid: ProcessIdentityEvidence("unknown"),
        )


def test_windows_current_process_identity_uses_stable_filetime_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeWindowsProcessAPI()
    monkeypatch.setattr(process_identity, "_is_windows", lambda: True)
    monkeypatch.setattr(process_identity, "_load_windows_process_api", lambda: adapter)
    monkeypatch.setattr(
        process_identity.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Windows identity launched a subprocess"),
    )

    first = process_identity.process_start_identity(os.getpid())
    second = process_identity.process_start_identity(os.getpid())

    assert (
        first
        == second
        == ProcessIdentityEvidence(
            "live",
            f"windows:{os.getpid()}:123456789abcdef0",
        )
    )
    other = process_identity.process_start_identity(os.getpid() + 1)
    assert other == ProcessIdentityEvidence(
        "live",
        f"windows:{os.getpid() + 1}:123456789abcdef0",
    )
    assert adapter.opened == [os.getpid(), os.getpid(), os.getpid() + 1]
    assert adapter.closed == [73, 73, 73]


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (ProcessLookupError("gone"), "dead"),
        (PermissionError("denied"), "unknown"),
        (OSError("api failure"), "unknown"),
    ],
)
def test_windows_other_pid_distinguishes_absence_from_query_failure(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    state: str,
) -> None:
    adapter = _FakeWindowsProcessAPI()
    adapter.open_error = error
    monkeypatch.setattr(process_identity, "_is_windows", lambda: True)
    monkeypatch.setattr(process_identity, "_load_windows_process_api", lambda: adapter)

    assert process_identity.process_start_identity(424242).state == state
    assert adapter.closed == []


@pytest.mark.parametrize("failure", ["unavailable", "open", "times", "close"])
def test_windows_process_identity_api_failures_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    adapter = _FakeWindowsProcessAPI()
    if failure == "open":
        adapter.open_failure = True
    elif failure == "times":
        adapter.creation_time = None
    elif failure == "close":
        adapter.close_failure = True
    monkeypatch.setattr(process_identity, "_is_windows", lambda: True)
    monkeypatch.setattr(
        process_identity,
        "_load_windows_process_api",
        lambda: None if failure == "unavailable" else adapter,
    )

    assert process_identity.process_start_identity(os.getpid()).state == "unknown"
    if failure in {"times", "close"}:
        assert adapter.closed == [73]


def test_windows_creation_mismatch_rejects_inherited_store_and_new_store_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeWindowsProcessAPI(creation_time=100)
    monkeypatch.setattr(process_identity, "_is_windows", lambda: True)
    monkeypatch.setattr(process_identity, "_load_windows_process_api", lambda: adapter)
    path = tmp_path / "windows-authority.sqlite3"
    store = AuthorityStore(path)
    assert store.security_epoch == 1

    adapter.creation_time = 101
    operations = (
        lambda: store.security_epoch,
        lambda: store.advance_security_epoch("pid-reuse"),
        lambda: store.get_lease("missing"),
        store.audit_events,
    )
    for operation in operations:
        with pytest.raises(AuthorityStoreError, match="identity"):
            operation()

    fresh = AuthorityStore(path)
    assert fresh.security_epoch == 1
    fresh.close()
    adapter.creation_time = 100
    assert store.security_epoch == 1
    store.close()


def test_windows_authority_construction_fails_when_creation_identity_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_identity, "_is_windows", lambda: True)
    monkeypatch.setattr(process_identity, "_load_windows_process_api", lambda: None)

    with pytest.raises(AuthorityStoreError, match="identity is unavailable"):
        AuthorityStore(tmp_path / "unavailable-windows.sqlite3")


@pytest.mark.parametrize("target", ["database", "key", "guard", "-wal", "-shm"])
def test_authority_files_reject_hardlinks_without_mutating_victim(
    tmp_path: Path,
    target: str,
) -> None:
    path = tmp_path / "authority" / "authority.sqlite3"
    store = AuthorityStore(path)
    key_path = path.with_suffix(".key")
    victim = tmp_path / f"victim-{target.replace('-', '')}"

    def construct_store() -> AuthorityStore:
        return AuthorityStore(path)

    def read_store() -> int:
        return store.security_epoch

    if target == "database":
        store.close()
        path.unlink()
        victim.write_bytes(b"outside database victim")
        os.link(victim, path)
        operation = construct_store
    elif target == "key":
        store.close()
        victim.write_bytes(key_path.read_bytes())
        key_path.unlink()
        os.link(victim, key_path)
        operation = construct_store
    elif target == "guard":
        store.close()
        guard_path = path.with_name(f".{path.name}.lifecycle.lock")
        victim.write_bytes(guard_path.read_bytes())
        guard_path.unlink()
        os.link(victim, guard_path)
        operation = construct_store
    else:
        victim.write_bytes(b"outside sidecar victim")
        os.link(victim, Path(f"{path}{target}"))
        operation = read_store

    before = victim.read_bytes()
    with pytest.raises(AuthorityStoreError, match="unsafe"):
        operation()
    assert victim.read_bytes() == before


def test_authority_rejects_non_regular_and_symlink_ancestor_paths(
    tmp_path: Path,
) -> None:
    directory_database = tmp_path / "directory.sqlite3"
    directory_database.mkdir()
    with pytest.raises(AuthorityStoreError, match="unsafe"):
        AuthorityStore(directory_database)

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(AuthorityStoreError, match="unsafe"):
        AuthorityStore(alias / "authority.sqlite3")


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="requires POSIX ownership")
def test_authority_rejects_file_not_owned_by_current_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AuthorityStore(tmp_path / "authority.sqlite3")
    current_user = os.getuid()
    monkeypatch.setattr(secure_paths.os, "getuid", lambda: current_user + 1)

    with pytest.raises(AuthorityStoreError, match="unsafe"):
        _ = store.security_epoch

    monkeypatch.undo()
    assert store.security_epoch == 1
    store.close()


def test_authority_rejects_ancestor_replacement_after_construction(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "authority"
    path = parent / "authority.sqlite3"
    store = AuthorityStore(path)
    moved = tmp_path / "authority-original"
    parent.rename(moved)
    parent.mkdir()

    with pytest.raises(AuthorityStoreError, match="unsafe"):
        _ = store.security_epoch

    parent.rmdir()
    moved.rename(parent)
    assert store.security_epoch == 1
    store.close()


def test_authority_detects_database_replacement_during_sqlite_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "authority" / "authority.sqlite3"
    store = AuthorityStore(path)
    original_connect = sqlite3.connect
    original = path.with_name("authority-original.sqlite3")
    replaced = False

    def replacing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal replaced
        if not replaced and str(args[0]) == str(path):
            replaced = True
            path.rename(original)
            shutil.copyfile(original, path)
            path.chmod(0o600)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", replacing_connect)
    with pytest.raises(AuthorityStoreError, match="unsafe"):
        _ = store.security_epoch

    path.unlink()
    original.rename(path)
    monkeypatch.setattr(sqlite3, "connect", original_connect)
    assert store.security_epoch == 1
    store.close()


def test_authority_rejects_connector_opening_a_different_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority" / "authority.sqlite3"
    wrong_path = tmp_path / "wrong.sqlite3"

    def wrong_connector(*_args: object, **kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(wrong_path, **kwargs)

    with pytest.raises(AuthorityStoreError, match="handle identity"):
        AuthorityStore(path, connection_connector=wrong_connector)

    assert path.read_bytes() == b""
    assert wrong_path.read_bytes() == b""


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity invariant")
def test_authority_rejects_mismatched_handle_when_reported_path_is_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority" / "authority.sqlite3"
    store = AuthorityStore(path)
    store.close()
    wrong_path = tmp_path / "wrong.sqlite3"
    keeper = sqlite3.connect(path, isolation_level=None)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("BEGIN IMMEDIATE")
    _MisreportedPathConnection.reported_path = path

    def mismatched_connector(*_args: object, **kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(
            wrong_path,
            factory=_MisreportedPathConnection,
            **kwargs,
        )

    try:
        with pytest.raises(AuthorityStoreError, match="handle identity"):
            AuthorityStore(path, connection_connector=mismatched_connector)
    finally:
        keeper.rollback()
        keeper.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity invariant")
def test_authority_ignores_unrelated_file_opened_concurrently_with_sqlite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority" / "authority.sqlite3"
    unrelated_path = tmp_path / "unrelated.txt"
    unrelated_path.write_text("not SQLite persistence", encoding="utf-8")
    start_open = threading.Event()
    unrelated_opened = threading.Event()
    opened_handle: list[Any] = []

    def open_unrelated_file() -> None:
        assert start_open.wait(5.0)
        handle = unrelated_path.open("rb")
        opened_handle.append(handle)
        unrelated_opened.set()

    opener = threading.Thread(target=open_unrelated_file)
    opener.start()
    first_connection = True

    def concurrent_connector(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal first_connection
        if first_connection:
            first_connection = False
            start_open.set()
            assert unrelated_opened.wait(5.0)
        return sqlite3.connect(*args, **kwargs)

    try:
        store = AuthorityStore(path, connection_connector=concurrent_connector)
        assert store.security_epoch == 1
        store.close()
    finally:
        start_open.set()
        opener.join(5.0)
        for handle in opened_handle:
            handle.close()

    assert not opener.is_alive()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity invariant")
def test_authority_operations_survive_unrelated_descriptor_churn(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority.sqlite3"
    unrelated_path = tmp_path / "unrelated.txt"
    unrelated_path.write_text("descriptor churn", encoding="utf-8")
    stop = threading.Event()
    started = threading.Event()

    def churn_unrelated_file() -> None:
        started.set()
        while not stop.is_set():
            with unrelated_path.open("rb") as handle:
                handle.read(1)

    opener = threading.Thread(target=churn_unrelated_file)
    opener.start()
    assert started.wait(5.0)
    try:
        store = AuthorityStore(path)
        for _ in range(100):
            assert store.security_epoch == 1
        store.close()
    finally:
        stop.set()
        opener.join(5.0)

    assert not opener.is_alive()


def test_spawned_authority_construction_serializes_sqlite_sidecar_lifecycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority.sqlite3"
    AuthorityStore(path).close()
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    results = context.Queue()
    processes = [
        context.Process(
            target=_race_authority_construction,
            args=(str(path), barrier, results),
        )
        for _ in range(4)
    ]

    for process in processes:
        process.start()
    outcomes = [results.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(30)

    assert outcomes == [("ok", "")] * 4
    assert [process.exitcode for process in processes] == [0] * 4


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity invariant")
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_authority_rejects_live_sidecar_replacement_after_pinning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    path = tmp_path / "authority.sqlite3"
    store = AuthorityStore(path)
    original_validate = AuthorityStore._validate_opened_database_files
    armed = True
    replaced = path.with_name(f"original{suffix}")

    def replace_sidecar(
        current: AuthorityStore,
        pinned: dict[str, object],
    ) -> None:
        nonlocal armed
        if armed and suffix in pinned:
            armed = False
            sidecar = Path(f"{path}{suffix}")
            sidecar.rename(replaced)
            shutil.copyfile(replaced, sidecar)
            sidecar.chmod(0o600)
        original_validate(current, pinned)  # type: ignore[arg-type]

    monkeypatch.setattr(
        AuthorityStore,
        "_validate_opened_database_files",
        replace_sidecar,
    )
    with pytest.raises(AuthorityStoreError, match="handle identity"):
        _ = store.security_epoch
    store.close()

    assert replaced.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity invariant")
@pytest.mark.parametrize("failure", ["missing", "permission"])
def test_authority_denies_descriptor_identity_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def unavailable() -> dict[int, secure_paths.FileIdentity]:
        error: OSError
        if failure == "missing":
            error = FileNotFoundError("descriptor directory is missing")
        else:
            error = PermissionError("descriptor directory is unreadable")
        raise AuthorityStoreError("authority database handle identity is unavailable") from error

    monkeypatch.setattr(
        AuthorityStore,
        "_open_descriptor_identities",
        staticmethod(unavailable),
    )
    with pytest.raises(AuthorityStoreError, match="identity is unavailable"):
        AuthorityStore(tmp_path / f"{failure}.sqlite3")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission invariant")
def test_authority_rejects_broad_sidecar_permissions_before_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "authority.sqlite3"
    original_pin = AuthorityStore._pin_opened_database_files

    def broaden_wal(
        store: AuthorityStore,
        connection: sqlite3.Connection,
        descriptors_before: dict[int, secure_paths.FileIdentity],
        suffixes: tuple[str, ...] = ("", "-wal", "-shm"),
    ) -> dict[str, object]:
        pinned = original_pin(store, connection, descriptors_before, suffixes)
        if "-wal" in suffixes:
            Path(f"{path}-wal").chmod(0o644)
        return pinned

    monkeypatch.setattr(AuthorityStore, "_pin_opened_database_files", broaden_wal)
    with pytest.raises(AuthorityStoreError, match="permissions"):
        AuthorityStore(path)
