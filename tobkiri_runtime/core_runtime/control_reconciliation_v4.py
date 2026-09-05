"""Durable Profile ceremony and frontend mutation reconciliation state."""

from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

if os.name != "nt":
    import fcntl

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.errors import CanonicalizationError
from tobkiri_protocol.platform_paths import canonical_platform_path

from .process_identity import (
    ProcessIdentityEvidence,
    process_start_identity as _process_start_identity,
)
from .secure_sqlite_path import (
    FileIdentity,
    SecureParent,
    SecurePathError,
    secure_parent,
)


def _current_boot_id() -> str | None:
    """Return a stable local boot identity without creating filesystem state."""

    if os.name == "nt":
        # The fallback below is POSIX-specific.  In particular, do not launch
        # shell utilities while constructing an otherwise lazy store on
        # Windows; an unavailable process probe must remain unknown.
        return None
    linux_boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        if linux_boot_id.is_file():
            return linux_boot_id.read_text(encoding="ascii").strip()
        result = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if result.returncode == 0 and result.stdout.strip():
            return canonical_digest({"boot": result.stdout.strip()})
    except (OSError, subprocess.SubprocessError):
        pass
    return None


class ControlReconciliationError(RuntimeError):
    """Raised when durable control state is missing, stale, or inconsistent."""


class ControlReconciliationConflictError(ControlReconciliationError):
    """Raised when a request conflicts with durable reconciliation state."""


class ControlReconciliationUnavailableError(ControlReconciliationError):
    """Raised when durable reconciliation state is not safely available."""


class ControlReconciliationCapacityError(ControlReconciliationUnavailableError):
    """Raised when bounded durable replay state cannot accept a new request."""


class _ControlReconciliationSnapshotChanged(ControlReconciliationUnavailableError):
    """Internal signal that a concurrent writer invalidated a snapshot copy."""


class ControlReconciliationStore:
    """SQLite-backed exact-once state for Host control mutations."""

    def __init__(
        self,
        path: Path,
        *,
        instance_id: str = "",
        lease_timeout_seconds: float = 30.0,
        heartbeat_interval_seconds: float | None = None,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        retry_sleep: Callable[[float], None] = time.sleep,
        open_retry_seconds: float = 0.5,
        boot_id: str | None = None,
        process_start_reader: Callable[[int], ProcessIdentityEvidence] | None = None,
        max_operation_records: int = 100_000,
        terminal_retention_seconds: float = 24 * 60 * 60,
        default_session_ttl_seconds: float = 8 * 60 * 60,
        compaction_batch_size: int = 1_000,
        max_recovery_audit_records: int = 1_000,
        max_database_bytes: int = 256 * 1024 * 1024,
        max_operation_result_bytes: int = 1024 * 1024,
        max_ceremony_records: int = 10_000,
        max_ceremony_bytes: int | None = None,
        ceremony_retention_seconds: float = 60 * 60,
        operation_database_reserve_bytes: int | None = None,
        session_renewal_debounce_seconds: float = 30.0,
    ) -> None:
        if lease_timeout_seconds <= 0:
            raise ValueError("lease_timeout_seconds must be positive")
        heartbeat_interval = heartbeat_interval_seconds or min(5.0, lease_timeout_seconds / 3.0)
        if heartbeat_interval <= 0 or heartbeat_interval >= lease_timeout_seconds:
            raise ValueError("heartbeat interval must be positive and shorter than lease")
        if open_retry_seconds <= 0 or open_retry_seconds > 5.0:
            raise ValueError("open_retry_seconds must be positive and bounded")
        if max_operation_records <= 0:
            raise ValueError("max_operation_records must be positive")
        if terminal_retention_seconds < 0 or default_session_ttl_seconds <= 0:
            raise ValueError("replay retention values are invalid")
        if compaction_batch_size <= 0 or max_recovery_audit_records <= 0:
            raise ValueError("journal compaction limits must be positive")
        if max_database_bytes < 1024 * 1024:
            raise ValueError("max_database_bytes must be at least one MiB")
        if max_operation_result_bytes <= 0:
            raise ValueError("max_operation_result_bytes must be positive")
        if max_ceremony_records <= 0 or ceremony_retention_seconds < 0:
            raise ValueError("ceremony retention limits are invalid")
        if session_renewal_debounce_seconds < 0:
            raise ValueError("session renewal debounce must not be negative")
        operation_reserve = (
            max(256 * 1024, max_database_bytes // 4)
            if operation_database_reserve_bytes is None
            else operation_database_reserve_bytes
        )
        if operation_reserve <= 0 or operation_reserve >= max_database_bytes:
            raise ValueError("operation database reserve is invalid")
        ceremony_bytes = (
            min(64 * 1024 * 1024, max_database_bytes - operation_reserve)
            if max_ceremony_bytes is None
            else max_ceremony_bytes
        )
        if ceremony_bytes <= 0 or ceremony_bytes > max_database_bytes - operation_reserve:
            raise ValueError("ceremony byte capacity exceeds the unreserved database space")
        self.path = canonical_platform_path(Path(path))
        self.instance_id = instance_id or f"store-{secrets.token_hex(16)}"
        self._process_id = os.getpid()
        self._process_token = secrets.token_hex(32)
        self._lease_timeout_seconds = lease_timeout_seconds
        self._heartbeat_interval_seconds = heartbeat_interval
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._retry_sleep = retry_sleep
        self._open_retry_seconds = open_retry_seconds
        self._boot_id = boot_id or _current_boot_id() or ""
        self._process_start_reader = process_start_reader or _process_start_identity
        self._max_operation_records = max_operation_records
        self._terminal_retention_seconds = terminal_retention_seconds
        self._default_session_ttl_seconds = default_session_ttl_seconds
        self._compaction_batch_size = compaction_batch_size
        self._max_recovery_audit_records = max_recovery_audit_records
        self._max_database_bytes = max_database_bytes
        self._max_operation_result_bytes = max_operation_result_bytes
        self._max_ceremony_records = max_ceremony_records
        self._max_ceremony_bytes = ceremony_bytes
        self._ceremony_retention_seconds = ceremony_retention_seconds
        self._operation_database_reserve_bytes = operation_reserve
        self._session_renewal_debounce_seconds = session_renewal_debounce_seconds
        self._session_renewal_cache: dict[str, float] = {}
        self._session_renewal_retry_after: dict[str, float] = {}
        self._session_renewal_lock = threading.Lock()
        process_evidence = self._process_start_reader(self._process_id)
        self._process_start = process_evidence.identity if process_evidence.state == "live" else ""
        self._initialization_lock = threading.RLock()
        self._initialized = False
        self._next_recovery_scan_at = 0.0
        self._heartbeat_lock = threading.RLock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._database_identity: FileIdentity | None = None
        self._parent_identity: FileIdentity | None = None
        self._lock_identity: FileIdentity | None = None

    def _prepare_parent_directory(self) -> None:
        """Create and pin the journal parent before taking the journal lock."""

        try:
            if self.path.is_symlink():
                raise ControlReconciliationUnavailableError(
                    "control journal path cannot be a symlink"
                )
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._secure_parent():
                pass
        except ControlReconciliationError:
            raise
        except OSError as error:
            raise ControlReconciliationUnavailableError(
                "control journal path is unavailable"
            ) from error

    def _prepare_path(self) -> None:
        try:
            self._prepare_parent_directory()
            with self._secure_parent() as parent:
                database_identity = parent.validate_open(
                    self.path.name,
                    required=False,
                    expected=self._database_identity,
                )
                if database_identity is None:
                    try:
                        database_identity = parent.create_empty_file(self.path.name)
                    except FileExistsError:
                        database_identity = parent.validate_open(
                            self.path.name,
                            required=True,
                        )
                if self._database_identity is None:
                    self._database_identity = database_identity
                for name in (
                    f"{self.path.name}-wal",
                    f"{self.path.name}-shm",
                ):
                    parent.validate_open(name, required=False)
        except ControlReconciliationError:
            raise
        except OSError as error:
            raise ControlReconciliationUnavailableError(
                "control journal path is unavailable"
            ) from error

    @contextmanager
    def _secure_parent(self) -> Iterator[SecureParent]:
        """Open the journal parent through the platform's secure path API."""

        try:
            with secure_parent(self.path) as parent:
                if self._parent_identity is not None and parent.identity != self._parent_identity:
                    raise SecurePathError("control journal parent identity changed")
                if self._parent_identity is None:
                    self._parent_identity = parent.identity
                yield parent
        except (OSError, SecurePathError) as error:
            if isinstance(error, FileNotFoundError) or isinstance(
                error.__cause__, FileNotFoundError
            ):
                raise ControlReconciliationUnavailableError(
                    "control journal is unavailable"
                ) from error
            raise ControlReconciliationUnavailableError(
                "control journal ancestor is unsafe"
            ) from error

    @staticmethod
    def _validate_owned_file(
        parent: SecureParent,
        name: str,
        *,
        required: bool,
    ) -> os.stat_result | None:
        try:
            return parent.stat_file(name, required=required)
        except SecurePathError:
            raise ControlReconciliationUnavailableError(
                "control journal file identity is unsafe"
            ) from None
        except OSError as error:
            raise ControlReconciliationUnavailableError("control journal is unavailable") from error

    def _secure_chmod_database(self) -> None:
        if os.name == "nt":
            return
        with self._secure_parent() as parent:
            identity = parent.validate_open(self.path.name, required=True)
            descriptor = parent.open_file(self.path.name, os.O_RDONLY)
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != os.getuid()
                ):
                    raise ControlReconciliationUnavailableError(
                        "control journal file identity is unsafe"
                    )
                if identity is None or identity != parent.validate_open(
                    self.path.name,
                    required=True,
                ):
                    raise ControlReconciliationUnavailableError(
                        "control journal file identity is unsafe"
                    )
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)

    @contextmanager
    def _journal_file_lock(self, *, deadline: float | None = None) -> Iterator[None]:
        """Serialize POSIX journal transactions using the pinned DB file."""

        if os.name == "nt":
            yield
            return
        try:
            with self._secure_parent() as parent:
                lock_name = f"{self.path.name}.lock"
                lock_identity = parent.validate_open(
                    lock_name,
                    required=False,
                    expected=self._lock_identity,
                )
                if lock_identity is None:
                    try:
                        lock_identity = parent.create_empty_file(lock_name)
                    except FileExistsError:
                        lock_identity = parent.validate_open(lock_name, required=True)
                if self._lock_identity is None:
                    self._lock_identity = lock_identity
                descriptor = parent.open_file(lock_name, os.O_RDWR)
                acquired = False
                try:
                    lock_deadline = (
                        self._monotonic_clock() + self._open_retry_seconds
                        if deadline is None
                        else deadline
                    )
                    while True:
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            acquired = True
                            break
                        except OSError as error:
                            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                                raise
                            remaining = lock_deadline - self._monotonic_clock()
                            if remaining <= 0:
                                raise ControlReconciliationUnavailableError(
                                    "control journal lock deadline exceeded"
                                ) from error
                            self._retry_sleep(min(0.01, remaining))
                    parent.validate_open(
                        lock_name,
                        required=True,
                        expected=self._lock_identity,
                    )
                    yield
                finally:
                    if acquired:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
        except ControlReconciliationError:
            raise
        except (OSError, SecurePathError) as error:
            raise ControlReconciliationUnavailableError(
                "control journal initialization lock is unavailable"
            ) from error

    def _validate_storage_files(self) -> None:
        """Validate the pinned database and any current SQLite sidecars."""

        try:
            with self._secure_parent() as parent:
                parent.validate_open(
                    self.path.name,
                    required=True,
                    expected=self._database_identity,
                )
                for suffix in ("-wal", "-shm"):
                    parent.validate_open(f"{self.path.name}{suffix}", required=False)
        except (OSError, SecurePathError) as error:
            raise ControlReconciliationUnavailableError(
                "control journal file identity is unsafe"
            ) from error

    def _open_connection(self, *, deadline: float | None = None) -> sqlite3.Connection:
        self._assert_current_process()
        open_deadline = (
            self._monotonic_clock() + self._open_retry_seconds if deadline is None else deadline
        )
        while True:
            connection: sqlite3.Connection | None = None
            remaining = open_deadline - self._monotonic_clock()
            if remaining <= 0:
                raise ControlReconciliationUnavailableError(
                    "control journal open deadline exceeded"
                )
            attempt_timeout = min(0.05, remaining)
            try:
                self._validate_storage_files()
                connection = sqlite3.connect(
                    str(self.path),
                    timeout=attempt_timeout,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                connection.execute(f"PRAGMA busy_timeout={max(1, int(attempt_timeout * 1000))}")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("PRAGMA foreign_keys=ON")
                if self._initialized:
                    self._configure_capacity(connection)
                self._validate_storage_files()
                return connection
            except ControlReconciliationError:
                if connection is not None:
                    connection.close()
                raise
            except sqlite3.OperationalError as error:
                if connection is not None:
                    connection.close()
                remaining = open_deadline - self._monotonic_clock()
                if "locked" not in str(error).lower() or remaining <= 0:
                    raise ControlReconciliationUnavailableError(
                        "control journal is unavailable"
                    ) from error
                self._retry_sleep(min(0.01, remaining))
            except (OSError, sqlite3.Error) as error:
                if connection is not None:
                    connection.close()
                raise ControlReconciliationUnavailableError(
                    "control journal is unavailable"
                ) from error

    def _configure_capacity(self, connection: sqlite3.Connection) -> None:
        """Apply byte limits only after first-open schema locking is established."""

        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        max_pages = max(1, self._max_database_bytes // page_size)
        applied_max_pages = int(
            connection.execute(f"PRAGMA max_page_count={max_pages}").fetchone()[0]
        )
        if applied_max_pages > max_pages:
            raise ControlReconciliationCapacityError(
                "control journal exceeds its configured byte capacity"
            )
        connection.execute(f"PRAGMA journal_size_limit={self._max_database_bytes}")

    def _assert_current_process(self) -> None:
        """Reject mutation through a store inherited from another process."""

        if os.getpid() != self._process_id:
            raise ControlReconciliationUnavailableError(
                "control reconciliation store cannot be used after fork"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield one connection and normalize every journal I/O failure."""

        self._initialize()
        connection: sqlite3.Connection | None = None
        deadline = self._monotonic_clock() + self._open_retry_seconds
        try:
            with self._journal_file_lock(deadline=deadline):
                connection = self._open_connection(deadline=deadline)
                try:
                    with connection:
                        yield connection
                finally:
                    connection.close()
                    connection = None
        except ControlReconciliationError:
            raise
        except sqlite3.OperationalError as error:
            if "full" in str(error).lower():
                raise ControlReconciliationCapacityError(
                    "control operation journal byte capacity is exhausted"
                ) from error
            raise ControlReconciliationUnavailableError(
                "control operation journal transaction failed"
            ) from error
        except (OSError, sqlite3.Error) as error:
            raise ControlReconciliationUnavailableError(
                "control operation journal transaction failed"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _connect_existing(self) -> Iterator[sqlite3.Connection]:
        """Read a stable private snapshot without touching source sidecars."""

        connection: sqlite3.Connection | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="tobkiri-control-read-") as temporary:
                snapshot = Path(temporary) / self.path.name
                self._copy_stable_snapshot(snapshot)
                connection = sqlite3.connect(
                    str(snapshot),
                    timeout=30.0,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("PRAGMA foreign_keys=ON")
                yield connection
        except ControlReconciliationError:
            raise
        except OSError as error:
            raise ControlReconciliationUnavailableError(
                "control journal path is unavailable"
            ) from error
        except sqlite3.Error as error:
            raise ControlReconciliationUnavailableError("control journal is unavailable") from error
        finally:
            if connection is not None:
                connection.close()

    def _copy_stable_snapshot(self, snapshot: Path) -> None:
        """Retry only concurrent snapshot changes within the bounded open deadline."""

        deadline = self._monotonic_clock() + self._open_retry_seconds
        while True:
            snapshot.unlink(missing_ok=True)
            Path(f"{snapshot}-wal").unlink(missing_ok=True)
            try:
                self._copy_immutable_snapshot(snapshot)
                return
            except _ControlReconciliationSnapshotChanged:
                remaining = deadline - self._monotonic_clock()
                if remaining <= 0:
                    raise
                self._retry_sleep(min(0.005, remaining))

    def _copy_immutable_snapshot(self, snapshot: Path) -> None:
        """Copy a stable database/WAL pair through no-follow file descriptors."""

        source_names = (self.path.name, f"{self.path.name}-wal")
        fingerprints: dict[str, tuple[int, int, int, int, int, int]] = {}
        try:
            with self._secure_parent() as parent:
                database_identity = parent.validate_open(
                    self.path.name,
                    required=True,
                    expected=self._database_identity,
                )
                if self._database_identity is None:
                    self._database_identity = database_identity
                for name in (*source_names, f"{self.path.name}-shm"):
                    metadata = self._validate_owned_file(
                        parent,
                        name,
                        required=name == self.path.name,
                    )
                    if metadata is not None and name in source_names:
                        fingerprints[name] = (
                            metadata.st_dev,
                            metadata.st_ino,
                            metadata.st_size,
                            metadata.st_mtime_ns,
                            metadata.st_nlink,
                            metadata.st_uid,
                        )
                for name, fingerprint in fingerprints.items():
                    descriptor = parent.open_file(name, os.O_RDONLY)
                    try:
                        opened = os.fstat(descriptor)
                        opened_fingerprint = (
                            opened.st_dev,
                            opened.st_ino,
                            opened.st_size,
                            opened.st_mtime_ns,
                            opened.st_nlink,
                            opened.st_uid,
                        )
                        if opened_fingerprint != fingerprint:
                            raise _ControlReconciliationSnapshotChanged(
                                "control journal changed during immutable read"
                            )
                        target = snapshot if name == self.path.name else Path(f"{snapshot}-wal")
                        with os.fdopen(os.dup(descriptor), "rb") as reader:
                            with target.open("wb") as writer:
                                shutil.copyfileobj(reader, writer)
                    finally:
                        os.close(descriptor)
                for name, fingerprint in fingerprints.items():
                    metadata = self._validate_owned_file(
                        parent,
                        name,
                        required=True,
                    )
                    assert metadata is not None
                    current = (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        metadata.st_nlink,
                        metadata.st_uid,
                    )
                    if current != fingerprint:
                        raise _ControlReconciliationSnapshotChanged(
                            "control journal changed during immutable read"
                        )
        except ControlReconciliationError:
            raise
        except SecurePathError as error:
            raise ControlReconciliationUnavailableError(
                "control journal file identity is unsafe"
            ) from error
        except OSError as error:
            raise ControlReconciliationUnavailableError(
                "control journal path is unavailable"
            ) from error

    def _read_existing_one(
        self,
        query: str,
        parameters: tuple[object, ...],
    ) -> sqlite3.Row | None:
        try:
            with self._connect_existing() as connection:
                return connection.execute(query, parameters).fetchone()
        except ControlReconciliationError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise ControlReconciliationUnavailableError("control journal read failed") from error

    @contextmanager
    def _connect_live_existing(self) -> Iterator[sqlite3.Connection]:
        """Yield a validated, read-only transaction against the live journal."""

        connection: sqlite3.Connection | None = None
        try:
            with self._secure_parent() as parent:
                database_identity = parent.validate_open(
                    self.path.name,
                    required=True,
                    expected=self._database_identity,
                )
                if self._database_identity is None:
                    self._database_identity = database_identity
            self._validate_storage_files()
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
                timeout=self._open_retry_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN")
            self._validate_storage_files()
            yield connection
            connection.execute("COMMIT")
            self._validate_storage_files()
        except ControlReconciliationError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise ControlReconciliationUnavailableError(
                "control journal live read failed"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    def _read_live_operation(self, request_id: str) -> sqlite3.Row | None:
        """Read one request through the primary-key index without snapshot copying."""

        with self._connect_live_existing() as connection:
            return connection.execute(
                "SELECT * FROM control_operations WHERE request_id = ?",
                (request_id,),
            ).fetchone()

    def _initialize(self) -> None:
        self._assert_current_process()
        if self._initialized:
            return
        with self._initialization_lock:
            if self._initialized:
                return
            self._prepare_parent_directory()
            deadline = self._monotonic_clock() + self._open_retry_seconds
            try:
                with self._journal_file_lock(deadline=deadline):
                    # WAL/SHM validation must be serialized with connection
                    # close, which can unlink those sidecars on POSIX.
                    self._prepare_path()
                    with self._open_connection(deadline=deadline) as connection:
                        connection.executescript(
                            """
                BEGIN EXCLUSIVE;
                CREATE TABLE IF NOT EXISTS profile_ceremonies (
                    candidate_id TEXT PRIMARY KEY,
                    candidate_digest TEXT NOT NULL UNIQUE,
                    session_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    expected_profile_revision TEXT NOT NULL,
                    expected_plan_digest TEXT NOT NULL,
                    profile_definition_digest TEXT NOT NULL,
                    profile_catalog_digest TEXT NOT NULL,
                    bundle_lock_digest TEXT NOT NULL,
                    authority_snapshot_digest TEXT NOT NULL,
                    security_epoch INTEGER NOT NULL,
                    review_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    approval_id TEXT UNIQUE,
                    approval_digest TEXT,
                    approval_decided_at REAL,
                    authority_record_json TEXT,
                    activation_json TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS profile_ceremony_state_idx
                    ON profile_ceremonies(state, expires_at);
                CREATE TABLE IF NOT EXISTS control_operations (
                    request_id TEXT PRIMARY KEY,
                    session_digest TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    owner_instance TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    owner_process_token TEXT NOT NULL,
                    owner_boot_id TEXT NOT NULL,
                    owner_process_start TEXT NOT NULL,
                    owner_proof_version INTEGER NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    result_json TEXT,
                    result_digest TEXT,
                    record_refs_json TEXT NOT NULL,
                    safe_error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS control_operation_state_idx
                    ON control_operations(state, updated_at);
                CREATE TABLE IF NOT EXISTS control_replay_sessions (
                    session_digest TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_recovery_audit (
                    recovery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recovered_at REAL NOT NULL,
                    recovered_by_instance TEXT NOT NULL,
                    recovered_by_process_token TEXT NOT NULL,
                    abandoned_owner_instance TEXT NOT NULL,
                    abandoned_owner_pid INTEGER NOT NULL,
                    abandoned_owner_process_token TEXT NOT NULL,
                    recovered_count INTEGER NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_journal_audit (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    compacted_operations INTEGER NOT NULL,
                    compacted_succeeded INTEGER NOT NULL,
                    compacted_failed INTEGER NOT NULL,
                    compacted_indeterminate INTEGER NOT NULL,
                    compacted_recovery_audits INTEGER NOT NULL,
                    compacted_ceremonies INTEGER NOT NULL DEFAULT 0,
                    first_compacted_at REAL,
                    last_compacted_at REAL
                );
                INSERT OR IGNORE INTO control_journal_audit(
                    singleton_id, compacted_operations, compacted_succeeded,
                    compacted_failed, compacted_indeterminate,
                    compacted_recovery_audits
                ) VALUES (1, 0, 0, 0, 0, 0);
                """
                        )
                        columns = {
                            str(row[1])
                            for row in connection.execute("PRAGMA table_info(control_operations)")
                        }
                        migrations = {
                            "owner_pid": "INTEGER NOT NULL DEFAULT 0",
                            "owner_process_token": "TEXT NOT NULL DEFAULT ''",
                            "owner_boot_id": "TEXT NOT NULL DEFAULT ''",
                            "owner_process_start": "TEXT NOT NULL DEFAULT ''",
                            "owner_proof_version": "INTEGER NOT NULL DEFAULT 0",
                            "lease_expires_at": "REAL NOT NULL DEFAULT 0",
                        }
                        for column, declaration in migrations.items():
                            if column not in columns:
                                connection.execute(
                                    "ALTER TABLE control_operations ADD COLUMN "
                                    f"{column} {declaration}"
                                )
                        audit_columns = {
                            str(row[1])
                            for row in connection.execute(
                                "PRAGMA table_info(control_journal_audit)"
                            )
                        }
                        if "compacted_ceremonies" not in audit_columns:
                            connection.execute(
                                "ALTER TABLE control_journal_audit ADD COLUMN "
                                "compacted_ceremonies INTEGER NOT NULL DEFAULT 0"
                            )
                        connection.execute(
                            """
                        INSERT OR IGNORE INTO control_replay_sessions(
                            session_digest, expires_at
                        )
                        SELECT session_digest, ? FROM control_operations
                        GROUP BY session_digest
                        """,
                            (self._clock() + self._default_session_ttl_seconds,),
                        )
                        self._configure_capacity(connection)
                        connection.commit()
                    self._secure_chmod_database()
                    self._prepare_path()
                    connection.close()
            except (OSError, sqlite3.Error) as error:
                raise ControlReconciliationUnavailableError(
                    "control journal initialization failed"
                ) from error
            self._initialized = True

    def prepare_for_operation(self) -> None:
        """Initialize and recover only provably expired operation leases."""

        if self._initialized and self._monotonic_clock() < self._next_recovery_scan_at:
            return
        with self._initialization_lock:
            if self._initialized and self._monotonic_clock() < self._next_recovery_scan_at:
                return
            self._initialize()
            try:
                self.recover_abandoned_operations()
            except ControlReconciliationError:
                self._next_recovery_scan_at = self._monotonic_clock() + min(
                    1.0, self._heartbeat_interval_seconds
                )
                raise
            self._next_recovery_scan_at = (
                self._monotonic_clock() + self._heartbeat_interval_seconds
            )

    def close(self) -> None:
        """Stop this store's lease heartbeat without altering durable state."""

        with self._heartbeat_lock:
            stop_event = self._heartbeat_stop
            thread = self._heartbeat_thread
            stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._heartbeat_join_timeout())
        with self._heartbeat_lock:
            if thread is self._heartbeat_thread and (thread is None or not thread.is_alive()):
                self._heartbeat_thread = None

    def _ensure_heartbeat(self) -> None:
        self._assert_current_process()
        with self._heartbeat_lock:
            if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
                if not self._heartbeat_stop.is_set():
                    return
                self._heartbeat_thread.join(timeout=self._heartbeat_join_timeout())
                if self._heartbeat_thread.is_alive():
                    raise ControlReconciliationUnavailableError(
                        "prior control reconciliation heartbeat did not stop"
                    )
            stop_event = threading.Event()
            self._heartbeat_stop = stop_event
            thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(stop_event,),
                name=f"control-reconciliation-{self.instance_id}",
                daemon=True,
            )
            self._heartbeat_thread = thread
            thread.start()

    def _heartbeat_join_timeout(self) -> float:
        """Cover one bounded lock/open attempt plus event observation."""

        return self._open_retry_seconds + self._heartbeat_interval_seconds + 0.25

    def _heartbeat_loop(self, stop_event: threading.Event) -> None:
        self._assert_current_process()
        while not stop_event.wait(self._heartbeat_interval_seconds):
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    cursor = connection.execute(
                        """
                        UPDATE control_operations
                        SET lease_expires_at=?, updated_at=?
                        WHERE state='pending' AND owner_instance=?
                            AND owner_process_token=?
                        """,
                        (
                            self._clock() + self._lease_timeout_seconds,
                            self._clock(),
                            self.instance_id,
                            self._process_token,
                        ),
                    )
                    connection.commit()
                    if cursor.rowcount == 0:
                        return
            except (ControlReconciliationError, OSError, sqlite3.Error):
                continue

    @staticmethod
    def session_digest(session_id: str) -> str:
        """Return an opaque durable binding for one authenticated session."""

        if not session_id:
            raise ControlReconciliationError("session binding is missing")
        return canonical_digest({"session_id": session_id})

    def save_candidate(
        self,
        *,
        candidate_id: str,
        candidate_digest: str,
        session_id: str,
        review: Mapping[str, Any],
        expires_at: float,
    ) -> Mapping[str, Any]:
        """Persist a resolved candidate or return its exact prior record."""

        plan = _mapping(review.get("resolved_plan"), "resolved plan")
        profile = _mapping(review.get("profile"), "Profile")
        binding = _mapping(review.get("catalog_binding"), "catalog binding")
        predecessor = _mapping(review.get("predecessor"), "predecessor")
        now = self._clock()
        encoded_review = _json(review)
        values = (
            candidate_id,
            candidate_digest,
            self.session_digest(session_id),
            "resolved",
            _required(predecessor.get("profile_revision"), "Profile revision"),
            _required(predecessor.get("plan_digest"), "predecessor plan digest"),
            _required(binding.get("profile_definition_digest"), "definition digest"),
            _required(binding.get("profile_catalog_digest"), "catalog digest"),
            _required(binding.get("bundle_lock_digest"), "bundle lock digest"),
            _required(profile.get("profile_authority_snapshot_digest"), "Authority digest"),
            _integer(plan.get("security_epoch"), "SecurityEpoch"),
            encoded_review,
            float(expires_at),
            now,
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing_row = connection.execute(
                    """
                    SELECT * FROM profile_ceremonies
                    WHERE candidate_id=? OR candidate_digest=?
                    """,
                    (candidate_id, candidate_digest),
                ).fetchone()
                existing = _ceremony_record(existing_row)
                if existing is not None:
                    if existing["candidate_digest"] != candidate_digest or existing[
                        "session_digest"
                    ] != self.session_digest(session_id):
                        raise ControlReconciliationError(
                            "candidate digest is already bound to another ceremony"
                        )
                    connection.commit()
                    return existing
                self._compact_ceremonies_locked(connection, now=now)
                ceremony_count, ceremony_bytes = self._ceremony_usage_locked(connection)
                incoming_bytes = sum(len(str(value).encode("utf-8")) for value in values) + 1024
                if ceremony_count >= self._max_ceremony_records:
                    raise ControlReconciliationCapacityError(
                        "profile ceremony record capacity is exhausted"
                    )
                if ceremony_bytes + incoming_bytes > self._max_ceremony_bytes:
                    raise ControlReconciliationCapacityError(
                        "profile ceremony byte capacity is exhausted"
                    )
                connection.execute(
                    """
                    INSERT INTO profile_ceremonies(
                        candidate_id, candidate_digest, session_digest, state,
                        expected_profile_revision, expected_plan_digest,
                        profile_definition_digest, profile_catalog_digest,
                        bundle_lock_digest, authority_snapshot_digest,
                        security_epoch, review_json, expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                connection.commit()
        except sqlite3.IntegrityError:
            existing = self.candidate_by_digest(candidate_digest, session_id=session_id)
            if existing is None:
                raise ControlReconciliationError(
                    "candidate digest is already bound to another ceremony"
                )
            return existing
        return self.require_candidate(candidate_id, candidate_digest, session_id=session_id)

    @staticmethod
    def _ceremony_usage_locked(connection: sqlite3.Connection) -> tuple[int, int]:
        """Return a conservative count/byte estimate for ceremony records."""

        row = connection.execute(
            """
            SELECT COUNT(*) AS records,
                   COALESCE(SUM(
                       LENGTH(CAST(candidate_id AS BLOB))
                       + LENGTH(CAST(candidate_digest AS BLOB))
                       + LENGTH(CAST(session_digest AS BLOB))
                       + LENGTH(CAST(state AS BLOB))
                       + LENGTH(CAST(review_json AS BLOB))
                       + LENGTH(CAST(COALESCE(authority_record_json, '') AS BLOB))
                       + LENGTH(CAST(COALESCE(activation_json, '') AS BLOB))
                       + 1024
                   ), 0) AS bytes
            FROM profile_ceremonies
            """
        ).fetchone()
        return int(row["records"]), int(row["bytes"])

    def _compact_ceremonies_locked(
        self,
        connection: sqlite3.Connection,
        *,
        now: float,
    ) -> int:
        """Delete one bounded batch of ceremonies past their replay horizon.

        Unexpired ceremonies remain available regardless of lifecycle state. Once
        both expiry and retention have elapsed, unsafe request outcomes remain in
        ``control_operations`` without retaining the larger ceremony payload.
        """

        cutoff = now - self._ceremony_retention_seconds
        rows = connection.execute(
            """
            SELECT candidate_id FROM profile_ceremonies
            WHERE expires_at <= ?
            ORDER BY expires_at, candidate_id LIMIT ?
            """,
            (cutoff, self._compaction_batch_size),
        ).fetchall()
        if not rows:
            return 0
        connection.execute(
            """
            DELETE FROM profile_ceremonies WHERE candidate_id IN (
                SELECT candidate_id FROM profile_ceremonies
                WHERE expires_at <= ?
                ORDER BY expires_at, candidate_id LIMIT ?
            )
            """,
            (cutoff, self._compaction_batch_size),
        )
        connection.execute(
            """
            UPDATE control_journal_audit
            SET compacted_ceremonies=compacted_ceremonies+?,
                first_compacted_at=COALESCE(first_compacted_at, ?),
                last_compacted_at=? WHERE singleton_id=1
            """,
            (len(rows), now, now),
        )
        return len(rows)

    def require_candidate(
        self,
        candidate_id: str,
        candidate_digest: str,
        *,
        session_id: str,
        allowed_states: tuple[str, ...] = (
            "resolved",
            "reviewed",
            "approval_prepared",
            "approved",
            "activated",
        ),
    ) -> Mapping[str, Any]:
        """Load one exact session-bound ceremony record."""

        row = self._read_existing_one(
            "SELECT * FROM profile_ceremonies WHERE candidate_id = ?",
            (candidate_id,),
        )
        record = _ceremony_record(row)
        if record is None:
            raise ControlReconciliationError("Profile ceremony candidate is unknown")
        if record["candidate_digest"] != candidate_digest or record[
            "session_digest"
        ] != self.session_digest(session_id):
            raise ControlReconciliationError("Profile ceremony binding does not match")
        if record["state"] not in allowed_states:
            raise ControlReconciliationError("Profile ceremony state is invalid")
        return record

    def candidate_by_digest(
        self, candidate_digest: str, *, session_id: str
    ) -> Mapping[str, Any] | None:
        """Return the candidate uniquely bound to a digest and session."""

        row = self._read_existing_one(
            "SELECT * FROM profile_ceremonies WHERE candidate_digest = ?",
            (candidate_digest,),
        )
        record = _ceremony_record(row)
        if record is None:
            return None
        if record["session_digest"] != self.session_digest(session_id):
            raise ControlReconciliationError("candidate digest belongs to another session")
        return record

    def profile_candidates(
        self,
        *,
        session_id: str,
        now: float | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """Return the newest unexpired ceremony per Profile for one session."""

        if not self.path.exists():
            return ()
        session_digest = self.session_digest(session_id)
        current_time = self._clock() if now is None else float(now)
        try:
            with self._connect_existing() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM profile_ceremonies
                    WHERE session_digest=? AND expires_at>? AND state!='activated'
                    ORDER BY updated_at DESC, candidate_id DESC
                    """,
                    (session_digest, current_time),
                ).fetchall()
        except ControlReconciliationError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise ControlReconciliationUnavailableError(
                "Profile ceremony catalog read failed"
            ) from error
        records: list[Mapping[str, Any]] = []
        seen_profiles: set[str] = set()
        for row in rows:
            record = _ceremony_record(row)
            if record is None:
                continue
            review = _mapping(record.get("review"), "review")
            profile = _mapping(review.get("profile"), "Profile")
            profile_id = _required(profile.get("profile_id"), "Profile identity")
            if profile_id in seen_profiles:
                continue
            seen_profiles.add(profile_id)
            records.append(record)
        return tuple(records)

    def transition_reviewed(
        self, candidate_id: str, candidate_digest: str, *, session_id: str
    ) -> Mapping[str, Any]:
        """Atomically acknowledge one resolved candidate."""

        return self._transition(
            candidate_id,
            candidate_digest,
            session_id=session_id,
            from_states=("resolved", "reviewed"),
            to_state="reviewed",
        )

    def prepare_approval(
        self, candidate_id: str, candidate_digest: str, *, session_id: str
    ) -> Mapping[str, Any]:
        """Persist the deterministic Authority record identity before commit."""

        session_digest = self.session_digest(session_id)
        approval_id = (
            "approval.profile-change."
            + canonical_digest(
                {
                    "candidate_digest": candidate_digest,
                    "session_digest": session_digest,
                }
            ).removeprefix("sha256:")[:48]
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM profile_ceremonies WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            record = self._check_candidate(_ceremony_record(row), candidate_digest, session_digest)
            if record["state"] in {"approved", "activated", "approval_prepared"}:
                connection.commit()
                return record
            if record["state"] != "reviewed":
                raise ControlReconciliationError("candidate was not reviewed")
            decided_at = time.time()
            connection.execute(
                """
                UPDATE profile_ceremonies
                SET state='approval_prepared', approval_id=?,
                    approval_decided_at=?, updated_at=?
                WHERE candidate_id=? AND state='reviewed'
                """,
                (approval_id, decided_at, decided_at, candidate_id),
            )
            connection.commit()
        return self.require_candidate(
            candidate_id,
            candidate_digest,
            session_id=session_id,
            allowed_states=("approval_prepared",),
        )

    def mark_approved(
        self,
        candidate_id: str,
        candidate_digest: str,
        *,
        session_id: str,
        approval_digest: str,
        authority_record: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Persist the exact committed Authority receipt idempotently."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM profile_ceremonies WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            record = self._check_candidate(
                _ceremony_record(row),
                candidate_digest,
                self.session_digest(session_id),
            )
            if record["state"] in {"approved", "activated"}:
                if record["approval_digest"] != approval_digest or record[
                    "authority_record"
                ] != dict(authority_record):
                    raise ControlReconciliationError("approval receipt changed")
                connection.commit()
                return record
            if record["state"] != "approval_prepared":
                raise ControlReconciliationError("approval was not prepared")
            connection.execute(
                """
                UPDATE profile_ceremonies
                SET state='approved', approval_digest=?, authority_record_json=?,
                    updated_at=?
                WHERE candidate_id=? AND state='approval_prepared'
                """,
                (approval_digest, _json(authority_record), time.time(), candidate_id),
            )
            connection.commit()
        return self.require_candidate(
            candidate_id,
            candidate_digest,
            session_id=session_id,
            allowed_states=("approved",),
        )

    def require_approval(
        self, approval_id: str, approval_digest: str, *, session_id: str
    ) -> Mapping[str, Any]:
        """Load one durable approval without accepting client authority claims."""

        row = self._read_existing_one(
            "SELECT * FROM profile_ceremonies WHERE approval_id = ?",
            (approval_id,),
        )
        record = _ceremony_record(row)
        if record is None or record["state"] not in {"approved", "activated"}:
            raise ControlReconciliationError("Profile approval is unavailable")
        if (
            record["session_digest"] != self.session_digest(session_id)
            or record["approval_digest"] != approval_digest
        ):
            raise ControlReconciliationError("Profile approval binding does not match")
        return record

    def mark_activated(
        self,
        approval_id: str,
        approval_digest: str,
        *,
        session_id: str,
        activation: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Commit the activation receipt without deleting referenced history."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM profile_ceremonies WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            record = _ceremony_record(row)
            if record is None:
                raise ControlReconciliationError("Profile approval is unavailable")
            if (
                record["session_digest"] != self.session_digest(session_id)
                or record["approval_digest"] != approval_digest
            ):
                raise ControlReconciliationError("Profile approval binding does not match")
            if record["state"] == "activated":
                if record["activation"] != dict(activation):
                    raise ControlReconciliationError("activation receipt changed")
                connection.commit()
                return record
            if record["state"] != "approved":
                raise ControlReconciliationError("Profile approval is not activatable")
            connection.execute(
                """
                UPDATE profile_ceremonies
                SET state='activated', activation_json=?, updated_at=?
                WHERE approval_id=? AND state='approved'
                """,
                (_json(activation), time.time(), approval_id),
            )
            connection.commit()
        return self.require_approval(approval_id, approval_digest, session_id=session_id)

    def begin_operation(
        self,
        *,
        request_id: str,
        session_id: str,
        operation_id: str,
        contract_id: str,
        request_digest: str,
        session_expires_at: float | None = None,
    ) -> tuple[Mapping[str, Any], bool]:
        """Reserve an unsafe frontend request or return its prior outcome."""

        self.prepare_for_operation()
        now = self._clock()
        requested_expiry = (
            now + self._default_session_ttl_seconds
            if session_expires_at is None
            else float(session_expires_at)
        )
        replay_expires_at = max(now, requested_expiry)
        session_digest = self.session_digest(session_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO control_replay_sessions(session_digest, expires_at)
                VALUES (?, ?)
                ON CONFLICT(session_digest) DO UPDATE SET
                    expires_at=MAX(expires_at, excluded.expires_at)
                """,
                (session_digest, replay_expires_at),
            )
            self._compact_locked(connection, now=now)
            row = connection.execute(
                "SELECT * FROM control_operations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            record = _operation_record(row)
            if record is not None:
                self._check_operation(
                    record,
                    session_digest=session_digest,
                    operation_id=operation_id,
                    contract_id=contract_id,
                    request_digest=request_digest,
                )
                connection.commit()
                return self._operation_projection(record), False
            operation_count = int(
                connection.execute("SELECT COUNT(*) FROM control_operations").fetchone()[0]
            )
            if operation_count >= self._max_operation_records:
                connection.rollback()
                raise ControlReconciliationCapacityError(
                    "control operation journal capacity is exhausted"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO control_operations(
                        request_id, session_digest, operation_id, contract_id,
                        request_digest, state, owner_instance, owner_pid,
                        owner_process_token, lease_expires_at, record_refs_json,
                        owner_boot_id, owner_process_start, owner_proof_version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, '[]', ?, ?, 1, ?, ?)
                    """,
                    (
                        request_id,
                        session_digest,
                        operation_id,
                        contract_id,
                        request_digest,
                        self.instance_id,
                        self._process_id,
                        self._process_token,
                        now + self._lease_timeout_seconds,
                        self._boot_id,
                        self._process_start,
                        now,
                        now,
                    ),
                )
            except sqlite3.OperationalError as error:
                connection.rollback()
                if "full" in str(error).lower():
                    raise ControlReconciliationCapacityError(
                        "control operation journal byte capacity is exhausted"
                    ) from error
                raise ControlReconciliationUnavailableError(
                    "control operation journal write failed"
                ) from error
            connection.commit()
        self._ensure_heartbeat()
        return self.operation_status(request_id, session_id=session_id), True

    def renew_session(self, session_id: str, *, expires_at: float) -> None:
        """Extend replay retention, coalescing nearby sliding-session writes."""

        try:
            if not self.path.exists():
                return
        except OSError as error:
            raise ControlReconciliationUnavailableError(
                "control journal path is unavailable"
            ) from error
        session_digest = self.session_digest(session_id)
        requested_expiry = float(expires_at)
        with self._session_renewal_lock:
            if self._session_renewal_retry_after.get(session_digest, 0.0) > (
                self._monotonic_clock()
            ):
                return
            cached_expiry = self._session_renewal_cache.get(session_digest)
            if cached_expiry is not None and requested_expiry <= (
                cached_expiry + self._session_renewal_debounce_seconds
            ):
                return
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        UPDATE control_replay_sessions
                        SET expires_at=MAX(expires_at, ?)
                        WHERE session_digest=?
                        """,
                        (requested_expiry, session_digest),
                    )
                    self._compact_locked(connection, now=self._clock())
                    connection.commit()
            except ControlReconciliationError:
                if len(self._session_renewal_retry_after) >= 4_096:
                    self._session_renewal_retry_after.pop(
                        next(iter(self._session_renewal_retry_after))
                    )
                self._session_renewal_retry_after[session_digest] = (
                    self._monotonic_clock()
                    + min(1.0, self._session_renewal_debounce_seconds)
                )
                raise
            if len(self._session_renewal_cache) >= 4_096:
                self._session_renewal_cache.pop(next(iter(self._session_renewal_cache)))
            self._session_renewal_cache[session_digest] = requested_expiry
            self._session_renewal_retry_after.pop(session_digest, None)

    def _compact_locked(self, connection: sqlite3.Connection, *, now: float) -> int:
        """Delete only terminal records whose session and replay windows ended."""

        compacted_ceremonies = self._compact_ceremonies_locked(connection, now=now)
        cutoff = now - self._terminal_retention_seconds
        rows = connection.execute(
            """
            SELECT operation.request_id, operation.state
            FROM control_operations AS operation
            JOIN control_replay_sessions AS session
                ON session.session_digest=operation.session_digest
            WHERE operation.state != 'pending' AND session.expires_at <= ?
                AND operation.updated_at <= ?
            ORDER BY operation.updated_at, operation.request_id LIMIT ?
            """,
            (now, cutoff, self._compaction_batch_size),
        ).fetchall()
        if rows:
            connection.execute(
                """
                DELETE FROM control_operations WHERE request_id IN (
                    SELECT operation.request_id
                    FROM control_operations AS operation
                    JOIN control_replay_sessions AS session
                        ON session.session_digest=operation.session_digest
                    WHERE operation.state != 'pending' AND session.expires_at <= ?
                        AND operation.updated_at <= ?
                    ORDER BY operation.updated_at, operation.request_id LIMIT ?
                )
                """,
                (now, cutoff, self._compaction_batch_size),
            )
            counts = {
                state: sum(1 for row in rows if str(row["state"]) == state)
                for state in ("succeeded", "failed", "indeterminate")
            }
            connection.execute(
                """
                UPDATE control_journal_audit
                SET compacted_operations=compacted_operations+?,
                    compacted_succeeded=compacted_succeeded+?,
                    compacted_failed=compacted_failed+?,
                    compacted_indeterminate=compacted_indeterminate+?,
                    first_compacted_at=COALESCE(first_compacted_at, ?),
                    last_compacted_at=? WHERE singleton_id=1
                """,
                (
                    len(rows),
                    counts["succeeded"],
                    counts["failed"],
                    counts["indeterminate"],
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                DELETE FROM control_replay_sessions
                WHERE NOT EXISTS (
                    SELECT 1 FROM control_operations
                    WHERE control_operations.session_digest =
                        control_replay_sessions.session_digest
                )
                """
            )
        self._compact_recovery_audit_locked(connection, now=now)
        return len(rows) + compacted_ceremonies

    def _compact_recovery_audit_locked(
        self,
        connection: sqlite3.Connection,
        *,
        now: float,
    ) -> None:
        recovery_count = int(
            connection.execute("SELECT COUNT(*) FROM control_recovery_audit").fetchone()[0]
        )
        excess = recovery_count - self._max_recovery_audit_records
        if excess > 0:
            connection.execute(
                """
                DELETE FROM control_recovery_audit WHERE recovery_id IN (
                    SELECT recovery_id FROM control_recovery_audit
                    ORDER BY recovery_id LIMIT ?
                )
                """,
                (excess,),
            )
            connection.execute(
                """
                UPDATE control_journal_audit
                SET compacted_recovery_audits=compacted_recovery_audits+?,
                    first_compacted_at=COALESCE(first_compacted_at, ?),
                    last_compacted_at=? WHERE singleton_id=1
                """,
                (excess, now, now),
            )

    def journal_snapshot(self) -> Mapping[str, int]:
        """Return bounded, non-sensitive journal counters for diagnostics."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS records,
                       SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending
                FROM control_operations
                """
            ).fetchone()
            ceremony_count, ceremony_bytes = self._ceremony_usage_locked(connection)
            return {
                "records": int(row["records"]),
                "pending": int(row["pending"] or 0),
                "capacity": self._max_operation_records,
                "max_database_bytes": self._max_database_bytes,
                "ceremony_records": ceremony_count,
                "ceremony_bytes": ceremony_bytes,
                "ceremony_record_capacity": self._max_ceremony_records,
                "ceremony_byte_capacity": self._max_ceremony_bytes,
                "operation_database_reserve_bytes": (self._operation_database_reserve_bytes),
            }

    def finish_operation(
        self,
        request_id: str,
        *,
        session_id: str,
        state: str,
        result: Mapping[str, Any] | None,
        record_refs: list[Mapping[str, str]] | None = None,
        safe_error_code: str | None = None,
    ) -> Mapping[str, Any]:
        """Publish a terminal operation result exactly once."""

        if state not in {"succeeded", "failed", "indeterminate"}:
            raise ControlReconciliationConflictError("operation terminal state is invalid")
        result_value = dict(result) if result is not None else None
        encoded_result = _json(result_value) if result_value is not None else None
        if (
            encoded_result is not None
            and len(encoded_result.encode("utf-8")) > self._max_operation_result_bytes
        ):
            state = "indeterminate"
            result_value = None
            encoded_result = None
            record_refs = []
            safe_error_code = "RESULT_TOO_LARGE"
        result_digest = canonical_digest(result_value) if result_value is not None else None
        stop_heartbeat = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM control_operations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            record = _operation_record(row)
            if record is None or record["session_digest"] != self.session_digest(session_id):
                raise ControlReconciliationConflictError("operation binding does not match")
            if record["state"] != "pending":
                if (
                    record["state"] != state
                    or record["result_digest"] != result_digest
                    or record["result"] != result_value
                ):
                    raise ControlReconciliationConflictError("operation outcome is immutable")
                connection.commit()
                return self._operation_projection(record)
            if (
                record["owner_instance"] != self.instance_id
                or record["owner_process_token"] != self._process_token
            ):
                raise ControlReconciliationConflictError("operation lease ownership changed")
            connection.execute(
                """
                UPDATE control_operations
                SET state=?, result_json=?, result_digest=?, record_refs_json=?,
                    safe_error_code=?, updated_at=?
                WHERE request_id=? AND state='pending'
                """,
                (
                    state,
                    encoded_result,
                    result_digest,
                    _json(record_refs or []),
                    safe_error_code,
                    self._clock(),
                    request_id,
                ),
            )
            stop_heartbeat = (
                connection.execute(
                    """
                    SELECT COUNT(*) FROM control_operations
                    WHERE state='pending' AND owner_instance=?
                        AND owner_process_token=?
                    """,
                    (self.instance_id, self._process_token),
                ).fetchone()[0]
                == 0
            )
            connection.commit()
        if stop_heartbeat:
            with self._heartbeat_lock:
                if not self._has_pending_owned_operation():
                    self._heartbeat_stop.set()
        return self.operation_status(request_id, session_id=session_id)

    def _has_pending_owned_operation(self) -> bool:
        with self._connect() as connection:
            return bool(
                connection.execute(
                    """
                    SELECT 1 FROM control_operations
                    WHERE state='pending' AND owner_instance=?
                        AND owner_process_token=? LIMIT 1
                    """,
                    (self.instance_id, self._process_token),
                ).fetchone()
            )

    def operation_status(self, request_id: str, *, session_id: str) -> Mapping[str, Any]:
        """Read one durable operation outcome for its originating session."""

        row = self._read_live_operation(request_id)
        record = _operation_record(row)
        if record is None:
            raise ControlReconciliationConflictError("operation request is unknown")
        if record["session_digest"] != self.session_digest(session_id):
            raise ControlReconciliationConflictError("operation request belongs to another session")
        return self._operation_projection(record)

    def lookup_operation(
        self,
        *,
        request_id: str,
        session_id: str,
        operation_id: str,
        contract_id: str,
        request_digest: str,
    ) -> Mapping[str, Any] | None:
        """Read a replay record without initializing or reserving journal state."""

        try:
            if not self.path.exists():
                return None
        except OSError as error:
            raise ControlReconciliationUnavailableError(
                "control journal path is unavailable"
            ) from error
        row = self._read_live_operation(request_id)
        record = _operation_record(row)
        if record is None:
            return None
        self._check_operation(
            record,
            session_digest=self.session_digest(session_id),
            operation_id=operation_id,
            contract_id=contract_id,
            request_digest=request_digest,
        )
        return self._operation_projection(record)

    def recover_abandoned_operations(self) -> int:
        """Recover only owners proven dead, PID-reused, rebooted, or legacy-expired."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._clock()
            pending = connection.execute(
                """
                SELECT request_id, owner_instance, owner_pid,
                       owner_process_token, owner_boot_id,
                       owner_process_start, owner_proof_version,
                       lease_expires_at
                FROM control_operations
                WHERE state='pending' AND NOT (
                    owner_instance=? AND owner_process_token=?
                )
                """,
                (self.instance_id, self._process_token),
            ).fetchall()
            recovered = 0
            for row in pending:
                reason = self._abandonment_reason(row, now=now)
                if reason is None:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE control_operations
                    SET state='indeterminate', safe_error_code='PROCESS_RESTART',
                        updated_at=?
                    WHERE request_id=? AND state='pending'
                        AND owner_process_token=?
                    """,
                    (now, str(row["request_id"]), str(row["owner_process_token"])),
                )
                if cursor.rowcount != 1:
                    continue
                recovered += 1
                connection.execute(
                    """
                    INSERT INTO control_recovery_audit(
                        recovered_at, recovered_by_instance,
                        recovered_by_process_token, abandoned_owner_instance,
                        abandoned_owner_pid, abandoned_owner_process_token,
                        recovered_count, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        now,
                        self.instance_id,
                        self._process_token,
                        str(row["owner_instance"]),
                        int(row["owner_pid"]),
                        str(row["owner_process_token"]),
                        reason,
                    ),
                )
            self._compact_recovery_audit_locked(connection, now=now)
            connection.commit()
            return recovered

    def _abandonment_reason(self, row: sqlite3.Row, *, now: float) -> str | None:
        owner_boot_id = str(row["owner_boot_id"])
        owner_start = str(row["owner_process_start"])
        proof_version = int(row["owner_proof_version"])
        if owner_boot_id and self._boot_id and owner_boot_id != self._boot_id:
            return "host_rebooted"
        if proof_version >= 1:
            if not owner_start:
                return None
            observed = self._process_start_reader(int(row["owner_pid"]))
            if observed.state == "dead":
                return "process_dead"
            if observed.state == "live" and observed.identity != owner_start:
                return "pid_reused"
            return None
        if proof_version == 0 and float(row["lease_expires_at"]) <= now:
            return "legacy_lease_expired"
        return None

    def _transition(
        self,
        candidate_id: str,
        candidate_digest: str,
        *,
        session_id: str,
        from_states: tuple[str, ...],
        to_state: str,
    ) -> Mapping[str, Any]:
        session_digest = self.session_digest(session_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM profile_ceremonies WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            record = self._check_candidate(_ceremony_record(row), candidate_digest, session_digest)
            if record["state"] not in from_states:
                raise ControlReconciliationError("Profile ceremony transition is invalid")
            if record["state"] != to_state:
                connection.execute(
                    "UPDATE profile_ceremonies SET state=?, updated_at=? WHERE candidate_id=?",
                    (to_state, time.time(), candidate_id),
                )
            connection.commit()
        return self.require_candidate(
            candidate_id,
            candidate_digest,
            session_id=session_id,
            allowed_states=(to_state,),
        )

    @staticmethod
    def _check_candidate(
        record: Mapping[str, Any] | None,
        candidate_digest: str,
        session_digest: str,
    ) -> Mapping[str, Any]:
        if record is None:
            raise ControlReconciliationError("Profile ceremony candidate is unknown")
        if (
            record["candidate_digest"] != candidate_digest
            or record["session_digest"] != session_digest
        ):
            raise ControlReconciliationError("Profile ceremony binding does not match")
        return record

    @staticmethod
    def _check_operation(
        record: Mapping[str, Any],
        *,
        session_digest: str,
        operation_id: str,
        contract_id: str,
        request_digest: str,
    ) -> None:
        expected = (session_digest, operation_id, contract_id, request_digest)
        actual = (
            record["session_digest"],
            record["operation_id"],
            record["contract_id"],
            record["request_digest"],
        )
        if actual != expected:
            raise ControlReconciliationConflictError("operation request binding changed")

    @staticmethod
    def _operation_projection(record: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "operation_status_api_version": "io.tobkiri.control-operation-status.v1",
            "request_id": record["request_id"],
            "operation_id": record["operation_id"],
            "contract_id": record["contract_id"],
            "request_digest": record["request_digest"],
            "state": record["state"],
            "result": record["result"],
            "result_digest": record["result_digest"],
            "record_refs": record["record_refs"],
            "safe_error_code": record["safe_error_code"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }


def _ceremony_record(row: sqlite3.Row | None) -> Mapping[str, Any] | None:
    if row is None:
        return None
    try:
        review = _mapping(json.loads(str(row["review_json"])), "review")
        authority_record = (
            json.loads(str(row["authority_record_json"]))
            if row["authority_record_json"] is not None
            else None
        )
        activation = (
            json.loads(str(row["activation_json"])) if row["activation_json"] is not None else None
        )
        predecessor = _mapping(review.get("predecessor"), "predecessor")
        binding = _mapping(review.get("catalog_binding"), "catalog binding")
        profile = _mapping(review.get("profile"), "Profile")
        plan = _mapping(review.get("resolved_plan"), "resolved plan")
    except (
        CanonicalizationError,
        ControlReconciliationError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        raise ControlReconciliationUnavailableError("Profile ceremony record is invalid") from error
    expected = (
        str(row["candidate_digest"]),
        str(row["expected_profile_revision"]),
        str(row["expected_plan_digest"]),
        str(row["profile_definition_digest"]),
        str(row["profile_catalog_digest"]),
        str(row["bundle_lock_digest"]),
        str(row["authority_snapshot_digest"]),
        int(row["security_epoch"]),
    )
    actual = (
        _record_digest(review),
        predecessor.get("profile_revision"),
        predecessor.get("plan_digest"),
        binding.get("profile_definition_digest"),
        binding.get("profile_catalog_digest"),
        binding.get("bundle_lock_digest"),
        profile.get("profile_authority_snapshot_digest"),
        plan.get("security_epoch"),
    )
    if actual != expected:
        raise ControlReconciliationUnavailableError("Profile ceremony record digest changed")
    return {
        **dict(row),
        "review": review,
        "authority_record": authority_record,
        "activation": activation,
    }


def _operation_record(row: sqlite3.Row | None) -> Mapping[str, Any] | None:
    if row is None:
        return None
    try:
        result = json.loads(str(row["result_json"])) if row["result_json"] is not None else None
        record_refs = json.loads(str(row["record_refs_json"]))
    except (CanonicalizationError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ControlReconciliationUnavailableError("operation record is invalid") from error
    if (
        row["state"] not in {"pending", "succeeded", "failed", "indeterminate"}
        or not isinstance(record_refs, list)
        or (result is None) != (row["result_digest"] is None)
        or (result is not None and _record_digest(result) != row["result_digest"])
    ):
        raise ControlReconciliationUnavailableError("operation record digest changed")
    return {
        **dict(row),
        "result": result,
        "record_refs": record_refs,
    }


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ControlReconciliationError(f"{label} is missing")
    return value


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ControlReconciliationError(f"{label} is missing")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ControlReconciliationError(f"{label} is invalid")
    return value


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_digest(value: object) -> str:
    try:
        return canonical_digest(value)
    except CanonicalizationError as error:
        raise ControlReconciliationUnavailableError(
            "durable control record is not canonical"
        ) from error


__all__ = [
    "ControlReconciliationConflictError",
    "ControlReconciliationCapacityError",
    "ControlReconciliationError",
    "ControlReconciliationStore",
    "ControlReconciliationUnavailableError",
    "ProcessIdentityEvidence",
]
