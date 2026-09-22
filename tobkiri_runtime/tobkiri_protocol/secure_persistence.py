"""Pinned, descriptor-relative persistence for security-sensitive local state."""

from __future__ import annotations

import os
import secrets
import stat
import ctypes
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .durability import replace_file_durable
from .platform_paths import canonical_platform_path


class SecurePersistenceError(OSError):
    """Raised when a persistence root or entry cannot be used safely."""


_Identity = tuple[int, int]
_Fingerprint = tuple[int, int, int, int, int, int]
_ReadFingerprint = tuple[int, int, int, int, int, int, int, int]
_WindowsFileId = tuple[int, int]

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_READ_ATTRIBUTES = 0x00000080
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


class _ByHandleFileInformation(ctypes.Structure):
    """Portable declaration of Win32 BY_HANDLE_FILE_INFORMATION."""

    _fields_ = [
        ("file_attributes", ctypes.c_uint32),
        ("creation_time_low", ctypes.c_uint32),
        ("creation_time_high", ctypes.c_uint32),
        ("last_access_time_low", ctypes.c_uint32),
        ("last_access_time_high", ctypes.c_uint32),
        ("last_write_time_low", ctypes.c_uint32),
        ("last_write_time_high", ctypes.c_uint32),
        ("volume_serial_number", ctypes.c_uint32),
        ("file_size_high", ctypes.c_uint32),
        ("file_size_low", ctypes.c_uint32),
        ("number_of_links", ctypes.c_uint32),
        ("file_index_high", ctypes.c_uint32),
        ("file_index_low", ctypes.c_uint32),
    ]


def _windows_kernel32() -> Any:
    """Return the configured Win32 file API."""

    win_dll: Any = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.GetFileInformationByHandle.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    kernel32.GetFileInformationByHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def _windows_error(operation: str) -> OSError:
    code = int(getattr(ctypes, "get_last_error", lambda: 0)())
    win_error = getattr(ctypes, "WinError", None)
    if win_error is not None:
        return win_error(code)
    return OSError(code, f"{operation} failed with Windows error {code}")


def _windows_open_directory(path: Path) -> tuple[Any, _WindowsFileId]:
    """Open a directory itself, reject reparses, and return its stable File ID."""

    kernel32 = _windows_kernel32()
    handle = kernel32.CreateFileW(
        str(path),
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        raise _windows_error("CreateFileW")
    information = _ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        kernel32.CloseHandle(handle)
        raise _windows_error("GetFileInformationByHandle")
    if not information.file_attributes & _FILE_ATTRIBUTE_DIRECTORY:
        kernel32.CloseHandle(handle)
        raise SecurePersistenceError("persistence child is not a directory")
    if information.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        kernel32.CloseHandle(handle)
        raise SecurePersistenceError("persistence directory is reparse-pointed")
    file_id = (
        int(information.volume_serial_number),
        (int(information.file_index_high) << 32) | int(information.file_index_low),
    )
    return handle, file_id


def _windows_close_handle(handle: Any) -> None:
    if not _windows_kernel32().CloseHandle(handle):
        raise _windows_error("CloseHandle")


def _windows_open_file_descriptor(
    path: Path,
    flags: int,
    *,
    disposition: int = _OPEN_EXISTING,
) -> tuple[int, _WindowsFileId]:
    """Open a non-reparse regular file without delete sharing."""

    desired_access = 0
    if flags & os.O_RDWR:
        desired_access = _GENERIC_READ | _GENERIC_WRITE
    elif flags & os.O_WRONLY:
        desired_access = _GENERIC_WRITE
    else:
        desired_access = _GENERIC_READ
    kernel32 = _windows_kernel32()
    handle = kernel32.CreateFileW(
        str(path),
        desired_access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        disposition,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        raise _windows_error("CreateFileW")
    information = _ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        kernel32.CloseHandle(handle)
        raise _windows_error("GetFileInformationByHandle")
    if (
        information.file_attributes & _FILE_ATTRIBUTE_DIRECTORY
        or information.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        or information.number_of_links != 1
    ):
        kernel32.CloseHandle(handle)
        raise SecurePersistenceError("persistence entry identity is unsafe")
    file_id = (
        int(information.volume_serial_number),
        (int(information.file_index_high) << 32) | int(information.file_index_low),
    )
    try:
        import msvcrt

        open_osfhandle: Any = getattr(msvcrt, "open_osfhandle")
        descriptor = open_osfhandle(
            int(handle),
            flags | getattr(os, "O_BINARY", 0),
        )
    except Exception:
        kernel32.CloseHandle(handle)
        raise
    return descriptor, file_id


def _windows_descriptor_file_id(descriptor: int) -> _WindowsFileId:
    """Read the Win32 File ID backing one CRT descriptor."""

    import msvcrt

    get_osfhandle: Any = getattr(msvcrt, "get_osfhandle")
    handle = get_osfhandle(descriptor)
    information = _ByHandleFileInformation()
    if not _windows_kernel32().GetFileInformationByHandle(
        handle,
        ctypes.byref(information),
    ):
        raise _windows_error("GetFileInformationByHandle")
    return (
        int(information.volume_serial_number),
        (int(information.file_index_high) << 32) | int(information.file_index_low),
    )


def _identity(metadata: os.stat_result) -> _Identity:
    return (int(metadata.st_dev), int(metadata.st_ino))


def _fingerprint(metadata: os.stat_result) -> _Fingerprint:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(getattr(metadata, "st_uid", 0)),
        int(metadata.st_size),
    )


def _read_fingerprint(metadata: os.stat_result) -> _ReadFingerprint:
    return (
        *_fingerprint(metadata),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _owned_regular(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and (not hasattr(os, "getuid") or metadata.st_uid == os.getuid())
    )


def _owned_directory(metadata: os.stat_result) -> bool:
    return stat.S_ISDIR(metadata.st_mode) and (
        not hasattr(os, "getuid") or metadata.st_uid == os.getuid()
    )


class SecureDirectory:
    """Pin one owned directory tree and perform relative, no-follow operations.

    POSIX operations remain below descriptors opened from a captured ancestor
    chain. Windows keeps the same validation contract with before/after
    identity checks and delegates publication durability to ``MoveFileExW``.
    """

    def __init__(self, root: Path, *, create: bool = True) -> None:
        requested = canonical_platform_path(Path(root))
        if not requested.is_absolute():
            requested = requested.absolute()
        self.root = requested
        self._chain = self._capture_chain(create=create)

    @staticmethod
    def _parts(relative: str | Path) -> tuple[str, ...]:
        candidate = Path(relative)
        if candidate.is_absolute() or not candidate.parts:
            raise SecurePersistenceError("persistence path must be relative")
        parts = tuple(candidate.parts)
        if any(part in {"", ".", ".."} for part in parts):
            raise SecurePersistenceError("persistence path is unsafe")
        return parts

    def _capture_chain(self, *, create: bool) -> tuple[tuple[Path, _Identity], ...]:
        if os.name == "nt":
            captured = self._capture_windows_chain(create=create)
            if not captured or captured[-1][0] != self.root:
                raise SecurePersistenceError("persistence root is unavailable")
            if not _owned_directory(self.root.lstat()):
                raise SecurePersistenceError("persistence root is unsafe")
            return captured

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        current = Path(self.root.anchor)
        descriptors: list[int] = []
        posix_captured: list[tuple[Path, _Identity]] = []
        try:
            descriptor = os.open(current, flags)
            descriptors.append(descriptor)
            posix_captured.append((current, _identity(os.fstat(descriptor))))
            for component in self.root.parts[1:]:
                current = current / component
                try:
                    descriptor = os.open(component, flags, dir_fd=descriptors[-1])
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=descriptors[-1])
                    descriptor = os.open(component, flags, dir_fd=descriptors[-1])
                descriptors.append(descriptor)
                posix_captured.append((current, _identity(os.fstat(descriptor))))
            root_metadata = os.fstat(descriptors[-1])
            if not _owned_directory(root_metadata):
                raise SecurePersistenceError("persistence root is unsafe")
            os.fchmod(descriptors[-1], 0o700)
            return tuple(posix_captured)
        except OSError as error:
            if isinstance(error, SecurePersistenceError):
                raise
            raise SecurePersistenceError("persistence ancestor is unsafe") from error
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _capture_windows_chain(
        self,
        *,
        create: bool,
    ) -> tuple[tuple[Path, _Identity], ...]:
        current = Path(self.root.anchor)
        paths = [current]
        for component in self.root.parts[1:]:
            current = current / component
            paths.append(current)
        captured: list[tuple[Path, _Identity]] = []
        handles: list[Any] = []
        try:
            for index, path in enumerate(paths):
                try:
                    handle, file_id = _windows_open_directory(path)
                except FileNotFoundError:
                    if not create or index == 0:
                        raise
                    path.mkdir(mode=0o700)
                    handle, file_id = _windows_open_directory(path)
                handles.append(handle)
                captured.append((path, file_id))
            return tuple(captured)
        except OSError as error:
            if isinstance(error, SecurePersistenceError):
                raise
            raise SecurePersistenceError("persistence ancestor is unsafe") from error
        finally:
            for handle in reversed(handles):
                _windows_close_handle(handle)

    def _validate_windows_chain(self) -> None:
        for path, expected in self._chain:
            try:
                handle, current = _windows_open_directory(path)
            except OSError as error:
                raise SecurePersistenceError("persistence ancestor identity changed") from error
            try:
                if current != expected:
                    raise SecurePersistenceError("persistence ancestor identity changed")
            finally:
                _windows_close_handle(handle)

    @contextmanager
    def _windows_parent(
        self,
        relative: str | Path,
        *,
        create: bool,
    ) -> Iterator[tuple[Path, str]]:
        """Pin every Windows directory component until an operation completes."""

        parts = self._parts(relative)
        handles: list[Any] = []
        pinned: list[tuple[Path, _WindowsFileId]] = []
        try:
            try:
                for path, expected in self._chain:
                    handle, current = _windows_open_directory(path)
                    handles.append(handle)
                    pinned.append((path, current))
                    if current != expected:
                        raise SecurePersistenceError("persistence ancestor identity changed")
                parent = self.root
                for component in parts[:-1]:
                    parent /= component
                    try:
                        handle, current = _windows_open_directory(parent)
                    except FileNotFoundError:
                        if not create:
                            raise
                        parent.mkdir(mode=0o700)
                        handle, current = _windows_open_directory(parent)
                    handles.append(handle)
                    pinned.append((parent, current))
            except OSError as error:
                if isinstance(error, SecurePersistenceError):
                    raise
                raise SecurePersistenceError("persistence directory is unsafe") from error
            try:
                yield parent, parts[-1]
            finally:
                try:
                    for path, expected in pinned:
                        check_handle, current = _windows_open_directory(path)
                        try:
                            if current != expected:
                                raise SecurePersistenceError(
                                    "persistence directory identity changed"
                                )
                        finally:
                            _windows_close_handle(check_handle)
                except OSError as error:
                    if isinstance(error, SecurePersistenceError):
                        raise
                    raise SecurePersistenceError("persistence directory is unsafe") from error
        finally:
            for handle in reversed(handles):
                _windows_close_handle(handle)

    @contextmanager
    def _root_descriptor(self) -> Iterator[int]:
        if os.name == "nt":
            raise SecurePersistenceError("directory descriptors are unavailable on Windows")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[int] = []
        try:
            try:
                for index, (path, expected) in enumerate(self._chain):
                    if index == 0:
                        descriptor = os.open(path, flags)
                    else:
                        descriptor = os.open(
                            path.name,
                            flags,
                            dir_fd=descriptors[-1],
                        )
                    descriptors.append(descriptor)
                    if _identity(os.fstat(descriptor)) != expected:
                        raise SecurePersistenceError("persistence ancestor identity changed")
            except OSError as error:
                if isinstance(error, SecurePersistenceError):
                    raise
                raise SecurePersistenceError("persistence ancestor is unsafe") from error
            yield descriptors[-1]
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @contextmanager
    def _parent_descriptor(
        self, relative: str | Path, *, create: bool
    ) -> Iterator[tuple[int, str]]:
        parts = self._parts(relative)
        with self._root_descriptor() as root_descriptor:
            descriptors: list[int] = []
            current = root_descriptor
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                try:
                    for component in parts[:-1]:
                        try:
                            descriptor = os.open(component, flags, dir_fd=current)
                        except FileNotFoundError:
                            if not create:
                                raise
                            os.mkdir(component, mode=0o700, dir_fd=current)
                            descriptor = os.open(component, flags, dir_fd=current)
                        metadata = os.fstat(descriptor)
                        if not _owned_directory(metadata):
                            os.close(descriptor)
                            raise SecurePersistenceError("persistence child directory is unsafe")
                        descriptors.append(descriptor)
                        current = descriptor
                except OSError as error:
                    if isinstance(error, SecurePersistenceError):
                        raise
                    raise SecurePersistenceError("persistence child directory is unsafe") from error
                yield current, parts[-1]
            finally:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)

    @staticmethod
    def _stat_entry(parent_descriptor: int, name: str, *, required: bool) -> os.stat_result | None:
        try:
            metadata = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if required:
                raise FileNotFoundError(name) from None
            return None
        if stat.S_ISLNK(metadata.st_mode):
            raise SecurePersistenceError("persistence entry is symlinked")
        if not _owned_regular(metadata):
            raise SecurePersistenceError("persistence entry identity is unsafe")
        return metadata

    @staticmethod
    def _windows_stat_entry(
        parent: Path,
        name: str,
        *,
        required: bool,
    ) -> tuple[_Fingerprint, _WindowsFileId] | None:
        """Inspect one Windows child through a no-reparse native handle."""

        try:
            descriptor, file_id = _windows_open_file_descriptor(
                parent / name,
                os.O_RDONLY,
            )
        except FileNotFoundError:
            if required:
                raise
            return None
        try:
            metadata = os.fstat(descriptor)
            if not _owned_regular(metadata):
                raise SecurePersistenceError("persistence entry identity is unsafe")
            return _fingerprint(metadata), file_id
        finally:
            os.close(descriptor)

    def exists(self, relative: str | Path) -> bool:
        """Return whether one safe regular entry exists."""

        if os.name == "nt":
            with self._windows_parent(relative, create=False) as (parent, name):
                return (
                    self._windows_stat_entry(
                        parent,
                        name,
                        required=False,
                    )
                    is not None
                )
        with self._parent_descriptor(relative, create=False) as (parent, name):
            return self._stat_entry(parent, name, required=False) is not None

    def read_bytes(self, relative: str | Path) -> bytes:
        """Read one owned single-link regular file with name/inode continuity."""

        return self.read_bytes_bounded(relative, max_bytes=None)

    def read_bytes_bounded(
        self,
        relative: str | Path,
        *,
        max_bytes: int | None,
    ) -> bytes:
        """Read one safe file while enforcing an optional allocation bound."""

        if max_bytes is not None and (
            isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0
        ):
            raise ValueError("max_bytes must be a non-negative integer or None")

        if os.name == "nt":
            return self._read_bytes_windows(relative, max_bytes=max_bytes)
        with self._parent_descriptor(relative, create=False) as (parent, name):
            before = self._stat_entry(parent, name, required=True)
            assert before is not None
            if max_bytes is not None and before.st_size > max_bytes:
                raise SecurePersistenceError("persistence entry exceeds read limit")
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            try:
                opened = os.fstat(descriptor)
                if not _owned_regular(opened) or _read_fingerprint(opened) != _read_fingerprint(
                    before
                ):
                    raise SecurePersistenceError("persistence entry changed before read")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise SecurePersistenceError("persistence entry exceeds read limit")
                    chunks.append(chunk)
                after_open = os.fstat(descriptor)
                after_name = self._stat_entry(parent, name, required=True)
                assert after_name is not None
                if _read_fingerprint(after_open) != _read_fingerprint(opened) or _read_fingerprint(
                    after_name
                ) != _read_fingerprint(opened):
                    raise SecurePersistenceError("persistence entry changed during read")
                return b"".join(chunks)
            finally:
                os.close(descriptor)

    def _read_bytes_windows(
        self,
        relative: str | Path,
        *,
        max_bytes: int | None,
    ) -> bytes:
        with self._windows_parent(relative, create=False) as (parent, name):
            descriptor, opened_id = _windows_open_file_descriptor(
                parent / name,
                os.O_RDONLY,
            )
            try:
                opened = os.fstat(descriptor)
                if not _owned_regular(opened):
                    raise SecurePersistenceError("persistence entry identity is unsafe")
                if max_bytes is not None and opened.st_size > max_bytes:
                    raise SecurePersistenceError("persistence entry exceeds read limit")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise SecurePersistenceError("persistence entry exceeds read limit")
                    chunks.append(chunk)
                if (
                    _read_fingerprint(os.fstat(descriptor)) != _read_fingerprint(opened)
                    or _windows_descriptor_file_id(descriptor) != opened_id
                ):
                    raise SecurePersistenceError("persistence entry changed during read")
                named = self._windows_stat_entry(parent, name, required=True)
                assert named is not None
                if named[1] != opened_id:
                    raise SecurePersistenceError("persistence entry changed during read")
                return b"".join(chunks)
            finally:
                os.close(descriptor)

    @staticmethod
    def _write_all(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SecurePersistenceError("persistence write was incomplete")
            view = view[written:]

    def write_bytes_atomic(self, relative: str | Path, data: bytes) -> None:
        """Durably replace one entry below the pinned tree."""

        if os.name == "nt":
            self._write_bytes_windows(relative, data)
            return
        with self._parent_descriptor(relative, create=True) as (parent, name):
            destination_before = self._stat_entry(parent, name, required=False)
            temporary = f".{name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
            published = False
            try:
                self._write_all(descriptor, data)
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                temporary_open = os.fstat(descriptor)
                temporary_name = self._stat_entry(parent, temporary, required=True)
                assert temporary_name is not None
                if _fingerprint(temporary_open) != _fingerprint(temporary_name):
                    raise SecurePersistenceError("persistence temporary changed")
                destination_now = self._stat_entry(parent, name, required=False)
                if (
                    destination_before is None
                    and destination_now is not None
                    or destination_before is not None
                    and destination_now is None
                    or destination_before is not None
                    and destination_now is not None
                    and _fingerprint(destination_before) != _fingerprint(destination_now)
                ):
                    raise SecurePersistenceError(
                        "persistence destination changed before publication"
                    )
                os.replace(
                    temporary,
                    name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
                published = True
                destination_after = self._stat_entry(parent, name, required=True)
                assert destination_after is not None
                if _fingerprint(destination_after) != _fingerprint(temporary_open):
                    raise SecurePersistenceError(
                        "persistence destination changed during publication"
                    )
                os.fsync(parent)
            finally:
                os.close(descriptor)
                if not published:
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except FileNotFoundError:
                        pass

    def _write_bytes_windows(self, relative: str | Path, data: bytes) -> None:
        with self._windows_parent(relative, create=True) as (parent, name):
            destination = parent / name
            before = self._windows_stat_entry(parent, name, required=False)
            temporary = parent / (f".{name}.{os.getpid()}.{secrets.token_hex(16)}.tmp")
            try:
                descriptor, temporary_id = _windows_open_file_descriptor(
                    temporary,
                    os.O_WRONLY,
                    disposition=_CREATE_NEW,
                )
                try:
                    self._write_all(descriptor, data)
                    os.fsync(descriptor)
                    temporary_fingerprint = _fingerprint(os.fstat(descriptor))
                    if _windows_descriptor_file_id(descriptor) != temporary_id:
                        raise SecurePersistenceError("persistence temporary changed")
                finally:
                    os.close(descriptor)
                temporary_named = self._windows_stat_entry(
                    parent,
                    temporary.name,
                    required=True,
                )
                assert temporary_named is not None
                if (
                    temporary_named[0] != temporary_fingerprint
                    or temporary_named[1] != temporary_id
                ):
                    raise SecurePersistenceError("persistence temporary changed")
                current = self._windows_stat_entry(parent, name, required=False)
                if current != before:
                    raise SecurePersistenceError(
                        "persistence destination changed before publication"
                    )
                replace_file_durable(temporary, destination)
                after = self._windows_stat_entry(parent, name, required=True)
                assert after is not None
                if after[0] != temporary_fingerprint or after[1] != temporary_id:
                    raise SecurePersistenceError(
                        "persistence destination changed during publication"
                    )
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def unlink(self, relative: str | Path, *, missing_ok: bool = False) -> None:
        """Remove one safe entry and durably flush the containing directory."""

        if os.name == "nt":
            with self._windows_parent(relative, create=False) as (parent, name):
                windows_before = self._windows_stat_entry(
                    parent,
                    name,
                    required=not missing_ok,
                )
                if windows_before is None:
                    return
                windows_current = self._windows_stat_entry(
                    parent,
                    name,
                    required=True,
                )
                if windows_current != windows_before:
                    raise SecurePersistenceError("persistence entry changed before unlink")
                (parent / name).unlink()
            return
        try:
            with self._parent_descriptor(relative, create=False) as (parent, name):
                posix_before = self._stat_entry(parent, name, required=not missing_ok)
                if posix_before is None:
                    return
                current = self._stat_entry(parent, name, required=True)
                assert current is not None
                if _fingerprint(current) != _fingerprint(posix_before):
                    raise SecurePersistenceError("persistence entry changed before unlink")
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def open_lock(self, relative: str | Path) -> int:
        """Open or create one owned single-link lock file below the pinned tree."""

        if os.name == "nt":
            with self._windows_parent(relative, create=True) as (parent, name):
                windows_before = self._windows_stat_entry(
                    parent,
                    name,
                    required=False,
                )
                descriptor, opened_id = _windows_open_file_descriptor(
                    parent / name,
                    os.O_RDWR,
                    disposition=_OPEN_ALWAYS,
                )
                try:
                    opened = os.fstat(descriptor)
                    windows_after = self._windows_stat_entry(
                        parent,
                        name,
                        required=True,
                    )
                    assert windows_after is not None
                    if (
                        not _owned_regular(opened)
                        or windows_after[1] != opened_id
                        or windows_before is not None
                        and windows_before[1] != opened_id
                    ):
                        raise SecurePersistenceError("persistence lock identity is unsafe")
                    return descriptor
                except Exception:
                    os.close(descriptor)
                    raise
        with self._parent_descriptor(relative, create=True) as (parent, name):
            before = self._stat_entry(parent, name, required=False)
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            if before is None:
                flags |= os.O_CREAT | os.O_EXCL
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=parent)
            except FileExistsError as error:
                raise SecurePersistenceError("persistence lock changed before open") from error
            try:
                opened = os.fstat(descriptor)
                after = self._stat_entry(parent, name, required=True)
                assert after is not None
                if (
                    not _owned_regular(opened)
                    or _fingerprint(opened) != _fingerprint(after)
                    or before is not None
                    and _fingerprint(before) != _fingerprint(opened)
                ):
                    raise SecurePersistenceError("persistence lock identity is unsafe")
                os.fchmod(descriptor, 0o600)
                return descriptor
            except Exception:
                os.close(descriptor)
                raise

    def validate_open_file(self, relative: str | Path, descriptor: int) -> None:
        """Require an open descriptor to remain selected by its pinned name."""

        opened = os.fstat(descriptor)
        if not _owned_regular(opened):
            raise SecurePersistenceError("persistence open file identity is unsafe")
        if os.name == "nt":
            with self._windows_parent(relative, create=False) as (parent, name):
                named = self._windows_stat_entry(parent, name, required=True)
                assert named is not None
                if named[0] != _fingerprint(opened) or named[1] != _windows_descriptor_file_id(
                    descriptor
                ):
                    raise SecurePersistenceError("persistence open file identity changed")
            return
        with self._parent_descriptor(relative, create=False) as (parent, name):
            posix_named = self._stat_entry(parent, name, required=True)
            assert posix_named is not None
            if _fingerprint(posix_named) != _fingerprint(opened):
                raise SecurePersistenceError("persistence open file identity changed")
