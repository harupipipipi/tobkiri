"""Host-owned exclusive leases for coordinated workspace mutations.

The lease serializes Tobkiri Host writers.  It deliberately does not claim to
exclude unrelated processes which ignore the kernel advisory lock; individual
mutation primitives must still validate their exact preimage immediately
before publication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import hashlib
import importlib
import json
import os
from pathlib import Path
import secrets
import stat
from threading import Lock, RLock
import time
from typing import TYPE_CHECKING, Any, Callable

from .errors import HostCoreError
from .models import OpaqueAuthorityRef, RequestContext

if TYPE_CHECKING:
    from .ports import (
        OpaqueWorkspaceMutationLease,
        WorkspaceMutationIdentity,
        WorkspaceMutationLeaseRequest,
        WorkspaceBatchMutation,
        WorkspaceBatchResult,
    )
    from .resources import OpaqueResourceHandle, ResourceHandleTable


class WorkspaceMutationError(HostCoreError):
    """A workspace mutation lease is unavailable, stale, or out of scope."""

    code = "workspace_mutation_denied"


@dataclass(frozen=True)
class WorkspaceMutationBinding:
    """Host-captured identity of one mounted workspace root."""

    profile_id: str
    workspace_id: str
    mount_revision: int
    canonical_root: Path
    root_st_dev: int
    root_st_ino: int

    def __post_init__(self) -> None:
        if not self.profile_id or not self.workspace_id:
            raise ValueError("workspace binding identifiers must be non-empty")
        if self.mount_revision <= 0:
            raise ValueError("workspace mount revision must be positive")
        if not self.canonical_root.is_absolute():
            raise ValueError("workspace root must be absolute")
        try:
            resolved_root = self.canonical_root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("workspace root must exist") from exc
        if resolved_root != self.canonical_root or self.canonical_root.is_symlink():
            raise ValueError("workspace root must be canonical and symlink-free")
        if self.root_st_dev < 0 or self.root_st_ino <= 0:
            raise ValueError("workspace root identity is invalid")

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, object],
        *,
        profile_id: str,
    ) -> "WorkspaceMutationBinding":
        """Parse an authoritative workspace-mount binding."""

        return cls(
            profile_id=profile_id,
            workspace_id=str(value.get("workspace_id") or ""),
            mount_revision=int(str(value.get("mount_revision") or 0)),
            canonical_root=Path(str(value.get("canonical_root") or "")),
            root_st_dev=int(str(value.get("root_st_dev") or -1)),
            root_st_ino=int(str(value.get("root_st_ino") or -1)),
        )

    @property
    def root_identity(self) -> tuple[int, int]:
        """Return the descriptor identity expected for the mounted root."""

        return (self.root_st_dev, self.root_st_ino)


@dataclass(frozen=True)
class _RequestBinding:
    request_id: str
    profile_id: str
    activation_id: str
    security_epoch: int
    caller_principal: OpaqueAuthorityRef
    caller_domain_id: str
    caller_boot_epoch: int
    target_principal: OpaqueAuthorityRef
    target_domain_id: str
    target_boot_epoch: int
    target_namespace: str


_PROCESS_LOCKS: dict[str, Lock] = {}
_PROCESS_LOCKS_GUARD = Lock()


def _process_lock(key: str) -> Lock:
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, Lock())


class WorkspaceMutationLease:
    """Opaque, request-bound lease held for one complete Host invocation."""

    def __init__(
        self,
        *,
        lease_id: str,
        binding: WorkspaceMutationBinding,
        request: _RequestBinding,
        root_fd: int,
        lock_fd: int,
        process_lock: Lock,
        lock_backend: Any,
        transaction_root: Path,
        binding_key: str,
        release_callback: Callable[["WorkspaceMutationLease"], None],
    ) -> None:
        self._lease_id = lease_id
        self._binding = binding
        self._request = request
        self._root_fd = root_fd
        self._lock_fd = lock_fd
        self._process_lock = process_lock
        self._lock_backend = lock_backend
        self._transaction_root = transaction_root
        self._binding_key = binding_key
        self._release_callback = release_callback
        self._closed = False
        self._guard = RLock()

    def __enter__(self) -> "WorkspaceMutationLease":
        self.revalidate_root()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def lease_id(self) -> str:
        """Return an opaque identifier for audit correlation."""

        return self._lease_id

    @property
    def target_namespace(self) -> str:
        """Return the execution namespace which owns this lease."""

        return self._request.target_namespace

    @property
    def root_identity(self) -> tuple[int, int]:
        """Return the pinned workspace root descriptor identity."""

        return self._binding.root_identity

    @property
    def closed(self) -> bool:
        """Return whether the kernel lock and pinned root have been released."""

        with self._guard:
            return self._closed

    def assert_bound(
        self,
        *,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        target_domain_id: str,
        target_boot_epoch: int,
        target_namespace: str,
    ) -> None:
        """Reject use outside the exact authenticated invocation binding."""

        with self._guard:
            self._require_open()
            expected = self._request
            mismatch = (
                context.request_id != expected.request_id
                or context.profile_id != expected.profile_id
                or context.activation_id != expected.activation_id
                or context.security_epoch != expected.security_epoch
                or context.caller_principal != expected.caller_principal
                or context.caller_domain_id != expected.caller_domain_id
                or context.caller_boot_epoch != expected.caller_boot_epoch
                or target != expected.target_principal
                or target_domain_id != expected.target_domain_id
                or target_boot_epoch != expected.target_boot_epoch
                or target_namespace != expected.target_namespace
            )
            if mismatch:
                raise WorkspaceMutationError("workspace lease binding mismatch")
            self._revalidate_root_locked()

    def assert_resource_root(self, identity: tuple[int, int]) -> None:
        """Require a resource to belong to this exact mounted root inode."""

        with self._guard:
            self._require_open()
            if identity != self._binding.root_identity:
                raise WorkspaceMutationError("resource root is outside workspace lease")
            self._revalidate_root_locked()

    def revalidate_root(self) -> None:
        """Reopen the canonical path without following links and compare it."""

        with self._guard:
            self._require_open()
            self._revalidate_root_locked()

    def host_batch_state(self) -> tuple[Path, str, int]:
        """Return Host-only batch state after exact root revalidation."""

        with self._guard:
            self._require_open()
            self._revalidate_root_locked()
            return self._transaction_root, self._binding_key, os.dup(self._root_fd)

    def close(self) -> None:
        """Release the OS lock and pinned descriptors exactly once."""

        with self._guard:
            if self._closed:
                return
            self._closed = True
            try:
                _unlock_file(self._lock_fd, self._lock_backend)
            finally:
                try:
                    os.close(self._lock_fd)
                finally:
                    try:
                        os.close(self._root_fd)
                    finally:
                        self._process_lock.release()
        self._release_callback(self)

    def _require_open(self) -> None:
        if self._closed:
            raise WorkspaceMutationError("workspace lease is closed")

    def _revalidate_root_locked(self) -> None:
        try:
            pinned = os.fstat(self._root_fd)
            current_fd = open_directory_nofollow(self._binding.canonical_root)
            try:
                current = os.fstat(current_fd)
            finally:
                os.close(current_fd)
        except OSError as exc:
            raise WorkspaceMutationError("workspace root is unavailable") from exc
        expected = self._binding.root_identity
        if not stat.S_ISDIR(pinned.st_mode):
            raise WorkspaceMutationError("pinned workspace root is not a directory")
        if (pinned.st_dev, pinned.st_ino) != expected:
            raise WorkspaceMutationError("pinned workspace root identity changed")
        if (current.st_dev, current.st_ino) != expected:
            raise WorkspaceMutationError("workspace root path identity changed")


class WorkspaceMutationCoordinator:
    """Acquire and track request-bound OS leases for Host workspace writers."""

    def __init__(
        self,
        state_root: Path,
        *,
        lock_timeout_seconds: float = 5.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
        retry_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if lock_timeout_seconds <= 0 or lock_timeout_seconds > 30:
            raise ValueError("lock timeout must be positive and bounded")
        self._state_root = Path(os.path.abspath(state_root))
        self._lock_timeout_seconds = lock_timeout_seconds
        self._monotonic_clock = monotonic_clock
        self._retry_sleep = retry_sleep
        self._leases: dict[str, WorkspaceMutationLease] = {}
        self._guard = RLock()

    def acquire(
        self,
        *,
        binding: WorkspaceMutationBinding,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        target_domain_id: str,
        target_boot_epoch: int,
        target_namespace: str,
    ) -> WorkspaceMutationLease:
        """Acquire an exclusive lease pinned to one authenticated invocation."""

        if binding.profile_id != context.profile_id:
            raise WorkspaceMutationError("workspace profile binding mismatch")
        request = _RequestBinding(
            request_id=context.request_id,
            profile_id=context.profile_id,
            activation_id=context.activation_id,
            security_epoch=context.security_epoch,
            caller_principal=context.caller_principal,
            caller_domain_id=context.caller_domain_id,
            caller_boot_epoch=context.caller_boot_epoch,
            target_principal=target,
            target_domain_id=target_domain_id,
            target_boot_epoch=target_boot_epoch,
            target_namespace=target_namespace,
        )
        key = _lock_key(binding)
        process_lock = _process_lock(key)
        if not process_lock.acquire(timeout=self._lock_timeout_seconds):
            raise WorkspaceMutationError("workspace lease deadline exceeded")
        root_fd: int | None = None
        lock_fd: int | None = None
        backend: Any = None
        try:
            root_fd = open_directory_nofollow(binding.canonical_root)
            root_stat = os.fstat(root_fd)
            if (root_stat.st_dev, root_stat.st_ino) != binding.root_identity:
                raise WorkspaceMutationError("workspace root binding is stale")
            lock_fd = self._open_lock_file(key)
            backend = _lock_file(
                lock_fd,
                timeout_seconds=self._lock_timeout_seconds,
                monotonic_clock=self._monotonic_clock,
                retry_sleep=self._retry_sleep,
            )
            lease = WorkspaceMutationLease(
                lease_id=secrets.token_urlsafe(32),
                binding=binding,
                request=request,
                root_fd=root_fd,
                lock_fd=lock_fd,
                process_lock=process_lock,
                lock_backend=backend,
                transaction_root=self._state_root / "transactions" / key,
                binding_key=key,
                release_callback=self._released,
            )
            lease.revalidate_root()
            with self._guard:
                self._leases[lease.lease_id] = lease
            return lease
        except Exception:
            if lock_fd is not None:
                if backend is not None:
                    _unlock_file(lock_fd, backend)
                os.close(lock_fd)
            if root_fd is not None:
                os.close(root_fd)
            process_lock.release()
            raise

    def close_namespace(self, namespace: str) -> None:
        """Release all leases owned by an execution namespace."""

        with self._guard:
            leases = [
                lease for lease in self._leases.values() if lease.target_namespace == namespace
            ]
        for lease in leases:
            lease.close()

    def close(self) -> None:
        """Release every outstanding lease managed by this coordinator."""

        with self._guard:
            leases = list(self._leases.values())
        for lease in leases:
            lease.close()

    def _open_lock_file(self, key: str) -> int:
        self._state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._state_root.is_symlink():
            raise WorkspaceMutationError("workspace lock root must not be a symlink")
        lock_root = self._state_root.resolve(strict=True)
        if lock_root != self._state_root:
            raise WorkspaceMutationError("workspace lock root must be canonical")
        root_fd = open_directory_nofollow(lock_root)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        name = f"{key}.lock"
        try:
            root_value = os.fstat(root_fd)
            _validate_private_file(root_value, directory=True)
            fd = os.open(name, flags, 0o600, dir_fd=root_fd)
            value = os.fstat(fd)
            path_value = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            _validate_private_file(value, directory=False)
            if value.st_nlink != 1:
                os.close(fd)
                raise WorkspaceMutationError("workspace lock file is unsafe")
            if (value.st_dev, value.st_ino) != (path_value.st_dev, path_value.st_ino):
                os.close(fd)
                raise WorkspaceMutationError("workspace lock file identity changed")
            return fd
        except OSError as exc:
            raise WorkspaceMutationError("workspace lock file is unavailable") from exc
        finally:
            os.close(root_fd)

    def _released(self, lease: WorkspaceMutationLease) -> None:
        with self._guard:
            self._leases.pop(lease.lease_id, None)


@dataclass
class _PortLeaseRecord:
    lease: WorkspaceMutationLease
    identity: "WorkspaceMutationIdentity"
    binding: WorkspaceMutationBinding
    handles: set[str] = field(default_factory=set)


class HostWorkspaceMutationPort:
    """Host implementation which exposes only opaque leases and handles."""

    def __init__(
        self,
        coordinator: WorkspaceMutationCoordinator,
        *,
        binding_resolver: Callable[[str, str], WorkspaceMutationBinding],
        batch_fault_injector: Callable[[str, int], None] | None = None,
    ) -> None:
        from .resources import ResourceHandleTable

        self._coordinator = coordinator
        self._binding_resolver = binding_resolver
        self._resources: ResourceHandleTable = ResourceHandleTable(
            batch_fault_injector=batch_fault_injector
        )
        self._leases: dict[str, _PortLeaseRecord] = {}
        self._guard = RLock()
        self._closed = False

    def acquire_lease(
        self,
        request: "WorkspaceMutationLeaseRequest",
    ) -> "OpaqueWorkspaceMutationLease":
        """Acquire a lease without returning its descriptor-bearing object."""

        from .ports import OpaqueWorkspaceMutationLease

        identity = request.identity
        with self._guard:
            if self._closed:
                raise WorkspaceMutationError("workspace mutation port is closed")
            authoritative = self._binding_resolver(
                identity.context.profile_id,
                request.binding.workspace_id,
            )
            if authoritative != request.binding:
                raise WorkspaceMutationError("workspace mount binding is not authoritative")
            lease = self._coordinator.acquire(
                binding=authoritative,
                context=identity.context,
                target=identity.target_principal,
                target_domain_id=identity.target_domain_id,
                target_boot_epoch=identity.target_boot_epoch,
                target_namespace=identity.target_namespace,
            )
            try:
                self._resources.recover_incomplete_batch(lease)
            except Exception:
                lease.close()
                raise
            opaque = OpaqueWorkspaceMutationLease(secrets.token_urlsafe(32))
            self._leases[opaque.value] = _PortLeaseRecord(
                lease=lease,
                identity=identity,
                binding=authoritative,
            )
        return opaque

    def bind_existing(
        self,
        lease: "OpaqueWorkspaceMutationLease",
        identity: "WorkspaceMutationIdentity",
        *,
        relative_path: str,
        ttl_seconds: float,
        max_uses: int,
        max_bytes: int,
    ) -> "OpaqueResourceHandle":
        """Bind one exact existing preimage beneath the leased root."""

        with self._guard:
            record = self._claim(lease, identity)
            handle = self._resources.bind_file(
                root=record.binding.canonical_root,
                relative_path=relative_path,
                operations=frozenset({"write"}),
                owner=identity.context.caller_principal,
                target=identity.target_principal,
                context=identity.context,
                target_domain_id=identity.target_domain_id,
                target_boot_epoch=identity.target_boot_epoch,
                target_namespace=identity.target_namespace,
                ttl_seconds=ttl_seconds,
                max_uses=max_uses,
                max_bytes=max_bytes,
                atomic_replace=True,
            )
            record.handles.add(handle.value)
            return handle

    def bind_absent(
        self,
        lease: "OpaqueWorkspaceMutationLease",
        identity: "WorkspaceMutationIdentity",
        *,
        relative_path: str,
        ttl_seconds: float,
        max_uses: int,
        max_bytes: int,
    ) -> "OpaqueResourceHandle":
        """Bind one exact absent preimage beneath the leased root."""

        with self._guard:
            record = self._claim(lease, identity)
            handle = self._resources.bind_absent_file(
                root=record.binding.canonical_root,
                relative_path=relative_path,
                owner=identity.context.caller_principal,
                target=identity.target_principal,
                context=identity.context,
                target_domain_id=identity.target_domain_id,
                target_boot_epoch=identity.target_boot_epoch,
                target_namespace=identity.target_namespace,
                ttl_seconds=ttl_seconds,
                max_uses=max_uses,
                max_bytes=max_bytes,
            )
            record.handles.add(handle.value)
            return handle

    def replace_file(
        self,
        lease: "OpaqueWorkspaceMutationLease",
        identity: "WorkspaceMutationIdentity",
        handle: "OpaqueResourceHandle",
        data: bytes,
    ) -> int:
        """Replace an owned opaque handle under its exact lease identity."""

        with self._guard:
            record = self._claim(lease, identity, handle=handle)
            return self._resources.compare_and_replace_file(
                handle,
                data,
                lease=record.lease,
                context=identity.context,
                target=identity.target_principal,
                domain_id=identity.target_domain_id,
                boot_epoch=identity.target_boot_epoch,
                namespace=identity.target_namespace,
            )

    def create_file(
        self,
        lease: "OpaqueWorkspaceMutationLease",
        identity: "WorkspaceMutationIdentity",
        handle: "OpaqueResourceHandle",
        data: bytes,
        *,
        mode: int = 0o600,
    ) -> int:
        """Create from an owned opaque absent-preimage handle."""

        with self._guard:
            record = self._claim(lease, identity, handle=handle)
            return self._resources.compare_and_create_file(
                handle,
                data,
                lease=record.lease,
                context=identity.context,
                target=identity.target_principal,
                domain_id=identity.target_domain_id,
                boot_epoch=identity.target_boot_epoch,
                namespace=identity.target_namespace,
                mode=mode,
            )

    def delete_file(
        self,
        lease: "OpaqueWorkspaceMutationLease",
        identity: "WorkspaceMutationIdentity",
        handle: "OpaqueResourceHandle",
    ) -> None:
        """Delete an owned opaque existing-preimage handle."""

        with self._guard:
            record = self._claim(lease, identity, handle=handle)
            self._resources.compare_and_delete_file(
                handle,
                lease=record.lease,
                context=identity.context,
                target=identity.target_principal,
                domain_id=identity.target_domain_id,
                boot_epoch=identity.target_boot_epoch,
                namespace=identity.target_namespace,
            )

    def publish_batch(
        self,
        lease: "OpaqueWorkspaceMutationLease",
        identity: "WorkspaceMutationIdentity",
        mutations: tuple["WorkspaceBatchMutation", ...],
    ) -> "WorkspaceBatchResult":
        """Publish one non-interleavable opaque-handle mutation batch."""

        from .ports import WorkspaceBatchResult
        from .resources import ResourceBatchMutation

        with self._guard:
            record = self._claim(lease, identity)
            if not mutations or len(mutations) > 64:
                raise WorkspaceMutationError("workspace batch count limit exceeded")
            if len({item.handle.value for item in mutations}) != len(mutations):
                raise WorkspaceMutationError("workspace batch handles are duplicated")
            for item in mutations:
                if item.handle.value not in record.handles:
                    raise WorkspaceMutationError("resource handle belongs to another lease")
            internal = tuple(
                ResourceBatchMutation(
                    operation=item.operation,
                    handle=item.handle,
                    data=item.data,
                    mode=item.mode,
                )
                for item in mutations
            )
            result = self._resources.publish_batch(
                internal,
                lease=record.lease,
                context=identity.context,
                target=identity.target_principal,
                domain_id=identity.target_domain_id,
                boot_epoch=identity.target_boot_epoch,
                namespace=identity.target_namespace,
            )
            return WorkspaceBatchResult(
                transaction_id=result.transaction_id,
                status="committed",
                mutation_count=result.mutation_count,
                total_bytes=result.total_bytes,
            )

    def close_lease(
        self,
        lease: "OpaqueWorkspaceMutationLease",
        identity: "WorkspaceMutationIdentity",
    ) -> None:
        """Close one exact identity-bound lease and its handles."""

        with self._guard:
            record = self._claim(lease, identity)
            self._leases.pop(lease.value, None)
        self._close_record(record)

    def close_namespace(self, namespace: str) -> None:
        """Close every lease and handle owned by one execution namespace."""

        with self._guard:
            values = [
                key
                for key, record in self._leases.items()
                if record.identity.target_namespace == namespace
            ]
            records = [self._leases.pop(key) for key in values]
        for record in records:
            self._close_record(record)
        self._resources.revoke_namespace(namespace)
        self._coordinator.close_namespace(namespace)

    def close(self) -> None:
        """Release every lease and handle owned by this Host port."""

        with self._guard:
            if self._closed:
                return
            self._closed = True
            records = list(self._leases.values())
            self._leases.clear()
        for record in records:
            self._close_record(record)
        self._resources.close()
        self._coordinator.close()

    def _claim(
        self,
        opaque: "OpaqueWorkspaceMutationLease",
        identity: "WorkspaceMutationIdentity",
        *,
        handle: "OpaqueResourceHandle | None" = None,
    ) -> _PortLeaseRecord:
        if self._closed:
            raise WorkspaceMutationError("workspace mutation port is closed")
        record = self._leases.get(opaque.value)
        if record is None or record.lease.closed:
            raise WorkspaceMutationError("workspace mutation lease is unknown")
        if record.identity != identity:
            raise WorkspaceMutationError("workspace mutation identity mismatch")
        record.lease.assert_bound(
            context=identity.context,
            target=identity.target_principal,
            target_domain_id=identity.target_domain_id,
            target_boot_epoch=identity.target_boot_epoch,
            target_namespace=identity.target_namespace,
        )
        if handle is not None and handle.value not in record.handles:
            raise WorkspaceMutationError("resource handle belongs to another lease")
        return record

    def _close_record(self, record: _PortLeaseRecord) -> None:
        from .resources import OpaqueResourceHandle

        for value in record.handles:
            self._resources.revoke(OpaqueResourceHandle(value))
        record.lease.close()


def open_directory_nofollow(path: Path) -> int:
    """Open an absolute directory one component at a time without symlinks."""

    absolute = Path(path)
    if not absolute.is_absolute():
        raise ValueError("directory path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.path.sep, flags | nofollow)
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(
                component,
                flags | nofollow,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        value = os.fstat(descriptor)
        if not stat.S_ISDIR(value.st_mode):
            raise NotADirectoryError(str(path))
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _lock_key(binding: WorkspaceMutationBinding) -> str:
    encoded = json.dumps(
        {
            "profile_id": binding.profile_id,
            "workspace_id": binding.workspace_id,
            "mount_revision": binding.mount_revision,
            "root_st_dev": binding.root_st_dev,
            "root_st_ino": binding.root_st_ino,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _lock_file(
    descriptor: int,
    *,
    timeout_seconds: float,
    monotonic_clock: Callable[[], float],
    retry_sleep: Callable[[float], None],
) -> Any:
    if os.name == "nt":
        backend = importlib.import_module("msvcrt")
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        mode = getattr(backend, "LK_NBLCK")
    else:
        backend = importlib.import_module("fcntl")
        mode = getattr(backend, "LOCK_EX") | getattr(backend, "LOCK_NB")
    deadline = monotonic_clock() + timeout_seconds
    while True:
        try:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                getattr(backend, "locking")(descriptor, mode, 1)
            else:
                getattr(backend, "flock")(descriptor, mode)
            return backend
        except OSError as exc:
            if exc.errno not in {None, errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise WorkspaceMutationError("workspace lease is unavailable") from exc
            remaining = deadline - monotonic_clock()
            if remaining <= 0:
                raise WorkspaceMutationError("workspace lease deadline exceeded") from exc
            retry_sleep(min(0.01, remaining))


def _unlock_file(descriptor: int, backend: Any) -> None:
    try:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            getattr(backend, "locking")(descriptor, getattr(backend, "LK_UNLCK"), 1)
        else:
            getattr(backend, "flock")(descriptor, getattr(backend, "LOCK_UN"))
    except OSError:
        # Closing the descriptor also releases the kernel lock.  Release must
        # remain idempotent during cancellation and namespace teardown.
        pass


def _validate_private_file(value: os.stat_result, *, directory: bool) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(value.st_mode):
        raise WorkspaceMutationError("workspace lock path has an invalid type")
    if hasattr(os, "geteuid") and value.st_uid != os.geteuid():
        raise WorkspaceMutationError("workspace lock path owner is invalid")
    if stat.S_IMODE(value.st_mode) & 0o022:
        raise WorkspaceMutationError("workspace lock path permissions are unsafe")


__all__ = [
    "HostWorkspaceMutationPort",
    "WorkspaceMutationBinding",
    "WorkspaceMutationCoordinator",
    "WorkspaceMutationError",
    "WorkspaceMutationLease",
    "open_directory_nofollow",
]
