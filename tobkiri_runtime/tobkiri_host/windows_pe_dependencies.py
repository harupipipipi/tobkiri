"""Verify the pinned amd64 PE and DLL closure without product-Pack imports."""

import struct
from pathlib import Path

from .windows_whpx_security import stable_file


def verify_amd64_pe(path: Path) -> None:
    with stable_file(path) as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("Windows PackVM binary must be an amd64 PE file")
        offset = struct.unpack_from("<I", header, 60)[0]
        if not 64 <= offset <= 16 * 1024 * 1024:
            raise ValueError("Windows PackVM PE offset is invalid")
        stream.seek(offset)
        pe = stream.read(26)
        if (
            len(pe) != 26
            or pe[:4] != b"PE\0\0"
            or struct.unpack_from("<H", pe, 4)[0] != 0x8664
            or struct.unpack_from("<H", pe, 24)[0] != 0x20B
        ):
            raise ValueError("Windows PackVM binary must use the amd64 PE32+ architecture")


_SYSTEM_DLLS = frozenset(
    {
        "advapi32.dll",
        "avrt.dll",
        "bcrypt.dll",
        "bcryptprimitives.dll",
        "cabinet.dll",
        "cfgmgr32.dll",
        "comctl32.dll",
        "comdlg32.dll",
        "crypt32.dll",
        "d3d11.dll",
        "d3d12.dll",
        "dbghelp.dll",
        "dnsapi.dll",
        "dsound.dll",
        "dwmapi.dll",
        "dwrite.dll",
        "dxgi.dll",
        "gdi32.dll",
        "gdiplus.dll",
        "hid.dll",
        "imm32.dll",
        "iphlpapi.dll",
        "kernel32.dll",
        "msimg32.dll",
        "msvcrt.dll",
        "mswsock.dll",
        "ncrypt.dll",
        "netapi32.dll",
        "normaliz.dll",
        "ntdll.dll",
        "ole32.dll",
        "oleaut32.dll",
        "opengl32.dll",
        "pdh.dll",
        "powrprof.dll",
        "propsys.dll",
        "psapi.dll",
        "rpcrt4.dll",
        "secur32.dll",
        "setupapi.dll",
        "shell32.dll",
        "shlwapi.dll",
        "ucrtbase.dll",
        "user32.dll",
        "userenv.dll",
        "usp10.dll",
        "uxtheme.dll",
        "version.dll",
        "virtdisk.dll",
        "winhttp.dll",
        "winhvplatform.dll",
        "wininet.dll",
        "winmm.dll",
        "winnsi.dll",
        "winspool.drv",
        "winsta.dll",
        "wintrust.dll",
        "wldap32.dll",
        "ws2_32.dll",
        "wtsapi32.dll",
    }
)


def verify_windows_qemu_dependency_closure(
    executable: Path,
    dependencies: tuple[Path, ...],
) -> None:
    """Check PE architecture and transitive imports without loading any code.

    Known operating-system DLL/API-set names resolve from Windows. Every other
    normal or delay-loaded import must be provided adjacent to QEMU and pinned
    in the manifest. Compiler runtimes are not assumed to be installed globally.
    """
    binaries = (executable, *dependencies)
    supplied = {path.name.casefold() for path in dependencies}
    if len(supplied) != len(dependencies):
        raise ValueError("Windows PackVM dependency names are ambiguous")
    if any(
        path.parent != executable.parent or path.suffix.lower() != ".dll" for path in dependencies
    ):
        raise ValueError("Windows PackVM DLLs must be adjacent to QEMU")
    for path in binaries:
        for name in _pe_imports(path):
            if name in supplied or name in _SYSTEM_DLLS:
                continue
            if name.startswith(("api-ms-win-", "ext-ms-win-")) and name.endswith(".dll"):
                continue
            raise ValueError(f"Windows PackVM has an unbundled DLL import: {name}")


def _pe_imports(path: Path) -> set[str]:
    """Read bounded PE import and delay-import tables using file-backed RVAs."""
    verify_amd64_pe(path)
    with stable_file(path) as stream:
        import os

        size = os.fstat(stream.fileno()).st_size

        def read(offset: int, length: int) -> bytes:
            if offset < 0 or length < 0 or offset + length > size:
                raise ValueError("Windows PackVM PE data is outside the file")
            stream.seek(offset)
            result = stream.read(length)
            if len(result) != length:
                raise ValueError("Windows PackVM PE data is truncated")
            return result

        pe_offset = struct.unpack("<I", read(60, 4))[0]
        coff = read(pe_offset + 4, 20)
        sections_count = struct.unpack_from("<H", coff, 2)[0]
        optional_size = struct.unpack_from("<H", coff, 16)[0]
        if not 1 <= sections_count <= 96 or not 112 <= optional_size <= 4096:
            raise ValueError("Windows PackVM PE header limits are invalid")
        optional = read(pe_offset + 24, optional_size)
        directory_count = struct.unpack_from("<I", optional, 108)[0]
        if directory_count > (optional_size - 112) // 8:
            raise ValueError("Windows PackVM PE directory count is invalid")
        sections = []
        for index in range(sections_count):
            raw = read(pe_offset + 24 + optional_size + index * 40, 40)
            virtual_size, address, raw_size, offset = struct.unpack_from("<IIII", raw, 8)
            sections.append((address, max(virtual_size, raw_size), offset, raw_size))

        def rva_offset(rva: int, length: int) -> int:
            for address, virtual_size, offset, raw_size in sections:
                if address <= rva and rva + length <= address + min(virtual_size, raw_size):
                    return offset + rva - address
            raise ValueError("Windows PackVM PE RVA is not backed by file bytes")

        def name_at(rva: int) -> str:
            content = bytearray()
            for index in range(256):
                char = read(rva_offset(rva + index, 1), 1)
                if char == b"\0":
                    break
                content.extend(char)
            else:
                raise ValueError("Windows PackVM DLL name is too long")
            try:
                name = content.decode("ascii").casefold()
            except UnicodeError as exc:
                raise ValueError("Windows PackVM DLL name is not ASCII") from exc
            if not name or any(
                c not in "abcdefghijklmnopqrstuvwxyz0123456789-_.+" for c in name
            ):
                raise ValueError("Windows PackVM DLL name contains a path")
            return name

        names: set[str] = set()
        for directory, entry_size, name_index in ((1, 20, 3), (13, 32, 1)):
            if directory_count <= directory:
                continue
            address, length = struct.unpack_from("<II", optional, 112 + directory * 8)
            if address == 0 and length == 0:
                continue
            if address == 0 or not entry_size <= length <= 1024 * 1024:
                raise ValueError("Windows PackVM PE import directory is invalid")
            for index in range(min(length // entry_size, 4096)):
                raw = read(rva_offset(address + index * entry_size, entry_size), entry_size)
                values = struct.unpack("<" + "I" * (entry_size // 4), raw)
                if not any(values):
                    break
                if directory == 13 and values[0] != 1:
                    raise ValueError("Windows PackVM delay imports must use RVA addressing")
                names.add(name_at(values[name_index]))
            else:
                raise ValueError("Windows PackVM PE import list is not terminated")
        return names
