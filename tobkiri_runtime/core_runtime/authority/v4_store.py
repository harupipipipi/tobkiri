"""Encrypted, crash-safe authority state and authoritative audit journal.

The store uses SQLite WAL transactions for atomic Grant-use, audit-reservation,
and InvocationLease issuance.  Authority payloads are encrypted at rest; indices
contain only opaque IDs and exact principal/domain digests needed for revocation.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import Any, Concatenate, Iterator, ParamSpec, TypeAlias, TypeVar

from cryptography.fernet import Fernet, InvalidToken
from tobkiri_protocol.platform_paths import canonical_platform_path

from ..process_identity import ProcessIdentityEvidence, process_start_identity
from ..secure_sqlite_path import (
    FileIdentity,
    SecureParent,
    SecurePathError,
    secure_parent as open_secure_parent,
)

from .v4_models import (
    ApprovalRecord,
    AuthorityDenied,
    AuthorityValidationError,
    DomainState,
    ExecutionDomain,
    GrantRecord,
    HostExtensionTrustRecord,
    InvocationLease,
    LeaseState,
    ProviderAuthorityRecord,
    SecurityEpoch,
    authority_digest,
    canonical_json,
)


class AuthorityStoreError(RuntimeError):
    """Raised when durable authority state cannot be read or committed."""


class AuditUnavailable(AuthorityStoreError):
    """Raised when an authoritative audit reservation cannot be committed."""


Record: TypeAlias = (
    ProviderAuthorityRecord
    | ApprovalRecord
    | GrantRecord
    | ExecutionDomain
    | HostExtensionTrustRecord
)

_P = ParamSpec("_P")
_R = TypeVar("_R")
_DATABASE_THREAD_LOCKS: dict[FileIdentity, threading.RLock] = {}
_DATABASE_THREAD_LOCKS_GUARD = threading.Lock()
_ACTIVE_DATABASE_GUARDS: set[int] = set()


def _reset_database_thread_locks() -> None:
    """Drop inherited thread locks and lifecycle-lock descriptors after fork."""

    global _ACTIVE_DATABASE_GUARDS
    global _DATABASE_THREAD_LOCKS, _DATABASE_THREAD_LOCKS_GUARD
    for descriptor in _ACTIVE_DATABASE_GUARDS:
        try:
            os.close(descriptor)
        except OSError:
            pass
    _ACTIVE_DATABASE_GUARDS = set()
    _DATABASE_THREAD_LOCKS = {}
    _DATABASE_THREAD_LOCKS_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_database_thread_locks)


@dataclass(frozen=True)
class _OpenedDatabaseIdentity:
    """Pinned pathname and native descriptor identity for one SQLite file."""

    identity: FileIdentity
    descriptors: tuple[int, ...]


class _IdentityBoundConnection:
    """Validate SQLite's live persistence handles before every operation."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        validator: Callable[[], None],
        close_validator: Callable[[], None],
        guard_descriptor: int,
        guard_locked: bool,
        thread_lock: threading.RLock,
    ) -> None:
        self._connection = connection
        self._validator = validator
        self._close_validator = close_validator
        self._guard_descriptor = guard_descriptor
        self._guard_locked = guard_locked
        self._thread_lock = thread_lock

    def __enter__(self) -> "_IdentityBoundConnection":
        self._validator()
        self._connection.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc_type is None:
            self._validator()
        return bool(self._connection.__exit__(exc_type, exc_value, traceback))

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        """Execute SQL only while all persistence identities remain pinned."""

        self._validator()
        return self._connection.execute(*args, **kwargs)

    def executescript(self, script: str) -> sqlite3.Cursor:
        """Execute a SQL script only while persistence identities remain pinned."""

        self._validator()
        return self._connection.executescript(script)

    def commit(self) -> None:
        """Commit only to the previously attested database handles."""

        self._validator()
        self._connection.commit()

    def close(self) -> None:
        """Close SQLite, revalidate storage, then release its lifecycle lock."""

        try:
            self._validator()
        finally:
            try:
                self._connection.close()
            finally:
                try:
                    self._close_validator()
                finally:
                    try:
                        if self._guard_locked:
                            self._release_guard(self._guard_descriptor)
                    finally:
                        try:
                            os.close(self._guard_descriptor)
                        finally:
                            self._thread_lock.release()

    @staticmethod
    def _release_guard(descriptor: int) -> None:
        _ACTIVE_DATABASE_GUARDS.discard(descriptor)
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            getattr(msvcrt, "locking")(
                descriptor,
                getattr(msvcrt, "LK_UNLCK"),
                1,
            )
            return
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _process_owned(
    method: Callable[Concatenate[Any, _P], _R],
) -> Callable[Concatenate[Any, _P], _R]:
    """Fence every public store entry before validation or state access."""

    @functools.wraps(method)
    def guarded(store: Any, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        store._assert_current_process()
        return method(store, *args, **kwargs)

    return guarded


class AuthorityStore:
    """Host-owned authority database for ADR-014/015 state.

    Args:
        path: SQLite database path.
        key_path: Optional encryption/MAC key path.  Defaults next to the DB.
        clock: Injectable wall-clock function for deterministic tests.
        audit_fault: Optional fault-injection hook called before audit appends.
        connection_connector: Optional injectable SQLite connector for tests.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        key_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        audit_fault: Callable[[], None] | None = None,
        process_start_reader: Callable[[int], ProcessIdentityEvidence] | None = None,
        connection_connector: Callable[..., sqlite3.Connection] | None = None,
    ) -> None:
        self.path = canonical_platform_path(Path(path))
        self.key_path = canonical_platform_path(
            Path(key_path) if key_path is not None else self.path.with_suffix(".key")
        )
        self._guard_path = canonical_platform_path(
            self.path.with_name(f".{self.path.name}.lifecycle.lock")
        )
        self._owner_pid = os.getpid()
        self._process_start_reader = process_start_reader or process_start_identity
        evidence = self._process_start_reader(self._owner_pid)
        if evidence.state != "live":
            raise AuthorityStoreError("authority process identity is unavailable")
        self._owner_process_start = evidence.identity
        self._refresh_process_identity = (
            process_start_reader is not None or self._owner_process_start.startswith("windows:")
        )
        self._clock = clock
        self._audit_fault = audit_fault
        self._connection_connector = connection_connector
        self._lock = threading.RLock()
        self._closed = False
        self._fork_fenced = False
        self._database_identity: FileIdentity | None = None
        self._key_identity: FileIdentity | None = None
        self._guard_identity: FileIdentity | None = None
        self._database_parent_identity: FileIdentity | None = None
        self._key_parent_identity: FileIdentity | None = None
        self._fernet_key = b""
        self._mac_key = b""
        self._fernet: Fernet | None = None
        self._register_fork_fence()
        self._prepare_paths()
        self._fernet_key = self._load_or_create_key()
        self._fernet = Fernet(self._fernet_key)
        self._mac_key = hashlib.sha256(self._fernet_key + b":lease-mac:v1").digest()
        self._ensure_database_file()
        self._ensure_lifecycle_guard()
        self._initialize()
        if os.name != "nt":
            self._secure_chmod(self.path, self._database_identity)

    def _register_fork_fence(self) -> None:
        register = getattr(os, "register_at_fork", None)
        if register is None:
            return
        store_reference = weakref.ref(self)

        def fence_child() -> None:
            store = store_reference()
            if store is not None:
                store._fork_fenced = True
                store._closed = True
                store._fernet_key = b""
                store._mac_key = b""
                store._fernet = None

        register(after_in_child=fence_child)

    def _assert_current_process(self) -> None:
        if self._fork_fenced or os.getpid() != self._owner_pid:
            raise AuthorityStoreError("authority store cannot be used after fork")
        if not self._refresh_process_identity:
            return
        evidence = self._process_start_reader(self._owner_pid)
        if evidence.state != "live" or evidence.identity != self._owner_process_start:
            raise AuthorityStoreError("authority process identity is unavailable or changed")

    def _prepare_paths(self) -> None:
        """Create parents, then validate DB, key, and SQLite sidecars safely."""

        try:
            for target in (self.path, self.key_path):
                parent_existed = target.parent.exists()
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if os.name != "nt" and not parent_existed:
                    os.chmod(target.parent, 0o700)
                with open_secure_parent(target) as parent:
                    if hasattr(os, "getuid") and parent.identity.owner != os.getuid():
                        raise SecurePathError("parent is not owned by the current user")
                    identity = parent.validate_open(target.name, required=False)
                    if target == self.path:
                        self._database_parent_identity = parent.identity
                        self._database_identity = identity
                    else:
                        self._key_parent_identity = parent.identity
                        self._key_identity = identity
        except (OSError, SecurePathError) as exc:
            raise AuthorityStoreError("authority state path is unsafe") from exc

    @contextmanager
    def _secure_parent(self, path: Path) -> Iterator[SecureParent]:
        expected = (
            self._key_parent_identity
            if path == self.key_path
            else self._database_parent_identity
        )
        with open_secure_parent(path) as parent:
            if expected is None or parent.identity != expected:
                raise SecurePathError("authority parent identity changed")
            yield parent

    def _ensure_database_file(self) -> None:
        if self._database_identity is not None:
            return
        try:
            with self._secure_parent(self.path) as parent:
                self._database_identity = parent.create_empty_file(self.path.name)
        except (FileExistsError, OSError, SecurePathError) as exc:
            if isinstance(exc, FileExistsError):
                try:
                    with self._secure_parent(self.path) as parent:
                        self._database_identity = parent.validate_open(
                            self.path.name,
                            required=True,
                        )
                    return
                except (OSError, SecurePathError) as validation_error:
                    exc = validation_error
            raise AuthorityStoreError("authority database path is unsafe") from exc

    def _secure_chmod(self, path: Path, expected: FileIdentity | None) -> None:
        try:
            with self._secure_parent(path) as parent:
                identity = parent.validate_open(path.name, required=True, expected=expected)
                descriptor = parent.open_file(path.name, os.O_RDONLY)
                try:
                    if identity != FileIdentity.from_stat(os.fstat(descriptor)):
                        raise SecurePathError("file changed before chmod")
                    os.fchmod(descriptor, 0o600)
                finally:
                    os.close(descriptor)
                parent.validate_open(path.name, required=True, expected=expected)
        except (OSError, SecurePathError) as exc:
            raise AuthorityStoreError("authority state path is unsafe") from exc

    def _ensure_lifecycle_guard(self) -> None:
        """Create or attest the stable file used to serialize SQLite sidecars."""

        try:
            with self._secure_parent(self._guard_path) as parent:
                try:
                    self._guard_identity = parent.create_empty_file(
                        self._guard_path.name,
                    )
                except FileExistsError:
                    self._guard_identity = parent.validate_open(
                        self._guard_path.name,
                        required=True,
                    )
                self._validate_private_storage_mode(parent, self._guard_path.name)
                descriptor = parent.open_file(self._guard_path.name, os.O_RDWR)
                try:
                    if FileIdentity.from_stat(os.fstat(descriptor)) != self._guard_identity:
                        raise SecurePathError("lifecycle guard identity changed")
                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"\0")
                        os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                parent.validate_open(
                    self._guard_path.name,
                    required=True,
                    expected=self._guard_identity,
                )
        except (OSError, SecurePathError) as exc:
            raise AuthorityStoreError("authority lifecycle guard path is unsafe") from exc

    def _load_or_create_key(self) -> bytes:
        try:
            with self._secure_parent(self.key_path) as parent:
                payload, identity = parent.read_bytes(
                    self.key_path.name,
                    expected=self._key_identity,
                )
            key = payload.strip()
            Fernet(key)
            if os.name != "nt":
                with self._secure_parent(self.key_path) as parent:
                    metadata = parent.stat_file(self.key_path.name, required=True)
                assert metadata is not None
                mode = stat.S_IMODE(metadata.st_mode)
                if mode & 0o077:
                    raise AuthorityStoreError("authority encryption key permissions are too broad")
            self._key_identity = identity
            return key
        except (FileNotFoundError, SecurePathError):
            if self._key_identity is not None:
                raise AuthorityStoreError("authority encryption key path is unsafe") from None
            pass
        except (OSError, ValueError) as exc:
            raise AuthorityStoreError("authority encryption key is invalid") from exc

        if self._database_identity is not None:
            try:
                with self._secure_parent(self.path) as parent:
                    metadata = parent.stat_file(self.path.name, required=True)
                assert metadata is not None
                if metadata.st_size > 0:
                    raise AuthorityStoreError(
                        "authority encryption key is missing for an existing database"
                    )
            except (OSError, SecurePathError) as exc:
                raise AuthorityStoreError("authority database path is unsafe") from exc

        key = Fernet.generate_key()
        temporary_name = f".{self.key_path.name}.{secrets.token_hex(8)}.tmp"
        try:
            with self._secure_parent(self.key_path) as parent:
                descriptor = parent.open_file(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(key + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    parent.assert_path_continuity()
                    parent.publish_new_file(temporary_name, self.key_path.name)
                except FileExistsError:
                    payload, identity = parent.read_bytes(self.key_path.name)
                    existing = payload.strip()
                    Fernet(existing)
                    self._key_identity = identity
                    return existing
                finally:
                    parent.unlink_file(temporary_name, missing_ok=True)
                payload, identity = parent.read_bytes(self.key_path.name)
            if payload.strip() != key:
                raise AuthorityStoreError("authority encryption key changed during creation")
            self._key_identity = identity
        except (OSError, SecurePathError, ValueError) as exc:
            raise AuthorityStoreError("authority encryption key is invalid") from exc
        return key

    def _validate_storage_files(self) -> None:
        try:
            with self._secure_parent(self.path) as parent:
                parent.validate_open(
                    self.path.name,
                    required=True,
                    expected=self._database_identity,
                )
                for suffix in ("-wal", "-shm"):
                    parent.validate_open(f"{self.path.name}{suffix}", required=False)
        except (OSError, SecurePathError) as exc:
            raise AuthorityStoreError("authority database path is unsafe") from exc

    @staticmethod
    def _open_descriptor_identities() -> dict[int, FileIdentity]:
        """Snapshot open regular-file descriptors on supported POSIX hosts."""

        descriptor_root = Path("/proc/self/fd")
        if not descriptor_root.is_dir():
            descriptor_root = Path("/dev/fd")
        if not descriptor_root.is_dir():
            raise AuthorityStoreError("authority database handle identity is unavailable")
        identities: dict[int, FileIdentity] = {}
        try:
            names = os.listdir(descriptor_root)
        except OSError as exc:
            raise AuthorityStoreError("authority database handle identity is unavailable") from exc
        for name in names:
            try:
                descriptor = int(name)
                metadata = os.fstat(descriptor)
            except (OSError, ValueError):
                continue
            if stat.S_ISREG(metadata.st_mode):
                identities[descriptor] = FileIdentity.from_stat(metadata)
        return identities

    def _reported_database_path(self, connection: sqlite3.Connection) -> Path:
        rows = sqlite3.Connection.execute(connection, "PRAGMA database_list").fetchall()
        main_paths = [str(row[2]) for row in rows if str(row[1]) == "main"]
        if len(main_paths) != 1 or not main_paths[0]:
            raise AuthorityStoreError("authority database handle identity is unavailable")
        return canonical_platform_path(Path(main_paths[0]))

    @staticmethod
    def _validate_private_storage_mode(parent: SecureParent, name: str) -> None:
        if os.name == "nt":
            return
        metadata = parent.stat_file(name, required=True)
        assert metadata is not None
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise AuthorityStoreError("authority database permissions are too broad")

    def _pin_opened_database_files(
        self,
        connection: sqlite3.Connection,
        descriptors_before: Mapping[int, FileIdentity],
        suffixes: tuple[str, ...] = ("", "-wal", "-shm"),
    ) -> dict[str, _OpenedDatabaseIdentity]:
        """Tie SQLite's destination to handles opened by this connection."""

        if self._reported_database_path(connection) != self.path:
            raise AuthorityStoreError("authority database handle identity is unsafe")
        pinned: dict[str, _OpenedDatabaseIdentity] = {}
        descriptor_identities = {} if os.name == "nt" else self._open_descriptor_identities()
        try:
            with self._secure_parent(self.path) as parent:
                for suffix in suffixes:
                    name = f"{self.path.name}{suffix}"
                    identity = parent.validate_open(
                        name,
                        required=True,
                        expected=self._database_identity if not suffix else None,
                    )
                    assert identity is not None
                    self._validate_private_storage_mode(parent, name)
                    descriptors: tuple[int, ...] = ()
                    if os.name != "nt":
                        all_matches = [
                            candidate
                            for candidate, opened_identity in descriptor_identities.items()
                            if opened_identity == identity
                        ]
                        new_matches = [
                            candidate
                            for candidate in all_matches
                            if descriptors_before.get(candidate) != identity
                        ]
                        # A concurrently finalized connection can close a descriptor
                        # and SQLite can reuse that same number for the same inode.
                        # Native database_list still binds this connection to the
                        # securely pinned path, so retain matching live handles when
                        # descriptor-number attribution encounters that ABA case.
                        matches = new_matches or all_matches
                        if not matches:
                            raise SecurePathError(
                                "SQLite handle does not match the pinned storage file"
                            )
                        descriptors = tuple(matches)
                    pinned[suffix] = _OpenedDatabaseIdentity(identity, descriptors)
        except (OSError, SecurePathError) as exc:
            raise AuthorityStoreError("authority database handle identity is unsafe") from exc
        return pinned

    def _validate_opened_database_files(
        self,
        pinned: Mapping[str, _OpenedDatabaseIdentity],
    ) -> None:
        """Revalidate pathname and live-handle identity before SQLite I/O."""

        try:
            with self._secure_parent(self.path) as parent:
                for suffix, opened in pinned.items():
                    name = f"{self.path.name}{suffix}"
                    parent.validate_open(name, required=True, expected=opened.identity)
                    self._validate_private_storage_mode(parent, name)
                    if opened.descriptors and not any(
                        self._descriptor_matches(descriptor, opened.identity)
                        for descriptor in opened.descriptors
                    ):
                        raise SecurePathError("SQLite descriptor identity changed")
        except (OSError, SecurePathError) as exc:
            raise AuthorityStoreError("authority database handle identity is unsafe") from exc

    @staticmethod
    def _descriptor_matches(descriptor: int, expected: FileIdentity) -> bool:
        try:
            return FileIdentity.from_stat(os.fstat(descriptor)) == expected
        except OSError:
            return False

    @staticmethod
    def _database_thread_lock(identity: FileIdentity) -> threading.RLock:
        with _DATABASE_THREAD_LOCKS_GUARD:
            return _DATABASE_THREAD_LOCKS.setdefault(identity, threading.RLock())

    def _open_database_guard(self) -> tuple[int, bool, threading.RLock]:
        """Lock the stable guard across one identity-pinned SQLite connection."""

        descriptor: int | None = None
        guard_locked = False
        thread_lock: threading.RLock | None = None
        try:
            with self._secure_parent(self._guard_path) as parent:
                descriptor = parent.open_file(self._guard_path.name, os.O_RDWR)
                identity = FileIdentity.from_stat(os.fstat(descriptor))
                parent.validate_open(
                    self._guard_path.name,
                    required=True,
                    expected=self._guard_identity,
                )
                if identity != self._guard_identity:
                    raise SecurePathError("lifecycle guard identity changed")
                os.set_inheritable(descriptor, False)
                thread_lock = self._database_thread_lock(identity)
                already_owned = bool(getattr(thread_lock, "_is_owned")())
                thread_lock.acquire()
                if not already_owned:
                    _ACTIVE_DATABASE_GUARDS.add(descriptor)
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        getattr(msvcrt, "locking")(
                            descriptor,
                            getattr(msvcrt, "LK_LOCK"),
                            1,
                        )
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_EX)
                    guard_locked = True
                parent.validate_open(
                    self._guard_path.name,
                    required=True,
                    expected=identity,
                )
            return descriptor, guard_locked, thread_lock
        except (OSError, SecurePathError) as exc:
            if descriptor is not None:
                _ACTIVE_DATABASE_GUARDS.discard(descriptor)
                os.close(descriptor)
            if thread_lock is not None:
                thread_lock.release()
            raise AuthorityStoreError("authority database handle identity is unsafe") from exc

    def _assert_crypto_material(self) -> None:
        self._assert_current_process()
        try:
            with self._secure_parent(self.key_path) as parent:
                parent.validate_open(
                    self.key_path.name,
                    required=True,
                    expected=self._key_identity,
                )
        except (OSError, SecurePathError) as exc:
            raise AuthorityStoreError("authority encryption key path is unsafe") from exc
        if self._fernet is None or not self._fernet_key or not self._mac_key:
            raise AuthorityStoreError("authority cryptographic material is unavailable")

    def _connect(self) -> _IdentityBoundConnection:
        self._assert_current_process()
        if self._closed:
            raise AuthorityStoreError("authority store is closed")
        connection: sqlite3.Connection | None = None
        guard_descriptor: int | None = None
        guard_locked = False
        thread_lock: threading.RLock | None = None
        try:
            guard_descriptor, guard_locked, thread_lock = self._open_database_guard()
            self._validate_storage_files()
            descriptors_before_connection = (
                {} if os.name == "nt" else self._open_descriptor_identities()
            )
            connector = self._connection_connector or sqlite3.connect
            connection = connector(
                str(self.path),
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            if self._reported_database_path(connection) != self.path:
                raise AuthorityStoreError("authority database handle identity is unsafe")
            main_pinned = self._pin_opened_database_files(
                connection,
                descriptors_before_connection,
                ("",),
            )
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            sidecars_pinned = self._pin_opened_database_files(
                connection,
                {},
                ("-wal", "-shm"),
            )
            pinned = {**main_pinned, **sidecars_pinned}
            self._validate_storage_files()
            self._validate_opened_database_files(pinned)
            return _IdentityBoundConnection(
                connection,
                lambda: self._validate_opened_database_files(pinned),
                self._validate_storage_files,
                guard_descriptor,
                guard_locked,
                thread_lock,
            )
        except AuthorityStoreError:
            self._discard_failed_connection(
                connection,
                guard_descriptor,
                guard_locked,
                thread_lock,
            )
            raise
        except (OSError, sqlite3.Error) as exc:
            self._discard_failed_connection(
                connection,
                guard_descriptor,
                guard_locked,
                thread_lock,
            )
            raise AuthorityStoreError("authority database is unavailable") from exc

    @staticmethod
    def _discard_failed_connection(
        connection: sqlite3.Connection | None,
        guard_descriptor: int | None,
        guard_locked: bool,
        thread_lock: threading.RLock | None,
    ) -> None:
        """Release every native resource after connection setup fails."""

        try:
            if connection is not None:
                connection.close()
        finally:
            try:
                if guard_descriptor is not None:
                    try:
                        if guard_locked:
                            _IdentityBoundConnection._release_guard(guard_descriptor)
                    finally:
                        os.close(guard_descriptor)
            finally:
                if thread_lock is not None:
                    thread_lock.release()

    @contextmanager
    def _connection(self) -> Iterator[_IdentityBoundConnection]:
        """Yield one transaction and always release its native file handle."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def close(self) -> None:
        """Idempotently prevent new work after all active transactions finish."""

        self._assert_current_process()
        with self._lock:
            self._closed = True

    def __enter__(self) -> "AuthorityStore":
        """Return this open store for explicit scoped ownership."""

        self._assert_current_process()
        with self._lock:
            if self._closed:
                raise AuthorityStoreError("authority store is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Close the store when its ownership scope exits."""

        del exc_type, exc_value, traceback
        self.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            existing_tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            existing_version: str | None = None
            if existing_tables:
                authority_tables = {
                    "authority_meta",
                    "authority_records",
                    "execution_sessions",
                    "grant_usage",
                    "invocation_leases",
                    "revocations",
                    "authority_audit",
                }
                supported_table_sets = {
                    frozenset(authority_tables),
                    frozenset({*authority_tables, "activation_reservations"}),
                }
                if frozenset(existing_tables) not in supported_table_sets:
                    raise AuthorityStoreError(
                        "authority database table set is partial or inconsistent"
                    )
                version_row = connection.execute(
                    "SELECT value FROM authority_meta WHERE key='schema_version'"
                ).fetchone()
                if version_row is None:
                    raise AuthorityStoreError("authority database schema version is missing")
                existing_version = str(version_row["value"])
                if existing_version not in {"1", "2", "3"}:
                    raise AuthorityStoreError("authority database schema version is unsupported")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS authority_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                ) STRICT;
                CREATE TABLE IF NOT EXISTS authority_records (
                    record_type TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    record_digest TEXT NOT NULL,
                    encrypted_payload BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (record_type, record_id)
                ) STRICT;
                CREATE TABLE IF NOT EXISTS execution_sessions (
                    session_id TEXT PRIMARY KEY,
                    domain_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    activation_id TEXT NOT NULL,
                    boot_epoch INTEGER NOT NULL,
                    channel_digest TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    created_at REAL NOT NULL
                ) STRICT;
                CREATE TABLE IF NOT EXISTS grant_usage (
                    grant_id TEXT PRIMARY KEY,
                    reserved_uses INTEGER NOT NULL DEFAULT 0,
                    committed_uses INTEGER NOT NULL DEFAULT 0
                ) STRICT;
                CREATE TABLE IF NOT EXISTS invocation_leases (
                    lease_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    lease_digest TEXT NOT NULL,
                    encrypted_payload BLOB NOT NULL,
                    caller_principal_id TEXT NOT NULL,
                    target_principal_id TEXT NOT NULL,
                    caller_artifact_digest TEXT NOT NULL,
                    target_artifact_digest TEXT NOT NULL,
                    caller_publisher_lineage TEXT NOT NULL,
                    target_publisher_lineage TEXT NOT NULL,
                    host_extension_id TEXT NOT NULL,
                    caller_domain_id TEXT NOT NULL,
                    target_domain_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    activation_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    provider_authority_id TEXT NOT NULL,
                    audit_reservation_id TEXT NOT NULL,
                    security_epoch INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    outcome_digest TEXT
                ) STRICT;
                CREATE TABLE IF NOT EXISTS revocations (
                    revocation_id TEXT PRIMARY KEY,
                    target_kind TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    security_epoch INTEGER NOT NULL,
                    reason_digest TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE (target_kind, target_id)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS revocations_target
                    ON revocations(target_kind, target_id);
                CREATE TABLE IF NOT EXISTS authority_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    event_state TEXT NOT NULL,
                    previous_digest TEXT NOT NULL,
                    event_digest TEXT NOT NULL,
                    encrypted_payload BLOB NOT NULL,
                    created_at REAL NOT NULL
                ) STRICT;
                CREATE TABLE IF NOT EXISTS activation_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    activation_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    profile_authority_digest TEXT NOT NULL,
                    security_epoch INTEGER NOT NULL,
                    fencing_token INTEGER NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'prepared', 'ready_without_authority', 'committing',
                            'active', 'aborted', 'retired'
                        )
                    ),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                ) STRICT;
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO authority_meta(key, value) VALUES"
                " ('schema_version', '1'), ('security_epoch', '1')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO authority_meta(key, value) VALUES (?, ?)",
                ("security_epoch_advanced_at", str(self._clock())),
            )
            connection.execute(
                "INSERT OR IGNORE INTO authority_meta(key, value) VALUES (?, ?)",
                (
                    "security_epoch_reason_digest",
                    authority_digest({"reason": "genesis"}),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO authority_meta(key, value) VALUES"
                " ('activation_fencing_token', '0')"
            )
            lease_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(invocation_leases)").fetchall()
            }
            expected_v2_columns = {
                "lease_id",
                "request_id",
                "lease_digest",
                "encrypted_payload",
                "caller_principal_id",
                "target_principal_id",
                "caller_artifact_digest",
                "target_artifact_digest",
                "caller_publisher_lineage",
                "target_publisher_lineage",
                "host_extension_id",
                "caller_domain_id",
                "target_domain_id",
                "profile_id",
                "activation_id",
                "grant_id",
                "provider_authority_id",
                "audit_reservation_id",
                "security_epoch",
                "expires_at",
                "state",
                "outcome_digest",
            }
            expected_columns = (
                expected_v2_columns - {"request_id"}
                if existing_version == "1"
                else expected_v2_columns
            )
            if lease_columns != expected_columns:
                raise AuthorityStoreError(
                    "authority database lease schema is partial or inconsistent"
                )
            self._verify_audit_connection(connection)
            if existing_version == "1":
                self._migrate_request_bound_leases(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS leases_request ON invocation_leases(request_id)"
            )
            connection.execute("UPDATE authority_meta SET value='3' WHERE key='schema_version'")
            if existing_version == "1":
                connection.commit()

    def _migrate_request_bound_leases(self, connection: _IdentityBoundConnection) -> None:
        """Fail closed when upgrading pre-adapter lease rows.

        Old leases lack request, activation-snapshot, and plan bindings. They
        cannot safely be reissued: unused leases are revoked and dispatched
        effects become ambiguous in the authoritative journal.
        """

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "ALTER TABLE invocation_leases ADD COLUMN request_id TEXT NOT NULL DEFAULT ''"
        )
        connection.execute("UPDATE invocation_leases SET request_id='legacy-' || lease_id")
        rows = connection.execute(
            "SELECT lease_id, lease_digest, encrypted_payload, grant_id,"
            " audit_reservation_id, state"
            " FROM invocation_leases WHERE state IN (?, ?)",
            (LeaseState.ISSUED.value, LeaseState.DISPATCHED.value),
        ).fetchall()
        for row in rows:
            legacy_payload = self._decrypt(row["encrypted_payload"])
            if legacy_payload.get("lease_id") != row["lease_id"]:
                raise AuthorityStoreError("historical InvocationLease identity is inconsistent")
            if not hmac.compare_digest(
                authority_digest(legacy_payload),
                str(row["lease_digest"]),
            ):
                raise AuthorityStoreError("historical InvocationLease digest is inconsistent")
            legacy_payload["request_id"] = f"legacy-{row['lease_id']}"
            legacy_payload["activation_digest"] = authority_digest(
                {
                    "legacy_unbound_activation": row["lease_id"],
                }
            )
            legacy_payload["plan_digest"] = authority_digest(
                {
                    "legacy_unbound_plan": row["lease_id"],
                }
            )
            migrated_lease = InvocationLease.from_dict(legacy_payload)
            connection.execute(
                "UPDATE invocation_leases SET lease_digest=?, encrypted_payload=? WHERE lease_id=?",
                (
                    migrated_lease.digest,
                    self._encrypt(migrated_lease.to_dict()),
                    row["lease_id"],
                ),
            )
            dispatched = row["state"] == LeaseState.DISPATCHED.value
            state = LeaseState.AMBIGUOUS if dispatched else LeaseState.REVOKED
            outcome_digest = authority_digest(
                {
                    "status": "ambiguous_after_authority_schema_upgrade"
                    if dispatched
                    else "revoked_after_authority_schema_upgrade",
                    "lease_id": row["lease_id"],
                }
            )
            connection.execute(
                "UPDATE invocation_leases SET state=?, outcome_digest=? WHERE lease_id=?",
                (state.value, outcome_digest, row["lease_id"]),
            )
            if dispatched:
                connection.execute(
                    "UPDATE grant_usage SET reserved_uses=reserved_uses-1,"
                    " committed_uses=committed_uses+1 WHERE grant_id=?"
                    " AND reserved_uses > 0",
                    (row["grant_id"],),
                )
            else:
                connection.execute(
                    "UPDATE grant_usage SET reserved_uses=reserved_uses-1"
                    " WHERE grant_id=? AND reserved_uses > 0",
                    (row["grant_id"],),
                )
            self._append_audit(
                connection,
                event_id=f"schema-v2-{row['lease_id']}",
                event_type="host_effect",
                event_state=state.value,
                payload={
                    "lease_id": row["lease_id"],
                    "reservation_id": row["audit_reservation_id"],
                    "outcome_digest": outcome_digest,
                    "legacy_lease_digest": row["lease_digest"],
                    "migrated_lease_digest": migrated_lease.digest,
                },
            )

    def _encrypt(self, payload: Mapping[str, Any]) -> bytes:
        self._assert_crypto_material()
        fernet = self._fernet
        assert fernet is not None
        return fernet.encrypt(canonical_json(dict(payload)))

    def _decrypt(self, payload: bytes) -> dict[str, Any]:
        self._assert_crypto_material()
        fernet = self._fernet
        assert fernet is not None
        try:
            value = json.loads(fernet.decrypt(payload).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthorityStoreError("authority record authentication failed") from exc
        if not isinstance(value, dict):
            raise AuthorityStoreError("authority record is not an object")
        return value

    @staticmethod
    def _record_type(record: Record) -> str:
        if isinstance(record, ProviderAuthorityRecord):
            return "provider_authority"
        if isinstance(record, ApprovalRecord):
            return "approval"
        if isinstance(record, GrantRecord):
            return "grant"
        if isinstance(record, ExecutionDomain):
            return "execution_domain"
        if isinstance(record, HostExtensionTrustRecord):
            return "host_extension_trust"
        raise TypeError(f"unsupported authority record: {type(record).__name__}")

    @staticmethod
    def _record_id(record: Record) -> str:
        if isinstance(record, ProviderAuthorityRecord):
            return record.record_id
        if isinstance(record, ApprovalRecord):
            return record.approval_id
        if isinstance(record, GrantRecord):
            return record.grant_id
        if isinstance(record, ExecutionDomain):
            return record.domain_id
        return record.trust_id

    @_process_owned
    def put_record(self, record: Record, *, replace: bool = False) -> None:
        """Persist an encrypted record, rejecting accidental mutation by default."""

        record_type = self._record_type(record)
        record_id = self._record_id(record)
        payload = record.to_dict()
        digest = authority_digest(payload)
        operation = "INSERT OR REPLACE" if replace else "INSERT"
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    f"{operation} INTO authority_records"
                    " (record_type, record_id, record_digest, encrypted_payload, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (record_type, record_id, digest, self._encrypt(payload), self._clock()),
                )
                if isinstance(record, GrantRecord):
                    connection.execute(
                        "INSERT OR IGNORE INTO grant_usage(grant_id) VALUES (?)",
                        (record.grant_id,),
                    )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise AuthorityStoreError("authority record is immutable") from exc
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityStoreError("authority record commit failed") from exc

    @_process_owned
    def put_records_atomically(self, records: Iterable[Record]) -> None:
        """Commit an approval transaction without leaving partial authority."""

        pending = list(records)
        if not pending:
            raise ValueError("authority transaction cannot be empty")
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for record in pending:
                    record_type = self._record_type(record)
                    record_id = self._record_id(record)
                    payload = record.to_dict()
                    connection.execute(
                        "INSERT INTO authority_records"
                        " (record_type, record_id, record_digest, encrypted_payload,"
                        " created_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            record_type,
                            record_id,
                            authority_digest(payload),
                            self._encrypt(payload),
                            self._clock(),
                        ),
                    )
                    if isinstance(record, GrantRecord):
                        connection.execute(
                            "INSERT INTO grant_usage(grant_id) VALUES (?)",
                            (record.grant_id,),
                        )
                self._append_audit(
                    connection,
                    event_id="authority-txn-" + secrets.token_hex(16),
                    event_type="authority_records_committed",
                    event_state="committed",
                    payload={
                        "records": [
                            {
                                "record_type": self._record_type(record),
                                "record_id": self._record_id(record),
                                "record_digest": authority_digest(record.to_dict()),
                            }
                            for record in pending
                        ]
                    },
                )
                connection.commit()
        except AuditUnavailable:
            raise
        except sqlite3.IntegrityError as exc:
            raise AuthorityStoreError("authority transaction conflicts") from exc
        except sqlite3.Error as exc:
            raise AuthorityStoreError("authority transaction failed") from exc

    @_process_owned
    def get_provider_authority(self, record_id: str) -> ProviderAuthorityRecord | None:
        """Load and authenticate a ProviderAuthorityRecord."""

        value = self._get_record("provider_authority", record_id)
        return ProviderAuthorityRecord.from_dict(value) if value else None

    @_process_owned
    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        """Load and authenticate an ApprovalRecord."""

        value = self._get_record("approval", approval_id)
        return ApprovalRecord.from_dict(value) if value else None

    @_process_owned
    def get_grant(self, grant_id: str) -> GrantRecord | None:
        """Load and authenticate a GrantRecord."""

        value = self._get_record("grant", grant_id)
        return GrantRecord.from_dict(value) if value else None

    @_process_owned
    def get_domain(self, domain_id: str) -> ExecutionDomain | None:
        """Load and authenticate an ExecutionDomain."""

        value = self._get_record("execution_domain", domain_id)
        return ExecutionDomain.from_dict(value) if value else None

    @_process_owned
    def get_host_extension_trust(self, trust_id: str) -> HostExtensionTrustRecord | None:
        """Load and authenticate a HostExtensionTrustRecord."""

        value = self._get_record("host_extension_trust", trust_id)
        return HostExtensionTrustRecord.from_dict(value) if value else None

    def _get_record(self, record_type: str, record_id: str) -> dict[str, Any] | None:
        try:
            with self._lock, self._connection() as connection:
                row = connection.execute(
                    "SELECT record_digest, encrypted_payload FROM authority_records"
                    " WHERE record_type=? AND record_id=?",
                    (record_type, record_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AuthorityStoreError("authority record read failed") from exc
        if row is None:
            return None
        value = self._decrypt(row["encrypted_payload"])
        if not hmac.compare_digest(str(row["record_digest"]), authority_digest(value)):
            raise AuthorityStoreError("authority record digest mismatch")
        return value

    @_process_owned
    def list_grants(self) -> list[GrantRecord]:
        """Return all authenticated Grants; callers must still filter exactly."""

        return [GrantRecord.from_dict(value) for value in self._list_records("grant")]

    @_process_owned
    def list_provider_authorities(self) -> list[ProviderAuthorityRecord]:
        """Return all authenticated Provider authority records."""

        return [
            ProviderAuthorityRecord.from_dict(value)
            for value in self._list_records("provider_authority")
        ]

    @_process_owned
    def list_domains(self) -> list[ExecutionDomain]:
        """Return all authenticated execution-domain records."""

        return [
            ExecutionDomain.from_dict(value) for value in self._list_records("execution_domain")
        ]

    def _list_records(self, record_type: str) -> list[dict[str, Any]]:
        try:
            with self._lock, self._connection() as connection:
                rows = connection.execute(
                    "SELECT record_digest, encrypted_payload FROM authority_records"
                    " WHERE record_type=? ORDER BY record_id",
                    (record_type,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise AuthorityStoreError("authority record listing failed") from exc
        output: list[dict[str, Any]] = []
        for row in rows:
            value = self._decrypt(row["encrypted_payload"])
            if not hmac.compare_digest(str(row["record_digest"]), authority_digest(value)):
                raise AuthorityStoreError("authority record digest mismatch")
            output.append(value)
        return output

    @property
    @_process_owned
    def security_epoch(self) -> int:
        """Return the Host-owned monotonic SecurityEpoch."""

        try:
            with self._lock, self._connection() as connection:
                row = connection.execute(
                    "SELECT value FROM authority_meta WHERE key='security_epoch'"
                ).fetchone()
        except sqlite3.Error as exc:
            raise AuthorityStoreError("security epoch read failed") from exc
        if row is None or int(row["value"]) < 1:
            raise AuthorityStoreError("security epoch is unavailable")
        return int(row["value"])

    @property
    @_process_owned
    def security_epoch_record(self) -> SecurityEpoch:
        """Return the complete current SecurityEpoch metadata."""

        try:
            with self._lock, self._connection() as connection:
                rows = connection.execute(
                    "SELECT key, value FROM authority_meta WHERE key IN"
                    " ('security_epoch', 'security_epoch_advanced_at',"
                    " 'security_epoch_reason_digest')"
                ).fetchall()
        except sqlite3.Error as exc:
            raise AuthorityStoreError("security epoch metadata read failed") from exc
        values = {str(row["key"]): str(row["value"]) for row in rows}
        try:
            return SecurityEpoch(
                value=int(values["security_epoch"]),
                advanced_at=float(values["security_epoch_advanced_at"]),
                reason_digest=values["security_epoch_reason_digest"],
            )
        except (KeyError, TypeError, ValueError, AuthorityValidationError) as exc:
            raise AuthorityStoreError("security epoch metadata is invalid") from exc

    @_process_owned
    def advance_security_epoch(self, reason: str) -> int:
        """Atomically advance SecurityEpoch and fence all old domains and Leases."""

        reason_digest = authority_digest({"reason": str(reason)})
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT value FROM authority_meta WHERE key='security_epoch'"
                ).fetchone()
                if row is None:
                    raise AuthorityStoreError("security epoch is unavailable")
                next_epoch = int(row["value"]) + 1
                connection.execute(
                    "UPDATE authority_meta SET value=? WHERE key='security_epoch'",
                    (str(next_epoch),),
                )
                connection.execute(
                    "UPDATE authority_meta SET value=? WHERE key='security_epoch_advanced_at'",
                    (str(self._clock()),),
                )
                connection.execute(
                    "UPDATE authority_meta SET value=? WHERE key='security_epoch_reason_digest'",
                    (reason_digest,),
                )
                connection.execute(
                    "UPDATE invocation_leases SET state=? WHERE security_epoch < ?"
                    " AND state IN (?, ?)",
                    (
                        LeaseState.REVOKED.value,
                        next_epoch,
                        LeaseState.ISSUED.value,
                        LeaseState.DISPATCHED.value,
                    ),
                )
                connection.execute("UPDATE execution_sessions SET active=0")
                connection.execute(
                    "UPDATE activation_reservations SET"
                    " state=CASE WHEN state='active' THEN 'retired' ELSE 'aborted' END,"
                    " updated_at=? WHERE security_epoch < ? AND state IN"
                    " ('prepared', 'ready_without_authority', 'committing', 'active')",
                    (self._clock(), next_epoch),
                )
                self._append_audit(
                    connection,
                    event_id=f"epoch-{next_epoch}",
                    event_type="security_epoch_advanced",
                    event_state="committed",
                    payload={
                        "security_epoch": next_epoch,
                        "reason_digest": reason_digest,
                    },
                )
                connection.commit()
                return next_epoch
        except AuditUnavailable:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuthorityStoreError("security epoch advance failed") from exc

    @_process_owned
    def reserve_activation(
        self,
        *,
        activation_id: str,
        profile_id: str,
        plan_digest: str,
        profile_authority_digest: str,
        security_epoch: int,
    ) -> tuple[str, int]:
        """Reserve one candidate activation and a never-reused fencing token."""

        reservation_id = "activation-reservation:" + secrets.token_urlsafe(24)
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                epoch_row = connection.execute(
                    "SELECT value FROM authority_meta WHERE key='security_epoch'"
                ).fetchone()
                token_row = connection.execute(
                    "SELECT value FROM authority_meta WHERE key='activation_fencing_token'"
                ).fetchone()
                if epoch_row is None or int(epoch_row["value"]) != security_epoch:
                    raise AuthorityDenied(
                        "activation has a stale SecurityEpoch", code="stale_epoch"
                    )
                if token_row is None:
                    raise AuthorityStoreError("activation fencing token is unavailable")
                fencing_token = int(token_row["value"]) + 1
                now = self._clock()
                duplicate = connection.execute(
                    "SELECT 1 FROM activation_reservations"
                    " WHERE activation_id=? AND state NOT IN ('aborted', 'retired')",
                    (activation_id,),
                ).fetchone()
                if duplicate is not None:
                    raise AuthorityDenied("activation identity is already reserved")
                connection.execute(
                    "INSERT INTO activation_reservations"
                    " (reservation_id, activation_id, profile_id, plan_digest,"
                    " profile_authority_digest, security_epoch, fencing_token, state,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        reservation_id,
                        activation_id,
                        profile_id,
                        plan_digest,
                        profile_authority_digest,
                        security_epoch,
                        fencing_token,
                        "prepared",
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE authority_meta SET value=? WHERE key='activation_fencing_token'",
                    (str(fencing_token),),
                )
                self._append_audit(
                    connection,
                    event_id=f"activation-prepared-{reservation_id}",
                    event_type="activation",
                    event_state="prepared",
                    payload={
                        "reservation_id": reservation_id,
                        "activation_id": activation_id,
                        "profile_id": profile_id,
                        "plan_digest": plan_digest,
                        "profile_authority_digest": profile_authority_digest,
                        "security_epoch": security_epoch,
                        "fencing_token": fencing_token,
                    },
                )
                connection.commit()
                return reservation_id, fencing_token
        except (AuthorityDenied, AuthorityStoreError, AuditUnavailable):
            raise
        except sqlite3.IntegrityError as exc:
            raise AuthorityDenied("activation identity was already reserved") from exc
        except sqlite3.Error as exc:
            raise AuthorityStoreError("activation reservation failed") from exc

    @_process_owned
    def transition_activation(
        self,
        reservation_id: str,
        *,
        expected_state: str,
        new_state: str,
    ) -> Mapping[str, Any]:
        """CAS one activation journal transition with an authoritative audit append."""

        allowed = {
            ("prepared", "ready_without_authority"),
            ("ready_without_authority", "committing"),
            ("committing", "active"),
            ("prepared", "aborted"),
            ("ready_without_authority", "aborted"),
            ("committing", "aborted"),
            ("active", "retired"),
        }
        if (expected_state, new_state) not in allowed:
            raise ValueError("invalid activation state transition")
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM activation_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                if row is None or str(row["state"]) != expected_state:
                    raise AuthorityDenied("activation reservation lost its state fence")
                epoch_row = connection.execute(
                    "SELECT value FROM authority_meta WHERE key='security_epoch'"
                ).fetchone()
                if new_state not in {"aborted", "retired"} and (
                    epoch_row is None or int(epoch_row["value"]) != row["security_epoch"]
                ):
                    raise AuthorityDenied(
                        "activation reservation has a stale SecurityEpoch",
                        code="stale_epoch",
                    )
                updated = connection.execute(
                    "UPDATE activation_reservations SET state=?, updated_at=?"
                    " WHERE reservation_id=? AND state=?",
                    (new_state, self._clock(), reservation_id, expected_state),
                )
                if updated.rowcount != 1:
                    raise AuthorityDenied("activation transition lost a race")
                payload = {
                    key: row[key]
                    for key in (
                        "reservation_id",
                        "activation_id",
                        "profile_id",
                        "plan_digest",
                        "profile_authority_digest",
                        "security_epoch",
                        "fencing_token",
                    )
                }
                if new_state == "active":
                    superseded = connection.execute(
                        "SELECT reservation_id, activation_id FROM activation_reservations"
                        " WHERE profile_id=? AND state='active' AND reservation_id<>?",
                        (row["profile_id"], reservation_id),
                    ).fetchall()
                    connection.execute(
                        "UPDATE activation_reservations SET state='retired', updated_at=?"
                        " WHERE profile_id=? AND state='active' AND reservation_id<>?",
                        (self._clock(), row["profile_id"], reservation_id),
                    )
                    for old in superseded:
                        self._append_audit(
                            connection,
                            event_id=f"activation-retired-{old['reservation_id']}",
                            event_type="activation",
                            event_state="retired",
                            payload={
                                "reservation_id": old["reservation_id"],
                                "activation_id": old["activation_id"],
                                "superseded_by": reservation_id,
                            },
                        )
                self._append_audit(
                    connection,
                    event_id=f"activation-{new_state}-{reservation_id}",
                    event_type="activation",
                    event_state=new_state,
                    payload=payload,
                )
                connection.commit()
                return {**payload, "state": new_state}
        except (AuthorityDenied, AuditUnavailable):
            raise
        except sqlite3.Error as exc:
            raise AuthorityStoreError("activation transition failed") from exc

    @_process_owned
    def activation_reservation(self, reservation_id: str) -> Mapping[str, Any] | None:
        """Return one durable activation reservation for Host recovery."""

        try:
            with self._lock, self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM activation_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AuthorityStoreError("activation reservation read failed") from exc
        return dict(row) if row is not None else None

    @_process_owned
    def incomplete_activation_reservations(self, profile_id: str) -> tuple[Mapping[str, Any], ...]:
        """Return candidate reservations that must be recovered before activation."""

        try:
            with self._lock, self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM activation_reservations WHERE profile_id=?"
                    " AND state IN ('prepared', 'ready_without_authority', 'committing')"
                    " ORDER BY fencing_token",
                    (profile_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise AuthorityStoreError("activation recovery inventory failed") from exc
        return tuple(dict(row) for row in rows)

    @_process_owned
    def active_activation_reservation(self, activation_id: str) -> Mapping[str, Any] | None:
        """Return the sole authoritative active reservation for restart capture."""

        try:
            with self._lock, self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM activation_reservations"
                    " WHERE activation_id=? AND state='active'",
                    (activation_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise AuthorityStoreError("active activation read failed") from exc
        if len(rows) > 1:
            raise AuthorityStoreError("multiple active authority reservations exist")
        return dict(rows[0]) if rows else None

    @_process_owned
    def bind_authenticated_session(
        self,
        *,
        session_id: str,
        domain: ExecutionDomain,
        channel_digest: str,
        principal_id: str,
    ) -> None:
        """Bind an authenticated channel to a Host-spawned domain principal."""

        if domain.state.value != "active":
            raise AuthorityDenied("execution domain is not active", code="domain_inactive")
        if domain.security_epoch != self.security_epoch:
            raise AuthorityDenied("execution domain has a stale SecurityEpoch", code="stale_epoch")
        if channel_digest != domain.authenticated_channel_digest:
            raise AuthorityDenied("authenticated channel does not match domain")
        if principal_id not in domain.principal_ids:
            raise AuthorityDenied("principal is not assigned to execution domain")
        persisted = self.get_domain(domain.domain_id)
        if persisted is None or persisted.identity_digest != domain.identity_digest:
            raise AuthorityDenied("execution domain is not registered")
        try:
            with self._lock, self._connection() as connection:
                connection.execute(
                    "INSERT INTO execution_sessions"
                    " (session_id, domain_id, profile_id, activation_id, boot_epoch,"
                    " channel_digest, principal_id, active, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (
                        session_id,
                        domain.domain_id,
                        domain.profile_id,
                        domain.activation_id,
                        domain.boot_epoch,
                        channel_digest,
                        principal_id,
                        self._clock(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthorityDenied("authenticated session cannot be replayed") from exc
        except sqlite3.Error as exc:
            raise AuthorityStoreError("session binding failed") from exc

    @_process_owned
    def transition_domain(
        self,
        domain_id: str,
        *,
        expected_boot_epoch: int,
        expected_state: DomainState,
        new_state: DomainState,
    ) -> ExecutionDomain:
        """Durably transition an ExecutionDomain with compare-and-swap semantics."""

        allowed = {
            DomainState.STARTING: {
                DomainState.ACTIVE,
                DomainState.REVOKED,
                DomainState.STOPPED,
            },
            DomainState.ACTIVE: {
                DomainState.DRAINING,
                DomainState.FENCED,
                DomainState.REVOKED,
                DomainState.STOPPED,
            },
            DomainState.DRAINING: {
                DomainState.FENCED,
                DomainState.REVOKED,
                DomainState.STOPPED,
            },
            DomainState.FENCED: {DomainState.REVOKED, DomainState.STOPPED},
            DomainState.REVOKED: {DomainState.STOPPED},
            DomainState.STOPPED: set(),
        }
        if new_state not in allowed[expected_state]:
            raise AuthorityDenied("invalid execution-domain lifecycle transition")
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT encrypted_payload FROM authority_records"
                    " WHERE record_type='execution_domain' AND record_id=?",
                    (domain_id,),
                ).fetchone()
                if row is None:
                    raise AuthorityDenied("execution domain is unavailable")
                current = ExecutionDomain.from_dict(self._decrypt(row["encrypted_payload"]))
                if current.boot_epoch != expected_boot_epoch or current.state is not expected_state:
                    raise AuthorityDenied("execution-domain lifecycle CAS failed")
                updated = replace(current, state=new_state)
                payload = updated.to_dict()
                connection.execute(
                    "UPDATE authority_records SET record_digest=?, encrypted_payload=?"
                    " WHERE record_type='execution_domain' AND record_id=?",
                    (authority_digest(payload), self._encrypt(payload), domain_id),
                )
                if new_state is not DomainState.ACTIVE:
                    connection.execute(
                        "UPDATE execution_sessions SET active=0 WHERE domain_id=?",
                        (domain_id,),
                    )
                if new_state in {
                    DomainState.FENCED,
                    DomainState.REVOKED,
                    DomainState.STOPPED,
                }:
                    connection.execute(
                        "UPDATE invocation_leases SET state=?"
                        " WHERE (caller_domain_id=? OR target_domain_id=?)"
                        " AND state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            domain_id,
                            domain_id,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                self._append_audit(
                    connection,
                    event_id="domain-transition-" + secrets.token_hex(16),
                    event_type="execution_domain_lifecycle",
                    event_state="committed",
                    payload={
                        "domain_id": domain_id,
                        "boot_epoch": expected_boot_epoch,
                        "old_state": expected_state.value,
                        "new_state": new_state.value,
                        "security_epoch": updated.security_epoch,
                    },
                )
                connection.commit()
                return updated
        except AuthorityDenied:
            raise
        except AuditUnavailable:
            raise
        except sqlite3.Error as exc:
            raise AuthorityStoreError("execution-domain transition failed") from exc

    @_process_owned
    def resolve_authenticated_session(self, session_id: str) -> tuple[ExecutionDomain, str]:
        """Resolve Host-authenticated caller identity; never use payload identity."""

        try:
            with self._lock, self._connection() as connection:
                row = connection.execute(
                    "SELECT domain_id, boot_epoch, channel_digest, principal_id, active"
                    " FROM execution_sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AuthorityStoreError("session lookup failed") from exc
        if row is None or not row["active"]:
            raise AuthorityDenied("caller session is unknown or inactive")
        domain = self.get_domain(str(row["domain_id"]))
        if domain is None:
            raise AuthorityDenied("caller execution domain is unavailable")
        if (
            domain.boot_epoch != int(row["boot_epoch"])
            or domain.authenticated_channel_digest != str(row["channel_digest"])
            or domain.state.value != "active"
            or domain.security_epoch != self.security_epoch
        ):
            raise AuthorityDenied("caller execution domain identity is stale")
        principal_id = str(row["principal_id"])
        if principal_id not in domain.principal_ids:
            raise AuthorityDenied("caller principal binding is invalid")
        return domain, principal_id

    @_process_owned
    def revoke(
        self,
        *,
        target_kind: str,
        target_id: str,
        reason: str,
    ) -> str:
        """Persist a revocation and immediately fence matching Lease/session state."""

        allowed_kinds = {
            "function_principal",
            "execution_domain",
            "pack_artifact",
            "publisher",
            "profile",
            "host_extension",
            "grant",
            "provider_authority",
            "activation",
            "global",
        }
        if target_kind not in allowed_kinds:
            raise ValueError("unsupported revocation target")
        revocation_id = "rev-" + secrets.token_hex(16)
        reason_digest = authority_digest({"reason": str(reason)})
        epoch = self.security_epoch
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO revocations"
                    " (revocation_id, target_kind, target_id, security_epoch,"
                    " reason_digest, created_at) VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(target_kind, target_id) DO NOTHING",
                    (
                        revocation_id,
                        target_kind,
                        target_id,
                        epoch,
                        reason_digest,
                        self._clock(),
                    ),
                )
                if target_kind == "execution_domain":
                    connection.execute(
                        "UPDATE execution_sessions SET active=0 WHERE domain_id=?",
                        (target_id,),
                    )
                    connection.execute(
                        "UPDATE invocation_leases SET state=?"
                        " WHERE (caller_domain_id=? OR target_domain_id=?)"
                        " AND state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            target_id,
                            target_id,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                elif target_kind == "function_principal":
                    connection.execute(
                        "UPDATE invocation_leases SET state=?"
                        " WHERE (caller_principal_id=? OR target_principal_id=?)"
                        " AND state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            target_id,
                            target_id,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                elif target_kind == "pack_artifact":
                    connection.execute(
                        "UPDATE invocation_leases SET state=?"
                        " WHERE (caller_artifact_digest=? OR target_artifact_digest=?)"
                        " AND state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            target_id,
                            target_id,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                elif target_kind == "publisher":
                    connection.execute(
                        "UPDATE invocation_leases SET state=?"
                        " WHERE (caller_publisher_lineage=?"
                        " OR target_publisher_lineage=?) AND state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            target_id,
                            target_id,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                elif target_kind == "host_extension":
                    connection.execute(
                        "UPDATE invocation_leases SET state=?"
                        " WHERE host_extension_id=? AND state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            target_id,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                elif target_kind == "profile":
                    connection.execute(
                        "UPDATE execution_sessions SET active=0 WHERE profile_id=?",
                        (target_id,),
                    )
                    connection.execute(
                        "UPDATE invocation_leases SET state=? WHERE profile_id=?"
                        " AND state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            target_id,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                elif target_kind == "grant":
                    connection.execute(
                        "UPDATE invocation_leases SET state=? WHERE grant_id=? AND state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            target_id,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                elif target_kind == "provider_authority":
                    connection.execute(
                        "UPDATE invocation_leases SET state=?"
                        " WHERE provider_authority_id=? AND state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            target_id,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                elif target_kind == "activation":
                    connection.execute(
                        "UPDATE execution_sessions SET active=0 WHERE activation_id=?",
                        (target_id,),
                    )
                    connection.execute(
                        "UPDATE invocation_leases SET state=?"
                        " WHERE activation_id=? AND state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            target_id,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                elif target_kind == "global":
                    connection.execute("UPDATE execution_sessions SET active=0")
                    connection.execute(
                        "UPDATE invocation_leases SET state=? WHERE state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                self._append_audit(
                    connection,
                    event_id=revocation_id,
                    event_type="authority_revoked",
                    event_state="committed",
                    payload={
                        "target_kind": target_kind,
                        "target_id": target_id,
                        "security_epoch": epoch,
                        "reason_digest": reason_digest,
                    },
                )
                connection.commit()
        except AuditUnavailable:
            raise
        except sqlite3.Error as exc:
            raise AuthorityStoreError("revocation commit failed") from exc
        return revocation_id

    @_process_owned
    def revoke_pack_approval(
        self,
        *,
        pack_id: str,
        approval_revision: str,
        profile_id: str,
        activation_id: str,
        artifact_digest: str,
        reason: str,
    ) -> tuple[str, tuple[str, ...]]:
        """Atomically revoke one Pack approval and its active exact Grants.

        The approval revision is a replay fence. Grants are selected from the
        authenticated authority records by exact Profile, activation, and target
        artifact, so a Pack-control caller cannot name arbitrary Grant IDs.
        """

        if not all(
            str(value or "").strip()
            for value in (
                approval_revision,
                pack_id,
                profile_id,
                activation_id,
                artifact_digest,
                reason,
            )
        ):
            raise ValueError("Pack approval revocation fields are required")
        revocation_id = "rev-" + secrets.token_hex(16)
        reason_digest = authority_digest({"reason": str(reason)})
        epoch = self.security_epoch
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._is_revoked(connection, "approval", approval_revision):
                    raise AuthorityDenied(
                        "Pack approval revision is already revoked",
                        code="revoked",
                    )
                rows = connection.execute(
                    "SELECT record_id, record_digest, encrypted_payload"
                    " FROM authority_records WHERE record_type='grant'"
                    " ORDER BY record_id"
                ).fetchall()
                grant_ids: list[str] = []
                for row in rows:
                    value = self._decrypt(row["encrypted_payload"])
                    if not hmac.compare_digest(str(row["record_digest"]), authority_digest(value)):
                        raise AuthorityStoreError("authority record digest mismatch")
                    grant = GrantRecord.from_dict(value)
                    if (
                        grant.profile_id == profile_id
                        and grant.activation_id == activation_id
                        and grant.target.parent_artifact_digest == artifact_digest
                    ):
                        grant_ids.append(grant.grant_id)
                now = self._clock()
                connection.execute(
                    "INSERT INTO revocations"
                    " (revocation_id, target_kind, target_id, security_epoch,"
                    " reason_digest, created_at) VALUES (?, 'approval', ?, ?, ?, ?)",
                    (
                        revocation_id,
                        approval_revision,
                        epoch,
                        reason_digest,
                        now,
                    ),
                )
                for grant_id in grant_ids:
                    connection.execute(
                        "INSERT OR IGNORE INTO revocations"
                        " (revocation_id, target_kind, target_id, security_epoch,"
                        " reason_digest, created_at) VALUES (?, 'grant', ?, ?, ?, ?)",
                        (
                            "rev-" + secrets.token_hex(16),
                            grant_id,
                            epoch,
                            reason_digest,
                            now,
                        ),
                    )
                if grant_ids:
                    placeholders = ",".join("?" for _item in grant_ids)
                    connection.execute(
                        "UPDATE invocation_leases SET state=?"
                        f" WHERE grant_id IN ({placeholders}) AND state IN (?, ?)",
                        (
                            LeaseState.REVOKED.value,
                            *grant_ids,
                            LeaseState.ISSUED.value,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                self._append_audit(
                    connection,
                    event_id=revocation_id,
                    event_type="pack_approval_revoked",
                    event_state="committed",
                    payload={
                        "approval_revision": approval_revision,
                        "pack_id": pack_id,
                        "profile_id": profile_id,
                        "activation_id": activation_id,
                        "artifact_digest": artifact_digest,
                        "grant_ids": grant_ids,
                        "security_epoch": epoch,
                        "reason_digest": reason_digest,
                    },
                )
                connection.commit()
        except (AuthorityDenied, AuditUnavailable, AuthorityStoreError):
            raise
        except sqlite3.IntegrityError as exc:
            raise AuthorityDenied(
                "Pack approval or active Grant is already revoked",
                code="revoked",
            ) from exc
        except sqlite3.Error as exc:
            raise AuthorityStoreError("Pack approval revocation failed") from exc
        return revocation_id, tuple(grant_ids)

    @_process_owned
    def is_revoked(self, target_kind: str, target_id: str) -> bool:
        """Return whether an exact target has an active Host revocation."""

        try:
            with self._lock, self._connection() as connection:
                return self._is_revoked(connection, target_kind, target_id)
        except sqlite3.Error as exc:
            raise AuthorityStoreError("revocation lookup failed") from exc

    @staticmethod
    def _is_revoked(connection: _IdentityBoundConnection, target_kind: str, target_id: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM revocations WHERE"
            " (target_kind=? AND target_id=?) OR target_kind='global' LIMIT 1",
            (target_kind, target_id),
        ).fetchone()
        return row is not None

    @_process_owned
    def issue_lease_with_audit(
        self,
        *,
        grant: GrantRecord,
        lease: InvocationLease,
        audit_payload: Mapping[str, Any],
        revocation_targets: Iterable[tuple[str, str]],
    ) -> str:
        """Atomically reserve Grant use, authoritative audit, and one Lease.

        If audit storage is unavailable every write is rolled back, including the
        Grant use.  This is the required fail-closed effect gate.
        """

        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                epoch_row = connection.execute(
                    "SELECT value FROM authority_meta WHERE key='security_epoch'"
                ).fetchone()
                if epoch_row is None or int(epoch_row["value"]) != lease.security_epoch:
                    raise AuthorityDenied("SecurityEpoch changed", code="stale_epoch")
                for target_kind, target_id in revocation_targets:
                    if self._is_revoked(connection, target_kind, target_id):
                        raise AuthorityDenied(f"{target_kind} is revoked", code="revoked")
                grant_row = connection.execute(
                    "SELECT record_digest FROM authority_records"
                    " WHERE record_type='grant' AND record_id=?",
                    (grant.grant_id,),
                ).fetchone()
                if grant_row is None or not hmac.compare_digest(
                    str(grant_row["record_digest"]), grant.digest
                ):
                    raise AuthorityDenied("Grant definition changed or is unavailable")
                checked_domains: dict[str, ExecutionDomain] = {}
                for domain_id, boot_epoch, principal_id in (
                    (
                        lease.caller_domain_id,
                        lease.caller_boot_epoch,
                        lease.caller.principal_id,
                    ),
                    (
                        lease.target_domain_id,
                        lease.target_boot_epoch,
                        lease.target.principal_id,
                    ),
                ):
                    domain_row = connection.execute(
                        "SELECT encrypted_payload FROM authority_records"
                        " WHERE record_type='execution_domain' AND record_id=?",
                        (domain_id,),
                    ).fetchone()
                    if domain_row is None:
                        raise AuthorityDenied("execution domain is unavailable")
                    domain = ExecutionDomain.from_dict(
                        self._decrypt(domain_row["encrypted_payload"])
                    )
                    if (
                        domain.state is not DomainState.ACTIVE
                        or domain.boot_epoch != boot_epoch
                        or domain.security_epoch != lease.security_epoch
                        or domain.profile_id != lease.profile_id
                        or domain.activation_id != lease.activation_id
                        or domain.fencing_token != lease.fencing_token
                        or principal_id not in domain.principal_ids
                    ):
                        raise AuthorityDenied("execution domain changed before reservation")
                    checked_domains[domain_id] = domain
                target_domain = checked_domains[lease.target_domain_id]
                if target_domain.resource_namespace != lease.resource_namespace:
                    raise AuthorityDenied("ResourceHandle namespace changed")
                usage = connection.execute(
                    "SELECT reserved_uses, committed_uses FROM grant_usage WHERE grant_id=?",
                    (grant.grant_id,),
                ).fetchone()
                if usage is None:
                    raise AuthorityDenied("Grant is not registered")
                total_uses = int(usage["reserved_uses"]) + int(usage["committed_uses"])
                if grant.max_uses is not None and total_uses >= grant.max_uses:
                    raise AuthorityDenied("Grant use limit is exhausted")
                connection.execute(
                    "UPDATE grant_usage SET reserved_uses=reserved_uses+1 WHERE grant_id=?",
                    (grant.grant_id,),
                )
                self._append_audit(
                    connection,
                    event_id=lease.audit_reservation_id,
                    event_type="host_effect",
                    event_state="reserved",
                    payload=dict(audit_payload),
                )
                connection.execute(
                    "INSERT INTO invocation_leases"
                    " (lease_id, lease_digest, encrypted_payload, caller_principal_id,"
                    " request_id,"
                    " target_principal_id, caller_artifact_digest, target_artifact_digest,"
                    " caller_publisher_lineage, target_publisher_lineage, host_extension_id,"
                    " caller_domain_id, target_domain_id, profile_id,"
                    " activation_id, grant_id, audit_reservation_id, security_epoch,"
                    " provider_authority_id, expires_at, state)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lease.lease_id,
                        lease.digest,
                        self._encrypt(lease.to_dict()),
                        lease.caller.principal_id,
                        lease.request_id,
                        lease.target.principal_id,
                        lease.caller.parent_artifact_digest,
                        lease.target.parent_artifact_digest,
                        lease.caller_publisher_lineage,
                        lease.target_publisher_lineage,
                        lease.host_extension_id,
                        lease.caller_domain_id,
                        lease.target_domain_id,
                        lease.profile_id,
                        lease.activation_id,
                        lease.grant_id,
                        lease.audit_reservation_id,
                        lease.security_epoch,
                        lease.provider_authority_id,
                        lease.expires_at,
                        LeaseState.ISSUED.value,
                    ),
                )
                connection.commit()
        except AuthorityDenied:
            raise
        except AuditUnavailable:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AuditUnavailable("authority reservation could not be committed") from exc
        return self._encode_lease_token(lease)

    def _append_audit(
        self,
        connection: _IdentityBoundConnection,
        *,
        event_id: str,
        event_type: str,
        event_state: str,
        payload: Mapping[str, Any],
    ) -> str:
        if self._audit_fault is not None:
            try:
                self._audit_fault()
            except Exception as exc:
                raise AuditUnavailable("authoritative audit is unavailable") from exc
        previous = connection.execute(
            "SELECT event_digest FROM authority_audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_digest = (
            str(previous["event_digest"]) if previous is not None else "sha256:" + "0" * 64
        )
        created_at = self._clock()
        event_digest = authority_digest(
            {
                "event_id": event_id,
                "event_type": event_type,
                "event_state": event_state,
                "previous_digest": previous_digest,
                "payload": dict(payload),
                "created_at": created_at,
            }
        )
        try:
            connection.execute(
                "INSERT INTO authority_audit"
                " (event_id, event_type, event_state, previous_digest, event_digest,"
                " encrypted_payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    event_type,
                    event_state,
                    previous_digest,
                    event_digest,
                    self._encrypt(dict(payload)),
                    created_at,
                ),
            )
        except sqlite3.Error as exc:
            raise AuditUnavailable("authoritative audit append failed") from exc
        return event_digest

    def _encode_lease_token(self, lease: InvocationLease) -> str:
        self._assert_crypto_material()
        payload = canonical_json({"lease_id": lease.lease_id, "digest": lease.digest})
        signature = hmac.new(self._mac_key, payload, hashlib.sha256).digest()
        encoded_payload = base64.urlsafe_b64encode(payload).decode("ascii")
        encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii")
        return f"{encoded_payload}.{encoded_signature}"

    def _decode_lease_token(self, token: str) -> tuple[str, str]:
        self._assert_crypto_material()
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            payload = base64.urlsafe_b64decode(encoded_payload.encode("ascii"))
            signature = base64.urlsafe_b64decode(encoded_signature.encode("ascii"))
            if not hmac.compare_digest(
                signature, hmac.new(self._mac_key, payload, hashlib.sha256).digest()
            ):
                raise AuthorityDenied("InvocationLease token is invalid")
            value = json.loads(payload.decode("utf-8"))
            return str(value["lease_id"]), str(value["digest"])
        except AuthorityDenied:
            raise
        except (ValueError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuthorityDenied("InvocationLease token is malformed") from exc

    @_process_owned
    def inspect_lease_token(self, token: str) -> tuple[InvocationLease, LeaseState]:
        """Authenticate a lease token and return its durable Host-side record.

        This is a TCB adapter operation.  It does not consume the lease and must
        never be exposed to a Provider or Pack process.
        """

        lease_id, expected_digest = self._decode_lease_token(token)
        result = self.get_lease(lease_id)
        if result is None:
            raise AuthorityDenied("InvocationLease is unknown")
        lease, state = result
        if not hmac.compare_digest(lease.digest, expected_digest):
            raise AuthorityDenied("InvocationLease digest does not match")
        return lease, state

    @_process_owned
    def fence_request(self, request_id: str) -> list[str]:
        """Revoke every unused lease for one exact Host request."""

        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT lease_id, grant_id FROM invocation_leases"
                    " WHERE request_id=? AND state=?",
                    (request_id, LeaseState.ISSUED.value),
                ).fetchall()
                lease_ids = [str(row["lease_id"]) for row in rows]
                for row in rows:
                    connection.execute(
                        "UPDATE invocation_leases SET state=? WHERE lease_id=? AND state=?",
                        (
                            LeaseState.REVOKED.value,
                            row["lease_id"],
                            LeaseState.ISSUED.value,
                        ),
                    )
                    connection.execute(
                        "UPDATE grant_usage SET reserved_uses=reserved_uses-1"
                        " WHERE grant_id=? AND reserved_uses > 0",
                        (row["grant_id"],),
                    )
                    self._append_audit(
                        connection,
                        event_id=f"fence-{row['lease_id']}",
                        event_type="host_effect",
                        event_state=LeaseState.REVOKED.value,
                        payload={
                            "lease_id": row["lease_id"],
                            "request_id": request_id,
                        },
                    )
                connection.commit()
                return lease_ids
        except AuditUnavailable:
            raise
        except sqlite3.Error as exc:
            raise AuthorityStoreError("request fencing failed") from exc

    @_process_owned
    def dispatch_lease(
        self,
        token: str,
        *,
        target_domain_id: str,
        target_boot_epoch: int,
        request_digest: str,
    ) -> InvocationLease:
        """Atomically consume a Lease immediately before the Provider effect."""

        self.expire_leases()
        lease_id, expected_digest = self._decode_lease_token(token)
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT encrypted_payload, lease_digest, state FROM invocation_leases"
                    " WHERE lease_id=?",
                    (lease_id,),
                ).fetchone()
                if row is None:
                    raise AuthorityDenied("InvocationLease is unknown")
                if row["state"] != LeaseState.ISSUED.value:
                    raise AuthorityDenied("InvocationLease was already used or revoked")
                value = self._decrypt(row["encrypted_payload"])
                lease = InvocationLease.from_dict(value)
                if not hmac.compare_digest(expected_digest, str(row["lease_digest"])):
                    raise AuthorityDenied("InvocationLease digest does not match")
                if lease.digest != expected_digest:
                    raise AuthorityDenied("InvocationLease payload was altered")
                if self._clock() >= lease.expires_at:
                    raise AuthorityDenied("InvocationLease expired")
                epoch_row = connection.execute(
                    "SELECT value FROM authority_meta WHERE key='security_epoch'"
                ).fetchone()
                if epoch_row is None or int(epoch_row["value"]) != lease.security_epoch:
                    raise AuthorityDenied("InvocationLease has a stale SecurityEpoch")
                if (
                    lease.target_domain_id != target_domain_id
                    or lease.target_boot_epoch != target_boot_epoch
                    or lease.request_digest != request_digest
                ):
                    raise AuthorityDenied("InvocationLease context does not match")
                for target_kind, target_id in (
                    ("function_principal", lease.caller.principal_id),
                    ("function_principal", lease.target.principal_id),
                    ("execution_domain", lease.caller_domain_id),
                    ("execution_domain", lease.target_domain_id),
                    ("profile", lease.profile_id),
                    ("activation", lease.activation_id),
                    ("grant", lease.grant_id),
                    ("provider_authority", lease.provider_authority_id),
                    ("pack_artifact", lease.caller.parent_artifact_digest),
                    ("pack_artifact", lease.target.parent_artifact_digest),
                    ("publisher", lease.caller_publisher_lineage),
                    ("publisher", lease.target_publisher_lineage),
                    ("host_extension", lease.host_extension_id),
                ):
                    if self._is_revoked(connection, target_kind, target_id):
                        raise AuthorityDenied("InvocationLease context was revoked")
                target_domain = self.get_domain(lease.target_domain_id)
                if (
                    target_domain is None
                    or target_domain.boot_epoch != lease.target_boot_epoch
                    or target_domain.state.value != "active"
                    or target_domain.security_epoch != lease.security_epoch
                    or lease.target.principal_id not in target_domain.principal_ids
                ):
                    raise AuthorityDenied("target execution domain is stale")
                updated = connection.execute(
                    "UPDATE invocation_leases SET state=? WHERE lease_id=? AND state=?",
                    (
                        LeaseState.DISPATCHED.value,
                        lease_id,
                        LeaseState.ISSUED.value,
                    ),
                )
                if updated.rowcount != 1:
                    raise AuthorityDenied("InvocationLease lost a dispatch race")
                self._append_audit(
                    connection,
                    event_id="dispatch-" + lease_id,
                    event_type="host_effect",
                    event_state="dispatched",
                    payload={
                        "lease_id": lease_id,
                        "reservation_id": lease.audit_reservation_id,
                        "request_digest": lease.request_digest,
                    },
                )
                connection.commit()
                return lease
        except AuthorityDenied:
            raise
        except AuditUnavailable:
            raise
        except sqlite3.Error as exc:
            raise AuthorityStoreError("InvocationLease dispatch failed") from exc

    @_process_owned
    def expire_leases(self) -> list[str]:
        """Expire unused Leases and release their Grant-use reservations."""

        expired: list[str] = []
        now = self._clock()
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT lease_id, grant_id, audit_reservation_id"
                    " FROM invocation_leases WHERE state=? AND expires_at <= ?",
                    (LeaseState.ISSUED.value, now),
                ).fetchall()
                for row in rows:
                    lease_id = str(row["lease_id"])
                    connection.execute(
                        "UPDATE invocation_leases SET state=? WHERE lease_id=? AND state=?",
                        (
                            LeaseState.EXPIRED.value,
                            lease_id,
                            LeaseState.ISSUED.value,
                        ),
                    )
                    connection.execute(
                        "UPDATE grant_usage SET reserved_uses=reserved_uses-1"
                        " WHERE grant_id=? AND reserved_uses > 0",
                        (row["grant_id"],),
                    )
                    self._append_audit(
                        connection,
                        event_id=f"expire-{lease_id}",
                        event_type="host_effect",
                        event_state=LeaseState.EXPIRED.value,
                        payload={
                            "lease_id": lease_id,
                            "reservation_id": row["audit_reservation_id"],
                        },
                    )
                    expired.append(lease_id)
                connection.commit()
        except AuditUnavailable:
            raise
        except sqlite3.Error as exc:
            raise AuthorityStoreError("InvocationLease expiry failed") from exc
        return expired

    @_process_owned
    def get_lease(self, lease_id: str) -> tuple[InvocationLease, LeaseState] | None:
        """Load and authenticate a Lease for Host-side delegation checks."""

        try:
            with self._lock, self._connection() as connection:
                row = connection.execute(
                    "SELECT encrypted_payload, lease_digest, state"
                    " FROM invocation_leases WHERE lease_id=?",
                    (lease_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AuthorityStoreError("InvocationLease read failed") from exc
        if row is None:
            return None
        lease = InvocationLease.from_dict(self._decrypt(row["encrypted_payload"]))
        if not hmac.compare_digest(lease.digest, str(row["lease_digest"])):
            raise AuthorityStoreError("InvocationLease digest mismatch")
        return lease, LeaseState(str(row["state"]))

    @_process_owned
    def finish_lease(
        self,
        lease_id: str,
        *,
        state: LeaseState,
        outcome_digest: str,
    ) -> None:
        """Durably finish a dispatched effect and its Grant reservation."""

        if state not in {LeaseState.COMMITTED, LeaseState.FAILED, LeaseState.AMBIGUOUS}:
            raise ValueError("invalid final Lease state")
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT grant_id, audit_reservation_id, state"
                    " FROM invocation_leases WHERE lease_id=?",
                    (lease_id,),
                ).fetchone()
                if row is None or row["state"] != LeaseState.DISPATCHED.value:
                    raise AuthorityDenied("InvocationLease is not dispatched")
                updated = connection.execute(
                    "UPDATE invocation_leases SET state=?, outcome_digest=?"
                    " WHERE lease_id=? AND state=?",
                    (state.value, outcome_digest, lease_id, LeaseState.DISPATCHED.value),
                )
                if updated.rowcount != 1:
                    raise AuthorityDenied("InvocationLease finish lost a race")
                if state in {LeaseState.COMMITTED, LeaseState.AMBIGUOUS}:
                    connection.execute(
                        "UPDATE grant_usage SET reserved_uses=reserved_uses-1,"
                        " committed_uses=committed_uses+1 WHERE grant_id=?"
                        " AND reserved_uses > 0",
                        (row["grant_id"],),
                    )
                else:
                    connection.execute(
                        "UPDATE grant_usage SET reserved_uses=reserved_uses-1"
                        " WHERE grant_id=? AND reserved_uses > 0",
                        (row["grant_id"],),
                    )
                self._append_audit(
                    connection,
                    event_id=f"finish-{lease_id}",
                    event_type="host_effect",
                    event_state=state.value,
                    payload={
                        "lease_id": lease_id,
                        "reservation_id": row["audit_reservation_id"],
                        "outcome_digest": outcome_digest,
                    },
                )
                connection.commit()
        except AuthorityDenied:
            raise
        except AuditUnavailable:
            raise
        except sqlite3.Error as exc:
            raise AuthorityStoreError("InvocationLease finalization failed") from exc

    @_process_owned
    def recover_incomplete_effects(self) -> list[str]:
        """Mark crash-surviving dispatched effects ambiguous, never retrying them."""

        recovered: list[str] = []
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT lease_id, grant_id, audit_reservation_id"
                    " FROM invocation_leases WHERE state=?",
                    (LeaseState.DISPATCHED.value,),
                ).fetchall()
                for row in rows:
                    lease_id = str(row["lease_id"])
                    outcome_digest = authority_digest(
                        {"status": "ambiguous_after_crash", "lease_id": lease_id}
                    )
                    connection.execute(
                        "UPDATE invocation_leases SET state=?, outcome_digest=?"
                        " WHERE lease_id=? AND state=?",
                        (
                            LeaseState.AMBIGUOUS.value,
                            outcome_digest,
                            lease_id,
                            LeaseState.DISPATCHED.value,
                        ),
                    )
                    connection.execute(
                        "UPDATE grant_usage SET reserved_uses=reserved_uses-1,"
                        " committed_uses=committed_uses+1 WHERE grant_id=?"
                        " AND reserved_uses > 0",
                        (row["grant_id"],),
                    )
                    self._append_audit(
                        connection,
                        event_id=f"recover-{lease_id}",
                        event_type="host_effect",
                        event_state=LeaseState.AMBIGUOUS.value,
                        payload={
                            "lease_id": lease_id,
                            "reservation_id": row["audit_reservation_id"],
                            "outcome_digest": outcome_digest,
                        },
                    )
                    recovered.append(lease_id)
                connection.commit()
        except AuditUnavailable:
            raise
        except sqlite3.Error as exc:
            raise AuthorityStoreError("effect recovery failed") from exc
        return recovered

    @_process_owned
    def audit_events(self) -> list[dict[str, Any]]:
        """Read and verify the complete authoritative audit hash chain."""

        try:
            with self._lock, self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM authority_audit ORDER BY sequence"
                ).fetchall()
        except sqlite3.Error as exc:
            raise AuthorityStoreError("audit read failed") from exc
        return self._verify_audit_rows(rows)

    def _verify_audit_connection(self, connection: _IdentityBoundConnection) -> None:
        """Verify the authoritative chain before any schema migration write."""

        rows = connection.execute("SELECT * FROM authority_audit ORDER BY sequence").fetchall()
        self._verify_audit_rows(rows)

    def _verify_audit_rows(self, rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        """Verify and decode ordered authoritative audit rows."""

        previous_digest = "sha256:" + "0" * 64
        output: list[dict[str, Any]] = []
        for row in rows:
            payload = self._decrypt(row["encrypted_payload"])
            expected = authority_digest(
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "event_state": row["event_state"],
                    "previous_digest": previous_digest,
                    "payload": payload,
                    "created_at": row["created_at"],
                }
            )
            if row["previous_digest"] != previous_digest or not hmac.compare_digest(
                str(row["event_digest"]), expected
            ):
                raise AuthorityStoreError("authoritative audit chain is invalid")
            output.append(
                {
                    "sequence": row["sequence"],
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "event_state": row["event_state"],
                    "previous_digest": row["previous_digest"],
                    "event_digest": row["event_digest"],
                    "payload": payload,
                    "created_at": row["created_at"],
                }
            )
            previous_digest = str(row["event_digest"])
        return output

    @_process_owned
    def grant_usage(self, grant_id: str) -> tuple[int, int]:
        """Return ``(reserved, committed)`` use counters for tests/operations."""

        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT reserved_uses, committed_uses FROM grant_usage WHERE grant_id=?",
                (grant_id,),
            ).fetchone()
        if row is None:
            raise AuthorityStoreError("Grant usage is unavailable")
        return int(row["reserved_uses"]), int(row["committed_uses"])
