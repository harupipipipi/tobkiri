"""Host-global active Profile pointer and activation snapshot verification.

The pointer in ``profiles/active.json`` is the only source of the Profile used
for a fresh runtime capture.  It is intentionally outside every
``workspaces/<profile_id>`` tree.  A pointer is accepted only when its exact
Profile revision, activation ID, Plan digest, and ProfileLock digest match the
corresponding per-Profile activation envelope.
"""

from __future__ import annotations

import copy
import errno
import importlib
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.ids import validate_artifact_digest, validate_canonical_id
from tobkiri_protocol.secure_persistence import SecureDirectory, SecurePersistenceError


ACTIVE_PROFILE_SCHEMA = "io.tobkiri.active-profile-pointer.v1"
_ACTIVATION_ID_RE = re.compile(r"^activation:[a-z0-9][a-z0-9._-]{7,127}$")


class ActiveProfileStoreError(RuntimeError):
    """Base error for active Profile pointer operations."""


class ActiveProfileStoreIntegrityError(ActiveProfileStoreError):
    """Raised when an active pointer or activation snapshot is unsafe."""


class ActiveProfileStoreConflict(ActiveProfileStoreError):
    """Raised when a stale pointer attempts to replace a newer pointer."""


class ActiveProfileStoreLockTimeout(ActiveProfileStoreError):
    """Raised when the Host-global pointer lock cannot be acquired."""


def _default_snapshot_path(profile_id: str, activation_id: str) -> str:
    """Return the only activation-envelope path valid for one identity."""

    suffix = activation_id.removeprefix("activation:")
    return (
        Path("workspaces")
        / profile_id
        / "activation"
        / "activations"
        / f"{suffix}.json"
    ).as_posix()


def _validate_snapshot_path_binding(
    profile_id: str,
    activation_id: str,
    snapshot_path: str,
) -> None:
    """Reject paths that are not canonical for the Profile activation."""

    if snapshot_path != _default_snapshot_path(profile_id, activation_id):
        raise ActiveProfileStoreIntegrityError(
            "activation snapshot path is not bound to the active Profile activation"
        )


@dataclass(frozen=True)
class ActiveProfilePointer:
    """The immutable identity binding published by the Host."""

    profile_id: str
    profile_revision: str
    activation_id: str
    plan_digest: str
    lock_digest: str
    activation_snapshot_path: str
    activation_snapshot_digest: str
    catalog_revision: str | None = None
    generation: int = 1
    updated_at: int = 0

    def __post_init__(self) -> None:
        try:
            validate_canonical_id(self.profile_id, field="profile_id")
            validate_artifact_digest(self.profile_revision, field="profile_revision")
            validate_artifact_digest(self.plan_digest, field="plan_digest")
            validate_artifact_digest(self.lock_digest, field="lock_digest")
            validate_artifact_digest(
                self.activation_snapshot_digest,
                field="activation_snapshot_digest",
            )
            if self.catalog_revision is not None:
                validate_artifact_digest(
                    self.catalog_revision,
                    field="catalog_revision",
                )
        except Exception as error:
            raise ActiveProfileStoreIntegrityError(str(error)) from error
        if _ACTIVATION_ID_RE.fullmatch(self.activation_id) is None:
            raise ActiveProfileStoreIntegrityError("activation_id is not canonical")
        if not self._safe_snapshot_path(self.activation_snapshot_path):
            raise ActiveProfileStoreIntegrityError(
                "activation snapshot path must be relative and traversal-free"
            )
        _validate_snapshot_path_binding(
            self.profile_id,
            self.activation_id,
            self.activation_snapshot_path,
        )
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ActiveProfileStoreIntegrityError("generation must be a positive integer")
        if (
            isinstance(self.updated_at, bool)
            or not isinstance(self.updated_at, int)
            or self.updated_at < 0
        ):
            raise ActiveProfileStoreIntegrityError("updated_at must be a non-negative integer")

    @staticmethod
    def _safe_snapshot_path(value: str) -> bool:
        if not isinstance(value, str):
            return False
        candidate = Path(value)
        return bool(
            isinstance(value, str)
            and value
            and not candidate.is_absolute()
            and ".." not in candidate.parts
            and "" not in candidate.parts
            and candidate.parts[0] == "workspaces"
        )

    def identity(self) -> tuple[str, str, str, str, str]:
        """Return the Profile, activation, Plan, and Lock identity tuple."""

        return (
            self.profile_id,
            self.profile_revision,
            self.activation_id,
            self.plan_digest,
            self.lock_digest,
        )

    def payload(self) -> dict[str, Any]:
        """Return the pointer without its integrity digest."""

        return {
            "schema": ACTIVE_PROFILE_SCHEMA,
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
            "activation_id": self.activation_id,
            "plan_digest": self.plan_digest,
            "lock_digest": self.lock_digest,
            "activation_snapshot_path": self.activation_snapshot_path,
            "activation_snapshot_digest": self.activation_snapshot_digest,
            "catalog_revision": self.catalog_revision,
            "generation": self.generation,
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize the pointer with a canonical integrity digest."""

        payload = self.payload()
        payload["pointer_digest"] = canonical_digest(payload)
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActiveProfilePointer":
        """Parse and authenticate a persisted pointer envelope."""

        required = {
            "schema",
            "profile_id",
            "profile_revision",
            "activation_id",
            "plan_digest",
            "lock_digest",
            "activation_snapshot_path",
            "activation_snapshot_digest",
            "catalog_revision",
            "generation",
            "updated_at",
            "pointer_digest",
        }
        if set(value) != required:
            raise ActiveProfileStoreIntegrityError(
                "active Profile pointer has unknown or missing fields"
            )
        if value.get("schema") != ACTIVE_PROFILE_SCHEMA:
            raise ActiveProfileStoreIntegrityError("active Profile pointer schema is unsupported")
        digest = value.get("pointer_digest")
        payload = {key: value[key] for key in required if key != "pointer_digest"}
        if not isinstance(digest, str) or digest != canonical_digest(payload):
            raise ActiveProfileStoreIntegrityError("active Profile pointer digest is invalid")
        try:
            return cls(
                profile_id=value["profile_id"],
                profile_revision=value["profile_revision"],
                activation_id=value["activation_id"],
                plan_digest=value["plan_digest"],
                lock_digest=value["lock_digest"],
                activation_snapshot_path=value["activation_snapshot_path"],
                activation_snapshot_digest=value["activation_snapshot_digest"],
                catalog_revision=value["catalog_revision"],
                generation=value["generation"],
                updated_at=value["updated_at"],
            )
        except (KeyError, TypeError) as error:
            raise ActiveProfileStoreIntegrityError(
                "active Profile pointer fields have invalid types"
            ) from error


_LOCAL_LOCKS: dict[Path, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _process_lock(path: Path) -> threading.RLock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(path, threading.RLock())


@contextmanager
def exclusive_profile_lock(
    directory: SecureDirectory,
    relative_path: str | Path,
    *,
    timeout_seconds: float,
    monotonic_clock: Callable[[], float],
    retry_sleep: Callable[[float], None],
) -> Iterator[None]:
    """Acquire a bounded lock while preserving descriptor/path identity."""

    if timeout_seconds <= 0 or timeout_seconds > 30:
        raise ValueError("timeout_seconds must be positive and bounded")
    process_lock = _process_lock(directory.root / Path(relative_path))
    if not process_lock.acquire(timeout=timeout_seconds):
        raise ActiveProfileStoreLockTimeout("active Profile lock deadline exceeded")
    descriptor: int | None = None
    acquired = False
    backend: Any = None
    try:
        try:
            descriptor = directory.open_lock(relative_path)
        except (OSError, SecurePersistenceError) as error:
            raise ActiveProfileStoreError("active Profile lock is unavailable") from error
        try:
            if os.name == "nt":
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                    os.fsync(descriptor)
                backend = importlib.import_module("msvcrt")
                lock_mode = getattr(backend, "LK_NBLCK")
            else:
                backend = importlib.import_module("fcntl")
                lock_mode = getattr(backend, "LOCK_EX") | getattr(backend, "LOCK_NB")
            deadline = monotonic_clock() + timeout_seconds
            while True:
                try:
                    if os.name == "nt":
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        getattr(backend, "locking")(descriptor, lock_mode, 1)
                    else:
                        getattr(backend, "flock")(descriptor, lock_mode)
                    acquired = True
                    break
                except OSError as error:
                    if error.errno not in {
                        None,
                        errno.EACCES,
                        errno.EAGAIN,
                        errno.EDEADLK,
                    }:
                        raise ActiveProfileStoreError("active Profile lock is unavailable") from error
                    remaining = deadline - monotonic_clock()
                    if remaining <= 0:
                        raise ActiveProfileStoreLockTimeout(
                            "active Profile lock deadline exceeded"
                        ) from error
                    retry_sleep(min(0.01, remaining))
            directory.validate_open_file(relative_path, descriptor)
            yield
            directory.validate_open_file(relative_path, descriptor)
        finally:
            if descriptor is not None:
                try:
                    if acquired:
                        if os.name == "nt":
                            os.lseek(descriptor, 0, os.SEEK_SET)
                            getattr(backend, "locking")(
                                descriptor,
                                getattr(backend, "LK_UNLCK"),
                                1,
                            )
                        else:
                            getattr(backend, "flock")(
                                descriptor,
                                getattr(backend, "LOCK_UN"),
                            )
                finally:
                    os.close(descriptor)
    finally:
        process_lock.release()


class ActiveProfileStore:
    """Read and atomically publish the active Profile pointer."""

    def __init__(
        self,
        user_data_root: Path,
        *,
        lock_timeout_seconds: float = 5.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
        retry_sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        requested = Path(user_data_root)
        if requested.is_symlink():
            raise ActiveProfileStoreIntegrityError("Host state root must not be a symlink")
        requested = requested.absolute()
        if requested.name == "profiles":
            self.profiles_root = requested
            self.user_data_root = requested.parent
        else:
            self.user_data_root = requested
            self.profiles_root = requested / "profiles"
        if self.profiles_root.is_symlink():
            raise ActiveProfileStoreIntegrityError("Profile authority directory must not be a symlink")
        self.pointer_path = self.profiles_root / "active.json"
        self._directory = SecureDirectory(self.profiles_root, create=True)
        self._user_directory = SecureDirectory(self.user_data_root, create=True)
        self._lock_timeout_seconds = lock_timeout_seconds
        self._monotonic_clock = monotonic_clock
        self._retry_sleep = retry_sleep
        self._clock = clock

    @property
    def path(self) -> Path:
        """Return the Host-global pointer path."""

        return self.pointer_path

    def load(self, *, verify_snapshot: bool = False) -> ActiveProfilePointer | None:
        """Load the current pointer, optionally checking its activation envelope."""

        try:
            if not self._directory.exists("active.json"):
                return None
            value = strict_loads(self._directory.read_bytes("active.json"))
        except FileNotFoundError:
            return None
        except (OSError, SecurePersistenceError, ValueError) as error:
            raise ActiveProfileStoreIntegrityError(
                "active Profile pointer is unavailable"
            ) from error
        if not isinstance(value, Mapping):
            raise ActiveProfileStoreIntegrityError("active Profile pointer is not an object")
        pointer = ActiveProfilePointer.from_mapping(value)
        if verify_snapshot:
            self.verify_activation_snapshot(pointer)
        return pointer

    read = load

    def require(self, *, verify_snapshot: bool = True) -> ActiveProfilePointer:
        """Return the active pointer or fail closed when none is committed."""

        pointer = self.load(verify_snapshot=verify_snapshot)
        if pointer is None:
            raise ActiveProfileStoreError("no active Profile pointer is committed")
        return pointer

    def commit(
        self,
        pointer: ActiveProfilePointer | Mapping[str, Any] | None = None,
        *,
        profile_id: str | None = None,
        profile_revision: str | None = None,
        activation_id: str | None = None,
        plan_digest: str | None = None,
        lock_digest: str | None = None,
        activation_snapshot: Mapping[str, Any] | None = None,
        activation_snapshot_path: str | None = None,
        catalog_revision: str | None = None,
        expected: ActiveProfilePointer | Mapping[str, Any] | None = None,
        allow_unverified: bool = False,
    ) -> ActiveProfilePointer:
        """Publish a pointer with mandatory CAS after the first commit.

        ``allow_unverified`` exists only for offline migration tooling.  The
        production path must pass an activation envelope, which is checked for
        exact Profile/Plan/Lock identity before publication.
        """

        snapshot_value: Mapping[str, Any] | None = None
        if activation_snapshot is not None:
            if not isinstance(activation_snapshot, Mapping):
                raise ActiveProfileStoreIntegrityError(
                    "activation snapshot must be an object"
                )
            snapshot_value = copy.deepcopy(dict(activation_snapshot))

        identity_values = (
            profile_id,
            profile_revision,
            activation_id,
            plan_digest,
            lock_digest,
        )
        if pointer is not None and any(value is not None for value in identity_values):
            raise ActiveProfileStoreError("pointer and identity fields cannot be mixed")
        if pointer is None:
            if not all(isinstance(value, str) and value for value in identity_values):
                raise ActiveProfileStoreError("complete active Profile identity is required")
            path = activation_snapshot_path or _default_snapshot_path(
                str(profile_id),
                str(activation_id),
            )
            snapshot_digest = _snapshot_digest(snapshot_value)
            if snapshot_digest is None:
                if not allow_unverified:
                    raise ActiveProfileStoreIntegrityError(
                        "activation snapshot is required for active Profile publication"
                    )
                snapshot_digest = "sha256:" + "0" * 64
            pointer = {
                "profile_id": profile_id,
                "profile_revision": profile_revision,
                "activation_id": activation_id,
                "plan_digest": plan_digest,
                "lock_digest": lock_digest,
                "activation_snapshot_path": path,
                "activation_snapshot_digest": snapshot_digest,
                "catalog_revision": catalog_revision,
            }
        requested = self._coerce_pointer(pointer)
        _validate_snapshot_path_binding(
            requested.profile_id,
            requested.activation_id,
            requested.activation_snapshot_path,
        )
        expected_pointer = self._coerce_expected(expected)
        with exclusive_profile_lock(
            self._directory,
            ".active-profile.lock",
            timeout_seconds=self._lock_timeout_seconds,
            monotonic_clock=self._monotonic_clock,
            retry_sleep=self._retry_sleep,
        ):
            current = self.load(verify_snapshot=False)
            if current is not None and expected_pointer is None:
                raise ActiveProfileStoreConflict(
                    "active Profile replacement requires its current predecessor"
                )
            if expected_pointer is not None and current != expected_pointer:
                raise ActiveProfileStoreConflict("active Profile predecessor is stale")
            if snapshot_value is not None:
                self._verify_snapshot_disk(requested, snapshot_value)
            elif not allow_unverified:
                # The existing activation envelope is read while holding the
                # pointer lock, so a caller cannot validate one snapshot and
                # publish another after a concurrent replacement.
                self._verify_snapshot_path(requested)
            generation = 1 if current is None else current.generation + 1
            committed = ActiveProfilePointer(
                profile_id=requested.profile_id,
                profile_revision=requested.profile_revision,
                activation_id=requested.activation_id,
                plan_digest=requested.plan_digest,
                lock_digest=requested.lock_digest,
                activation_snapshot_path=requested.activation_snapshot_path,
                activation_snapshot_digest=requested.activation_snapshot_digest,
                catalog_revision=requested.catalog_revision,
                generation=generation,
                updated_at=max(int(self._clock()), 0),
            )
            try:
                self._directory.write_bytes_atomic(
                    "active.json",
                    canonical_json(committed.to_dict()) + b"\n",
                )
            except (OSError, SecurePersistenceError, ValueError) as error:
                raise ActiveProfileStoreError(
                    "active Profile pointer could not be committed"
                ) from error
            return committed

    def commit_activation(
        self,
        activation: Mapping[str, Any],
        *,
        activation_snapshot: Mapping[str, Any],
        expected: ActiveProfilePointer | Mapping[str, Any] | None = None,
        activation_snapshot_path: str | None = None,
        catalog_revision: str | None = None,
    ) -> ActiveProfilePointer:
        """Publish an ActivationRecord and its exact envelope binding."""

        required = (
            "profile_id",
            "profile_revision",
            "activation_id",
            "plan_digest",
            "lock_digest",
        )
        if any(key not in activation for key in required):
            raise ActiveProfileStoreIntegrityError("ActivationRecord lacks active identity")
        return self.commit(
            profile_id=str(activation["profile_id"]),
            profile_revision=str(activation["profile_revision"]),
            activation_id=str(activation["activation_id"]),
            plan_digest=str(activation["plan_digest"]),
            lock_digest=str(activation["lock_digest"]),
            activation_snapshot=activation_snapshot,
            activation_snapshot_path=activation_snapshot_path,
            catalog_revision=catalog_revision
            if catalog_revision is not None
            else _optional_string(activation.get("catalog_revision")),
            expected=expected,
        )

    def verify_activation_snapshot(
        self,
        pointer: ActiveProfilePointer | None = None,
        *,
        snapshot: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Verify an exact per-Profile activation envelope against a pointer."""

        current = pointer or self.require(verify_snapshot=False)
        value = snapshot if snapshot is not None else self._read_snapshot(current)
        self._verify_snapshot_mapping(current, value)
        return value

    def matches(
        self,
        *,
        profile_id: str,
        profile_revision: str,
        activation_id: str,
        plan_digest: str,
        lock_digest: str,
    ) -> bool:
        """Return whether the current pointer has the exact identity."""

        pointer = self.load(verify_snapshot=False)
        return pointer is not None and pointer.identity() == (
            profile_id,
            profile_revision,
            activation_id,
            plan_digest,
            lock_digest,
        )

    def _read_snapshot(self, pointer: ActiveProfilePointer) -> Mapping[str, Any]:
        relative = self._relative_to_root(pointer)
        try:
            value = strict_loads(self._read_relative(relative))
        except (OSError, SecurePersistenceError, ValueError) as error:
            raise ActiveProfileStoreIntegrityError(
                "active Profile activation snapshot is unavailable"
            ) from error
        if not isinstance(value, Mapping):
            raise ActiveProfileStoreIntegrityError("activation snapshot is not an object")
        return value

    def _read_snapshot_bytes(self, pointer: ActiveProfilePointer) -> bytes:
        """Read the pinned activation envelope bytes below the Host root."""

        relative = self._relative_to_root(pointer)
        try:
            return self._read_relative(relative)
        except (OSError, SecurePersistenceError) as error:
            raise ActiveProfileStoreIntegrityError(
                "active Profile activation snapshot is unavailable"
            ) from error

    def _read_relative(self, relative: Path) -> bytes:
        return self._user_directory.read_bytes(relative)

    def _verify_snapshot_path(self, pointer: ActiveProfilePointer) -> None:
        raw = self._read_snapshot_bytes(pointer)
        try:
            snapshot = strict_loads(raw)
        except ValueError as error:
            raise ActiveProfileStoreIntegrityError(
                "active Profile activation snapshot is invalid"
            ) from error
        if not isinstance(snapshot, Mapping):
            raise ActiveProfileStoreIntegrityError("activation snapshot is not an object")
        self._verify_snapshot_mapping(pointer, snapshot)

    def _verify_snapshot_disk(
        self,
        pointer: ActiveProfilePointer,
        expected_snapshot: Mapping[str, Any],
    ) -> None:
        """Require a supplied snapshot to match its pinned on-disk envelope."""

        raw = self._read_snapshot_bytes(pointer)
        try:
            disk_snapshot = strict_loads(raw)
        except ValueError as error:
            raise ActiveProfileStoreIntegrityError(
                "active Profile activation snapshot is invalid"
            ) from error
        if not isinstance(disk_snapshot, Mapping):
            raise ActiveProfileStoreIntegrityError("activation snapshot is not an object")
        if canonical_json(disk_snapshot) != canonical_json(expected_snapshot):
            raise ActiveProfileStoreIntegrityError(
                "activation snapshot differs from its on-disk envelope"
            )
        self._verify_snapshot_mapping(pointer, disk_snapshot)

    @staticmethod
    def _verify_snapshot_mapping(
        pointer: ActiveProfilePointer,
        snapshot: Mapping[str, Any],
    ) -> None:
        if canonical_digest(snapshot) != pointer.activation_snapshot_digest:
            raise ActiveProfileStoreIntegrityError(
                "activation snapshot digest does not match active pointer"
            )
        raw_envelope = snapshot.get("envelope")
        envelope: Mapping[str, Any] = (
            raw_envelope if isinstance(raw_envelope, Mapping) else snapshot
        )
        profile = envelope.get("profile")
        lock = envelope.get("lock")
        plan = envelope.get("plan")
        activation = envelope.get("activation")
        if (
            not isinstance(profile, Mapping)
            or not isinstance(lock, Mapping)
            or not isinstance(plan, Mapping)
            or not isinstance(activation, Mapping)
        ):
            raise ActiveProfileStoreIntegrityError("activation snapshot records are incomplete")
        expected = (
            pointer.profile_id,
            pointer.profile_revision,
            pointer.activation_id,
            pointer.plan_digest,
            pointer.lock_digest,
        )
        actual = (
            str(profile.get("profile_id") or ""),
            str(plan.get("profile_revision") or ""),
            str(activation.get("activation_id") or ""),
            str(plan.get("plan_digest") or ""),
            str(lock.get("lock_digest") or ""),
        )
        if actual != expected:
            raise ActiveProfileStoreIntegrityError(
                "activation snapshot identity does not match active Profile pointer"
            )
        if (
            activation.get("profile_id") != pointer.profile_id
            or activation.get("profile_revision") != pointer.profile_revision
            or activation.get("plan_digest") != pointer.plan_digest
            or activation.get("lock_digest") != pointer.lock_digest
            or activation.get("state") != "active"
        ):
            raise ActiveProfileStoreIntegrityError(
                "ActivationRecord is stale or does not match active Profile pointer"
            )
        if pointer.catalog_revision is not None and (
            plan.get("catalog_revision") != pointer.catalog_revision
        ):
            raise ActiveProfileStoreIntegrityError(
                "activation snapshot catalog revision does not match active pointer"
            )

    def _coerce_pointer(
        self,
        value: ActiveProfilePointer | Mapping[str, Any],
    ) -> ActiveProfilePointer:
        if isinstance(value, ActiveProfilePointer):
            return value
        if not isinstance(value, Mapping):
            raise ActiveProfileStoreError("active pointer must be an object")
        if "pointer_digest" in value:
            return ActiveProfilePointer.from_mapping(value)
        allowed = {
            "profile_id",
            "profile_revision",
            "activation_id",
            "plan_digest",
            "lock_digest",
            "activation_snapshot_path",
            "activation_snapshot_digest",
            "catalog_revision",
            "generation",
            "updated_at",
        }
        if set(value) - allowed:
            raise ActiveProfileStoreError("active pointer contains unknown fields")
        return ActiveProfilePointer(
            profile_id=str(value.get("profile_id") or ""),
            profile_revision=str(value.get("profile_revision") or ""),
            activation_id=str(value.get("activation_id") or ""),
            plan_digest=str(value.get("plan_digest") or ""),
            lock_digest=str(value.get("lock_digest") or ""),
            activation_snapshot_path=str(value.get("activation_snapshot_path") or ""),
            activation_snapshot_digest=str(value.get("activation_snapshot_digest") or ""),
            catalog_revision=_optional_string(value.get("catalog_revision")),
            generation=int(value.get("generation") or 1),
            updated_at=int(value.get("updated_at") or 0),
        )

    def _coerce_expected(
        self,
        value: ActiveProfilePointer | Mapping[str, Any] | None,
    ) -> ActiveProfilePointer | None:
        if value is None or isinstance(value, ActiveProfilePointer):
            return value
        if not isinstance(value, Mapping):
            raise ActiveProfileStoreError("active Profile predecessor must be an object")
        return self._coerce_pointer(value)

    def _relative_to_root(self, pointer: ActiveProfilePointer) -> Path:
        value = pointer.activation_snapshot_path
        _validate_snapshot_path_binding(
            pointer.profile_id,
            pointer.activation_id,
            value,
        )
        candidate = Path(value)
        if not ActiveProfilePointer._safe_snapshot_path(value):
            raise ActiveProfileStoreIntegrityError("activation snapshot path is unsafe")
        # The SecureDirectory is rooted at profiles/, while the public pointer
        # path is rooted at user_data/.  All snapshot reads therefore use the
        # user-data root as a separate pinned directory.
        return candidate


def _snapshot_digest(snapshot: Mapping[str, Any] | None) -> str | None:
    if snapshot is None:
        return None
    if not isinstance(snapshot, Mapping):
        raise ActiveProfileStoreIntegrityError("activation snapshot must be an object")
    return canonical_digest(snapshot)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ActiveProfileStoreIntegrityError("optional pointer field must be a string")
    return value


__all__ = [
    "ACTIVE_PROFILE_SCHEMA",
    "ActiveProfilePointer",
    "ActiveProfileStore",
    "ActiveProfileStoreConflict",
    "ActiveProfileStoreError",
    "ActiveProfileStoreIntegrityError",
    "ActiveProfileStoreLockTimeout",
    "exclusive_profile_lock",
]
