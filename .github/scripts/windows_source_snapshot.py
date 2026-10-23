"""Handle-bound Windows committed-source snapshots used by the sealed builder.

This is the Python counterpart of windows_packaging_fs.rs.  POSIX mode bits
are not Windows authority: protected owner/System/Administrators DACLs and
non-write/non-delete-sharing handles form this boundary instead.
"""

from __future__ import annotations

import contextlib
import ctypes as c
import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

H = c.c_void_p
D = c.c_uint32
READ = 0x120089
WRITE = 0x120116
WRITE_DAC = 0x40000
DELETE = 0x10000
SEALED = 0x1200A9  # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
FULL = 0x1F01FF
SYSTEM_OWNERS = frozenset(
    {
        "S-1-5-18",
        "S-1-5-32-544",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }
)


class SnapshotError(OSError):
    """The native snapshot boundary could not be established or retained."""


class Info(c.Structure):
    _fields_ = [
        (name, D)
        for name in (
            "attributes",
            "created_low",
            "created_high",
            "access_low",
            "access_high",
            "written_low",
            "written_high",
            "volume",
            "size_high",
            "size_low",
            "links",
            "index_high",
            "index_low",
        )
    ]


class UnicodeString(c.Structure):
    _fields_ = [("length", c.c_uint16), ("maximum", c.c_uint16), ("buffer", H)]


class ObjectAttributes(c.Structure):
    _fields_ = [
        ("length", D),
        ("root", H),
        ("name", c.POINTER(UnicodeString)),
        ("attributes", D),
        ("security", H),
        ("quality", H),
    ]


class IoStatus(c.Structure):
    _fields_ = [("status", H), ("information", c.c_size_t)]


@dataclass
class Pin:
    path: Path
    handle: int
    identity: tuple[int, int, bool]
    sealed: bool = False


def safe_relative(value: str) -> tuple[str, ...]:
    """Reject Win32 aliases, alternate streams, devices, and path traversal."""
    if not value or "\\" in value or value.startswith("/"):
        raise SnapshotError("unsafe Windows source path")
    parts = tuple(value.split("/"))
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or part.endswith((".", " "))
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
            or re.fullmatch(
                r"(?i)(CON|CONIN\$|CONOUT\$|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])",
                part.split(".")[0],
            )
        ):
            raise SnapshotError("unsafe Windows source path component")
    return parts


class NativeApi:
    """Small typed Win32 API; all security reads and mutations use handles."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise SnapshotError("native Windows snapshot API is unavailable")
        self.kernel = c.WinDLL("kernel32", use_last_error=True, winmode=0x800)
        self.advapi = c.WinDLL("advapi32", use_last_error=True, winmode=0x800)
        self.ntdll = c.WinDLL("ntdll", use_last_error=True, winmode=0x800)
        signatures = {
            "CreateFileW": ([c.c_wchar_p, D, D, H, D, D, H], H),
            "GetSystemWindowsDirectoryW": ([H, D], D),
            "GetSystemDirectoryW": ([H, D], D),
            "CloseHandle": ([H], c.c_int),
            "GetFileInformationByHandle": ([H, c.POINTER(Info)], c.c_int),
            "GetCurrentProcess": ([], H),
            "LocalFree": ([H], H),
            "SetFilePointerEx": ([H, c.c_int64, c.POINTER(c.c_int64), D], c.c_int),
            "ReadFile": ([H, H, D, c.POINTER(D), H], c.c_int),
            "WriteFile": ([H, H, D, c.POINTER(D), H], c.c_int),
            "FlushFileBuffers": ([H], c.c_int),
            "SetFileInformationByHandle": ([H, c.c_int, H, D], c.c_int),
        }
        security = {
            "OpenProcessToken": ([H, D, c.POINTER(H)], c.c_int),
            "GetTokenInformation": ([H, c.c_int, H, D, c.POINTER(D)], c.c_int),
            "ConvertSidToStringSidW": ([H, c.POINTER(H)], c.c_int),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
                [c.c_wchar_p, D, c.POINTER(H), c.POINTER(D)],
                c.c_int,
            ),
            "GetSecurityDescriptorDacl": (
                [H, c.POINTER(c.c_int), c.POINTER(H), c.POINTER(c.c_int)],
                c.c_int,
            ),
            "GetSecurityInfo": (
                [H, c.c_int, D, c.POINTER(H), H, c.POINTER(H), H, c.POINTER(H)],
                D,
            ),
            "SetSecurityInfo": ([H, c.c_int, D, H, H, H, H], D),
            "GetSecurityDescriptorControl": (
                [H, c.POINTER(c.c_uint16), c.POINTER(D)],
                c.c_int,
            ),
            "GetAce": ([H, D, c.POINTER(H)], c.c_int),
            "IsValidSid": ([H], c.c_int),
            "GetLengthSid": ([H], D),
        }
        for dll, definitions in ((self.kernel, signatures), (self.advapi, security)):
            for name, (args, result) in definitions.items():
                fn = getattr(dll, name)
                fn.argtypes, fn.restype = args, result
        self.ntdll.NtCreateFile.argtypes = [
            c.POINTER(H),
            D,
            c.POINTER(ObjectAttributes),
            c.POINTER(IoStatus),
            H,
            D,
            D,
            D,
            D,
            H,
            D,
        ]
        self.ntdll.NtCreateFile.restype = c.c_int32
        self.ntdll.RtlNtStatusToDosError.argtypes = [c.c_int32]
        self.ntdll.RtlNtStatusToDosError.restype = D
        self.sid = self._current_sid()

    @staticmethod
    def check(ok: object) -> None:
        if not ok:
            raise c.WinError(c.get_last_error())

    def close(self, handle: int) -> None:
        self.check(self.kernel.CloseHandle(handle))

    def sid_text(self, sid: int) -> str:
        if not sid or not self.advapi.IsValidSid(sid):
            raise SnapshotError("invalid Windows snapshot SID")
        text = H()
        self.check(self.advapi.ConvertSidToStringSidW(sid, c.byref(text)))
        try:
            return c.wstring_at(text)
        finally:
            self.kernel.LocalFree(text)

    def _current_sid(self) -> str:
        token = H()
        self.check(
            self.advapi.OpenProcessToken(
                self.kernel.GetCurrentProcess(), 8, c.byref(token)
            )
        )
        try:
            size = D()
            self.advapi.GetTokenInformation(token, 1, None, 0, c.byref(size))
            if not 0 < size.value <= 65536:
                raise SnapshotError("invalid Windows token size")
            data = c.create_string_buffer(size.value)
            self.check(
                self.advapi.GetTokenInformation(token, 1, data, size, c.byref(size))
            )
            return self.sid_text(c.cast(data, c.POINTER(H))[0])
        finally:
            self.close(token)

    def descriptor(self, sealed: bool) -> H:
        rights = "FRFX" if sealed else "FA"
        text = (
            f"O:{self.sid}D:P(A;OICI;{rights};;;{self.sid})"
            "(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        )
        result = H()
        self.check(
            self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                text, 1, c.byref(result), None
            )
        )
        return result

    def verify_acl(self, handle: int, sealed: bool, *, system: bool = False) -> None:
        owner, acl, descriptor = H(), H(), H()
        status = self.advapi.GetSecurityInfo(
            handle, 1, 5, c.byref(owner), None, c.byref(acl), None, c.byref(descriptor)
        )
        try:
            control, revision = c.c_uint16(), D()
            if (
                status
                or not owner
                or not acl
                or self.sid_text(owner.value)
                not in (SYSTEM_OWNERS if system else {self.sid})
                or not self.advapi.GetSecurityDescriptorControl(
                    descriptor, c.byref(control), c.byref(revision)
                )
                or (not system and not control.value & 0x1000)
            ):
                raise SnapshotError("source snapshot ownership or DACL is unsafe")
            header = c.string_at(acl, 8)
            size = int.from_bytes(header[2:4], "little")
            count = int.from_bytes(header[4:6], "little")
            if size < 8 or count == 0 or count > (size - 8) // 8:
                raise SnapshotError("malformed source snapshot DACL")
            for index in range(count):
                ace = H()
                self.check(self.advapi.GetAce(acl, index, c.byref(ace)))
                if (
                    not ace.value
                    or ace.value < acl.value + 8
                    or ace.value + 8 > acl.value + size
                ):
                    raise SnapshotError("malformed source snapshot ACE pointer")
                raw = c.string_at(ace, 8)
                length = int.from_bytes(raw[2:4], "little")
                if (
                    not ace.value
                    or ace.value < acl.value + 8
                    or length < 16
                    or ace.value + length > acl.value + size
                    or raw[0] not in (0, 1)
                ):
                    raise SnapshotError("unsupported source snapshot ACE")
                sid = ace.value + 8
                if (
                    not self.advapi.IsValidSid(sid)
                    or 8 + self.advapi.GetLengthSid(sid) > length
                ):
                    raise SnapshotError("malformed source snapshot ACE SID")
                if raw[0] == 1:  # A deny can only reduce permissions.
                    continue
                trustee = self.sid_text(sid)
                mask = int.from_bytes(raw[4:8], "little")
                if system:
                    # System directories can inherit their effective ACL. Only
                    # system administrators/TrustedInstaller may mutate entries;
                    # inherit-only grants do not apply to this directory itself.
                    if raw[1] & 0x08:
                        continue
                    privileged = trustee in SYSTEM_OWNERS | {"S-1-3-4"}
                    allowed = (
                        (FULL | 0xF0000000) if privileged else (SEALED | 0xA0000000)
                    )
                    safe_flags = 0x1F
                else:
                    allowed = (
                        FULL
                        if trustee in {"S-1-5-18", "S-1-5-32-544"}
                        else (
                            (SEALED if sealed else FULL) if trustee == self.sid else 0
                        )
                    )
                    safe_flags = 0x03
                if not allowed or mask & ~allowed or raw[1] & ~safe_flags:
                    raise SnapshotError("source snapshot DACL grants unsafe access")
        finally:
            if descriptor:
                self.kernel.LocalFree(descriptor)

    def set_acl(self, handle: int, sealed: bool) -> None:
        descriptor = self.descriptor(sealed)
        try:
            present, defaulted, acl = c.c_int(), c.c_int(), H()
            self.check(
                self.advapi.GetSecurityDescriptorDacl(
                    descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)
                )
            )
            if not present.value or not acl:
                raise SnapshotError("private snapshot DACL is missing")
            status = self.advapi.SetSecurityInfo(
                handle, 1, 0x80000004, None, None, acl, None
            )
            if status:
                raise c.WinError(status)
            self.verify_acl(handle, sealed)
        finally:
            self.kernel.LocalFree(descriptor)

    def identity(self, handle: int, directory: bool) -> tuple[int, int, bool]:
        info = Info()
        self.check(self.kernel.GetFileInformationByHandle(handle, c.byref(info)))
        if (
            info.attributes & 0x400
            or bool(info.attributes & 0x10) != directory
            or (not directory and info.links != 1)
        ):
            raise SnapshotError(
                "source snapshot contains a reparse, hardlink or wrong type"
            )
        return (info.volume, (info.index_high << 32) | info.index_low, directory)

    def system_paths(self) -> tuple[Path, Path]:
        """Get OS-owned paths from Win32, never SystemRoot/PATH environment text."""
        result = []
        for name in ("GetSystemWindowsDirectoryW", "GetSystemDirectoryW"):
            buffer = c.create_unicode_buffer(32768)
            length = getattr(self.kernel, name)(buffer, len(buffer))
            if not 0 < length < len(buffer):
                raise SnapshotError("Windows system directory query failed")
            result.append(Path(buffer.value))
        if result[1].parent != result[0]:
            raise SnapshotError("Windows system directory is outside the OS root")
        return result[0], result[1]

    def open(self, path: Path, directory: bool, *, delete: bool = False) -> Pin:
        handle = self.kernel.CreateFileW(
            str(path),
            READ | (DELETE if delete else 0),
            1,
            None,
            3,
            0x200000 | (0x2000000 if directory else 0),
            None,
        )
        if handle in (None, c.c_void_p(-1).value):
            raise c.WinError(c.get_last_error())
        try:
            return Pin(path, handle, self.identity(handle, directory))
        except BaseException:
            self.close(handle)
            raise

    def create(self, parent: Pin, name: str, directory: bool) -> Pin:
        if len(safe_relative(name)) != 1:
            raise SnapshotError("snapshot creation requires one path component")
        utf16 = name.encode("utf-16-le")
        encoded = c.create_string_buffer(utf16 + b"\0\0")
        length = len(utf16)
        if length > 65532:
            raise SnapshotError("snapshot filename is too long")
        text = UnicodeString(length, length + 2, c.cast(encoded, H))
        descriptor = self.descriptor(False)
        attributes = ObjectAttributes(
            c.sizeof(ObjectAttributes),
            parent.handle,
            c.pointer(text),
            0x40,
            descriptor,
            None,
        )
        status, handle = IoStatus(), H()
        try:
            code = self.ntdll.NtCreateFile(
                c.byref(handle),
                READ | WRITE_DAC | (0 if directory else WRITE),
                c.byref(attributes),
                c.byref(status),
                None,
                0x80,
                1,
                2,
                0x200000 | 0x20 | (1 if directory else 0x40),
                None,
                0,
            )
            if code < 0:
                raise c.WinError(self.ntdll.RtlNtStatusToDosError(code))
            if not handle or code != 0 or status.information != 2:
                if handle:
                    self.close(handle)
                raise SnapshotError("snapshot creation did not prove ownership")
            try:
                pin = Pin(
                    parent.path / name,
                    handle.value,
                    self.identity(handle.value, directory),
                )
                self.verify_acl(pin.handle, False)
                return pin
            except BaseException:
                self.close(handle)
                raise
        finally:
            self.kernel.LocalFree(descriptor)

    def read(self, pin: Pin) -> bytes:
        self.check(self.kernel.SetFilePointerEx(pin.handle, 0, None, 0))
        chunks = []
        buffer, count = c.create_string_buffer(1024 * 1024), D()
        while True:
            self.check(
                self.kernel.ReadFile(
                    pin.handle, buffer, len(buffer), c.byref(count), None
                )
            )
            if count.value == 0:
                return b"".join(chunks)
            chunks.append(buffer.raw[: count.value])

    def write(self, pin: Pin, payload: bytes) -> None:
        offset, count = 0, D()
        while offset < len(payload):
            chunk = payload[offset : offset + 1024 * 1024]
            self.check(
                self.kernel.WriteFile(
                    pin.handle, c.c_char_p(chunk), len(chunk), c.byref(count), None
                )
            )
            if not count.value:
                raise SnapshotError("short source snapshot write")
            offset += count.value
        self.check(self.kernel.FlushFileBuffers(pin.handle))
        self.set_acl(pin.handle, True)
        # A reduced DuplicateHandle still shares the writer's FILE_OBJECT and
        # its write-sharing reservation. Open independent read objects instead,
        # retaining a no-delete-sharing pin throughout both transitions.
        for sharing in (3, 1):  # READ|WRITE temporarily, then READ only.
            handle = self.kernel.CreateFileW(
                str(pin.path), READ | WRITE_DAC, sharing, None, 3, 0x200000, None
            )
            if handle in (None, c.c_void_p(-1).value):
                raise c.WinError(c.get_last_error())
            try:
                if self.identity(handle, False) != pin.identity:
                    raise SnapshotError(
                        "snapshot write/read transition identity changed"
                    )
                self.verify_acl(handle, True)
            except BaseException:
                self.close(handle)
                raise
            previous, pin.handle = pin.handle, handle
            self.close(previous)
        pin.sealed = True
        if self.read(pin) != payload:
            raise SnapshotError("source snapshot bytes changed during sealing")

    def delete(self, pin: Pin) -> None:
        remove = c.c_ubyte(1)
        self.check(
            self.kernel.SetFileInformationByHandle(pin.handle, 4, c.byref(remove), 1)
        )


class WindowsSnapshot:
    """Own all source/destination pins until build use and safe cleanup finish."""

    def __init__(self, source: Path, destination: Path | None, *, api=None) -> None:
        self.api = api or NativeApi()
        self.source, self.destination = source, destination
        self.ancestors: list[Pin] = []
        self.inputs: dict[str, Pin] = {}
        self.outputs: dict[str, Pin] = {}

    def pin_chain(self, path: Path) -> Pin:
        parsed = PureWindowsPath(str(path))
        if not parsed.is_absolute() or not re.fullmatch(
            r"(?:\\\\\?\\)?[A-Za-z]:", parsed.drive
        ):
            raise SnapshotError(
                "source snapshot requires a local absolute Windows path"
            )
        safe_relative("/".join(parsed.parts[1:]))
        pin = None
        for entry in reversed((path, *path.parents)):
            pin = self.api.open(entry, True)
            self.ancestors.append(pin)
        return pin

    def __enter__(self):
        try:
            root = self.pin_chain(self.source)
            self.api.verify_acl(root.handle, True)
            root.sealed = True
            self.inputs[""] = root
            self._capture(self.source, "")
            if self.destination is not None:
                parent = self.pin_chain(self.destination.parent)
                self.outputs[""] = self.api.create(parent, self.destination.name, True)
            return self
        except BaseException:
            self._close()
            raise

    def _capture(self, directory: Path, relative: str) -> None:
        names = list(os.scandir(directory))
        folded = set()
        for entry in sorted(names, key=lambda item: item.name):
            safe_relative(entry.name)
            if entry.name.casefold() in folded:
                raise SnapshotError("case-aliased source snapshot entry")
            folded.add(entry.name.casefold())
            name = f"{relative}/{entry.name}" if relative else entry.name
            is_directory = entry.is_dir(follow_symlinks=False)
            pin = self.api.open(Path(entry.path), is_directory)
            self.inputs[name] = pin
            self.api.verify_acl(pin.handle, True)
            pin.sealed = True
            if is_directory:
                self._capture(pin.path, name)

    def read(self, relative: str) -> bytes:
        safe_relative(relative)
        pin = self.inputs.get(relative)
        if pin is None or pin.identity[2]:
            raise SnapshotError("source snapshot file is missing")
        self.api.verify_acl(pin.handle, True)
        if self.api.identity(pin.handle, False) != pin.identity:
            raise SnapshotError("held source snapshot identity changed")
        payload = self.api.read(pin)
        self.api.verify_acl(pin.handle, True)
        if self.api.identity(pin.handle, False) != pin.identity:
            raise SnapshotError("held source snapshot identity changed")
        return payload

    def write(self, relative: str, payload: bytes) -> None:
        parts = safe_relative(relative)
        parent = ""
        for part in parts[:-1]:
            child = f"{parent}/{part}" if parent else part
            if child not in self.outputs:
                self.outputs[child] = self.api.create(self.outputs[parent], part, True)
            parent = child
        pin = self.api.create(self.outputs[parent], parts[-1], False)
        self.outputs[relative] = pin
        self.api.write(pin, payload)

    @staticmethod
    def _names(root: Path) -> set[str]:
        result = {""}
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                relative = (Path(directory) / name).relative_to(root).as_posix()
                safe_relative(relative)
                result.add(relative)
        return result

    def verify(self, pins: dict[str, Pin], root: Path) -> None:
        if self._names(root) != set(pins):
            raise SnapshotError("source snapshot inventory changed; residue retained")
        for pin in pins.values():
            if self.api.identity(pin.handle, pin.identity[2]) != pin.identity:
                raise SnapshotError("held source snapshot identity changed")
            self.api.verify_acl(pin.handle, pin.sealed)
            probe = self.api.open(pin.path, pin.identity[2])
            try:
                if probe.identity != pin.identity:
                    raise SnapshotError("named source snapshot identity changed")
            finally:
                self.api.close(probe.handle)

    def verify_inventory(self, files: list[str], manifest: str) -> None:
        expected = {"", manifest, *files}
        for relative in files:
            parts = safe_relative(relative)
            expected.update("/".join(parts[:index]) for index in range(1, len(parts)))
        if set(self.inputs) != expected:
            raise SnapshotError(
                "committed source inventory has missing or extra entries"
            )

    def seal(self) -> None:
        for pin in reversed(list(self.outputs.values())):
            if not pin.sealed:
                self.api.set_acl(pin.handle, True)
                pin.sealed = True
        self.verify(self.inputs, self.source)
        self.verify(self.outputs, self.destination)

    def cleanup(self) -> None:
        if not self.outputs:
            return
        self.verify(self.outputs, self.destination)
        for pin in self.outputs.values():
            self.api.set_acl(pin.handle, False)
            pin.sealed = False
        # Transition one leaf at a time to a DELETE handle, proving the same
        # identity again. Parent pins remain live. Never delete a substitute.
        for relative in sorted(
            self.outputs, key=lambda key: key.count("/") + bool(key), reverse=True
        ):
            pin = self.outputs[relative]
            self.api.close(pin.handle)
            pin.handle = 0
            deletion = self.api.open(pin.path, pin.identity[2], delete=True)
            try:
                if deletion.identity != pin.identity:
                    raise SnapshotError("cleanup source snapshot identity changed")
                self.api.verify_acl(deletion.handle, False)
                if pin.identity[2] and list(os.scandir(pin.path)):
                    raise SnapshotError(
                        "unowned snapshot cleanup entry; residue retained"
                    )
                self.api.delete(deletion)
            finally:
                self.api.close(deletion.handle)

    def _close(self) -> None:
        seen = set()
        for pin in reversed(
            [*self.ancestors, *self.inputs.values(), *self.outputs.values()]
        ):
            if pin.handle and pin.handle not in seen:
                seen.add(pin.handle)
                self.api.close(pin.handle)
                pin.handle = 0

    def __exit__(self, kind, value, traceback) -> None:
        try:
            self.cleanup()
        finally:
            self._close()


@contextlib.contextmanager
def system_environment(cache: Path):
    """Hold trusted OS directories while a clear-environment child executes."""
    api = NativeApi()
    guard = WindowsSnapshot(cache, None, api=api)
    try:
        windows, system = api.system_paths()
        trusted = []
        for path in (windows, system):
            pin = guard.pin_chain(path)
            api.verify_acl(pin.handle, False, system=True)
            trusted.append(pin)
        yield {
            "SystemRoot": os.fspath(windows),
            "WINDIR": os.fspath(windows),
            "PATH": os.fspath(system),
            "TEMP": os.fspath(cache),
            "TMP": os.fspath(cache),
            "USERPROFILE": os.fspath(cache),
            "APPDATA": os.fspath(cache),
            "LOCALAPPDATA": os.fspath(cache),
        }
        for pin in guard.ancestors:
            if api.identity(pin.handle, True) != pin.identity:
                raise SnapshotError("Windows system directory identity changed")
        for pin in trusted:
            api.verify_acl(pin.handle, False, system=True)
    finally:
        guard._close()
