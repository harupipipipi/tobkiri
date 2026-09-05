"""Opaque descriptor-backed ResourceHandle table with TOCTOU defenses."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePath
import secrets
import stat
from threading import RLock
import time
from typing import Callable, Literal

from .errors import ResourceHandleError
from .models import OpaqueAuthorityRef, RequestContext
from .workspace_mutation import WorkspaceMutationLease, open_directory_nofollow

ResourceOperation = Literal["read", "write"]
BatchOperation = Literal["replace", "create", "delete"]
MAX_BATCH_MUTATIONS = 64
MAX_BATCH_BYTES = 16 * 1024 * 1024
_BATCH_PREFIX = ".tobkiri-batch-"


@dataclass(frozen=True)
class ResourceBatchMutation:
    """Host-internal batch mutation using only an opaque handle."""

    operation: BatchOperation
    handle: OpaqueResourceHandle
    data: bytes = b""
    mode: int = 0o600


@dataclass(frozen=True)
class ResourceBatchResult:
    """Content-free result of one durably committed batch."""

    transaction_id: str
    mutation_count: int
    total_bytes: int


@dataclass(frozen=True)
class OpaqueResourceHandle:
    """Opaque identifier safe to place in a Request payload."""

    value: str


@dataclass
class _FileRecord:
    handle: OpaqueResourceHandle
    root_fd: int
    root_identity: tuple[int, int]
    parent_fd: int
    parent_identity: tuple[int, int]
    parent_parts: tuple[str, ...]
    file_name: str
    fd: int
    relative_path: str
    identity: tuple[int, int]
    generation: tuple[int, int]
    content_sha256: str
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


@dataclass
class _AbsentFileRecord:
    handle: OpaqueResourceHandle
    root_fd: int
    root_identity: tuple[int, int]
    parent_fd: int
    parent_identity: tuple[int, int]
    parent_parts: tuple[str, ...]
    file_name: str
    relative_path: str
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
    revoked: bool = False


class ResourceHandleTable:
    """Host-owned table which never exposes raw paths or descriptors."""

    def __init__(
        self,
        *,
        batch_fault_injector: Callable[[str, int], None] | None = None,
    ) -> None:
        self._records: dict[str, _FileRecord | _AbsentFileRecord] = {}
        self._lock = RLock()
        self._batch_fault_injector = batch_fault_injector

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
        if "write" in operations and not (version_precondition is not None or atomic_replace):
            raise ResourceHandleError(
                "write handles require a version precondition or atomic replace"
            )
        root_path = Path(root)
        if not root_path.is_absolute():
            raise ResourceHandleError("resource root must be absolute")
        try:
            root_fd = open_directory_nofollow(root_path)
        except (OSError, ValueError) as exc:
            raise ResourceHandleError("resource root cannot be safely opened") from exc
        parts = tuple(PurePath(relative_path).parts)
        parent_fd: int | None = None
        file_flags = os.O_RDWR if "write" in operations else os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        try:
            parent_fd = self._open_parent(root_fd, parts[:-1])
            fd = os.open(parts[-1], file_flags, dir_fd=parent_fd)
            root_stat = os.fstat(root_fd)
            parent_stat = os.fstat(parent_fd)
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ResourceHandleError("resource must be a regular file")
            if not allow_hardlinks and file_stat.st_nlink != 1:
                raise ResourceHandleError("hardlinked resources are denied")
            identity = (file_stat.st_dev, file_stat.st_ino)
            generation = (file_stat.st_size, file_stat.st_mtime_ns)
            if version_precondition is not None:
                if version_precondition != generation:
                    raise ResourceHandleError("version precondition is stale")
            handle = OpaqueResourceHandle(secrets.token_urlsafe(32))
            record = _FileRecord(
                handle=handle,
                root_fd=root_fd,
                root_identity=(root_stat.st_dev, root_stat.st_ino),
                parent_fd=parent_fd,
                parent_identity=(parent_stat.st_dev, parent_stat.st_ino),
                parent_parts=parts[:-1],
                file_name=parts[-1],
                fd=fd,
                relative_path=relative_path,
                identity=identity,
                generation=generation,
                content_sha256=self._sha256_fd(fd),
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
            if parent_fd is not None:
                os.close(parent_fd)
            os.close(root_fd)
            if isinstance(exc, ResourceHandleError):
                raise
            raise ResourceHandleError("resource cannot be safely bound") from exc

    def bind_absent_file(
        self,
        *,
        root: Path,
        relative_path: str,
        owner: OpaqueAuthorityRef,
        target: OpaqueAuthorityRef,
        context: RequestContext,
        target_domain_id: str,
        target_boot_epoch: int,
        target_namespace: str,
        ttl_seconds: float,
        max_uses: int,
        max_bytes: int,
    ) -> OpaqueResourceHandle:
        """Bind a Host-verified absent path for compare-and-create."""

        self._validate_relative_path(relative_path)
        if ttl_seconds <= 0 or max_uses <= 0 or max_bytes < 0:
            raise ResourceHandleError("invalid handle limit")
        root_path = Path(root)
        if not root_path.is_absolute():
            raise ResourceHandleError("resource root must be absolute")
        root_fd: int | None = None
        parent_fd: int | None = None
        try:
            root_fd = open_directory_nofollow(root_path)
            parts = tuple(PurePath(relative_path).parts)
            parent_fd = self._open_parent(root_fd, parts[:-1])
            try:
                os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ResourceHandleError("resource path is not absent")
            root_stat = os.fstat(root_fd)
            parent_stat = os.fstat(parent_fd)
            handle = OpaqueResourceHandle(secrets.token_urlsafe(32))
            record = _AbsentFileRecord(
                handle=handle,
                root_fd=root_fd,
                root_identity=(root_stat.st_dev, root_stat.st_ino),
                parent_fd=parent_fd,
                parent_identity=(parent_stat.st_dev, parent_stat.st_ino),
                parent_parts=parts[:-1],
                file_name=parts[-1],
                relative_path=relative_path,
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
            )
            with self._lock:
                self._records[handle.value] = record
            return handle
        except Exception as exc:
            if parent_fd is not None:
                os.close(parent_fd)
            if root_fd is not None:
                os.close(root_fd)
            if isinstance(exc, ResourceHandleError):
                raise
            raise ResourceHandleError("absent resource cannot be safely bound") from exc

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
        record.content_sha256 = self._sha256_fd(record.fd)
        return written

    def compare_and_replace_file(
        self,
        handle: OpaqueResourceHandle,
        data: bytes,
        *,
        lease: WorkspaceMutationLease,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        domain_id: str,
        boot_epoch: int,
        namespace: str,
    ) -> int:
        """Replace one file iff its exact Host-captured preimage is unchanged.

        The required workspace lease serializes Host writers.  Unrelated
        processes do not honor that advisory lock, so this method revalidates
        the inode, size, mtime, and content digest immediately before the
        atomic rename; it does not claim to lock arbitrary external writers.
        """

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
        if not record.atomic_replace:
            raise ResourceHandleError("handle does not permit atomic replacement")
        lease.assert_bound(
            context=context,
            target=target,
            target_domain_id=domain_id,
            target_boot_epoch=boot_epoch,
            target_namespace=namespace,
        )
        lease.assert_resource_root(record.root_identity)
        mode = stat.S_IMODE(os.fstat(record.fd).st_mode)
        temporary_name = f".{record.file_name}.{secrets.token_hex(16)}.tmp"
        operation_parent_fd = os.dup(record.parent_fd)
        temporary_fd: int | None = None
        published = False
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            temporary_fd = os.open(
                temporary_name,
                flags,
                mode,
                dir_fd=operation_parent_fd,
            )
            os.fchmod(temporary_fd, mode)
            self._write_all(temporary_fd, data)
            os.fsync(temporary_fd)
            self._revalidate_identity(record, verify_content=True)
            lease.revalidate_root()
            os.replace(
                temporary_name,
                record.file_name,
                src_dir_fd=operation_parent_fd,
                dst_dir_fd=operation_parent_fd,
            )
            published = True
            os.fsync(operation_parent_fd)
            replacement_path_stat = os.stat(
                record.file_name,
                dir_fd=operation_parent_fd,
                follow_symlinks=False,
            )
            replacement_stat = os.fstat(temporary_fd)
            replacement_identity = (
                replacement_stat.st_dev,
                replacement_stat.st_ino,
            )
            if replacement_identity != (
                replacement_path_stat.st_dev,
                replacement_path_stat.st_ino,
            ):
                self._revoke_locked(record)
                raise ResourceHandleError("published resource identity changed")
            if hashlib.sha256(data).hexdigest() != self._sha256_fd(temporary_fd):
                self._revoke_locked(record)
                raise ResourceHandleError("published resource content changed")
            os.close(record.fd)
            record.fd = temporary_fd
            temporary_fd = None
            record.identity = replacement_identity
            record.generation = (
                replacement_stat.st_size,
                replacement_stat.st_mtime_ns,
            )
            record.content_sha256 = hashlib.sha256(data).hexdigest()
            record.version_precondition = record.generation
            return len(data)
        except ResourceHandleError:
            raise
        except OSError as exc:
            raise ResourceHandleError("resource replacement failed") from exc
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if not published:
                try:
                    os.unlink(temporary_name, dir_fd=operation_parent_fd)
                except FileNotFoundError:
                    pass
            os.close(operation_parent_fd)

    def compare_and_create_file(
        self,
        handle: OpaqueResourceHandle,
        data: bytes,
        *,
        lease: WorkspaceMutationLease,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        domain_id: str,
        boot_epoch: int,
        namespace: str,
        mode: int = 0o600,
    ) -> int:
        """Create one regular file iff the Host-bound destination stays absent."""

        if mode < 0 or mode & ~0o777:
            raise ResourceHandleError("resource creation mode is invalid")
        record = self._claim_absent(
            handle,
            byte_count=len(data),
            context=context,
            target=target,
            domain_id=domain_id,
            boot_epoch=boot_epoch,
            namespace=namespace,
        )
        self._assert_lease(
            lease,
            record,
            context=context,
            target=target,
            domain_id=domain_id,
            boot_epoch=boot_epoch,
            namespace=namespace,
        )
        operation_parent_fd = os.dup(record.parent_fd)
        temporary_name = f".{record.file_name}.{secrets.token_hex(16)}.tmp"
        temporary_fd: int | None = None
        published = False
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            temporary_fd = os.open(
                temporary_name,
                flags,
                mode,
                dir_fd=operation_parent_fd,
            )
            os.fchmod(temporary_fd, mode)
            self._write_all(temporary_fd, data)
            os.fsync(temporary_fd)
            self._revalidate_absence(record)
            lease.revalidate_root()
            os.replace(
                temporary_name,
                record.file_name,
                src_dir_fd=operation_parent_fd,
                dst_dir_fd=operation_parent_fd,
            )
            published = True
            path_value = os.stat(
                record.file_name,
                dir_fd=operation_parent_fd,
                follow_symlinks=False,
            )
            descriptor_value = os.fstat(temporary_fd)
            if (path_value.st_dev, path_value.st_ino) != (
                descriptor_value.st_dev,
                descriptor_value.st_ino,
            ):
                self._revoke_locked(record)
                raise ResourceHandleError("created resource identity changed")
            os.fsync(operation_parent_fd)
            self._revoke_locked(record)
            return len(data)
        except ResourceHandleError:
            raise
        except OSError as exc:
            raise ResourceHandleError("resource creation failed") from exc
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if not published:
                try:
                    os.unlink(temporary_name, dir_fd=operation_parent_fd)
                except FileNotFoundError:
                    pass
            os.close(operation_parent_fd)

    def compare_and_delete_file(
        self,
        handle: OpaqueResourceHandle,
        *,
        lease: WorkspaceMutationLease,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        domain_id: str,
        boot_epoch: int,
        namespace: str,
    ) -> None:
        """Delete one regular file iff its exact bound preimage is unchanged."""

        record = self._claim(
            handle,
            operation="write",
            byte_count=0,
            context=context,
            target=target,
            domain_id=domain_id,
            boot_epoch=boot_epoch,
            namespace=namespace,
        )
        if not record.atomic_replace:
            raise ResourceHandleError("handle does not permit atomic deletion")
        self._assert_lease(
            lease,
            record,
            context=context,
            target=target,
            domain_id=domain_id,
            boot_epoch=boot_epoch,
            namespace=namespace,
        )
        operation_parent_fd = os.dup(record.parent_fd)
        try:
            self._revalidate_identity(record, verify_content=True)
            lease.revalidate_root()
            os.unlink(record.file_name, dir_fd=operation_parent_fd)
            os.fsync(operation_parent_fd)
            self._revoke_locked(record)
        except ResourceHandleError:
            raise
        except OSError as exc:
            raise ResourceHandleError("resource deletion failed") from exc
        finally:
            os.close(operation_parent_fd)

    def publish_batch(
        self,
        mutations: tuple[ResourceBatchMutation, ...],
        *,
        lease: WorkspaceMutationLease,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        domain_id: str,
        boot_epoch: int,
        namespace: str,
    ) -> ResourceBatchResult:
        """Durably publish one bounded Host-writer batch or roll it back.

        Portable POSIX rename cannot hide intermediate per-file publications
        from arbitrary external readers.  The guarantee is a deterministic
        committed or recovered Host-writer outcome, not snapshot isolation.
        """

        total_bytes = sum(len(item.data) for item in mutations)
        if not mutations or len(mutations) > MAX_BATCH_MUTATIONS:
            raise ResourceHandleError("workspace batch count limit exceeded")
        if total_bytes > MAX_BATCH_BYTES:
            raise ResourceHandleError("workspace batch byte limit exceeded")
        if len({item.handle.value for item in mutations}) != len(mutations):
            raise ResourceHandleError("workspace batch contains duplicate handles")
        records = self._validate_batch_records(
            mutations,
            context=context,
            target=target,
            domain_id=domain_id,
            boot_epoch=boot_epoch,
            namespace=namespace,
        )
        if len({record.relative_path for record in records}) != len(records):
            raise ResourceHandleError("workspace batch contains duplicate paths")
        lease.assert_bound(
            context=context,
            target=target,
            target_domain_id=domain_id,
            target_boot_epoch=boot_epoch,
            target_namespace=namespace,
        )
        for record in records:
            lease.assert_resource_root(record.root_identity)
        transaction_root, binding_key, root_fd = lease.host_batch_state()
        transaction_id = secrets.token_hex(16)
        entries = self._batch_entries(transaction_id, mutations, records)
        journal = {
            "version": "tobkiri.workspace-batch.v1",
            "transaction_id": transaction_id,
            "binding_key": binding_key,
            "phase": "planned",
            "published_count": 0,
            "entries": entries,
        }
        journal_fd = self._open_batch_journal_root(transaction_root)
        try:
            self._cleanup_orphan_journal_temps(journal_fd)
            if self._quarantine_exists(journal_fd):
                raise ResourceHandleError("workspace batch journal is quarantined")
            if self._journal_exists(journal_fd):
                raise ResourceHandleError("incomplete workspace batch requires recovery")
            try:
                self._write_journal(journal_fd, journal)
                self._fault("after_journal", 0)
                self._prepare_batch_files(mutations, records, entries)
                journal["phase"] = "prepared"
                self._write_journal(journal_fd, journal)
                self._fault("after_prepare", 0)
                self._revalidate_batch_records(mutations, records)
                self._consume_batch_records(mutations, records)
                journal["phase"] = "publishing"
                self._write_journal(journal_fd, journal)
                self._fault("before_publish", 0)
                for index, (mutation, record, entry) in enumerate(
                    zip(mutations, records, entries, strict=True),
                    start=1,
                ):
                    self._publish_batch_entry(mutation, record, entry)
                    journal["published_count"] = index
                    self._write_journal(journal_fd, journal)
                    self._fault("after_publish", index)
                journal["phase"] = "committed"
                self._write_journal(journal_fd, journal)
            except Exception as exc:
                try:
                    self._rollback_batch(root_fd, journal_fd, journal)
                    self._cleanup_orphan_journal_temps(journal_fd)
                except Exception as rollback_error:
                    self._quarantine_journal(journal_fd, journal)
                    raise ResourceHandleError(
                        "workspace batch rollback requires quarantine"
                    ) from rollback_error
                if isinstance(exc, ResourceHandleError):
                    raise
                raise ResourceHandleError("workspace batch publication failed") from exc
            try:
                self._fault("after_commit", len(entries))
                self._cleanup_batch_entries(root_fd, entries)
                self._remove_journal(journal_fd)
            except Exception:
                # The fsynced committed record is the durable decision.  A
                # later lease deterministically finishes private-file cleanup.
                pass
        finally:
            os.close(journal_fd)
            os.close(root_fd)
        return ResourceBatchResult(transaction_id, len(mutations), total_bytes)

    def recover_incomplete_batch(self, lease: WorkspaceMutationLease) -> None:
        """Recover an exact-root journal before another opaque lease is issued."""

        transaction_root, binding_key, root_fd = lease.host_batch_state()
        journal_fd = self._open_batch_journal_root(transaction_root)
        try:
            self._cleanup_orphan_journal_temps(journal_fd)
            if self._quarantine_exists(journal_fd):
                raise ResourceHandleError("workspace batch journal is quarantined")
            if not self._journal_exists(journal_fd):
                return
            try:
                journal = self._read_journal(journal_fd)
            except Exception:
                self._quarantine_journal(journal_fd, {})
                raise
            if journal.get("binding_key") != binding_key:
                self._quarantine_journal(journal_fd, journal)
                raise ResourceHandleError("workspace batch binding cannot be recovered")
            if journal.get("phase") == "committed":
                self._cleanup_batch_entries(root_fd, journal["entries"])
                self._remove_journal(journal_fd)
                return
            self._rollback_batch(root_fd, journal_fd, journal)
        finally:
            os.close(journal_fd)
            os.close(root_fd)

    def _validate_batch_records(
        self,
        mutations: tuple[ResourceBatchMutation, ...],
        *,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        domain_id: str,
        boot_epoch: int,
        namespace: str,
    ) -> list[_FileRecord | _AbsentFileRecord]:
        records: list[_FileRecord | _AbsentFileRecord] = []
        with self._lock:
            for mutation in mutations:
                record = self._records.get(mutation.handle.value)
                if record is None or record.revoked:
                    raise ResourceHandleError("unknown or revoked batch handle")
                expected_type = _AbsentFileRecord if mutation.operation == "create" else _FileRecord
                if not isinstance(record, expected_type):
                    raise ResourceHandleError("workspace batch preimage kind mismatch")
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
                    raise ResourceHandleError("workspace batch handle binding mismatch")
                if time.monotonic() >= record.expires_at:
                    raise ResourceHandleError("workspace batch handle expired")
                if record.remaining_uses <= 0 or len(mutation.data) > record.remaining_bytes:
                    raise ResourceHandleError("workspace batch handle quota exceeded")
                records.append(record)
        self._revalidate_batch_records(mutations, records)
        return records

    def _revalidate_batch_records(
        self,
        mutations: tuple[ResourceBatchMutation, ...],
        records: list[_FileRecord | _AbsentFileRecord],
    ) -> None:
        for mutation, record in zip(mutations, records, strict=True):
            if mutation.operation == "create":
                assert isinstance(record, _AbsentFileRecord)
                self._revalidate_absence(record)
            else:
                assert isinstance(record, _FileRecord)
                self._revalidate_identity(record, verify_content=True)

    def _consume_batch_records(
        self,
        mutations: tuple[ResourceBatchMutation, ...],
        records: list[_FileRecord | _AbsentFileRecord],
    ) -> None:
        with self._lock:
            for mutation, record in zip(mutations, records, strict=True):
                if record.revoked or record.remaining_uses <= 0:
                    raise ResourceHandleError("workspace batch handle was consumed")
                record.remaining_uses -= 1
                record.remaining_bytes -= len(mutation.data)

    def _batch_entries(
        self,
        transaction_id: str,
        mutations: tuple[ResourceBatchMutation, ...],
        records: list[_FileRecord | _AbsentFileRecord],
    ) -> list[dict[str, object]]:
        entries = []
        for index, (mutation, record) in enumerate(zip(mutations, records, strict=True)):
            existing = record if isinstance(record, _FileRecord) else None
            entries.append(
                {
                    "operation": mutation.operation,
                    "relative_path": record.relative_path,
                    "stage_name": (
                        f"{_BATCH_PREFIX}{transaction_id}-stage-{index}"
                        if mutation.operation != "delete"
                        else ""
                    ),
                    "backup_name": (
                        f"{_BATCH_PREFIX}{transaction_id}-backup-{index}"
                        if existing is not None
                        else ""
                    ),
                    "old_sha256": existing.content_sha256 if existing else "",
                    "new_sha256": hashlib.sha256(mutation.data).hexdigest()
                    if mutation.operation != "delete"
                    else "",
                    "old_mode": stat.S_IMODE(os.fstat(existing.fd).st_mode) if existing else 0,
                    "old_atime_ns": os.fstat(existing.fd).st_atime_ns if existing else 0,
                    "old_mtime_ns": os.fstat(existing.fd).st_mtime_ns if existing else 0,
                    "new_mode": stat.S_IMODE(os.fstat(existing.fd).st_mode)
                    if mutation.operation == "replace" and existing
                    else mutation.mode,
                }
            )
        return entries

    def _prepare_batch_files(
        self,
        mutations: tuple[ResourceBatchMutation, ...],
        records: list[_FileRecord | _AbsentFileRecord],
        entries: list[dict[str, object]],
    ) -> None:
        for index, (mutation, record, entry) in enumerate(
            zip(mutations, records, entries, strict=True),
            start=1,
        ):
            if isinstance(record, _FileRecord):
                backup = str(entry["backup_name"])
                backup_fd = self._open_private_batch_file(record.parent_fd, backup)
                try:
                    self._copy_fd(record.fd, backup_fd)
                    os.fchmod(backup_fd, int(str(entry["old_mode"])))
                    os.utime(
                        backup_fd,
                        ns=(
                            int(str(entry["old_atime_ns"])),
                            int(str(entry["old_mtime_ns"])),
                        ),
                    )
                    os.fsync(backup_fd)
                finally:
                    os.close(backup_fd)
            if mutation.operation != "delete":
                stage = str(entry["stage_name"])
                stage_fd = self._open_private_batch_file(record.parent_fd, stage)
                try:
                    self._write_all(stage_fd, mutation.data)
                    os.fchmod(stage_fd, int(str(entry["new_mode"])))
                    os.fsync(stage_fd)
                finally:
                    os.close(stage_fd)
            os.fsync(record.parent_fd)
            self._fault("after_stage", index)

    @staticmethod
    def _open_private_batch_file(parent_fd: int, name: str) -> int:
        if not name.startswith(_BATCH_PREFIX) or len(name) > 160:
            raise ResourceHandleError("workspace batch private name is invalid")
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            os.close(fd)
            raise ResourceHandleError("workspace batch private file is unsafe")
        return fd

    @staticmethod
    def _copy_fd(source_fd: int, destination_fd: int) -> None:
        offset = os.lseek(source_fd, 0, os.SEEK_CUR)
        try:
            os.lseek(source_fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                ResourceHandleTable._write_all(destination_fd, chunk)
        finally:
            os.lseek(source_fd, offset, os.SEEK_SET)

    def _publish_batch_entry(
        self,
        mutation: ResourceBatchMutation,
        record: _FileRecord | _AbsentFileRecord,
        entry: dict[str, object],
    ) -> None:
        if mutation.operation == "delete":
            os.unlink(record.file_name, dir_fd=record.parent_fd)
        else:
            os.replace(
                str(entry["stage_name"]),
                record.file_name,
                src_dir_fd=record.parent_fd,
                dst_dir_fd=record.parent_fd,
            )
        os.fsync(record.parent_fd)

    def _open_batch_journal_root(self, transaction_root: Path) -> int:
        transaction_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if transaction_root.is_symlink() or transaction_root.resolve() != transaction_root:
            raise ResourceHandleError("workspace batch journal root is unsafe")
        fd = open_directory_nofollow(transaction_root)
        value = os.fstat(fd)
        if hasattr(os, "geteuid") and value.st_uid != os.geteuid():
            os.close(fd)
            raise ResourceHandleError("workspace batch journal owner is invalid")
        if stat.S_IMODE(value.st_mode) & 0o022:
            os.close(fd)
            raise ResourceHandleError("workspace batch journal mode is unsafe")
        return fd

    @staticmethod
    def _journal_exists(journal_fd: int) -> bool:
        try:
            os.stat("active.json", dir_fd=journal_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _quarantine_exists(journal_fd: int) -> bool:
        return any(name.startswith("quarantine-") for name in os.listdir(journal_fd))

    @staticmethod
    def _cleanup_orphan_journal_temps(journal_fd: int) -> None:
        for name in os.listdir(journal_fd):
            if not name.startswith(".journal-") or not name.endswith(".tmp"):
                continue
            if len(name) > 80:
                raise ResourceHandleError("workspace batch journal temp is invalid")
            value = os.stat(name, dir_fd=journal_fd, follow_symlinks=False)
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise ResourceHandleError("workspace batch journal temp is unsafe")
            os.unlink(name, dir_fd=journal_fd)
        os.fsync(journal_fd)

    def _write_journal(self, journal_fd: int, journal: dict[str, object]) -> None:
        data = json.dumps(
            journal,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = f".journal-{secrets.token_hex(12)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600, dir_fd=journal_fd)
        try:
            self._write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(
            temporary,
            "active.json",
            src_dir_fd=journal_fd,
            dst_dir_fd=journal_fd,
        )
        os.fsync(journal_fd)

    def _read_journal(self, journal_fd: int) -> dict[str, object]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open("active.json", flags, dir_fd=journal_fd)
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise ResourceHandleError("workspace batch journal is unsafe")
            data = bytearray()
            while len(data) <= 1024 * 1024:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > 1024 * 1024:
                raise ResourceHandleError("workspace batch journal is oversized")
        finally:
            os.close(fd)
        raw = json.loads(bytes(data))
        if not isinstance(raw, dict) or raw.get("version") != "tobkiri.workspace-batch.v1":
            raise ResourceHandleError("workspace batch journal is invalid")
        if raw.get("phase") not in {"planned", "prepared", "publishing", "committed"}:
            raise ResourceHandleError("workspace batch journal phase is invalid")
        transaction_id = str(raw.get("transaction_id") or "")
        if len(transaction_id) != 32 or not all(
            character in "0123456789abcdef" for character in transaction_id
        ):
            raise ResourceHandleError("workspace batch transaction ID is invalid")
        entries = raw.get("entries")
        if not isinstance(entries, list) or not entries or len(entries) > MAX_BATCH_MUTATIONS:
            raise ResourceHandleError("workspace batch journal entries are invalid")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ResourceHandleError("workspace batch journal entry is invalid")
            self._validate_relative_path(str(entry.get("relative_path") or ""))
            operation = str(entry.get("operation") or "")
            if operation not in {"replace", "create", "delete"}:
                raise ResourceHandleError("workspace batch journal operation is invalid")
            for key in ("stage_name", "backup_name"):
                name = str(entry.get(key) or "")
                if name and (not name.startswith(_BATCH_PREFIX) or len(name) > 160):
                    raise ResourceHandleError("workspace batch journal name is invalid")
            old_digest = str(entry.get("old_sha256") or "")
            new_digest = str(entry.get("new_sha256") or "")
            if operation != "create" and not self._is_sha256(old_digest):
                raise ResourceHandleError("workspace batch old digest is invalid")
            if operation != "delete" and not self._is_sha256(new_digest):
                raise ResourceHandleError("workspace batch new digest is invalid")
        return raw

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)

    def _rollback_batch(
        self,
        root_fd: int,
        journal_fd: int,
        journal: dict[str, object],
    ) -> None:
        entries = journal.get("entries")
        if not isinstance(entries, list):
            raise ResourceHandleError("workspace batch rollback journal is invalid")
        for entry in reversed(entries):
            assert isinstance(entry, dict)
            parts = tuple(PurePath(str(entry["relative_path"])).parts)
            parent_fd = self._open_parent(root_fd, parts[:-1])
            try:
                name = parts[-1]
                operation = str(entry["operation"])
                current = self._path_digest(parent_fd, name)
                old_digest = str(entry.get("old_sha256") or "")
                new_digest = str(entry.get("new_sha256") or "")
                backup = str(entry.get("backup_name") or "")
                if operation == "create":
                    if current is not None:
                        if current != new_digest:
                            raise ResourceHandleError(
                                "workspace batch create rollback is ambiguous"
                            )
                        os.unlink(name, dir_fd=parent_fd)
                else:
                    backup_exists = self._private_exists(parent_fd, backup)
                    allowed = {old_digest, new_digest if operation == "replace" else None}
                    if current not in allowed:
                        raise ResourceHandleError("workspace batch existing rollback is ambiguous")
                    if backup_exists:
                        os.replace(
                            backup,
                            name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                    elif current != old_digest:
                        raise ResourceHandleError("workspace batch backup is unavailable")
                self._safe_private_unlink(parent_fd, str(entry.get("stage_name") or ""))
                self._safe_private_unlink(parent_fd, backup)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        self._remove_journal(journal_fd)

    def _cleanup_batch_entries(
        self,
        root_fd: int,
        entries: object,
    ) -> None:
        if not isinstance(entries, list):
            raise ResourceHandleError("workspace batch cleanup entries are invalid")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ResourceHandleError("workspace batch cleanup entry is invalid")
            parts = tuple(PurePath(str(entry["relative_path"])).parts)
            parent_fd = self._open_parent(root_fd, parts[:-1])
            try:
                self._safe_private_unlink(parent_fd, str(entry.get("stage_name") or ""))
                self._safe_private_unlink(parent_fd, str(entry.get("backup_name") or ""))
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    @staticmethod
    def _path_digest(parent_fd: int, name: str) -> str | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode):
                raise ResourceHandleError("workspace batch target is not regular")
            return ResourceHandleTable._sha256_fd(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _private_exists(parent_fd: int, name: str) -> bool:
        if not name:
            return False
        if not name.startswith(_BATCH_PREFIX):
            raise ResourceHandleError("workspace batch private name is invalid")
        try:
            value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise ResourceHandleError("workspace batch private file is unsafe")
        return True

    @classmethod
    def _safe_private_unlink(cls, parent_fd: int, name: str) -> None:
        if not name or not cls._private_exists(parent_fd, name):
            return
        os.unlink(name, dir_fd=parent_fd)

    @staticmethod
    def _remove_journal(journal_fd: int) -> None:
        try:
            os.unlink("active.json", dir_fd=journal_fd)
        except FileNotFoundError:
            return
        os.fsync(journal_fd)

    def _quarantine_journal(
        self,
        journal_fd: int,
        journal: dict[str, object],
    ) -> None:
        transaction_id = str(journal.get("transaction_id") or "")
        if len(transaction_id) != 32 or not all(
            character in "0123456789abcdef" for character in transaction_id
        ):
            transaction_id = secrets.token_hex(16)
        name = f"quarantine-{transaction_id}.json"
        try:
            os.replace(
                "active.json",
                name,
                src_dir_fd=journal_fd,
                dst_dir_fd=journal_fd,
            )
            os.fsync(journal_fd)
        except FileNotFoundError:
            pass

    def _fault(self, phase: str, index: int) -> None:
        if self._batch_fault_injector is not None:
            self._batch_fault_injector(phase, index)

    def revoke(self, handle: OpaqueResourceHandle) -> None:
        """Revoke and close one Handle namespace entry."""
        with self._lock:
            record = self._records.get(handle.value)
        if record is not None:
            self._revoke_locked(record)

    def revoke_namespace(self, namespace: str) -> None:
        """Revoke every handle bound to an execution-domain namespace."""
        with self._lock:
            handles = [
                record.handle for record in self._records.values() if record.namespace == namespace
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
            if not isinstance(record, _FileRecord):
                raise ResourceHandleError("handle is bound to an absent resource")
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
            self._revalidate_identity(record, verify_content=False)
            if record.remaining_uses <= 0:
                self._revoke_locked(record)
                raise ResourceHandleError("handle use count exhausted")
            record.remaining_uses -= 1
            record.remaining_bytes -= byte_count
            return record

    def _claim_absent(
        self,
        handle: OpaqueResourceHandle,
        *,
        byte_count: int,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        domain_id: str,
        boot_epoch: int,
        namespace: str,
    ) -> _AbsentFileRecord:
        with self._lock:
            record = self._records.get(handle.value)
            if record is None or record.revoked:
                raise ResourceHandleError("unknown or revoked handle")
            if not isinstance(record, _AbsentFileRecord):
                raise ResourceHandleError("handle is bound to an existing resource")
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
            if byte_count < 0 or byte_count > record.remaining_bytes:
                raise ResourceHandleError("handle byte quota exceeded")
            self._revalidate_absence(record)
            if record.remaining_uses <= 0:
                self._revoke_locked(record)
                raise ResourceHandleError("handle use count exhausted")
            record.remaining_uses -= 1
            record.remaining_bytes -= byte_count
            return record

    @staticmethod
    def _assert_lease(
        lease: WorkspaceMutationLease,
        record: _FileRecord | _AbsentFileRecord,
        *,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        domain_id: str,
        boot_epoch: int,
        namespace: str,
    ) -> None:
        lease.assert_bound(
            context=context,
            target=target,
            target_domain_id=domain_id,
            target_boot_epoch=boot_epoch,
            target_namespace=namespace,
        )
        lease.assert_resource_root(record.root_identity)

    def _revalidate_identity(
        self,
        record: _FileRecord,
        *,
        verify_content: bool,
    ) -> None:
        current_parent_fd: int | None = None
        try:
            root_stat = os.fstat(record.root_fd)
            current_parent_fd = self._open_parent(
                record.root_fd,
                record.parent_parts,
            )
            current_parent = os.fstat(current_parent_fd)
            path_stat = os.stat(
                record.file_name,
                dir_fd=current_parent_fd,
                follow_symlinks=False,
            )
            fd_stat = os.fstat(record.fd)
        except OSError as exc:
            self._revoke_locked(record)
            raise ResourceHandleError("resource identity is unavailable") from exc
        finally:
            if current_parent_fd is not None:
                os.close(current_parent_fd)
        if (root_stat.st_dev, root_stat.st_ino) != record.root_identity:
            self._revoke_locked(record)
            raise ResourceHandleError("resource root identity changed")
        if (current_parent.st_dev, current_parent.st_ino) != record.parent_identity:
            self._revoke_locked(record)
            raise ResourceHandleError("resource parent identity changed")
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
        if verify_content and self._sha256_fd(record.fd) != record.content_sha256:
            self._revoke_locked(record)
            raise ResourceHandleError("resource content changed")

    def _revalidate_absence(self, record: _AbsentFileRecord) -> None:
        current_parent_fd: int | None = None
        try:
            root_stat = os.fstat(record.root_fd)
            current_parent_fd = self._open_parent(
                record.root_fd,
                record.parent_parts,
            )
            current_parent = os.fstat(current_parent_fd)
            try:
                os.stat(
                    record.file_name,
                    dir_fd=current_parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                self._revoke_locked(record)
                raise ResourceHandleError("resource path is no longer absent")
        except ResourceHandleError:
            raise
        except OSError as exc:
            self._revoke_locked(record)
            raise ResourceHandleError("resource parent is unavailable") from exc
        finally:
            if current_parent_fd is not None:
                os.close(current_parent_fd)
        if (root_stat.st_dev, root_stat.st_ino) != record.root_identity:
            self._revoke_locked(record)
            raise ResourceHandleError("resource root identity changed")
        if (current_parent.st_dev, current_parent.st_ino) != record.parent_identity:
            self._revoke_locked(record)
            raise ResourceHandleError("resource parent identity changed")

    def _revoke_locked(self, record: _FileRecord | _AbsentFileRecord) -> None:
        with self._lock:
            if record.revoked:
                return
            self._records.pop(record.handle.value, None)
            record.revoked = True
            if isinstance(record, _FileRecord):
                os.close(record.fd)
            os.close(record.parent_fd)
            os.close(record.root_fd)

    @staticmethod
    def _open_parent(root_fd: int, parts: tuple[str, ...]) -> int:
        descriptor = os.dup(root_fd)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            for component in parts:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _sha256_fd(fd: int) -> str:
        digest = hashlib.sha256()
        offset = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            os.lseek(fd, offset, os.SEEK_SET)
        return digest.hexdigest()

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError("short resource write")
            written += count

    @staticmethod
    def _validate_relative_path(relative_path: str) -> None:
        path = PurePath(relative_path)
        if not relative_path or path.is_absolute() or ".." in path.parts:
            raise ResourceHandleError("path must remain relative to the bound root")
        if any(part in {"", "."} for part in path.parts):
            raise ResourceHandleError("ambiguous relative path")
        if any(part.casefold() == ".git" for part in path.parts):
            raise ResourceHandleError("Git control paths require a dedicated provider")
