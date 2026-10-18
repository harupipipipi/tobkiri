"""Pure ELF validation shared by packaged Linux VM builds and admission."""

from __future__ import annotations

import os
import struct
from pathlib import Path


def validate_static_amd64_elf(path: Path) -> None:
    """Reject dynamic loaders/dependencies; this does not certify a QEMU build."""
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        header = stream.read(64)
        if len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01":
            raise ValueError("QEMU must be a little-endian ELF64 executable")
        fields = struct.unpack("<16sHHIQQQIHHHHHH", header)
        _, elf_type, machine, version, _, phoff, _, _, _, phsize, phnum, _, _, _ = fields
        if (
            elf_type not in {2, 3}
            or machine != 62
            or version != 1
            or phsize != 56
            or not 1 <= phnum <= 1024
            or phoff < 64
            or phoff + phsize * phnum > size
        ):
            raise ValueError("QEMU must have a valid amd64 ELF program table")
        executable_segment = False
        for index in range(phnum):
            stream.seek(phoff + index * phsize)
            kind, flags, offset, _, _, file_size, _, _ = struct.unpack(
                "<IIQQQQQQ", stream.read(phsize)
            )
            if offset + file_size > size:
                raise ValueError("QEMU ELF segment exceeds its file")
            if kind == 3:
                raise ValueError("dynamic QEMU (PT_INTERP) is unsupported; use static QEMU")
            executable_segment |= kind == 1 and bool(flags & 1)
            if kind == 2:
                if file_size % 16 or file_size > 1024 * 1024:
                    raise ValueError("QEMU ELF dynamic table is invalid")
                stream.seek(offset)
                for _ in range(file_size // 16):
                    tag, _ = struct.unpack("<qQ", stream.read(16))
                    if tag in {1, 15, 29, 0x7FFFFFFD, 0x7FFFFFFF}:
                        raise ValueError("QEMU has dynamic dependencies or search paths")
                    if tag == 0:
                        break
        if not executable_segment:
            raise ValueError("QEMU ELF has no executable load segment")
