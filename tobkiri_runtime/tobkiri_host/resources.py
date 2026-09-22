"""Opaque descriptor-backed ResourceHandle table with TOCTOU defenses."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePath
import secrets
from threading import RLock
import time
from typing import Literal

from .errors import ResourceHandleError
from .models import OpaqueAuthorityRef, RequestContext

ResourceOperation = Literal["read", "write"]


@dataclass(frozen=True)
class OpaqueResourceHandle:
    """Opaque identifier safe to place in a Request payload."""

    value: str


@dataclass
class _FileRecord:
    handle: OpaqueResourceHandle
    root_fd: int
    fd: int
    relative_path: str
    identity: tuple[int, int]
    generation: tuple[int, int]
    allowed_operations: frozenset[ResourceOperation]
    owner: OpaqueAuthorityRef
    target: OpaqueAuthorityRef
    request_id: str
    profile_id: str
    activation_id: str
    domain_id: str
    boot_epoch: int
    namespace: str
    security_epoch: int
    expires_at: float
    remaining_uses: int
    remaining_bytes: int
    allow_hardlinks: bool
    version_precondition: tuple[int, int] | None
    atomic_replace: bool
    revoked: bool = False


class ResourceHandleTable:
    """Host-owned table which never exposes raw paths or descriptors."""

    def __init__(self) -> None:
        self._records: dict[str, _FileRecord] = {}
        self._lock = RLock()

    def bind_file(
        self,
        *,
        root: Path,
        relative_path: str,
        operations: frozenset[ResourceOperation],
        owner: OpaqueAuthorityRef,
        target: OpaqueAuthorityRef,
        context: RequestContext,
        target_domain_id: str,
        target_boot_epoch: int,
        target_namespace: str,
        ttl_seconds: float,
        max_uses: int,
        max_bytes: int,
        allow_hardlinks: bool = False,
        version_precondition: tuple[int, int] | None = None,
        atomic_replace: bool = False,
    ) -> OpaqueResourceHandle:
        """Open and bind a file beneath a fixed Host root without following links."""
        self._validate_relative_path(relative_path)
        if not operations or not operations <= {"read", "write"}:
            raise ResourceHandleError("invalid file operations")
        if ttl_seconds <= 0 or max_uses <= 0 or max_bytes < 0:
            raise ResourceHandleError("invalid handle limit")
        if "write" in operations and not (
            version_precondition is not None or atomic_replace
        ):
            raise ResourceHandleError(
                "write handles require a version precondition or atomic replace"
            )
        root_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            root_flags |= os.O_DIRECTORY
        root_fd = os.open(root, root_flags)
        file_flags = os.O_RDWR if "write" in operations else os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        try:
            fd = os.open(relative_path, file_flags, dir_fd=root_fd)
            stat = os.fstat(fd)
            if not allow_hardlinks and stat.st_nlink != 1:
                raise ResourceHandleError("hardlinked resources are denied")
            identity = (stat.st_dev, stat.st_ino)
            generation = (stat.st_size, stat.st_mtime_ns)
            if version_precondition is not None:
                if version_precondition != generation:
                    raise ResourceHandleError("version precondition is stale")
            handle = OpaqueResourceHandle(secrets.token_urlsafe(32))
            record = _FileRecord(
                handle=handle,
                root_fd=root_fd,
                fd=fd,
                relative_path=relative_path,
                identity=identity,
                generation=generation,
                allowed_operations=operations,
                owner=owner,
                target=target,
                request_id=context.request_id,
                profile_id=context.profile_id,
                activation_id=context.activation_id,
                domain_id=target_domain_id,
                boot_epoch=target_boot_epoch,
                namespace=target_namespace,
                security_epoch=context.security_epoch,
                expires_at=time.monotonic() + ttl_seconds,
                remaining_uses=max_uses,
                remaining_bytes=max_bytes,
                allow_hardlinks=allow_hardlinks,
                version_precondition=version_precondition,
                atomic_replace=atomic_replace,
            )
            with self._lock:
                self._records[handle.value] = record
            return handle
        except Exception as exc:
            if "fd" in locals():
                os.close(fd)
            os.close(root_fd)
            if isinstance(exc, ResourceHandleError):
                raise
            raise ResourceHandleError("resource cannot be safely bound") from exc

    def read(
        self,
        handle: OpaqueResourceHandle,
        *,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        domain_id: str,
        boot_epoch: int,
        namespace: str,
        max_bytes: int,
    ) -> bytes:
        """Read through the already-open descriptor after revalidation."""
        record = self._claim(
            handle,
            operation="read",
            byte_count=max_bytes,
            context=context,
            target=target,
            domain_id=domain_id,
            boot_epoch=boot_epoch,
            namespace=namespace,
        )
        os.lseek(record.fd, 0, os.SEEK_SET)
        return os.read(record.fd, max_bytes)

    def write(
        self,
        handle: OpaqueResourceHandle,
        data: bytes,
        *,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        domain_id: str,
        boot_epoch: int,
        namespace: str,
    ) -> int:
        """Write only to the bound descriptor under an explicit consistency mode."""
        record = self._claim(
            handle,
            operation="write",
            byte_count=len(data),
            context=context,
            target=target,
            domain_id=domain_id,
            boot_epoch=boot_epoch,
            namespace=namespace,
        )
        if record.atomic_replace:
            raise ResourceHandleError(
                "atomic replacement must be performed by a Host Broker primitive"
            )
        os.lseek(record.fd, 0, os.SEEK_SET)
        written = os.write(record.fd, data)
        os.ftruncate(record.fd, written)
        os.fsync(record.fd)
        stat = os.fstat(record.fd)
        record.generation = (stat.st_size, stat.st_mtime_ns)
        record.version_precondition = record.generation
        return written

    def revoke(self, handle: OpaqueResourceHandle) -> None:
        """Revoke and close one Handle namespace entry."""
        with self._lock:
            record = self._records.pop(handle.value, None)
        if record is not None:
            record.revoked = True
            os.close(record.fd)
            os.close(record.root_fd)

    def revoke_namespace(self, namespace: str) -> None:
        """Revoke every handle bound to an execution-domain namespace."""
        with self._lock:
            handles = [
                record.handle
                for record in self._records.values()
                if record.namespace == namespace
            ]
        for handle in handles:
            self.revoke(handle)

    def close(self) -> None:
        """Close all table entries."""
        with self._lock:
            handles = [record.handle for record in self._records.values()]
        for handle in handles:
            self.revoke(handle)

    def _claim(
        self,
        handle: OpaqueResourceHandle,
        *,
        operation: ResourceOperation,
        byte_count: int,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        domain_id: str,
        boot_epoch: int,
        namespace: str,
    ) -> _FileRecord:
        with self._lock:
            record = self._records.get(handle.value)
            if record is None or record.revoked:
                raise ResourceHandleError("unknown or revoked handle")
            mismatch = (
                record.owner != context.caller_principal
                or record.target != target
                or record.request_id != context.request_id
                or record.profile_id != context.profile_id
                or record.activation_id != context.activation_id
                or record.domain_id != domain_id
                or record.boot_epoch != boot_epoch
                or record.namespace != namespace
                or record.security_epoch != context.security_epoch
            )
            if mismatch:
                self._revoke_locked(record)
                raise ResourceHandleError("handle binding mismatch")
            if time.monotonic() >= record.expires_at:
                self._revoke_locked(record)
                raise ResourceHandleError("handle expired")
            if operation not in record.allowed_operations:
                raise ResourceHandleError("operation is outside handle scope")
            if byte_count < 0 or byte_count > record.remaining_bytes:
                raise ResourceHandleError("handle byte quota exceeded")
            self._revalidate_identity(record)
            if record.remaining_uses <= 0:
                self._revoke_locked(record)
                raise ResourceHandleError("handle use count exhausted")
            record.remaining_uses -= 1
            record.remaining_bytes -= byte_count
            return record

    def _revalidate_identity(self, record: _FileRecord) -> None:
        try:
            path_stat = os.stat(
                record.relative_path,
                dir_fd=record.root_fd,
                follow_symlinks=False,
            )
            fd_stat = os.fstat(record.fd)
        except OSError as exc:
            self._revoke_locked(record)
            raise ResourceHandleError("resource identity is unavailable") from exc
        identity = (path_stat.st_dev, path_stat.st_ino)
        fd_identity = (fd_stat.st_dev, fd_stat.st_ino)
        generation = (fd_stat.st_size, fd_stat.st_mtime_ns)
        if identity != record.identity or fd_identity != record.identity:
            self._revoke_locked(record)
            raise ResourceHandleError("resource identity changed")
        if generation != record.generation:
            self._revoke_locked(record)
            raise ResourceHandleError("resource generation changed")
        if not record.allow_hardlinks and fd_stat.st_nlink != 1:
            self._revoke_locked(record)
            raise ResourceHandleError("resource became hardlinked")

    def _revoke_locked(self, record: _FileRecord) -> None:
        self._records.pop(record.handle.value, None)
        record.revoked = True
        os.close(record.fd)
        os.close(record.root_fd)

    @staticmethod
    def _validate_relative_path(relative_path: str) -> None:
        path = PurePath(relative_path)
        if not relative_path or path.is_absolute() or ".." in path.parts:
            raise ResourceHandleError("path must remain relative to the bound root")
        if any(part in {"", "."} for part in path.parts):
            raise ResourceHandleError("ambiguous relative path")
