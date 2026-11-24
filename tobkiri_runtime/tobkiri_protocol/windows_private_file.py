"""Create Windows files with owner-only access from their first instant."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from typing import Any, Iterator

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_SDDL_REVISION_1 = 1


class SecurityAttributes(ctypes.Structure):
    """Win32 SECURITY_ATTRIBUTES with pointer-width-correct field alignment."""

    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", ctypes.c_uint32)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("user", _SidAndAttributes)]


def _security_apis() -> tuple[Any, Any]:
    """Load native APIs lazily, keeping imports portable to non-Windows hosts."""

    win_dll: Any = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise OSError("Windows private-file security APIs are unavailable")
    kernel32 = win_dll("kernel32", use_last_error=True)
    advapi32 = win_dll("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.OpenProcessToken.restype = ctypes.c_int
    advapi32.GetTokenInformation.argtypes = (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    )
    advapi32.GetTokenInformation.restype = ctypes.c_int
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    return kernel32, advapi32


def _last_error() -> int:
    return int(getattr(ctypes, "get_last_error", lambda: 0)())


def _api_error(operation: str, code: int | None = None) -> OSError:
    if code is None:
        code = _last_error()
    return OSError(code, f"{operation} failed with Windows error {code}")


@contextmanager
def owner_only_security_attributes() -> Iterator[SecurityAttributes]:
    """Yield creation attributes with one protected current-process-user grant.

    Pass the yielded structure by reference to CreateFileW with CREATE_NEW.
    Its descriptor stays allocated until this context exits. The owner is the
    process-token user, matching the separately launched ACL verifier, and the
    sole ACE grants that user FullControl without inheritance or propagation.
    No filesystem ACL is modified by this helper.
    """

    kernel32, advapi32 = _security_apis()
    token = ctypes.c_void_p()
    sid_string = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    try:
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            raise _api_error("OpenProcessToken")
        if not token.value:
            raise OSError("OpenProcessToken returned an empty handle")

        required = ctypes.c_uint32()
        queried = advapi32.GetTokenInformation(
            token, _TOKEN_USER, None, 0, ctypes.byref(required)
        )
        error = _last_error()
        if queried or error != _ERROR_INSUFFICIENT_BUFFER:
            raise _api_error("GetTokenInformation size query", error)
        if required.value < ctypes.sizeof(_TokenUser) or required.value > 1024 * 1024:
            raise OSError("GetTokenInformation returned an invalid buffer size")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token, _TOKEN_USER, buffer, ctypes.sizeof(buffer), ctypes.byref(required)
        ):
            raise _api_error("GetTokenInformation")
        if required.value < ctypes.sizeof(_TokenUser) or required.value > ctypes.sizeof(buffer):
            raise OSError("GetTokenInformation returned an invalid user record")
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        if not user.user.sid:
            raise OSError("GetTokenInformation returned an empty user SID")
        if not advapi32.ConvertSidToStringSidW(user.user.sid, ctypes.byref(sid_string)):
            raise _api_error("ConvertSidToStringSidW")
        if not sid_string.value:
            raise OSError("ConvertSidToStringSidW returned an empty SID")
        sid = ctypes.wstring_at(sid_string.value)
        sddl = f"O:{sid}D:P(A;;FA;;;{sid})"
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise _api_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        if not descriptor.value:
            raise OSError("Windows returned an empty security descriptor")

        yield SecurityAttributes(
            ctypes.sizeof(SecurityAttributes), descriptor.value, 0
        )
    finally:
        cleanup_error: OSError | None = None
        for allocation in (descriptor, sid_string):
            if allocation.value and kernel32.LocalFree(allocation):
                if cleanup_error is None:
                    cleanup_error = _api_error("LocalFree")
        if token.value and not kernel32.CloseHandle(token):
            if cleanup_error is None:
                cleanup_error = _api_error("CloseHandle")
        if cleanup_error is not None:
            raise cleanup_error
