"""Scoped, fail-closed cleanup helpers for packaging workflows.

Windows packaging can briefly retain an executable after a child process has
exited.  Cleanup must tolerate only the small class of Windows sharing and
access-denied errors that can represent that race.  It must never turn an
arbitrary deletion error into a successful package, or remove a caller's
scope root by mistake.
"""

from __future__ import annotations

import errno
import ctypes
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Optional, Sequence, Union


_IS_WINDOWS = os.name == "nt"
_REAL_WINDOWS = os.name == "nt"
_TRANSIENT_WINDOWS_WINERRORS = frozenset({5, 32, 33})
_TRANSIENT_WINDOWS_ERRNOS = frozenset({errno.EACCES, errno.EBUSY})
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BACKOFF_SECONDS = (0.1, 0.25)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_NOFOLLOW_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EINVAL),
    }
)
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_DELETE = 0x00010000
_WINDOWS_FILE_READ_ATTRIBUTES = 0x00000080
_WINDOWS_FILE_LIST_DIRECTORY = 0x00000001
_WINDOWS_FILE_TRAVERSE = 0x00000020
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_FILE_SHARE_DELETE = 0x00000004
_WINDOWS_HANDLE_SHARE_MODE = _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_RENAME_INFO_CLASS = 3
_WINDOWS_FILE_DISPOSITION_INFO_CLASS = 4
_WINDOWS_INVALID_HANDLE = ctypes.c_void_p(-1).value
_PosixMountIdentity = tuple[str, int]


class _WindowsByHandleFileInformation(ctypes.Structure):
    """Win32 BY_HANDLE_FILE_INFORMATION layout."""

    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _WindowsFileRenameInfo(ctypes.Structure):
    """Prefix of the variable-length FILE_RENAME_INFO structure.

    The first member is a four-byte union: ``ReplaceIfExists`` for
    ``FileRenameInfo`` and ``Flags`` for ``FileRenameInfoEx``.  Keeping the
    union's ABI width explicit avoids relying on the one-byte BOOLEAN member
    to supply its padding.
    """

    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_uint16 * 1),
    ]


class _WindowsFileDispositionInfo(ctypes.Structure):
    """FILE_DISPOSITION_INFO structure used for handle-bound deletion."""

    _fields_ = [("DeleteFile", wintypes.BOOLEAN)]


@dataclass(frozen=True)
class _WindowsFileIdentity:
    """Native volume/file identity and attributes for one open handle."""

    volume_serial: int
    file_index: int
    file_attributes: int


@dataclass(frozen=True)
class _WindowsHandleRecord:
    """An owned native handle and the identity that makes retry safe."""

    path: Path
    handle: int
    identity: Optional[_WindowsFileIdentity]


@dataclass
class _WindowsCloseReport:
    """Close errors plus handles whose ownership must remain explicit."""

    errors: list[OSError] = field(default_factory=list)
    unclosed: list[_WindowsHandleRecord] = field(default_factory=list)


class _WindowsApi:
    """ctypes surface for no-delete-sharing handle-relative cleanup."""

    def __init__(self) -> None:
        if not _REAL_WINDOWS:
            raise RuntimeError("native Windows cleanup is unavailable on this host")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self._create_file = kernel32.CreateFileW
        self._create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._create_file.restype = wintypes.HANDLE
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL
        self._get_file_information = kernel32.GetFileInformationByHandle
        self._get_file_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_WindowsByHandleFileInformation),
        ]
        self._get_file_information.restype = wintypes.BOOL
        self._set_file_information = kernel32.SetFileInformationByHandle
        self._set_file_information.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._set_file_information.restype = wintypes.BOOL

    @staticmethod
    def _last_error(path: Path) -> OSError:
        error = getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
        error.filename = os.fspath(path)
        return error

    def open(
        self,
        path: Path,
        *,
        directory: bool,
        share_mode: int = _WINDOWS_HANDLE_SHARE_MODE,
    ) -> int:
        """Open one final component without reparse or delete sharing."""

        if share_mode & _WINDOWS_FILE_SHARE_DELETE:
            raise ValueError("Windows cleanup handles must not share delete access")

        access = _WINDOWS_DELETE | _WINDOWS_FILE_READ_ATTRIBUTES
        flags = _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            # Root/ancestor handles are also the non-delete-sharing trust
            # boundary for relative traversal and identity checks.
            access |= _WINDOWS_FILE_LIST_DIRECTORY | _WINDOWS_FILE_TRAVERSE
            flags |= _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
        handle = self._create_file(
            os.fspath(path),
            access,
            share_mode,
            None,
            _WINDOWS_OPEN_EXISTING,
            flags,
            None,
        )
        value = getattr(handle, "value", handle)
        if value is None or int(value) == _WINDOWS_INVALID_HANDLE:
            raise self._last_error(path)
        return int(value)

    def close(self, handle: int) -> None:
        """Close one native handle."""

        if not self._close_handle(wintypes.HANDLE(handle)):
            raise self._last_error(Path("<native-handle>"))

    def identity(self, handle: int) -> _WindowsFileIdentity:
        """Read native volume/file identity and reparse attributes."""

        information = _WindowsByHandleFileInformation()
        if not self._get_file_information(
            wintypes.HANDLE(handle), ctypes.byref(information)
        ):
            raise self._last_error(Path("<native-handle>"))
        file_index = (int(information.nFileIndexHigh) << 32) | int(
            information.nFileIndexLow
        )
        return _WindowsFileIdentity(
            volume_serial=int(information.dwVolumeSerialNumber),
            file_index=file_index,
            file_attributes=int(information.dwFileAttributes),
        )

    def path_identity(self, path: Path, *, directory: bool) -> _WindowsFileIdentity:
        """Probe the current no-follow pathname mapping by native file identity.

        The long-lived ownership handles intentionally deny delete sharing.
        This short-lived probe requests no delete access and permits delete
        sharing, making it compatible with those handles while independently
        proving that the pathname still resolves to the bound object.
        """

        flags = _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
        handle = self._create_file(
            os.fspath(path),
            _WINDOWS_FILE_READ_ATTRIBUTES,
            _WINDOWS_FILE_SHARE_READ
            | _WINDOWS_FILE_SHARE_WRITE
            | _WINDOWS_FILE_SHARE_DELETE,
            None,
            _WINDOWS_OPEN_EXISTING,
            flags,
            None,
        )
        value = getattr(handle, "value", handle)
        if value is None or int(value) == _WINDOWS_INVALID_HANDLE:
            raise self._last_error(path)
        probe = int(value)
        try:
            return self.identity(probe)
        finally:
            self.close(probe)

    def rename_same_parent(
        self,
        handle: int,
        parent_handle: int,
        parent_path: Path,
        name: str,
    ) -> None:
        """Rename to a validated absolute sibling while the parent is pinned.

        Windows accepts a relative ``FILE_RENAME_INFO`` name with a
        ``RootDirectory`` handle according to the API documentation, but the
        supported Windows runner rejects that form with ``ERROR_INVALID_PARAMETER``.
        Use the other documented form: an absolute destination and a NULL
        ``RootDirectory``.  The caller still holds every ancestor and the
        source parent without delete sharing and validates their identities
        immediately before this call, so path resolution cannot redirect the
        operation through a swapped ancestor.
        """

        if not parent_handle:
            raise ValueError("Windows quarantine rename requires a held parent")
        if name in ("", ".", "..") or "\\" in name or "/" in name or "\0" in name:
            raise ValueError("Windows quarantine rename requires a simple filename")
        raw_parent = Path(parent_path)
        if not raw_parent.is_absolute():
            raise ValueError("Windows quarantine rename requires an absolute parent")
        destination = _windows_absolute_path(raw_parent / name)
        encoded_name = destination.encode("utf-16-le")
        file_name_offset = _WindowsFileRenameInfo.FileName.offset
        required_size = file_name_offset + len(encoded_name) + 2
        alignment = ctypes.alignment(_WindowsFileRenameInfo)
        buffer_size = (required_size + alignment - 1) // alignment * alignment
        storage = ctypes.create_string_buffer(buffer_size + alignment - 1)
        storage_address = ctypes.addressof(storage)
        aligned_address = (storage_address + alignment - 1) & ~(alignment - 1)
        buffer = (ctypes.c_ubyte * buffer_size).from_address(aligned_address)
        information = ctypes.cast(
            buffer, ctypes.POINTER(_WindowsFileRenameInfo)
        ).contents
        # FileRenameInfo (class 3) interprets the union as ReplaceIfExists;
        # zero means do not replace a colliding destination.  FileRenameInfoEx
        # would interpret the same four bytes as Flags, so the ABI is shared.
        information.Flags = 0
        # The destination is absolute and already bound to the held parent;
        # NULL is the documented form for an absolute FileName.  A bare name
        # with NULL would resolve through the process current directory and
        # can select a different volume.
        information.RootDirectory = None
        information.FileNameLength = len(encoded_name)
        ctypes.memmove(
            ctypes.addressof(buffer) + file_name_offset,
            encoded_name,
            len(encoded_name),
        )
        # create_string_buffer zero-initializes the explicit UTF-16 NUL and
        # any trailing alignment padding. FileNameLength excludes the NUL.
        if not self._set_file_information(
            wintypes.HANDLE(handle),
            _WINDOWS_FILE_RENAME_INFO_CLASS,
            ctypes.cast(buffer, wintypes.LPVOID),
            buffer_size,
        ):
            raise self._last_error(Path(destination))

    def mark_delete(self, handle: int) -> None:
        """Mark an open file or empty directory for deletion on close."""

        information = _WindowsFileDispositionInfo(DeleteFile=wintypes.BOOLEAN(True))
        if not self._set_file_information(
            wintypes.HANDLE(handle),
            _WINDOWS_FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise self._last_error(Path("<native-handle>"))


def _close_windows_handle(
    api: _WindowsApi,
    record: _WindowsHandleRecord,
) -> _WindowsCloseReport:
    """Close once, then retry only after revalidating the handle identity."""

    try:
        api.close(record.handle)
        return _WindowsCloseReport()
    except OSError as first_error:
        report = _WindowsCloseReport(errors=[first_error])

    # A record created immediately after CreateFileW may not have a native
    # identity yet because the first identity query failed.  The handle is
    # still exclusively owned by this cleanup transaction: revalidate it now,
    # then permit the same single bounded retry.  If revalidation fails, keep
    # the numeric handle owned and report it rather than guessing.
    try:
        current_identity = api.identity(record.handle)
    except OSError as identity_error:
        report.errors.append(identity_error)
        report.unclosed.append(record)
        return report
    if record.identity is not None and current_identity != record.identity:
        report.errors.append(
            OSError(
                errno.EIO,
                f"handle {record.handle} identity changed before close retry",
            )
        )
        report.unclosed.append(record)
        return report

    try:
        api.close(record.handle)
    except OSError as retry_error:
        report.errors.append(retry_error)
        report.unclosed.append(record)
    else:
        return _WindowsCloseReport()
    return report


_WINDOWS_API: Optional[_WindowsApi] = None
# Test-only seam.  Production callers leave this unset; tests can install a
# disposable-fixture callback to exercise the exact validation/mutation race.
_BEFORE_WINDOWS_QUARANTINE_MUTATION: Optional[Callable[[Path], None]] = None
# Test-only seam for deterministic POSIX rename/substitution races. Production
# leaves this unset. Every invocation is followed by identity revalidation
# before the descriptor-relative mutation.
_BEFORE_POSIX_MUTATION: Optional[Callable[[Path], None]] = None


def _get_windows_api(*, operation: str, path: Path) -> _WindowsApi:
    """Load the native API or fail closed on an actual Windows host."""

    global _WINDOWS_API
    if not _REAL_WINDOWS:
        raise _security_error(
            operation=operation,
            path=path,
            reason="native Windows cleanup is unavailable on this host",
        )
    if _WINDOWS_API is None:
        try:
            _WINDOWS_API = _WindowsApi()
        except (AttributeError, OSError, RuntimeError) as error:
            diagnostic = _diagnostic(
                operation=operation,
                path=path,
                attempts=0,
                error=error,
                reason="native handle-relative Windows cleanup is unavailable",
            )
            raise PackagingCleanupError(diagnostic) from error
    return _WINDOWS_API


@dataclass
class _WindowsBindingState:
    """Native handles bound to the original parent and target objects."""

    api: _WindowsApi
    ancestor_handles: tuple[tuple[Path, int, _WindowsFileIdentity], ...]
    target_handle: Optional[int]
    target_identity: Optional[_WindowsFileIdentity]
    target_is_directory: bool
    deletion_marked: bool = False
    recursive_close_errors: list[OSError] = field(default_factory=list)
    recursive_unclosed: list[_WindowsHandleRecord] = field(default_factory=list)

    @property
    def parent_handle(self) -> int:
        """Return the handle for the target's originally bound parent."""

        return self.ancestor_handles[-1][1]

    @property
    def parent_identity(self) -> _WindowsFileIdentity:
        """Return the identity for the target's originally bound parent."""

        return self.ancestor_handles[-1][2]

    def assert_current(
        self,
        *,
        operation: str,
        path: Path,
        attempts: int,
        target_path: Optional[Path] = None,
    ) -> None:
        """Reject native handle identity or reparse changes before mutation."""

        try:
            for ancestor_path, handle, expected in self.ancestor_handles:
                ancestor_identity = self.api.identity(handle)
                if ancestor_identity != expected:
                    raise _security_error(
                        operation=operation,
                        path=path,
                        reason=(
                            "bound Windows ancestor handle identity changed: "
                            f"{ancestor_path}"
                        ),
                        attempts=attempts,
                    )
                if (
                    not ancestor_identity.file_attributes
                    & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
                ):
                    raise _security_error(
                        operation=operation,
                        path=path,
                        reason=f"bound Windows ancestor is no longer a directory: {ancestor_path}",
                        attempts=attempts,
                    )
                if ancestor_identity.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                    raise _security_error(
                        operation=operation,
                        path=path,
                        reason=(
                            "bound Windows ancestor became a reparse point: "
                            f"{ancestor_path}"
                        ),
                        attempts=attempts,
                    )
                pathname_identity = self.api.path_identity(
                    ancestor_path,
                    directory=True,
                )
                if pathname_identity != expected:
                    raise _security_error(
                        operation=operation,
                        path=path,
                        reason=(
                            "bound Windows ancestor pathname identity changed: "
                            f"{ancestor_path}"
                        ),
                        attempts=attempts,
                    )
            if self.target_handle is not None:
                target_identity = self.api.identity(self.target_handle)
                if target_identity != self.target_identity:
                    raise _security_error(
                        operation=operation,
                        path=path,
                        reason="bound Windows target handle identity changed",
                        attempts=attempts,
                    )
                if self.target_is_directory and not (
                    target_identity.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
                ):
                    raise _security_error(
                        operation=operation,
                        path=path,
                        reason="bound Windows target is no longer a directory",
                        attempts=attempts,
                    )
                if not self.target_is_directory and (
                    target_identity.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
                ):
                    raise _security_error(
                        operation=operation,
                        path=path,
                        reason="bound Windows target changed from a file to a directory",
                        attempts=attempts,
                    )
                if target_identity.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                    raise _security_error(
                        operation=operation,
                        path=path,
                        reason="bound Windows target became a reparse point",
                        attempts=attempts,
                    )
                current_target_path = target_path or path
                pathname_identity = self.api.path_identity(
                    current_target_path,
                    directory=self.target_is_directory,
                )
                if pathname_identity != self.target_identity:
                    raise _security_error(
                        operation=operation,
                        path=path,
                        reason="bound Windows target pathname identity changed",
                        attempts=attempts,
                    )
        except PackagingCleanupError:
            raise
        except OSError as error:
            diagnostic = _diagnostic(
                operation=operation,
                path=path,
                attempts=attempts,
                error=error,
                reason="could not revalidate bound Windows handles",
            )
            raise PackagingCleanupError(diagnostic) from error

    def close(self) -> _WindowsCloseReport:
        """Attempt every bound close and retain persistent failures."""

        report = _WindowsCloseReport(
            errors=list(self.recursive_close_errors),
            unclosed=list(self.recursive_unclosed),
        )
        if self.target_handle is not None:
            target_record = _WindowsHandleRecord(
                path=Path("<bound-target>"),
                handle=self.target_handle,
                identity=self.target_identity,
            )
            target_report = _close_windows_handle(self.api, target_record)
            report.errors.extend(target_report.errors)
            report.unclosed.extend(target_report.unclosed)
            self.target_handle = (
                target_record.handle if target_report.unclosed else None
            )
        remaining_ancestors: list[tuple[Path, int, _WindowsFileIdentity]] = []
        for ancestor_path, handle, identity in reversed(self.ancestor_handles):
            ancestor_record = _WindowsHandleRecord(
                path=ancestor_path,
                handle=handle,
                identity=identity,
            )
            ancestor_report = _close_windows_handle(self.api, ancestor_record)
            report.errors.extend(ancestor_report.errors)
            report.unclosed.extend(ancestor_report.unclosed)
            if ancestor_report.unclosed:
                remaining_ancestors.append((ancestor_path, handle, identity))
        self.ancestor_handles = tuple(reversed(remaining_ancestors))
        return report


@dataclass(frozen=True)
class _PathIdentity:
    """Identity of one inspected component in an owned path chain."""

    path: Path
    exists: bool
    signature: tuple[Optional[int], Optional[int], int, Optional[int]]


@dataclass
class _PathBinding:
    """Validated path identities and any no-follow directory handles."""

    target: Path
    owner: Path
    identities: tuple[_PathIdentity, ...]
    directory_fds: tuple[int, ...] = ()
    posix_mount_identity: Optional[_PosixMountIdentity] = None
    windows_state: Optional[_WindowsBindingState] = None
    quarantine_path: Optional[Path] = None
    quarantine_signature: Optional[
        tuple[Optional[int], Optional[int], int, Optional[int]]
    ] = None

    @property
    def parent_fd(self) -> Optional[int]:
        """Return the held descriptor for the target's parent, if available."""

        return self.directory_fds[-1] if self.directory_fds else None

    def assert_current(self, *, operation: str, attempts: int) -> None:
        """Reject any component replacement since this binding was captured."""

        current = _capture_path_identities(
            self.target,
            self.owner,
            operation=operation,
            attempts=attempts,
        )
        if self.quarantine_path is None:
            identity_changed = current != self.identities
        else:
            identity_changed = (
                current[:-1] != self.identities[:-1]
                or current[-1].exists
                or self.quarantine_signature
                != _inspect_existing_identity(self.quarantine_path)
            )
        if identity_changed:
            raise PackagingCleanupError(
                _diagnostic(
                    operation=operation,
                    path=self.target,
                    attempts=attempts,
                    reason=("owned scope or path identity changed; cleanup refused"),
                )
            )
        if self.windows_state is not None:
            self.windows_state.assert_current(
                operation=operation,
                path=self.target,
                attempts=attempts,
                target_path=self.quarantine_path,
            )

    def bind_quarantine(self, path: Path) -> None:
        """Bind a successful same-scope quarantine rename to its identity."""

        self.quarantine_path = path
        self.quarantine_signature = _inspect_existing_identity(path)

    def close(self) -> _WindowsCloseReport:
        """Close every held descriptor and retain all closure failures."""

        report = _WindowsCloseReport()
        if self.windows_state is not None:
            windows_report = self.windows_state.close()
            report.errors.extend(windows_report.errors)
            report.unclosed.extend(windows_report.unclosed)
        for descriptor in reversed(self.directory_fds):
            try:
                os.close(descriptor)
            except OSError as error:
                report.errors.append(error)
        return report


@dataclass(frozen=True)
class CleanupDiagnostic:
    """Structured information about a refused or failed cleanup."""

    operation: str
    path: Path
    attempts: int
    error_type: Optional[str]
    error_message: Optional[str]
    errno: Optional[int]
    winerror: Optional[int]
    transient: bool
    exhausted: bool
    child_alive: bool = False
    reason: Optional[str] = None

    def format_message(self) -> str:
        """Return a concise diagnostic suitable for CI logs."""

        details = [
            f"{self.operation} failed for owned path {self.path}",
            f"attempts={self.attempts}",
        ]
        if self.reason:
            details.append(f"reason={self.reason}")
        if self.error_type:
            details.append(f"error={self.error_type}")
        if self.winerror is not None:
            details.append(f"winerror={self.winerror}")
        if self.errno is not None:
            details.append(f"errno={self.errno}")
        if self.error_message:
            details.append(self.error_message)
        return "; ".join(details)


class PackagingCleanupError(RuntimeError):
    """Raised when owned packaging output cannot be safely cleaned."""

    cleanup_close_failures: tuple[OSError, ...]
    cleanup_unclosed_windows: tuple[_WindowsHandleRecord, ...]

    def __init__(self, diagnostic: CleanupDiagnostic) -> None:
        self.diagnostic = diagnostic
        self.close_failures: tuple[OSError, ...] = ()
        self.unclosed_windows: tuple[_WindowsHandleRecord, ...] = ()
        self.cleanup_close_failures = ()
        self.cleanup_unclosed_windows = ()
        super().__init__(diagnostic.format_message())

    def record_close_failures(
        self,
        errors: Sequence[OSError],
        unclosed: Sequence[_WindowsHandleRecord] = (),
    ) -> str:
        """Expose close failures without replacing this primary exception."""

        self.close_failures += tuple(errors)
        self.unclosed_windows += tuple(unclosed)
        self._set_close_failure_attributes()
        unclosed_text = (
            f"; retained handles={[record.handle for record in unclosed]}"
            if unclosed
            else ""
        )
        message = (
            "; ".join(
                [
                    f"failed to close {len(errors)} cleanup handle(s)",
                    *(f"close error: {error}" for error in errors),
                ]
            )
            + unclosed_text
        )
        self.args = (f"{self.diagnostic.format_message()}; {message}",)
        return message

    def _set_close_failure_attributes(self) -> None:
        """Expose close metadata through the common wrapper attributes."""

        self.cleanup_close_failures = self.close_failures
        self.cleanup_unclosed_windows = self.unclosed_windows

    @property
    def path(self) -> Path:
        """Return the path whose cleanup was refused or failed."""

        return self.diagnostic.path

    @property
    def attempts(self) -> int:
        """Return the number of removal attempts made."""

        return self.diagnostic.attempts


def _close_failure_error(
    *,
    operation: str,
    path: Path,
    attempts: int,
    errors: Sequence[OSError],
    unclosed: Sequence[_WindowsHandleRecord] = (),
) -> PackagingCleanupError:
    """Build a failure for one or more cleanup-handle close errors."""

    first = errors[0]
    retained_text = (
        f"; retained handles={[record.handle for record in unclosed]}"
        if unclosed
        else ""
    )
    result = PackagingCleanupError(
        _diagnostic(
            operation=operation,
            path=path,
            attempts=attempts,
            error=first,
            reason=(f"failed to close {len(errors)} cleanup handle(s){retained_text}"),
        )
    )
    result.close_failures = tuple(errors)
    result.unclosed_windows = tuple(unclosed)
    result._set_close_failure_attributes()
    return result


def _note_close_failures(
    primary: BaseException,
    *,
    operation: str,
    path: Path,
    attempts: int,
    errors: Sequence[OSError],
    unclosed: Sequence[_WindowsHandleRecord] = (),
) -> None:
    """Preserve a primary exception while exposing every close failure."""

    close_error = _close_failure_error(
        operation=operation,
        path=path,
        attempts=attempts,
        errors=errors,
        unclosed=unclosed,
    )
    if isinstance(primary, PackagingCleanupError):
        message = primary.record_close_failures(errors, unclosed)
    else:
        message = close_error.diagnostic.format_message()
        try:
            previous_errors = getattr(primary, "cleanup_close_failures", ())
            previous_unclosed = getattr(primary, "cleanup_unclosed_windows", ())
            setattr(
                primary,
                "cleanup_close_failures",
                tuple(previous_errors) + tuple(errors),
            )
            setattr(
                primary,
                "cleanup_unclosed_windows",
                tuple(previous_unclosed) + tuple(unclosed),
            )
        except Exception:
            # The primary exception type may not allow custom attributes, but
            # retaining its identity and cause is still safer than replacing
            # it with a close diagnostic.
            pass
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        add_note(message)


def _finish_binding_close(
    binding: _PathBinding,
    *,
    operation: str,
    path: Path,
    attempts: int,
    primary: Optional[BaseException],
) -> None:
    """Report close failures without replacing an earlier cleanup error."""

    report = binding.close()
    if not report.errors:
        return
    if primary is not None:
        _note_close_failures(
            primary,
            operation=operation,
            path=path,
            attempts=attempts,
            errors=report.errors,
            unclosed=report.unclosed,
        )
        return
    raise _close_failure_error(
        operation=operation,
        path=path,
        attempts=attempts,
        errors=report.errors,
        unclosed=report.unclosed,
    )


def _diagnostic(
    *,
    operation: str,
    path: Path,
    attempts: int,
    error: Optional[BaseException] = None,
    transient: bool = False,
    exhausted: bool = False,
    child_alive: bool = False,
    reason: Optional[str] = None,
) -> CleanupDiagnostic:
    """Build a stable diagnostic without relying on platform-specific text."""

    return CleanupDiagnostic(
        operation=operation,
        path=path,
        attempts=attempts,
        error_type=type(error).__name__ if error else None,
        error_message=str(error) if error else None,
        errno=getattr(error, "errno", None) if error else None,
        winerror=getattr(error, "winerror", None) if error else None,
        transient=transient,
        exhausted=exhausted,
        child_alive=child_alive,
        reason=reason,
    )


def is_transient_windows_cleanup_error(
    error: OSError, *, platform_name: Optional[str] = None
) -> bool:
    """Return whether ``error`` is a recognized Windows lock race.

    The optional platform argument exists for deterministic tests.  On a real
    non-Windows host, access-denied errors are not retried because their cause
    and semantics differ from Windows sharing violations.
    """

    is_windows = _IS_WINDOWS if platform_name is None else platform_name.lower() == "nt"
    if not is_windows:
        return False
    return bool(
        getattr(error, "winerror", None) in _TRANSIENT_WINDOWS_WINERRORS
        or getattr(error, "errno", None) in _TRANSIENT_WINDOWS_ERRNOS
    )


def _absolute_lexical_path(path: Path) -> Path:
    """Normalize ``..`` without resolving symlinks or reparse points."""

    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _windows_absolute_path(path: Path) -> str:
    """Return an absolute Windows rename path without resolving links."""

    lexical = _absolute_lexical_path(path)
    value = os.fspath(lexical)
    if not os.path.isabs(value):
        raise ValueError("Windows rename destination must be absolute")
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        value = "\\\\?\\UNC\\" + value[2:]
    elif len(value) >= 248:
        value = "\\\\?\\" + value
    return value


def _lstat_no_follow(path: Path) -> os.stat_result:
    """Inspect one path component without following its final link."""

    return os.lstat(path)


def _is_reparse_point(path: Path, result: os.stat_result) -> bool:
    """Return whether a component is a Windows reparse point or junction."""

    attributes = getattr(result, "st_file_attributes", 0) or 0
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    junction_checker = getattr(path, "is_junction", None)
    if callable(junction_checker):
        try:
            return bool(junction_checker())
        except OSError:
            # An uncertain junction check is unsafe for deletion.
            return True
    return False


def _security_error(
    *,
    operation: str,
    path: Path,
    reason: str,
    attempts: int = 0,
) -> PackagingCleanupError:
    """Create a typed fail-closed path-security error."""

    return PackagingCleanupError(
        _diagnostic(
            operation=operation,
            path=path,
            attempts=attempts,
            reason=reason,
        )
    )


def _identity_signature(
    result: os.stat_result,
) -> tuple[Optional[int], Optional[int], int, Optional[int]]:
    """Extract stable identity fields without depending on platform text."""

    return (
        getattr(result, "st_dev", None),
        getattr(result, "st_ino", None),
        stat.S_IFMT(result.st_mode),
        getattr(result, "st_file_attributes", None),
    )


def _posix_owner(result: os.stat_result) -> Optional[int]:
    """Return the numeric owner of a POSIX object when the platform exposes it."""

    return getattr(result, "st_uid", None)


def _parse_linux_mount_id(lines: Sequence[str]) -> int:
    """Parse exactly one valid ``mnt_id`` field from Linux fdinfo."""

    mount_ids: list[int] = []
    for line in lines:
        key, separator, value = line.partition(":")
        if key.strip() != "mnt_id" or not separator:
            continue
        parsed = value.strip()
        if not parsed.isdigit():
            raise ValueError("fdinfo mnt_id is not decimal")
        mount_ids.append(int(parsed))
    if len(mount_ids) != 1 or mount_ids[0] <= 0:
        raise ValueError("fdinfo mnt_id is missing or ambiguous")
    return mount_ids[0]


def _linux_fdinfo_mount_identity(fd: int) -> _PosixMountIdentity:
    """Read fdinfo through an identity-checked CLOEXEC duplicate."""

    duplicate: Optional[int] = None
    result: Optional[_PosixMountIdentity] = None
    primary_error: Optional[BaseException] = None
    close_error: Optional[OSError] = None
    try:
        duplicate = os.dup(fd)
        if duplicate == fd:
            raise OSError(errno.EIO, "fd duplication reused the original descriptor")
        os.set_inheritable(duplicate, False)
        original_before = os.fstat(fd)
        duplicate_before = os.fstat(duplicate)
        if _posix_identity(original_before) != _posix_identity(duplicate_before):
            raise OSError(errno.EIO, "POSIX mount identity duplicate mismatch")

        with open(
            f"/proc/self/fdinfo/{duplicate}",
            "r",
            encoding="ascii",
        ) as fdinfo:
            mount_id = _parse_linux_mount_id(list(fdinfo))

        duplicate_after = os.fstat(duplicate)
        original_after = os.fstat(fd)
        if _posix_identity(duplicate_before) != _posix_identity(duplicate_after):
            raise OSError(errno.EIO, "duplicated POSIX FD identity changed")
        if _posix_identity(original_before) != _posix_identity(original_after):
            raise OSError(errno.EIO, "original POSIX FD identity changed")
        if _posix_identity(original_after) != _posix_identity(duplicate_after):
            raise OSError(errno.EIO, "POSIX FD identities diverged after fdinfo read")
        result = ("linux-mnt-id", mount_id)
    except BaseException as error:
        primary_error = error
    finally:
        if duplicate is not None:
            try:
                os.close(duplicate)
            except OSError as error:
                close_error = error

    if close_error is not None:
        if primary_error is not None:
            raise OSError(
                getattr(errno, "ENOTSUP", errno.EINVAL),
                f"could not close fdinfo duplicate {duplicate}: {close_error}",
            ) from primary_error
        raise close_error
    if primary_error is not None:
        raise primary_error
    if result is None:
        raise OSError(errno.EIO, "Linux fdinfo mount identity was not produced")
    return result


def _posix_mount_identity(fd: int) -> _PosixMountIdentity:
    """Return a kernel-backed mount identity for an open POSIX descriptor.

    Linux ``st_dev`` is not sufficient for bind mounts: a bind mount can
    retain the same device number while changing the mount instance.  Linux
    procfs exposes the mount ID for the descriptor itself, so use that rather
    than resolving a pathname.  On Darwin and other POSIX systems, the
    descriptor's filesystem ID is the available volume/mount identity.  An
    unavailable or malformed identity is a security failure; callers must
    never fall back to pathname or ``st_dev`` alone.
    """

    if sys.platform == "linux":
        try:
            return _linux_fdinfo_mount_identity(fd)
        except (OSError, UnicodeError, ValueError) as error:
            raise OSError(
                getattr(errno, "ENOTSUP", errno.EINVAL),
                f"could not obtain Linux fd mount identity for {fd}",
            ) from error

    try:
        filesystem = os.fstatvfs(fd)
        fsid = getattr(filesystem, "f_fsid", None)
        if fsid is None or int(fsid) <= 0:
            raise ValueError("descriptor filesystem identity is unavailable")
        return ("posix-fsid", int(fsid))
    except (OSError, TypeError, ValueError) as error:
        raise OSError(
            getattr(errno, "ENOTSUP", errno.EINVAL),
            f"could not obtain POSIX fd mount identity for {fd}",
        ) from error


def _assert_posix_mount_identity(
    fd: int,
    expected: _PosixMountIdentity,
    *,
    path: Path,
    operation: str,
    attempts: int,
) -> None:
    """Require an opened descriptor to remain on the bound mount."""

    try:
        actual = _posix_mount_identity(fd)
    except OSError as error:
        diagnostic = _diagnostic(
            operation=operation,
            path=path,
            attempts=attempts,
            error=error,
            reason="could not obtain descriptor mount identity",
        )
        raise PackagingCleanupError(diagnostic) from error
    if actual != expected:
        raise _security_error(
            operation=operation,
            path=path,
            reason=(
                "descriptor crossed a mount boundary: "
                f"expected {expected[0]}={expected[1]}, "
                f"got {actual[0]}={actual[1]}"
            ),
            attempts=attempts,
        )


def _inspect_existing_identity(
    path: Path,
    *,
    operation: str = "validate packaging quarantine",
    attempts: int = 0,
) -> Optional[tuple[Optional[int], Optional[int], int, Optional[int]]]:
    """Inspect a bound quarantine path without following its final component."""

    try:
        result = _lstat_no_follow(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(result.st_mode) or _is_reparse_point(path, result):
        raise _security_error(
            operation=operation,
            path=path,
            reason="quarantine path became a symlink or reparse point",
            attempts=attempts,
        )
    return _identity_signature(result)


def _capture_path_identities(
    target: Path,
    owner: Path,
    *,
    operation: str,
    attempts: int,
) -> tuple[_PathIdentity, ...]:
    """Inspect every owner-to-target component without following links."""

    try:
        relative_parts = target.relative_to(owner).parts
    except ValueError as error:
        raise _security_error(
            operation=operation,
            path=target,
            reason=f"path is outside owned scope {owner}",
            attempts=attempts,
        ) from error
    if not relative_parts:
        raise _security_error(
            operation=operation,
            path=target,
            reason="scope root itself is not removable",
            attempts=attempts,
        )

    components = [owner]
    current = owner
    for part in relative_parts:
        current /= part
        components.append(current)

    identities: list[_PathIdentity] = []
    root_device: Optional[int] = None
    last_index = len(components) - 1
    for index, component in enumerate(components):
        try:
            result = _lstat_no_follow(component)
        except FileNotFoundError as error:
            if index == last_index:
                identities.append(
                    _PathIdentity(
                        component,
                        False,
                        (None, None, 0, None),
                    )
                )
                break
            raise _security_error(
                operation=operation,
                path=target,
                reason=f"owned path ancestor disappeared: {component}",
                attempts=attempts,
            ) from error
        except OSError as error:
            diagnostic = _diagnostic(
                operation=operation,
                path=target,
                attempts=attempts,
                error=error,
                reason=f"could not inspect owned path component: {component}",
            )
            raise PackagingCleanupError(diagnostic) from error

        if stat.S_ISLNK(result.st_mode) or _is_reparse_point(component, result):
            raise _security_error(
                operation=operation,
                path=target,
                reason=f"symlink or reparse component is forbidden: {component}",
                attempts=attempts,
            )
        if index == 0:
            if not stat.S_ISDIR(result.st_mode):
                raise _security_error(
                    operation=operation,
                    path=target,
                    reason=f"owned scope root is not a directory: {component}",
                    attempts=attempts,
                )
            root_device = getattr(result, "st_dev", None)
        elif index < last_index and not stat.S_ISDIR(result.st_mode):
            raise _security_error(
                operation=operation,
                path=target,
                reason=f"owned path ancestor is not a directory: {component}",
                attempts=attempts,
            )

        component_device = getattr(result, "st_dev", None)
        if (
            root_device is not None
            and component_device is not None
            and component_device != root_device
        ):
            raise _security_error(
                operation=operation,
                path=target,
                reason=f"mount/device substitution is forbidden: {component}",
                attempts=attempts,
            )
        identities.append(
            _PathIdentity(
                component,
                True,
                _identity_signature(result),
            )
        )
    return tuple(identities)


def _open_parent_directories(
    target: Path,
    owner: Path,
    *,
    operation: str,
) -> tuple[int, ...]:
    """Hold POSIX no-follow directory handles through the removal attempt."""

    if _IS_WINDOWS:
        return ()
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if not all(hasattr(os, flag) for flag in required_flags):
        return ()
    relative_parts = target.relative_to(owner).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    try:
        current = os.open(owner, flags)
        descriptors.append(current)
        for part in relative_parts[:-1]:
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
    except (NotImplementedError, TypeError, ValueError):
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        return ()
    except OSError as error:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        if error.errno in _NOFOLLOW_UNSUPPORTED_ERRNOS:
            return ()
        diagnostic = _diagnostic(
            operation=operation,
            path=target,
            attempts=0,
            error=error,
            reason="could not open owned path without following links",
        )
        raise PackagingCleanupError(diagnostic) from error
    return tuple(descriptors)


def _assert_native_identity_matches_path(
    native_identity: _WindowsFileIdentity,
    expected: _PathIdentity,
    *,
    directory: bool,
    operation: str,
    path: Path,
) -> None:
    """Bind an open Windows object to its already-lstatted path identity."""

    expected_inode = expected.signature[1]
    if expected_inode in (None, 0) or native_identity.file_index != expected_inode:
        raise _security_error(
            operation=operation,
            path=path,
            reason=(
                "Windows handle identity was unavailable or did not match "
                f"the validated path: {path}"
            ),
        )
    is_directory = bool(
        native_identity.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
    )
    if is_directory != directory:
        raise _security_error(
            operation=operation,
            path=path,
            reason=f"Windows handle type did not match validated path: {path}",
        )
    if native_identity.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise _security_error(
            operation=operation,
            path=path,
            reason=f"Windows handle opened a reparse point: {path}",
        )


def _close_windows_handles(
    api: _WindowsApi,
    target_handle: Optional[int],
    ancestor_handles: Sequence[tuple[Path, int, _WindowsFileIdentity]],
    target_identity: Optional[_WindowsFileIdentity] = None,
) -> _WindowsCloseReport:
    """Close every partial binding and retain persistent failures."""

    records: list[_WindowsHandleRecord] = []
    if target_handle is not None:
        records.append(
            _WindowsHandleRecord(
                path=Path("<partial-target>"),
                handle=target_handle,
                identity=target_identity,
            )
        )
    records.extend(
        _WindowsHandleRecord(path, handle, identity)
        for path, handle, identity in reversed(ancestor_handles)
    )
    return _close_windows_records(api, records)


def _close_windows_records(
    api: _WindowsApi,
    records: Sequence[_WindowsHandleRecord],
) -> _WindowsCloseReport:
    """Close an active ownership set in reverse-open order."""

    report = _WindowsCloseReport()
    for record in records:
        close_report = _close_windows_handle(api, record)
        report.errors.extend(close_report.errors)
        report.unclosed.extend(close_report.unclosed)
    return report


def _bind_windows_handles(
    target: Path,
    identities: tuple[_PathIdentity, ...],
    *,
    operation: str,
) -> _WindowsBindingState:
    """Hold read/write-only, no-reparse handles for the verified chain.

    ``CreateFileW`` does not provide an ordinary Python ``openat`` equivalent.
    Each component is therefore opened with ``OPEN_REPARSE_POINT`` and matched
    to the pre-open ``lstat`` identity, followed by an immediate full-chain
    recapture.  Every held handle excludes ``FILE_SHARE_DELETE`` so a
    competing delete/rename cannot substitute a path component while the
    chain is live.  Mutation itself is still handle-relative through
    ``SetFileInformationByHandle``; an identity mismatch fails closed rather
    than falling back to a pathname-based delete.
    """

    api = _get_windows_api(operation=operation, path=target)
    ancestor_handles: list[tuple[Path, int, _WindowsFileIdentity]] = []
    owned_handles: list[_WindowsHandleRecord] = []
    target_handle: Optional[int] = None
    target_identity: Optional[_WindowsFileIdentity] = None
    try:
        for expected in identities[:-1]:
            if not expected.exists:
                raise _security_error(
                    operation=operation,
                    path=target,
                    reason=f"validated Windows ancestor disappeared: {expected.path}",
                )
            handle = api.open(
                expected.path,
                directory=True,
                share_mode=_WINDOWS_HANDLE_SHARE_MODE,
            )
            owned_handles.append(_WindowsHandleRecord(expected.path, handle, None))
            native_identity = api.identity(handle)
            owned_handles[-1] = _WindowsHandleRecord(
                expected.path,
                handle,
                native_identity,
            )
            _assert_native_identity_matches_path(
                native_identity,
                expected,
                directory=True,
                operation=operation,
                path=expected.path,
            )
            ancestor_handles.append((expected.path, handle, native_identity))

        expected_target = identities[-1]
        target_is_directory = bool(
            expected_target.exists and stat.S_ISDIR(expected_target.signature[2])
        )
        if expected_target.exists:
            target_handle = api.open(
                target,
                directory=target_is_directory,
                share_mode=_WINDOWS_HANDLE_SHARE_MODE,
            )
            owned_handles.append(_WindowsHandleRecord(target, target_handle, None))
            target_identity = api.identity(target_handle)
            owned_handles[-1] = _WindowsHandleRecord(
                target,
                target_handle,
                target_identity,
            )
            _assert_native_identity_matches_path(
                target_identity,
                expected_target,
                directory=target_is_directory,
                operation=operation,
                path=target,
            )

        state = _WindowsBindingState(
            api=api,
            ancestor_handles=tuple(ancestor_handles),
            target_handle=target_handle,
            target_identity=target_identity,
            target_is_directory=target_is_directory,
        )
        current = _capture_path_identities(
            target,
            identities[0].path,
            operation=operation,
            attempts=0,
        )
        if current != identities:
            raise _security_error(
                operation=operation,
                path=target,
                reason=("owned path identity changed while binding Windows handles"),
            )
        state.assert_current(operation=operation, path=target, attempts=0)
        return state
    except BaseException as error:
        close_report = _close_windows_records(api, list(reversed(owned_handles)))
        if close_report.errors:
            _note_close_failures(
                error,
                operation=operation,
                path=target,
                attempts=0,
                errors=close_report.errors,
                unclosed=close_report.unclosed,
            )
        raise


def _bind_owned_path(
    path: Path,
    owner_root: Path,
    *,
    operation: str,
) -> _PathBinding:
    """Capture the path chain and bind no-follow parent handles where possible."""

    target = _absolute_lexical_path(path)
    owner = _absolute_lexical_path(owner_root)
    try:
        if target == owner or not target.is_relative_to(owner):
            raise _security_error(
                operation=operation,
                path=target,
                reason=f"path is outside owned scope {owner}",
            )
    except AttributeError as error:
        raise _security_error(
            operation=operation,
            path=target,
            reason="path containment check is unavailable on this interpreter",
        ) from error
    identities = _capture_path_identities(
        target,
        owner,
        operation=operation,
        attempts=0,
    )
    descriptors = _open_parent_directories(
        target,
        owner,
        operation=operation,
    )
    posix_mount_identity: Optional[_PosixMountIdentity] = None
    windows_state: Optional[_WindowsBindingState] = None
    try:
        if not _IS_WINDOWS:
            if not descriptors:
                raise _security_error(
                    operation=operation,
                    path=target,
                    reason=(
                        "descriptor-relative POSIX cleanup requires a held "
                        "mount identity; pathname fallback is forbidden"
                    ),
                )
            try:
                posix_mount_identity = _posix_mount_identity(descriptors[0])
            except OSError as error:
                diagnostic = _diagnostic(
                    operation=operation,
                    path=target,
                    attempts=0,
                    error=error,
                    reason="could not obtain owned root mount identity",
                )
                raise PackagingCleanupError(diagnostic) from error
            for descriptor in descriptors[1:]:
                _assert_posix_mount_identity(
                    descriptor,
                    posix_mount_identity,
                    path=target,
                    operation=operation,
                    attempts=0,
                )
        if _REAL_WINDOWS:
            windows_state = _bind_windows_handles(
                target,
                identities,
                operation=operation,
            )
        binding = _PathBinding(
            target=target,
            owner=owner,
            identities=identities,
            directory_fds=descriptors,
            posix_mount_identity=posix_mount_identity,
            windows_state=windows_state,
        )
        binding.assert_current(operation=operation, attempts=0)
    except BaseException as error:
        close_report = _WindowsCloseReport()
        if windows_state is not None:
            windows_report = windows_state.close()
            close_report.errors.extend(windows_report.errors)
            close_report.unclosed.extend(windows_report.unclosed)
        else:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError as close_error:
                    close_report.errors.append(close_error)
        if close_report.errors:
            _note_close_failures(
                error,
                operation=operation,
                path=target,
                attempts=0,
                errors=close_report.errors,
                unclosed=close_report.unclosed,
            )
        raise
    return binding


def _assert_tree_has_no_reparse_points(
    root: Path,
    *,
    operation: str,
    root_device: Optional[int],
) -> None:
    """Inspect a quarantine tree without following links or mount points."""

    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as error:
            diagnostic = _diagnostic(
                operation=operation,
                path=current,
                attempts=0,
                error=error,
                reason="could not inspect quarantined packaging output",
            )
            raise PackagingCleanupError(diagnostic) from error
        for entry in entries:
            child = Path(entry.path)
            try:
                result = _lstat_no_follow(child)
            except OSError as error:
                diagnostic = _diagnostic(
                    operation=operation,
                    path=child,
                    attempts=0,
                    error=error,
                    reason="quarantined packaging entry changed during inspection",
                )
                raise PackagingCleanupError(diagnostic) from error
            if stat.S_ISLNK(result.st_mode) or _is_reparse_point(child, result):
                raise _security_error(
                    operation=operation,
                    path=child,
                    reason="quarantined tree contains a symlink or reparse point",
                )
            child_device = getattr(result, "st_dev", None)
            if (
                root_device is not None
                and child_device is not None
                and child_device != root_device
            ):
                raise _security_error(
                    operation=operation,
                    path=child,
                    reason="quarantined tree crosses a mount/device boundary",
                )
            if stat.S_ISDIR(result.st_mode):
                pending.append(child)


def _new_quarantine_path(path: Path, *, operation: str) -> Path:
    """Choose a fresh sibling quarantine name without following links."""

    for _ in range(8):
        candidate = path.parent / f".tobkiri-cleanup-{uuid.uuid4().hex}"
        try:
            _lstat_no_follow(candidate)
        except FileNotFoundError:
            return candidate
        except OSError as error:
            diagnostic = _diagnostic(
                operation=operation,
                path=candidate,
                attempts=0,
                error=error,
                reason="could not inspect quarantine destination",
            )
            raise PackagingCleanupError(diagnostic) from error
    raise _security_error(
        operation=operation,
        path=path,
        reason="could not allocate a unique quarantine destination",
    )


def _run_windows_mutation_hook(path: Path) -> None:
    """Run the deterministic pre-mutation test seam, when one is installed."""

    if _BEFORE_WINDOWS_QUARANTINE_MUTATION is not None:
        _BEFORE_WINDOWS_QUARANTINE_MUTATION(path)


def _open_windows_child_handle(
    api: _WindowsApi,
    path: Path,
    result: os.stat_result,
    *,
    operation: str,
) -> tuple[int, _WindowsFileIdentity]:
    """Open and identity-bind one quarantined child without following links."""

    expected = _PathIdentity(path, True, _identity_signature(result))
    is_directory = stat.S_ISDIR(result.st_mode)
    handle = api.open(
        path,
        directory=is_directory,
        share_mode=_WINDOWS_HANDLE_SHARE_MODE,
    )
    native_identity: Optional[_WindowsFileIdentity] = None
    try:
        native_identity = api.identity(handle)
        _assert_native_identity_matches_path(
            native_identity,
            expected,
            directory=is_directory,
            operation=operation,
            path=path,
        )
        return handle, native_identity
    except BaseException as error:
        close_report = _close_windows_handle(
            api,
            _WindowsHandleRecord(path, handle, native_identity),
        )
        if close_report.errors:
            _note_close_failures(
                error,
                operation=operation,
                path=path,
                attempts=0,
                errors=close_report.errors,
                unclosed=close_report.unclosed,
            )
        raise


def _assert_windows_directory_current(
    directory: Path,
    directory_handle: int,
    expected: _PathIdentity,
    *,
    binding: _PathBinding,
    operation: str,
    attempts: int,
) -> None:
    """Bind a recursive directory pathname to its held native handle."""

    binding.assert_current(operation=operation, attempts=attempts)
    try:
        result = _lstat_no_follow(directory)
    except OSError as error:
        diagnostic = _diagnostic(
            operation=operation,
            path=directory,
            attempts=attempts,
            error=error,
            reason="quarantined directory changed during handle-bound cleanup",
        )
        raise PackagingCleanupError(diagnostic) from error
    if stat.S_ISLNK(result.st_mode) or _is_reparse_point(directory, result):
        raise _security_error(
            operation=operation,
            path=directory,
            reason="quarantined directory became a symlink or reparse point",
            attempts=attempts,
        )
    if _identity_signature(result) != expected.signature:
        raise _security_error(
            operation=operation,
            path=directory,
            reason="quarantined directory pathname identity changed",
            attempts=attempts,
        )
    native_state = binding.windows_state
    if native_state is None:
        raise _security_error(
            operation=operation,
            path=directory,
            reason="native Windows directory handle state is unavailable",
            attempts=attempts,
        )
    native_identity = native_state.api.identity(directory_handle)
    _assert_native_identity_matches_path(
        native_identity,
        expected,
        directory=True,
        operation=operation,
        path=directory,
    )


def _remove_windows_tree_by_handles(
    root: Path,
    root_handle: int,
    *,
    binding: _PathBinding,
    operation: str,
    attempts: int,
    root_device: Optional[int],
) -> None:
    """Recursively delete a quarantined directory through bound handles.

    Python's ``shutil.rmtree`` is pathname-based on Windows and cannot prove
    that a redirected ancestor was not substituted between enumeration and
    deletion.  Each child below is therefore opened with an explicit
    no-reparse handle, matched to its pre-open ``lstat`` identity, and deleted
    by ``SetFileInformationByHandle``.  A directory that changes or becomes
    non-empty at its final handle operation fails closed.
    """

    state = binding.windows_state
    if state is None:
        raise _security_error(
            operation=operation,
            path=root,
            reason="native Windows tree deletion requires bound handles",
            attempts=attempts,
        )

    def remove_directory(
        directory: Path,
        directory_handle: int,
        expected: _PathIdentity,
    ) -> None:
        _assert_windows_directory_current(
            directory,
            directory_handle,
            expected,
            binding=binding,
            operation=operation,
            attempts=attempts,
        )

        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            diagnostic = _diagnostic(
                operation=operation,
                path=directory,
                attempts=attempts,
                error=error,
                reason="could not enumerate quarantined packaging output",
            )
            raise PackagingCleanupError(diagnostic) from error

        for entry in entries:
            child = Path(entry.path)
            _assert_windows_directory_current(
                directory,
                directory_handle,
                expected,
                binding=binding,
                operation=operation,
                attempts=attempts,
            )
            try:
                child_result = _lstat_no_follow(child)
            except OSError as error:
                diagnostic = _diagnostic(
                    operation=operation,
                    path=child,
                    attempts=attempts,
                    error=error,
                    reason="quarantined child changed before handle binding",
                )
                raise PackagingCleanupError(diagnostic) from error
            if stat.S_ISLNK(child_result.st_mode) or _is_reparse_point(
                child, child_result
            ):
                raise _security_error(
                    operation=operation,
                    path=child,
                    reason="quarantined tree contains a symlink or reparse point",
                    attempts=attempts,
                )
            child_device = getattr(child_result, "st_dev", None)
            if (
                root_device is not None
                and child_device is not None
                and child_device != root_device
            ):
                raise _security_error(
                    operation=operation,
                    path=child,
                    reason="quarantined tree crosses a mount/device boundary",
                    attempts=attempts,
                )

            child_handle, child_identity = _open_windows_child_handle(
                state.api,
                child,
                child_result,
                operation=operation,
            )
            try:
                # This catches replacement of the bound quarantine root while
                # the child pathname was being opened.  The child handle also
                # remains identity-bound for the actual delete operation.
                _assert_windows_directory_current(
                    directory,
                    directory_handle,
                    expected,
                    binding=binding,
                    operation=operation,
                    attempts=attempts,
                )
                if stat.S_ISDIR(child_result.st_mode):
                    remove_directory(
                        child,
                        child_handle,
                        _PathIdentity(child, True, _identity_signature(child_result)),
                    )
                else:
                    state.api.mark_delete(child_handle)
            finally:
                child_report = _close_windows_handle(
                    state.api,
                    _WindowsHandleRecord(child, child_handle, child_identity),
                )
                state.recursive_close_errors.extend(child_report.errors)
                state.recursive_unclosed.extend(child_report.unclosed)

            if state.recursive_close_errors:
                raise _close_failure_error(
                    operation=operation,
                    path=child,
                    attempts=attempts,
                    errors=state.recursive_close_errors,
                    unclosed=state.recursive_unclosed,
                )

        _assert_windows_directory_current(
            directory,
            directory_handle,
            expected,
            binding=binding,
            operation=operation,
            attempts=attempts,
        )
        state.api.mark_delete(directory_handle)

    root_result = _lstat_no_follow(root)
    remove_directory(
        root,
        root_handle,
        _PathIdentity(root, True, _identity_signature(root_result)),
    )


def _remove_windows_with_quarantine(
    path: Path,
    *,
    binding: _PathBinding,
    operation: str,
    attempts: int = 0,
) -> None:
    """Quarantine one verified path before recursive Windows deletion."""

    native_state = binding.windows_state
    if _REAL_WINDOWS:
        if native_state is None:
            raise _security_error(
                operation=operation,
                path=path,
                reason=(
                    "native handle-relative Windows cleanup is unavailable; "
                    "pathname deletion is forbidden"
                ),
                attempts=attempts,
            )
        if native_state.target_handle is None:
            # The binding was made against an absent final component.  The
            # caller's boundary assertion has already rejected a new target.
            return

        if binding.quarantine_path is None:
            try:
                result = _lstat_no_follow(path)
            except FileNotFoundError as error:
                raise _security_error(
                    operation=operation,
                    path=path,
                    reason="bound Windows target disappeared before quarantine",
                    attempts=attempts,
                ) from error
            if stat.S_ISLNK(result.st_mode) or _is_reparse_point(path, result):
                raise _security_error(
                    operation=operation,
                    path=path,
                    reason=(
                        "target became a symlink or reparse point before quarantine"
                    ),
                    attempts=attempts,
                )
            if native_state.target_is_directory:
                _assert_tree_has_no_reparse_points(
                    path,
                    operation=operation,
                    root_device=binding.identities[0].signature[0],
                )
            quarantine = _new_quarantine_path(path, operation=operation)
            if _absolute_lexical_path(quarantine.parent) != _absolute_lexical_path(
                path.parent
            ):
                raise _security_error(
                    operation=operation,
                    path=quarantine,
                    reason="quarantine destination escaped the bound parent",
                    attempts=attempts,
                )
            if (
                native_state.target_identity is None
                or native_state.parent_identity.volume_serial
                != native_state.target_identity.volume_serial
            ):
                raise _security_error(
                    operation=operation,
                    path=quarantine,
                    reason="quarantine source and parent are on different volumes",
                    attempts=attempts,
                )

            # This is the final race seam.  The second assertion is
            # intentional: a test or another process may replace an ancestor
            # after validation, and no native mutation is attempted then.
            binding.assert_current(operation=operation, attempts=attempts)
            _run_windows_mutation_hook(path)
            binding.assert_current(operation=operation, attempts=attempts)
            native_state.api.rename_same_parent(
                native_state.target_handle,
                native_state.parent_handle,
                path.parent,
                quarantine.name,
            )
            binding.bind_quarantine(quarantine)
            if binding.quarantine_signature != binding.identities[-1].signature:
                raise _security_error(
                    operation=operation,
                    path=path,
                    reason="quarantine identity did not match the bound target",
                    attempts=attempts,
                )

        quarantine_path = binding.quarantine_path
        if quarantine_path is None:
            return
        binding.assert_current(operation=operation, attempts=attempts)
        quarantine_signature = _inspect_existing_identity(
            quarantine_path,
            operation=operation,
            attempts=attempts,
        )
        if quarantine_signature != binding.quarantine_signature:
            raise _security_error(
                operation=operation,
                path=quarantine_path,
                reason="quarantine identity changed before deletion",
                attempts=attempts,
            )
        if native_state.target_is_directory:
            _remove_windows_tree_by_handles(
                quarantine_path,
                native_state.target_handle,
                binding=binding,
                operation=operation,
                attempts=attempts,
                root_device=binding.identities[0].signature[0],
            )
        else:
            native_state.api.mark_delete(native_state.target_handle)
        native_state.deletion_marked = True
        return

    # Non-Windows tests may set _IS_WINDOWS to exercise retry behavior.  This
    # compatibility branch is never used by production Windows code because
    # _REAL_WINDOWS remains true there and path-based mutation is forbidden.
    if binding.quarantine_path is None:
        try:
            result = _lstat_no_follow(path)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(result.st_mode) or _is_reparse_point(path, result):
            raise _security_error(
                operation=operation,
                path=path,
                reason="target became a symlink or reparse point before quarantine",
                attempts=attempts,
            )
        quarantine = _new_quarantine_path(path, operation=operation)
        binding.assert_current(operation=operation, attempts=attempts)
        _run_windows_mutation_hook(path)
        binding.assert_current(operation=operation, attempts=attempts)
        # This branch is only a test simulation on non-Windows hosts; actual
        # Windows uses the handle-relative API above.
        os.rename(path, quarantine)
        binding.bind_quarantine(quarantine)
        if binding.quarantine_signature != binding.identities[-1].signature:
            raise _security_error(
                operation=operation,
                path=path,
                reason="quarantine identity did not match the bound target",
                attempts=attempts,
            )

    quarantine_path = binding.quarantine_path
    if quarantine_path is None:
        return
    quarantine_signature = _inspect_existing_identity(
        quarantine_path,
        operation=operation,
        attempts=attempts,
    )
    if quarantine_signature != binding.quarantine_signature:
        raise _security_error(
            operation=operation,
            path=quarantine_path,
            reason="quarantine identity changed before deletion",
            attempts=attempts,
        )
    try:
        quarantine_result = _lstat_no_follow(quarantine_path)
    except FileNotFoundError as error:
        raise _security_error(
            operation=operation,
            path=quarantine_path,
            reason="quarantine disappeared before deletion",
            attempts=attempts,
        ) from error
    if stat.S_ISDIR(quarantine_result.st_mode):
        _assert_tree_has_no_reparse_points(
            quarantine_path,
            operation=operation,
            root_device=binding.identities[0].signature[0],
        )
        shutil.rmtree(quarantine_path)
    else:
        os.unlink(quarantine_path)


def _posix_identity(result: os.stat_result) -> tuple[int, int, int]:
    """Return the mandatory identity fields for one descriptor-bound object."""

    return (int(result.st_dev), int(result.st_ino), stat.S_IFMT(result.st_mode))


def _assert_posix_object(
    result: os.stat_result,
    expected: os.stat_result,
    *,
    path: Path,
    operation: str,
    attempts: int,
    root_device: Optional[int],
) -> None:
    """Reject replacement, mount crossing, links, and unsupported object types."""

    if _posix_identity(result) != _posix_identity(expected):
        raise _security_error(
            operation=operation,
            path=path,
            reason="descriptor-bound POSIX object identity changed",
            attempts=attempts,
        )
    if root_device is None or int(result.st_dev) != int(root_device):
        raise _security_error(
            operation=operation,
            path=path,
            reason="descriptor-bound POSIX tree crossed a mount/device boundary",
            attempts=attempts,
        )
    if stat.S_ISLNK(result.st_mode):
        raise _security_error(
            operation=operation,
            path=path,
            reason="descriptor-bound POSIX tree contains a symlink",
            attempts=attempts,
        )
    if stat.S_ISREG(result.st_mode) and int(result.st_nlink) != 1:
        raise _security_error(
            operation=operation,
            path=path,
            reason="descriptor-bound POSIX tree contains a hard-linked file",
            attempts=attempts,
        )
    if not stat.S_ISDIR(result.st_mode) and not stat.S_ISREG(result.st_mode):
        raise _security_error(
            operation=operation,
            path=path,
            reason="descriptor-bound POSIX tree contains an unsupported object type",
            attempts=attempts,
        )


def _assert_posix_host_owner(
    result: os.stat_result,
    *,
    path: Path,
    operation: str,
    attempts: int,
) -> None:
    """Require a sealed-tree object to belong to the current build host."""

    effective_uid = getattr(os, "geteuid", None)
    owner = _posix_owner(result)
    if effective_uid is None or owner is None or owner != effective_uid():
        raise _security_error(
            operation=operation,
            path=path,
            reason="descriptor-bound POSIX tree entry is not host-owned",
            attempts=attempts,
        )


def _normalize_expected_tree(
    expected_tree: Mapping[str, bool],
    *,
    operation: str,
    path: Path,
    attempts: int,
) -> dict[str, bool]:
    """Normalize a manifest file inventory and derive its parent directories."""

    normalized: dict[str, bool] = {"": True}

    def insert(relative: str, is_directory: bool) -> None:
        previous = normalized.get(relative)
        if relative in normalized and previous != is_directory:
            raise _security_error(
                operation=operation,
                path=path,
                reason="sealed manifest has a file/directory collision",
                attempts=attempts,
            )
        normalized[relative] = is_directory

    for relative, is_directory in expected_tree.items():
        if not isinstance(relative, str) or not isinstance(is_directory, bool):
            raise _security_error(
                operation=operation,
                path=path,
                reason="sealed manifest inventory has an invalid entry",
                attempts=attempts,
            )
        if relative == "":
            if not is_directory:
                raise _security_error(
                    operation=operation,
                    path=path,
                    reason="sealed manifest root must be a directory",
                    attempts=attempts,
                )
            continue
        portable = PurePosixPath(relative)
        if (
            portable.is_absolute()
            or "\\" in relative
            or portable.as_posix() != relative
            or any(part in {"", ".", ".."} for part in portable.parts)
        ):
            raise _security_error(
                operation=operation,
                path=path,
                reason="sealed manifest path is not canonical",
                attempts=attempts,
            )
        insert(relative, is_directory)
        parts = relative.split("/")
        for index in range(1, len(parts)):
            insert("/".join(parts[:index]), True)
    return normalized


def _lstat_at(parent_fd: int, name: str) -> os.stat_result:
    """Inspect one child relative to a held parent without following links."""

    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _run_posix_mutation_hook(path: Path) -> None:
    """Run the deterministic POSIX race seam, when installed by a test."""

    if _BEFORE_POSIX_MUTATION is not None:
        _BEFORE_POSIX_MUTATION(path)


def _new_posix_quarantine_name(parent_fd: int, path: Path, *, operation: str) -> str:
    """Allocate an absent sibling name through the already-bound parent fd."""

    for _ in range(8):
        name = f".tobkiri-cleanup-{uuid.uuid4().hex}"
        try:
            _lstat_at(parent_fd, name)
        except FileNotFoundError:
            return name
        except OSError as error:
            diagnostic = _diagnostic(
                operation=operation,
                path=path,
                attempts=0,
                error=error,
                reason="could not inspect descriptor-relative quarantine destination",
            )
            raise PackagingCleanupError(diagnostic) from error
    raise _security_error(
        operation=operation,
        path=path,
        reason="could not allocate a descriptor-relative quarantine destination",
    )


def _quarantine_posix_target(
    parent_fd: int,
    name: str,
    path: Path,
    *,
    expected: os.stat_result,
    binding: _PathBinding,
    operation: str,
    attempts: int,
    root_device: Optional[int],
) -> tuple[str, os.stat_result]:
    """Atomically move the bound target aside, then verify what was moved."""

    quarantine_name = _new_posix_quarantine_name(parent_fd, path, operation=operation)
    _run_posix_mutation_hook(path)
    binding.assert_current(operation=operation, attempts=attempts)
    os.rename(
        name,
        quarantine_name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )
    moved = _lstat_at(parent_fd, quarantine_name)
    _assert_posix_object(
        moved,
        expected,
        path=path,
        operation=operation,
        attempts=attempts,
        root_device=root_device,
    )
    binding.quarantine_path = path.parent / quarantine_name
    binding.quarantine_signature = _identity_signature(moved)
    binding.assert_current(operation=operation, attempts=attempts)
    return quarantine_name, moved


def _open_posix_at(parent_fd: int, name: str, *, directory: bool) -> int:
    """Open one already-lstatted object through its held parent descriptor."""

    required = ("O_NOFOLLOW", "O_CLOEXEC")
    if not all(hasattr(os, flag) for flag in required):
        raise OSError(errno.ENOTSUP, "required no-follow open flags are unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    if directory:
        if not hasattr(os, "O_DIRECTORY"):
            raise OSError(errno.ENOTSUP, "required directory open flag is unavailable")
        flags |= os.O_DIRECTORY
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
    return os.open(name, flags, dir_fd=parent_fd)


def _prepare_posix_sealed_tree(
    parent_fd: int,
    name: str,
    path: Path,
    *,
    binding: _PathBinding,
    expected_tree: Optional[Mapping[str, bool]],
    operation: str,
    attempts: int,
) -> dict[str, bool]:
    """Validate and unseal one host-owned manifest-bound POSIX tree.

    The complete descriptor-relative inventory is checked before any mode is
    changed.  Directory descriptors remain held while they are changed to
    owner-only writable mode, so the later quarantine/delete step never needs
    to resolve an untrusted pathname to make a sealed tree removable.
    """

    if _IS_WINDOWS or binding.posix_mount_identity is None:
        raise _security_error(
            operation=operation,
            path=path,
            reason="sealed POSIX reset requires descriptor and mount identity",
            attempts=attempts,
        )
    dynamic_inventory = expected_tree is None
    normalized = (
        {"": True}
        if dynamic_inventory
        else _normalize_expected_tree(
            expected_tree,
            operation=operation,
            path=path,
            attempts=attempts,
        )
    )
    for index, descriptor in enumerate(binding.directory_fds):
        current = os.fstat(descriptor)
        identity = binding.identities[index].signature
        if _posix_identity(current) != (
            identity[0],
            identity[1],
            identity[2],
        ):
            raise _security_error(
                operation=operation,
                path=path,
                reason="bound owner directory identity changed before sealed reset",
                attempts=attempts,
            )
        _assert_posix_host_owner(
            current,
            path=binding.identities[index].path,
            operation=operation,
            attempts=attempts,
        )

    try:
        root_result = _lstat_at(parent_fd, name)
    except FileNotFoundError:
        return normalized
    root_device = binding.identities[0].signature[0]
    _assert_posix_object(
        root_result,
        root_result,
        path=path,
        operation=operation,
        attempts=attempts,
        root_device=root_device,
    )
    if not stat.S_ISDIR(root_result.st_mode) or normalized.get("") is not True:
        raise _security_error(
            operation=operation,
            path=path,
            reason="sealed manifest root is not the expected directory",
            attempts=attempts,
        )

    directory_records: list[tuple[int, str, Path, os.stat_result]] = []
    seen: set[str] = set()
    root_fd = _open_posix_at(parent_fd, name, directory=True)
    try:
        directory_records.append((root_fd, "", path, root_result))
        _assert_posix_mount_identity(
            root_fd,
            binding.posix_mount_identity,
            path=path,
            operation=operation,
            attempts=attempts,
        )
        opened_root = os.fstat(root_fd)
        _assert_posix_object(
            opened_root,
            root_result,
            path=path,
            operation=operation,
            attempts=attempts,
            root_device=root_device,
        )
        _assert_posix_host_owner(
            opened_root,
            path=path,
            operation=operation,
            attempts=attempts,
        )

        def visit(directory_fd: int, relative: str, directory_path: Path) -> None:
            if normalized.get(relative) is not True:
                raise _security_error(
                    operation=operation,
                    path=directory_path,
                    reason="sealed manifest does not identify an owned directory",
                    attempts=attempts,
                )
            seen.add(relative)
            for child_name in os.listdir(directory_fd):
                if not isinstance(child_name, str) or child_name in (
                    "",
                    ".",
                    "..",
                ):
                    raise _security_error(
                        operation=operation,
                        path=directory_path,
                        reason="sealed directory enumeration returned an unsafe name",
                        attempts=attempts,
                    )
                child_path = directory_path / child_name
                child_relative = (
                    child_name if not relative else f"{relative}/{child_name}"
                )
                child_result = _lstat_at(directory_fd, child_name)
                _assert_posix_object(
                    child_result,
                    child_result,
                    path=child_path,
                    operation=operation,
                    attempts=attempts,
                    root_device=root_device,
                )
                actual_directory = stat.S_ISDIR(child_result.st_mode)
                if dynamic_inventory:
                    normalized[child_relative] = actual_directory
                elif child_relative not in normalized:
                    raise _security_error(
                        operation=operation,
                        path=child_path,
                        reason="sealed tree contains an unowned extra entry",
                        attempts=attempts,
                    )
                expected_directory = normalized[child_relative]
                if actual_directory != expected_directory:
                    raise _security_error(
                        operation=operation,
                        path=child_path,
                        reason="sealed tree entry type differs from its manifest",
                        attempts=attempts,
                    )
                if actual_directory:
                    child_fd = _open_posix_at(
                        directory_fd,
                        child_name,
                        directory=True,
                    )
                    directory_records.append(
                        (child_fd, child_relative, child_path, child_result)
                    )
                    _assert_posix_mount_identity(
                        child_fd,
                        binding.posix_mount_identity,
                        path=child_path,
                        operation=operation,
                        attempts=attempts,
                    )
                    opened_child = os.fstat(child_fd)
                    _assert_posix_object(
                        opened_child,
                        child_result,
                        path=child_path,
                        operation=operation,
                        attempts=attempts,
                        root_device=root_device,
                    )
                    _assert_posix_host_owner(
                        opened_child,
                        path=child_path,
                        operation=operation,
                        attempts=attempts,
                    )
                    visit(child_fd, child_relative, child_path)
                else:
                    file_fd = _open_posix_at(
                        directory_fd,
                        child_name,
                        directory=False,
                    )
                    try:
                        _assert_posix_mount_identity(
                            file_fd,
                            binding.posix_mount_identity,
                            path=child_path,
                            operation=operation,
                            attempts=attempts,
                        )
                        opened_child = os.fstat(file_fd)
                        _assert_posix_object(
                            opened_child,
                            child_result,
                            path=child_path,
                            operation=operation,
                            attempts=attempts,
                            root_device=root_device,
                        )
                        _assert_posix_host_owner(
                            opened_child,
                            path=child_path,
                            operation=operation,
                            attempts=attempts,
                        )
                    finally:
                        os.close(file_fd)
                    seen.add(child_relative)

        visit(root_fd, "", path)
        if seen != set(normalized):
            raise _security_error(
                operation=operation,
                path=path,
                reason="sealed tree does not match the exact host-owned manifest",
                attempts=attempts,
            )
        binding.assert_current(operation=operation, attempts=attempts)
        for descriptor, _, directory_path, expected in directory_records:
            current = os.fstat(descriptor)
            _assert_posix_object(
                current,
                expected,
                path=directory_path,
                operation=operation,
                attempts=attempts,
                root_device=root_device,
            )
            _assert_posix_host_owner(
                current,
                path=directory_path,
                operation=operation,
                attempts=attempts,
            )
            _assert_posix_mount_identity(
                descriptor,
                binding.posix_mount_identity,
                path=directory_path,
                operation=operation,
                attempts=attempts,
            )
            os.fchmod(descriptor, stat.S_IRWXU)
        binding.assert_current(operation=operation, attempts=attempts)
    except PackagingCleanupError:
        raise
    except OSError as error:
        diagnostic = _diagnostic(
            operation=operation,
            path=path,
            attempts=attempts,
            error=error,
            reason="could not validate or unseal the host-owned sealed tree",
        )
        raise PackagingCleanupError(diagnostic) from error
    finally:
        for descriptor, _, _, _ in reversed(directory_records):
            os.close(descriptor)
    return normalized


def _remove_posix_file_at(
    parent_fd: int,
    name: str,
    path: Path,
    *,
    expected: os.stat_result,
    binding: _PathBinding,
    operation: str,
    attempts: int,
    root_device: Optional[int],
    root_mount_identity: _PosixMountIdentity,
) -> None:
    """Unlink one regular file only while its open identity remains current."""

    _assert_posix_object(
        expected,
        expected,
        path=path,
        operation=operation,
        attempts=attempts,
        root_device=root_device,
    )
    descriptor = _open_posix_at(parent_fd, name, directory=False)
    try:
        _assert_posix_mount_identity(
            descriptor,
            root_mount_identity,
            path=path,
            operation=operation,
            attempts=attempts,
        )
        opened = os.fstat(descriptor)
        _assert_posix_object(
            opened,
            expected,
            path=path,
            operation=operation,
            attempts=attempts,
            root_device=root_device,
        )
        _run_posix_mutation_hook(path)
        binding.assert_current(operation=operation, attempts=attempts)
        current = _lstat_at(parent_fd, name)
        _assert_posix_object(
            current,
            opened,
            path=path,
            operation=operation,
            attempts=attempts,
            root_device=root_device,
        )
        os.unlink(name, dir_fd=parent_fd)
    finally:
        os.close(descriptor)


def _remove_posix_tree_at(
    parent_fd: int,
    name: str,
    path: Path,
    *,
    expected: os.stat_result,
    binding: _PathBinding,
    operation: str,
    attempts: int,
    root_device: Optional[int],
    root_mount_identity: _PosixMountIdentity,
    expected_tree: Optional[Mapping[str, bool]] = None,
    relative: str = "",
    seen: Optional[set[str]] = None,
) -> None:
    """Recursively remove a directory using only no-follow held descriptors."""

    expected_root = expected_tree is not None and seen is None
    if expected_tree is not None:
        if seen is None:
            seen = set()
        if expected_tree.get(relative) is not True:
            raise _security_error(
                operation=operation,
                path=path,
                reason="sealed tree directory is not present in its manifest",
                attempts=attempts,
            )
        seen.add(relative)

    _assert_posix_object(
        expected,
        expected,
        path=path,
        operation=operation,
        attempts=attempts,
        root_device=root_device,
    )
    directory_fd = _open_posix_at(parent_fd, name, directory=True)
    try:
        _assert_posix_mount_identity(
            directory_fd,
            root_mount_identity,
            path=path,
            operation=operation,
            attempts=attempts,
        )
        opened = os.fstat(directory_fd)
        _assert_posix_object(
            opened,
            expected,
            path=path,
            operation=operation,
            attempts=attempts,
            root_device=root_device,
        )
        for child_name in os.listdir(directory_fd):
            if not isinstance(child_name, str) or child_name in ("", ".", ".."):
                raise _security_error(
                    operation=operation,
                    path=path,
                    reason="descriptor-relative directory enumeration returned an unsafe name",
                    attempts=attempts,
                )
            child_path = path / child_name
            child_result = _lstat_at(directory_fd, child_name)
            _assert_posix_object(
                child_result,
                child_result,
                path=child_path,
                operation=operation,
                attempts=attempts,
                root_device=root_device,
            )
            child_relative = child_name if not relative else f"{relative}/{child_name}"
            if expected_tree is not None:
                if child_relative not in expected_tree:
                    raise _security_error(
                        operation=operation,
                        path=child_path,
                        reason="sealed tree contains an unowned extra entry",
                        attempts=attempts,
                    )
                if stat.S_ISDIR(child_result.st_mode) != expected_tree[child_relative]:
                    raise _security_error(
                        operation=operation,
                        path=child_path,
                        reason="sealed tree entry type differs from its manifest",
                        attempts=attempts,
                    )
            if stat.S_ISDIR(child_result.st_mode):
                _remove_posix_tree_at(
                    directory_fd,
                    child_name,
                    child_path,
                    expected=child_result,
                    binding=binding,
                    operation=operation,
                    attempts=attempts,
                    root_device=root_device,
                    root_mount_identity=root_mount_identity,
                    expected_tree=expected_tree,
                    relative=child_relative,
                    seen=seen,
                )
            else:
                _remove_posix_file_at(
                    directory_fd,
                    child_name,
                    child_path,
                    expected=child_result,
                    binding=binding,
                    operation=operation,
                    attempts=attempts,
                    root_device=root_device,
                    root_mount_identity=root_mount_identity,
                )
                if seen is not None:
                    seen.add(child_relative)

        if expected_root and seen != set(expected_tree or {}):
            raise _security_error(
                operation=operation,
                path=path,
                reason="sealed tree is missing an entry from its manifest",
                attempts=attempts,
            )
        _run_posix_mutation_hook(path)
        binding.assert_current(operation=operation, attempts=attempts)
        current = _lstat_at(parent_fd, name)
        _assert_posix_object(
            current,
            opened,
            path=path,
            operation=operation,
            attempts=attempts,
            root_device=root_device,
        )
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(directory_fd)


def _remove_once(
    path: Path,
    *,
    parent_fd: Optional[int] = None,
    operation: str = "remove owned packaging path",
    binding: Optional[_PathBinding] = None,
    attempts: int = 0,
    expected_tree: Optional[Mapping[str, bool]] = None,
) -> None:
    """Remove one already-bound path without following its final link."""

    if _IS_WINDOWS:
        if binding is None:
            raise _security_error(
                operation=operation,
                path=path,
                reason="Windows cleanup requires an identity-bound path",
            )
        _remove_windows_with_quarantine(
            path,
            binding=binding,
            operation=operation,
            attempts=attempts,
        )
        return

    if parent_fd is not None:
        name = path.name
        try:
            result = os.lstat(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(result.st_mode) or _is_reparse_point(path, result):
            raise _security_error(
                operation=operation,
                path=path,
                reason="target became a symlink or reparse point during cleanup",
            )
        if binding is None:
            raise _security_error(
                operation=operation,
                path=path,
                reason="POSIX cleanup requires an identity binding",
            )
        if binding.posix_mount_identity is None:
            raise _security_error(
                operation=operation,
                path=path,
                reason="POSIX cleanup has no verified root mount identity",
                attempts=attempts,
            )
        root_device = binding.identities[0].signature[0]
        quarantine_name, quarantine_result = _quarantine_posix_target(
            parent_fd,
            name,
            path,
            expected=result,
            binding=binding,
            operation=operation,
            attempts=attempts,
            root_device=root_device,
        )
        if stat.S_ISDIR(quarantine_result.st_mode):
            _remove_posix_tree_at(
                parent_fd,
                quarantine_name,
                path,
                expected=quarantine_result,
                binding=binding,
                operation=operation,
                attempts=attempts,
                root_device=root_device,
                root_mount_identity=binding.posix_mount_identity,
                expected_tree=expected_tree,
            )
            return
        if not stat.S_ISREG(quarantine_result.st_mode):
            raise OSError(errno.EINVAL, f"unsupported packaging output type: {path}")
        _remove_posix_file_at(
            parent_fd,
            quarantine_name,
            path,
            expected=quarantine_result,
            binding=binding,
            operation=operation,
            attempts=attempts,
            root_device=root_device,
            root_mount_identity=binding.posix_mount_identity,
        )
        return

    raise _security_error(
        operation=operation,
        path=path,
        reason="descriptor-relative POSIX cleanup is unavailable; pathname fallback is forbidden",
    )


def _close_child_streams(child: object) -> None:
    """Close any standard streams exposed by a child process object."""

    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(child, stream_name, None)
        if stream is not None and not getattr(stream, "closed", False):
            stream.close()


def run_process_and_wait(
    command: Sequence[Union[str, os.PathLike[str]]],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> None:
    """Run a packaging child to completion and close its process handles."""

    popen_kwargs: dict[str, object] = {"cwd": os.fspath(cwd)}
    if env is not None:
        popen_kwargs["env"] = dict(env)
    with subprocess.Popen(command, **popen_kwargs) as child:
        return_code = child.wait()
        _close_child_streams(child)
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)


_ISOLATED_ENVIRONMENT_KEYS = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)
_ISOLATED_MODULE_CODE = (
    "import runpy,sys;"
    "source_root=sys.argv[1];"
    "module_name=sys.argv[2];"
    "sys.path.insert(0,source_root);"
    "sys.argv=[module_name,*sys.argv[3:]];"
    "runpy.run_module(module_name,run_name='__main__',alter_sys=True)"
)


def isolated_packaging_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Rebuild a neutral environment for an official source generator child."""
    inherited = os.environ if source is None else source
    environment = {
        key: value
        for key, value in inherited.items()
        if key in _ISOLATED_ENVIRONMENT_KEYS
    }
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    return environment


def isolated_python_module_command(
    python: Union[str, os.PathLike[str]],
    module: str,
    source_root: Path,
    arguments: Sequence[Union[str, os.PathLike[str]]] = (),
) -> list[Union[str, os.PathLike[str]]]:
    """Build the canonical ``-I -B -c`` module launcher command.

    Python isolated mode removes the working directory and all ``PYTHON*``
    environment influence.  The only application path explicitly added is
    the caller-verified canonical source root, then ``runpy`` loads the named
    module with normal module semantics.
    """
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError(f"isolated module source root is unavailable: {source_root}")
    canonical_root = source_root.resolve(strict=True)
    if not canonical_root.is_dir():
        raise ValueError(f"isolated module source root is not a directory: {source_root}")
    if not module or any(character.isspace() for character in module):
        raise ValueError(f"isolated module name is invalid: {module!r}")
    return [
        python,
        "-I",
        "-B",
        "-c",
        _ISOLATED_MODULE_CODE,
        os.fspath(canonical_root),
        module,
        *arguments,
    ]
def _ensure_child_exited(child: object, *, operation: str, path: Path) -> None:
    """Refuse cleanup while a child is alive, then close its streams."""

    try:
        return_code = child.poll()  # type: ignore[attr-defined]
        if return_code is None:
            try:
                return_code = child.wait(timeout=0)  # type: ignore[attr-defined]
            except subprocess.TimeoutExpired as error:
                diagnostic = _diagnostic(
                    operation=operation,
                    path=path,
                    attempts=0,
                    error=error,
                    child_alive=True,
                    reason="child process is still alive; cleanup refused",
                )
                raise PackagingCleanupError(diagnostic) from error
        _close_child_streams(child)
    except PackagingCleanupError:
        raise
    except (OSError, ValueError) as error:
        diagnostic = _diagnostic(
            operation=operation,
            path=path,
            attempts=0,
            error=error,
            reason="child process handle or stream could not be closed",
        )
        raise PackagingCleanupError(diagnostic) from error


def remove_owned_path(
    path: Path,
    *,
    owner_root: Path,
    operation: str,
    child: Optional[object] = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: tuple[float, ...] = _DEFAULT_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    expected_tree: Optional[Mapping[str, bool]] = None,
    unseal_read_only: bool = False,
    expected_identity: Optional[tuple[int, int]] = None,
) -> None:
    """Remove an owned path with bounded Windows lock-race retries.

    ``owner_root`` is an explicit parent scope; the scope itself can never be
    removed.  Only Windows sharing/access-denied errors are retried, and an
    exhausted retry or any other error raises ``PackagingCleanupError`` while
    leaving the path in place.

    ``expected_tree`` and ``unseal_read_only`` are reserved for a producer's
    host-owned sealed tree.  When no manifest is available, a creation-time
    ``expected_identity`` permits a descriptor-bound inventory to be captured
    before any directory mode is changed.  Ordinary cleanup callers retain the
    existing quarantine-only behavior.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if expected_identity is not None and (
        not isinstance(expected_identity, tuple)
        or len(expected_identity) != 2
        or any(not isinstance(value, int) or value <= 0 for value in expected_identity)
    ):
        raise ValueError("expected_identity must contain positive device and inode values")
    if unseal_read_only and expected_tree is None and expected_identity is None:
        raise ValueError(
            "dynamic read-only reset requires a creation-time path identity"
        )
    if unseal_read_only and _IS_WINDOWS:
        raise ValueError("read-only sealed-tree reset requires POSIX descriptors")
    binding: Optional[_PathBinding] = None
    bind_attempt = 0
    while binding is None:
        bind_attempt += 1
        try:
            binding = _bind_owned_path(
                Path(path),
                Path(owner_root),
                operation=operation,
            )
        except PackagingCleanupError:
            raise
        except OSError as error:
            retained = getattr(error, "cleanup_unclosed_windows", ())
            if retained:
                close_errors = getattr(error, "cleanup_close_failures", (error,))
                raise _close_failure_error(
                    operation=operation,
                    path=_absolute_lexical_path(Path(path)),
                    attempts=bind_attempt,
                    errors=close_errors,
                    unclosed=retained,
                ) from error
            transient = is_transient_windows_cleanup_error(error)
            if not transient or bind_attempt == max_attempts:
                diagnostic = _diagnostic(
                    operation=operation,
                    path=_absolute_lexical_path(Path(path)),
                    attempts=bind_attempt,
                    error=error,
                    transient=transient,
                    exhausted=transient and bind_attempt == max_attempts,
                    reason=(
                        "recognized transient Windows lock persisted while "
                        "binding cleanup handles"
                        if transient and bind_attempt == max_attempts
                        else "could not bind cleanup path"
                    ),
                )
                raise PackagingCleanupError(diagnostic) from error
            if backoff_seconds:
                delay_index = min(bind_attempt - 1, len(backoff_seconds) - 1)
                sleep(backoff_seconds[delay_index])
    assert binding is not None
    target = binding.target
    target_identity = binding.identities[-1]
    if expected_identity is not None and target_identity.exists:
        actual_identity = target_identity.signature[:2]
        if actual_identity != expected_identity:
            error = _security_error(
                operation=operation,
                path=target,
                reason="owned path differs from its creation-time identity",
            )
            _finish_binding_close(
                binding,
                operation=operation,
                path=target,
                attempts=0,
                primary=error,
            )
            raise error
    if child is not None:
        try:
            _ensure_child_exited(child, operation=operation, path=target)
        except BaseException as error:
            _finish_binding_close(
                binding,
                operation=operation,
                path=target,
                attempts=0,
                primary=error,
            )
            raise

    normalized_expected_tree: Optional[dict[str, bool]] = None
    try:
        if expected_tree is not None:
            normalized_expected_tree = _normalize_expected_tree(
                expected_tree,
                operation=operation,
                path=target,
                attempts=0,
            )
        if unseal_read_only:
            normalized_expected_tree = _prepare_posix_sealed_tree(
                binding.parent_fd,
                target.name,
                target,
                binding=binding,
                expected_tree=normalized_expected_tree,
                operation=operation,
                attempts=0,
            )
    except BaseException as error:
        _finish_binding_close(
            binding,
            operation=operation,
            path=target,
            attempts=0,
            primary=error,
        )
        raise

    primary_error: Optional[BaseException] = None
    last_attempt = 0
    try:
        for attempt in range(1, max_attempts + 1):
            last_attempt = attempt
            binding.assert_current(operation=operation, attempts=attempt)
            try:
                _remove_once(
                    target,
                    parent_fd=binding.parent_fd,
                    operation=operation,
                    binding=binding,
                    attempts=attempt,
                    expected_tree=normalized_expected_tree,
                )
                return
            except PackagingCleanupError:
                raise
            except OSError as error:
                retained = getattr(error, "cleanup_unclosed_windows", ())
                if retained:
                    close_errors = getattr(error, "cleanup_close_failures", (error,))
                    raise _close_failure_error(
                        operation=operation,
                        path=target,
                        attempts=attempt,
                        errors=close_errors,
                        unclosed=retained,
                    ) from error
                transient = is_transient_windows_cleanup_error(error)
                if not transient or attempt == max_attempts:
                    diagnostic = _diagnostic(
                        operation=operation,
                        path=target,
                        attempts=attempt,
                        error=error,
                        transient=transient,
                        exhausted=transient and attempt == max_attempts,
                        reason=(
                            "recognized transient Windows lock persisted"
                            if transient and attempt == max_attempts
                            else "non-retryable cleanup error"
                        ),
                    )
                    raise PackagingCleanupError(diagnostic) from error
                if backoff_seconds:
                    delay_index = min(attempt - 1, len(backoff_seconds) - 1)
                    sleep(backoff_seconds[delay_index])
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _finish_binding_close(
            binding,
            operation=operation,
            path=target,
            attempts=last_attempt,
            primary=primary_error,
        )
