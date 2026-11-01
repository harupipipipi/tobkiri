"""Windows private VM roots and stable, no-reparse artifact reads."""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, BinaryIO, cast

from tobkiri_protocol.secure_persistence import SecureDirectory

from .windows_abi_types import WindowsCRuntime, WindowsCTypes, WindowsOS

_HANDLE = ctypes.c_void_p
_DWORD = ctypes.c_uint32


def _security_api() -> tuple[Any, Any]:
    loader = cast(WindowsCTypes, ctypes).WinDLL
    advapi = loader("advapi32", use_last_error=True, winmode=0x800)
    kernel = loader("kernel32", use_last_error=True, winmode=0x800)
    specifications = {
        "OpenProcessToken": ([_HANDLE, _DWORD, ctypes.POINTER(_HANDLE)], ctypes.c_int),
        "GetTokenInformation": (
            [_HANDLE, ctypes.c_int, _HANDLE, _DWORD, ctypes.POINTER(_DWORD)],
            ctypes.c_int,
        ),
        "ConvertSidToStringSidW": ([_HANDLE, ctypes.POINTER(ctypes.c_wchar_p)], ctypes.c_int),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
            [ctypes.c_wchar_p, _DWORD, ctypes.POINTER(_HANDLE), ctypes.POINTER(_DWORD)],
            ctypes.c_int,
        ),
        "SetFileSecurityW": ([ctypes.c_wchar_p, _DWORD, _HANDLE], ctypes.c_int),
        "GetNamedSecurityInfoW": (
            [
                ctypes.c_wchar_p,
                ctypes.c_int,
                _DWORD,
                ctypes.POINTER(_HANDLE),
                ctypes.POINTER(_HANDLE),
                ctypes.POINTER(_HANDLE),
                ctypes.POINTER(_HANDLE),
                ctypes.POINTER(_HANDLE),
            ],
            _DWORD,
        ),
        "GetSecurityDescriptorControl": (
            [_HANDLE, ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(_DWORD)],
            ctypes.c_int,
        ),
        "EqualSid": ([_HANDLE, _HANDLE], ctypes.c_int),
        "GetAce": ([_HANDLE, _DWORD, ctypes.POINTER(_HANDLE)], ctypes.c_int),
    }
    for name, (args, result) in specifications.items():
        fn = getattr(advapi, name)
        fn.argtypes, fn.restype = args, result
    kernel.GetCurrentProcess.restype = _HANDLE
    kernel.CloseHandle.argtypes = [_HANDLE]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [_HANDLE], _HANDLE
    return advapi, kernel


@contextmanager
def _user_sid() -> Iterator[tuple[Any, Any, Any, str]]:
    advapi, kernel = _security_api()
    token = _HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
        raise OSError("Windows current-user token is unavailable")
    try:
        needed = _DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not 0 < needed.value <= 65536:
            raise OSError("Windows current-user SID length is invalid")
        content = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, content, needed, ctypes.byref(needed)):
            raise OSError("Windows current-user SID is unavailable")
        sid = ctypes.cast(content, ctypes.POINTER(_HANDLE))[0]
        value = ctypes.c_wchar_p()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise OSError("Windows current-user SID cannot be encoded")
        try:
            sid_text = value.value
            if not sid_text:
                raise OSError("Windows current-user SID encoding is empty")
            yield advapi, kernel, sid, sid_text
        finally:
            kernel.LocalFree(ctypes.cast(value, _HANDLE))
    finally:
        kernel.CloseHandle(token)


def private_directory(path: Path, *, create: bool = True) -> SecureDirectory:
    """Create an owner-only root or verify its existing protected Windows ACL."""
    existed = path.exists()
    storage = SecureDirectory(path, create=create)
    if os.name != "nt":
        # Used only by deterministic cross-platform tests and bundle tooling.
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("PackVM private directory permissions are unsafe")
        return storage
    with _user_sid() as (advapi, kernel, sid, sid_text):
        if not existed:
            descriptor = _HANDLE()
            sddl = f"O:{sid_text}D:P(A;OICI;FA;;;{sid_text})"
            if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl,
                1,
                ctypes.byref(descriptor),
                None,
            ):
                raise OSError("PackVM private ACL could not be constructed")
            try:
                if not advapi.SetFileSecurityW(str(path), 0x80000005, descriptor):
                    raise OSError("PackVM private ACL could not be installed")
            finally:
                kernel.LocalFree(descriptor)
        owner, dacl, descriptor = _HANDLE(), _HANDLE(), _HANDLE()
        if advapi.GetNamedSecurityInfoW(
            str(path),
            1,
            0x5,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        ):
            raise OSError("PackVM private ACL cannot be verified")
        try:
            control, revision = ctypes.c_uint16(), _DWORD()
            if (
                not advapi.GetSecurityDescriptorControl(
                    descriptor, ctypes.byref(control), ctypes.byref(revision)
                )
                or not control.value & 0x1000
                or not advapi.EqualSid(owner, sid)
                or not dacl
            ):
                raise ValueError("PackVM directory ownership or ACL protection changed")
            dacl_address = dacl.value
            if dacl_address is None:
                raise ValueError("PackVM directory ACL is null")
            # ACL header: revision, sbz1, size, ace_count, sbz2.
            count = ctypes.c_uint16.from_address(dacl_address + 4).value
            ace = _HANDLE()
            if count != 1 or not advapi.GetAce(dacl, 0, ctypes.byref(ace)):
                raise ValueError("PackVM directory ACL contains unexpected principals")
            ace_address = ace.value
            if ace_address is None:
                raise ValueError("PackVM directory ACL entry is null")
            kind = ctypes.c_ubyte.from_address(ace_address).value
            flags = ctypes.c_ubyte.from_address(ace_address + 1).value
            rights = _DWORD.from_address(ace_address + 4).value
            if (
                kind != 0
                or flags != 3
                or rights != 0x1F01FF
                or not advapi.EqualSid(ace_address + 8, sid)
            ):
                raise ValueError("PackVM directory ACL is not owner-only inheritable access")
        finally:
            kernel.LocalFree(descriptor)
    return storage


@contextmanager
def _stable_file(path: Path) -> Iterator[BinaryIO]:
    """Hold a regular single-link file against rename/write while it is read."""
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("PackVM asset path must be canonical")
    for component in (path, *path.parents):
        metadata = component.lstat()
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("PackVM path contains a reparse point")
    if os.name == "nt":
        import msvcrt

        from tobkiri_protocol.secure_persistence import _ByHandleFileInformation, _windows_kernel32

        api = _windows_kernel32()
        handle = api.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise OSError("PackVM asset cannot be pinned for reading")
        info = _ByHandleFileInformation()
        if (
            not api.GetFileInformationByHandle(handle, ctypes.byref(info))
            or info.file_attributes & (0x10 | 0x400)
            or info.number_of_links != 1
        ):
            api.CloseHandle(handle)
            raise ValueError("PackVM asset identity is unsafe")
        try:
            fd = cast(WindowsCRuntime, msvcrt).open_osfhandle(int(handle), os.O_RDONLY | cast(WindowsOS, os).O_BINARY)
        except BaseException:
            api.CloseHandle(handle)
            raise
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("PackVM asset must be a single-link regular file")
        yield stream


def file_digest(path: Path) -> str:
    """Hash exact stable bytes, rejecting mutation during the read."""
    with stable_file(path) as stream:
        before = os.fstat(stream.fileno())
        digest = _stream_hash(stream)
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, f) != getattr(after, f) for f in fields):
            raise ValueError("PackVM file changed while hashing")
        return "sha256:" + digest.hexdigest()


def _stream_hash(stream: BinaryIO) -> Any:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest


@contextmanager
def stable_file(path: Path) -> Iterator[BinaryIO]:
    """Pin every Windows ancestor and the leaf against rename for the read."""
    with ExitStack() as pins:
        if os.name == "nt":
            storage = SecureDirectory(path.parent, create=False)
            pins.enter_context(storage._windows_parent(path.name, create=False))
        with _stable_file(path) as stream:
            yield stream
