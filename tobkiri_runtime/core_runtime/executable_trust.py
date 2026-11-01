"""Host-side trust checks for fixed native executable paths.

The checks deliberately do not infer trust from an installation directory.
Instead, they bind the resolved file, its bytes, and the operating system's
write controls at capture time and re-check the same identity before use.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
from typing import Any


class ExecutableTrustError(PermissionError):
    """Raised when a native executable cannot be safely bound to the Host."""


@dataclass(frozen=True)
class WindowsPathSecurity:
    """DACL evidence for one Windows file-system object.

    ``principal_rights`` contains effective rights for broad, untrusted
    principals. ``dacl_principal_rights`` covers every concrete trustee SID
    enumerated from the DACL after ordered deny evaluation. Both are explicit
    injected values in tests so Windows policy is exercised on every platform
    without pretending POSIX mode bits describe a Windows ACL.
    """

    owner_sid: str
    caller_sid: str
    caller_rights: int
    descriptor_sha256: str
    principal_rights: tuple[tuple[str, int], ...]
    dacl_principal_rights: tuple[tuple[str, int], ...] = ()


_WINDOWS_FILE_MUTATION_RIGHTS = (
    0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
    | 0x00000002  # FILE_WRITE_DATA
    | 0x00000004  # FILE_APPEND_DATA
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
)
_WINDOWS_DIRECTORY_OBJECT_MUTATION_RIGHTS = (
    0x00010000  # DELETE this directory through its parent
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
)
_WINDOWS_PARENT_CHILD_REPLACEMENT_RIGHTS = 0x00000040  # FILE_DELETE_CHILD
_WINDOWS_TRUSTED_OWNER_SIDS = frozenset(
    {
        "S-1-5-18",  # LocalSystem
        "S-1-5-32-544",  # Builtin Administrators
        # NT SERVICE\TrustedInstaller.  Use the immutable SID, never a localized
        # account name or an installation-directory allowlist.
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }
)
_WINDOWS_TRUSTED_DACL_PRINCIPAL_SIDS = _WINDOWS_TRUSTED_OWNER_SIDS | frozenset(
    {
        # OWNER RIGHTS resolves to the already-validated system owner; it
        # is not an independently logon-capable principal.
        "S-1-3-4",
    }
)
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_INHERIT_ONLY_ACE = 0x08
_WINDOWS_SIMPLE_ACCESS_ACE_TYPES = frozenset({0, 1, 9, 10})
_WINDOWS_OBJECT_ACCESS_ACE_TYPES = frozenset({5, 6, 11, 12})
_WINDOWS_NON_ACCESS_ACE_TYPES = frozenset({2, 3, 7, 8, 13, 14, 15, 17, 18, 19})
_POSIX_TRUSTED_OWNER_UIDS = frozenset({0})


def capture_trusted_executable(value: str | Path) -> tuple[Path, dict[str, str]]:
    """Return a non-replaceable executable and its immutable identity.

    The result is suitable for capture-to-invoke comparison.  A path prefix
    (including ``Program Files``) is never treated as a trust decision.
    """

    path = _resolve_regular_executable(value)
    try:
        info = path.stat()
    except OSError as exc:
        raise ExecutableTrustError("native executable identity is unavailable") from exc
    if _is_windows_platform():
        evidence = _assert_windows_non_replaceable(path)
        identity = {
            "platform": "windows",
            "path": str(path),
            "device": str(info.st_dev),
            "inode": str(info.st_ino),
            "mode": str(info.st_mode),
            "size": str(info.st_size),
            "mtime_ns": str(info.st_mtime_ns),
            "owner_sid": evidence.owner_sid,
            "caller_sid": evidence.caller_sid,
            "security_descriptor_sha256": evidence.descriptor_sha256,
            "ancestry_security_sha256": _windows_ancestry_digest(path),
        }
    else:
        _assert_posix_non_replaceable(path)
        identity = {
            "platform": "posix",
            "path": str(path),
            "device": str(info.st_dev),
            "inode": str(info.st_ino),
            "mode": str(info.st_mode),
            "uid": str(info.st_uid),
            "gid": str(info.st_gid),
            "size": str(info.st_size),
            "mtime_ns": str(info.st_mtime_ns),
        }
    identity["sha256"] = _file_sha256(path)
    return path, identity


def trusted_executable_path(
    value: str | Path,
    *,
    expected_identity: Mapping[str, Any] | None = None,
) -> Path:
    """Re-check one executable and optionally require its captured identity."""

    path, identity = capture_trusted_executable(value)
    if expected_identity is not None and dict(expected_identity) != identity:
        raise ExecutableTrustError("native executable identity changed")
    return path


def _resolve_regular_executable(value: str | Path) -> Path:
    """Resolve one absolute, regular, non-symlink executable path."""

    candidate = Path(str(value or ""))
    if not candidate.is_absolute():
        raise ExecutableTrustError("native executable path is not absolute")
    _assert_no_symlink_components(candidate)
    if _is_windows_platform():
        _assert_no_windows_reparse_components(candidate)
    try:
        resolved = candidate.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise ExecutableTrustError("native executable identity is unavailable") from exc
    if (
        not resolved.is_file()
        or resolved.is_symlink()
        or not os.access(resolved, os.X_OK)
    ):
        raise ExecutableTrustError("native executable is not executable")
    if info.st_size <= 0:
        raise ExecutableTrustError("native executable is empty")
    return resolved


def _is_windows_platform() -> bool:
    """Return whether the active Host uses Windows native ACL semantics."""

    return os.name == "nt"


def _assert_no_symlink_components(path: Path) -> None:
    """Reject an executable path whose supplied path traverses a symlink."""

    current = path
    while True:
        try:
            if current.is_symlink():
                raise ExecutableTrustError("native executable symlink is denied")
        except OSError as exc:
            raise ExecutableTrustError("native executable path is unavailable") from exc
        if current == current.parent:
            return
        current = current.parent


def _assert_posix_non_replaceable(path: Path) -> None:
    """Require a root-owned, non-writable executable ancestry on POSIX.

    Owner mode bits are not a sufficient trust boundary: an ordinary owner
    can chmod and replace a nominally ``0755`` file or ancestor after capture.
    The supported macOS and Linux packaged toolchains live below root-owned
    system paths (for example ``/usr/bin``), so accepting only UID 0 keeps the
    capture-to-invoke identity meaningful without a user-controlled allowlist.
    """

    current = path
    while True:
        try:
            info = current.stat()
        except OSError as exc:
            raise ExecutableTrustError(
                "native executable ancestry is unavailable"
            ) from exc
        if (
            current.is_symlink()
            or info.st_uid not in _POSIX_TRUSTED_OWNER_UIDS
            or info.st_mode & 0o022
        ):
            raise ExecutableTrustError(
                "native executable is writable by an untrusted principal"
            )
        if current == current.parent:
            return
        current = current.parent


def _assert_no_windows_reparse_components(path: Path) -> None:
    """Reject junctions and every other Windows reparse-point traversal."""

    current = path
    while True:
        if _windows_file_attributes(current) & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
            raise ExecutableTrustError("native executable reparse point is denied")
        if current == current.parent:
            return
        current = current.parent


def _windows_file_attributes(path: Path) -> int:
    """Return native file attributes for one supplied Windows path component."""

    # Cross-platform unit tests inject this seam.  Production Windows always
    # takes the GetFileAttributesW branch.
    if os.name != "nt":
        return 0
    try:
        kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        kernel.GetFileAttributesW.restype = wintypes.DWORD
        attributes = int(kernel.GetFileAttributesW(str(path)))
    except (AttributeError, OSError, ValueError, ctypes.ArgumentError) as exc:
        raise ExecutableTrustError(
            "native executable Windows attributes are unavailable"
        ) from exc
    if attributes == 0xFFFFFFFF:
        raise ExecutableTrustError(
            "native executable Windows attributes are unavailable"
        )
    return attributes


def _assert_windows_non_replaceable(path: Path) -> WindowsPathSecurity:
    """Reject file/ancestor DACLs that let broad users replace the executable."""

    evidence = _windows_path_security(path)
    _assert_windows_rights_safe(evidence, _WINDOWS_FILE_MUTATION_RIGHTS)
    current = path.parent
    while True:
        parent_evidence = _windows_path_security(current)
        _assert_windows_rights_safe(
            parent_evidence,
            _WINDOWS_DIRECTORY_OBJECT_MUTATION_RIGHTS
            | _WINDOWS_PARENT_CHILD_REPLACEMENT_RIGHTS,
        )
        if current == current.parent:
            return evidence
        current = current.parent


def _assert_windows_rights_safe(
    evidence: WindowsPathSecurity,
    denied_rights: int,
) -> None:
    """Fail closed when a broad Windows principal can mutate the object."""

    if not evidence.owner_sid or not evidence.descriptor_sha256:
        raise ExecutableTrustError("native executable Windows owner is unavailable")
    if evidence.owner_sid not in _WINDOWS_TRUSTED_OWNER_SIDS:
        raise ExecutableTrustError(
            "native executable Windows owner is not a trusted system principal"
        )
    if not evidence.caller_sid:
        raise ExecutableTrustError("native executable Windows caller is unavailable")
    if int(evidence.caller_rights) & denied_rights:
        raise ExecutableTrustError("native executable is writable by the Host caller")
    for _principal, rights in evidence.principal_rights:
        if int(rights) & denied_rights:
            raise ExecutableTrustError(
                "native executable is writable by an untrusted principal"
            )
    for principal_sid, rights in evidence.dacl_principal_rights:
        if (
            principal_sid not in _WINDOWS_TRUSTED_DACL_PRINCIPAL_SIDS
            and int(rights) & denied_rights
        ):
            raise ExecutableTrustError(
                "native executable DACL grants mutation to an untrusted principal"
            )


def _windows_ancestry_digest(path: Path) -> str:
    """Hash Windows owner/DACL evidence for every parent used in resolution."""

    digest = hashlib.sha256()
    current = path.parent
    while True:
        evidence = _windows_path_security(current)
        digest.update(str(current).encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
        digest.update(evidence.owner_sid.encode("ascii", "strict"))
        digest.update(b"\0")
        digest.update(evidence.descriptor_sha256.encode("ascii", "strict"))
        if current == current.parent:
            return digest.hexdigest()
        current = current.parent


def _file_sha256(path: Path) -> str:
    """Return a bounded-memory digest of exact executable bytes."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ExecutableTrustError("native executable bytes are unavailable") from exc
    return digest.hexdigest()


def _windows_path_security(path: Path) -> WindowsPathSecurity:
    """Read owner, DACL fingerprint, and every trustee's effective rights.

    This function is the small OS seam used by tests.  The Windows APIs are
    called only on Windows; any unavailable API or malformed descriptor is a
    fail-closed trust error rather than a POSIX permission-bit fallback.
    """

    if not _is_windows_platform():
        raise ExecutableTrustError("Windows executable security is unavailable")
    try:
        return _windows_path_security_ctypes(path)
    except (AttributeError, OSError, ValueError, ctypes.ArgumentError) as exc:
        raise ExecutableTrustError(
            "Windows executable security is unavailable"
        ) from exc


def _windows_path_security_ctypes(path: Path) -> WindowsPathSecurity:
    """Implement the Windows DACL check with advapi32/Kernel32 primitives."""

    win_dll = getattr(ctypes, "WinDLL")
    advapi = win_dll("advapi32", use_last_error=True)
    kernel = win_dll("kernel32", use_last_error=True)
    dword = wintypes.DWORD
    pointer = ctypes.c_void_p

    class TrusteeW(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", pointer),
            ("MultipleTrusteeOperation", dword),
            ("TrusteeForm", dword),
            ("TrusteeType", dword),
            ("ptstrName", pointer),
        ]

    class GenericMapping(ctypes.Structure):
        _fields_ = [
            ("GenericRead", dword),
            ("GenericWrite", dword),
            ("GenericExecute", dword),
            ("GenericAll", dword),
        ]

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", dword),
            ("AclBytesInUse", dword),
            ("AclBytesFree", dword),
        ]

    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        dword,
        dword,
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
    ]
    advapi.GetNamedSecurityInfoW.restype = dword
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorLength.argtypes = [pointer]
    advapi.GetSecurityDescriptorLength.restype = dword
    advapi.GetEffectiveRightsFromAclW.argtypes = [
        pointer,
        ctypes.POINTER(TrusteeW),
        ctypes.POINTER(dword),
    ]
    advapi.GetEffectiveRightsFromAclW.restype = dword
    advapi.GetAclInformation.argtypes = [
        pointer,
        pointer,
        dword,
        dword,
    ]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [pointer, dword, ctypes.POINTER(pointer)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.IsValidSid.argtypes = [pointer]
    advapi.IsValidSid.restype = wintypes.BOOL
    advapi.GetLengthSid.argtypes = [pointer]
    advapi.GetLengthSid.restype = dword
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, dword, ctypes.POINTER(pointer)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.DuplicateToken.argtypes = [wintypes.HANDLE, dword, ctypes.POINTER(pointer)]
    advapi.DuplicateToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        dword,
        pointer,
        dword,
        ctypes.POINTER(dword),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.AccessCheck.argtypes = [
        pointer,
        wintypes.HANDLE,
        dword,
        ctypes.POINTER(GenericMapping),
        pointer,
        ctypes.POINTER(dword),
        ctypes.POINTER(dword),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi.AccessCheck.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer

    owner = pointer()
    dacl = pointer()
    descriptor = pointer()
    result = advapi.GetNamedSecurityInfoW(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000001
        | 0x00000004,  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0 or not owner.value or not dacl.value or not descriptor.value:
        raise OSError("GetNamedSecurityInfoW failed")
    try:
        owner_sid = _windows_sid_text(advapi, kernel, owner)
        descriptor_size = int(advapi.GetSecurityDescriptorLength(descriptor))
        if descriptor_size <= 0:
            raise ValueError("security descriptor is empty")
        descriptor_sha256 = hashlib.sha256(
            ctypes.string_at(descriptor, descriptor_size)
        ).hexdigest()
        caller_sid, caller_rights = _windows_caller_effective_access(
            advapi,
            kernel,
            descriptor,
            GenericMapping,
            dword,
        )
        dacl_principal_rights = _windows_dacl_principal_rights(
            advapi,
            kernel,
            dacl,
            TrusteeW,
            AclSizeInformation,
            dword,
        )
        return WindowsPathSecurity(
            owner_sid=owner_sid,
            caller_sid=caller_sid,
            caller_rights=caller_rights,
            descriptor_sha256=descriptor_sha256,
            # Retained as an injection seam for compatibility tests. Native
            # evidence enumerates every DACL trustee below, including broad
            # principals such as Everyone and Authenticated Users.
            principal_rights=(),
            dacl_principal_rights=dacl_principal_rights,
        )
    finally:
        kernel.LocalFree(descriptor)


def _windows_caller_effective_access(
    advapi: Any,
    kernel: Any,
    descriptor: Any,
    generic_mapping_type: type[ctypes.Structure],
    dword: Any,
) -> tuple[str, int]:
    """Return the actual Host caller SID and AccessCheck-granted file rights.

    AccessCheck evaluates the complete token (including deny ACEs and groups)
    and applies Windows owner semantics.  In particular, an object owner has
    implicit WRITE_DAC authority even when that bit is absent from its DACL.
    """

    process_token = ctypes.c_void_p()
    impersonation_token = ctypes.c_void_p()
    if not advapi.OpenProcessToken(
        kernel.GetCurrentProcess(),
        0x0008 | 0x0002,  # TOKEN_QUERY | TOKEN_DUPLICATE
        ctypes.byref(process_token),
    ):
        raise OSError("OpenProcessToken failed")
    try:
        if not advapi.DuplicateToken(
            process_token,
            2,  # SecurityImpersonation
            ctypes.byref(impersonation_token),
        ):
            raise OSError("DuplicateToken failed")
        caller_sid = _windows_token_user_sid(advapi, kernel, process_token, dword)
        mapping = generic_mapping_type(
            0x00120089,  # FILE_GENERIC_READ
            0x00120116,  # FILE_GENERIC_WRITE
            0x001200A0,  # FILE_GENERIC_EXECUTE
            0x001F01FF,  # FILE_ALL_ACCESS
        )
        privilege_size = dword(0)
        granted = dword(0)
        access_status = wintypes.BOOL(False)
        advapi.AccessCheck(
            descriptor,
            impersonation_token,
            0x02000000,  # MAXIMUM_ALLOWED
            ctypes.byref(mapping),
            None,
            ctypes.byref(privilege_size),
            ctypes.byref(granted),
            ctypes.byref(access_status),
        )
        if int(privilege_size.value) <= 0:
            raise OSError("AccessCheck privilege sizing failed")
        privileges = ctypes.create_string_buffer(int(privilege_size.value))
        if not advapi.AccessCheck(
            descriptor,
            impersonation_token,
            0x02000000,  # MAXIMUM_ALLOWED
            ctypes.byref(mapping),
            ctypes.byref(privileges),
            ctypes.byref(privilege_size),
            ctypes.byref(granted),
            ctypes.byref(access_status),
        ):
            raise OSError("AccessCheck failed")
        if not access_status.value:
            return caller_sid, 0
        return caller_sid, int(granted.value)
    finally:
        if impersonation_token.value:
            kernel.CloseHandle(impersonation_token)
        if process_token.value:
            kernel.CloseHandle(process_token)


def _windows_token_user_sid(
    advapi: Any,
    kernel: Any,
    token: Any,
    dword: Any,
) -> str:
    """Read the immutable user SID from one Windows access token."""

    size = dword(0)
    advapi.GetTokenInformation(
        token,
        1,  # TokenUser
        None,
        0,
        ctypes.byref(size),
    )
    if int(size.value) <= 0:
        raise OSError("GetTokenInformation size lookup failed")
    token_user = ctypes.create_string_buffer(int(size.value))
    if not advapi.GetTokenInformation(
        token,
        1,  # TokenUser
        ctypes.byref(token_user),
        size,
        ctypes.byref(size),
    ):
        raise OSError("GetTokenInformation failed")
    sid = ctypes.cast(token_user, ctypes.POINTER(ctypes.c_void_p)).contents
    if not sid.value:
        raise OSError("Windows token user SID is unavailable")
    return _windows_sid_text(advapi, kernel, sid)


def _windows_sid_text(advapi: Any, kernel: Any, owner: Any) -> str:
    """Convert a Windows SID into immutable identity text."""

    text = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(owner, ctypes.byref(text)) or not text.value:
        raise OSError("ConvertSidToStringSidW failed")
    try:
        return str(text.value)
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _windows_effective_rights_for_sid(
    advapi: Any,
    dacl: Any,
    sid: Any,
    trustee_type: type[ctypes.Structure],
    dword: Any,
) -> int:
    """Evaluate ordered allow/deny ACEs for one concrete DACL trustee SID."""

    trustee = trustee_type(
        None,
        0,  # NO_MULTIPLE_TRUSTEE
        0,  # TRUSTEE_IS_SID
        0,  # TRUSTEE_IS_UNKNOWN
        ctypes.cast(sid, ctypes.c_void_p),
    )
    rights = dword(0)
    if (
        advapi.GetEffectiveRightsFromAclW(
            dacl, ctypes.byref(trustee), ctypes.byref(rights)
        )
        != 0
    ):
        raise OSError("GetEffectiveRightsFromAclW failed")
    return int(rights.value)


def _windows_dacl_principal_rights(
    advapi: Any,
    kernel: Any,
    dacl: Any,
    trustee_type: type[ctypes.Structure],
    acl_size_information_type: type[ctypes.Structure],
    dword: Any,
) -> tuple[tuple[str, int], ...]:
    """Enumerate every effective DACL trustee rather than selected groups.

    Each distinct trustee is evaluated through GetEffectiveRightsFromAclW, so
    ordered deny ACEs are honored. Unknown access ACE layouts are rejected:
    silently skipping one could turn a mutation grant into trusted evidence.
    """

    information = acl_size_information_type()
    if not advapi.GetAclInformation(
        dacl,
        ctypes.byref(information),
        ctypes.sizeof(information),
        2,  # AclSizeInformation
    ):
        raise OSError("GetAclInformation failed")
    ace_count = int(information.AceCount)
    if ace_count < 0 or ace_count > 4096:
        raise ValueError("Windows DACL ACE count is invalid")
    principals: dict[str, int] = {}
    for index in range(ace_count):
        ace = ctypes.c_void_p()
        if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
            raise OSError("GetAce failed")
        header = ctypes.string_at(ace, 4)
        ace_type = header[0]
        ace_flags = header[1]
        ace_size = int.from_bytes(header[2:4], "little")
        if ace_size < 8:
            raise ValueError("Windows DACL ACE is malformed")
        if ace_type in _WINDOWS_NON_ACCESS_ACE_TYPES:
            continue
        sid_offset = _windows_access_ace_sid_offset(ace, ace_type, ace_size)
        sid = ctypes.c_void_p(int(ace.value) + sid_offset)
        if not advapi.IsValidSid(sid):
            raise ValueError("Windows DACL trustee SID is malformed")
        sid_size = int(advapi.GetLengthSid(sid))
        if sid_size <= 0 or sid_offset + sid_size > ace_size:
            raise ValueError("Windows DACL trustee SID exceeds its ACE")
        # Inherit-only access ACEs do not apply to this object, but their type,
        # layout, and SID still must be structurally valid. A malformed ACE may
        # never become trusted merely because its flag says to defer it.
        if ace_flags & _WINDOWS_INHERIT_ONLY_ACE:
            continue
        sid_text = _windows_sid_text(advapi, kernel, sid)
        if sid_text not in principals:
            principals[sid_text] = _windows_effective_rights_for_sid(
                advapi,
                dacl,
                sid,
                trustee_type,
                dword,
            )
    return tuple(sorted(principals.items()))


def _windows_access_ace_sid_offset(ace: Any, ace_type: int, ace_size: int) -> int:
    """Return the SID offset for supported allow/deny ACE layouts."""

    if ace_type in _WINDOWS_SIMPLE_ACCESS_ACE_TYPES:
        return 8
    if ace_type not in _WINDOWS_OBJECT_ACCESS_ACE_TYPES or ace_size < 12:
        raise ValueError("Windows DACL contains an unsupported access ACE")
    object_flags = int.from_bytes(ctypes.string_at(int(ace.value) + 8, 4), "little")
    if object_flags & ~0x3:
        raise ValueError("Windows object ACE flags are invalid")
    return 12 + (16 if object_flags & 0x1 else 0) + (16 if object_flags & 0x2 else 0)


__all__ = [
    "ExecutableTrustError",
    "WindowsPathSecurity",
    "capture_trusted_executable",
    "trusted_executable_path",
]
