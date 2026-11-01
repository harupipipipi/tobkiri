"""Read/hash an existing SDK only. No tool execution, copying or downloading."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import struct

BUFFER = 1024 * 1024
MAX_FILES = 40_000
MAX_INVENTORY = 3 * 1024 * 1024
MAX_SDK_BYTES = 1024 * 1024 * 1024


def canonical(value: object) -> bytes:
    """Encode the exact path-sorted SDK metadata digest input."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def inventory(root: Path) -> tuple[list[dict], dict, dict]:
    """Hash each ordinary SDK file in 1 MiB chunks, preserving symlink targets."""
    records = []
    total = 0
    native = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        folders.sort()
        files.sort()
        names = sorted(set(folders + files))
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            relative.encode("utf-8", "strict")
            info = path.lstat()
            item = {"path": relative, "mode": f"{stat.S_IMODE(info.st_mode):04o}"}
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                target.encode("utf-8", "strict")
                if target.startswith("/"):
                    raise ValueError("absolute SDK symlink target")
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
                if resolved == ".." or resolved.startswith("../"):
                    raise ValueError("SDK symlink escape")
                item.update(type="symlink", target=target, size=len(target.encode()),
                            sha256=hashlib.sha256(target.encode()).hexdigest())
            elif stat.S_ISDIR(info.st_mode):
                item.update(type="directory", size=0, sha256=hashlib.sha256(b"").hexdigest())
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > MAX_SDK_BYTES:
                    raise ValueError("SDK total read budget")
                digest = hashlib.sha256()
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    before = os.fstat(fd)
                    first = True
                    while block := os.read(fd, BUFFER):
                        digest.update(block)
                        if first:
                            first = False
                            if block[:4] == b"\xcf\xfa\xed\xfe":
                                native[relative] = macho_dependencies(block)
                    after = os.fstat(fd)
                finally:
                    os.close(fd)
                if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                        (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise ValueError("SDK file changed during hashing")
                item.update(type="file", size=info.st_size, sha256=digest.hexdigest())
            else:
                raise ValueError("unsupported SDK entry type")
            records.append(item)
            if len(records) > MAX_FILES:
                raise ValueError("SDK metadata count budget")
    records.sort(key=lambda item: item["path"].encode())
    encoded = canonical(records)
    return records, {"entries": len(records), "file_bytes": total,
                     "metadata_bytes": len(encoded),
                     "tree_sha256": hashlib.sha256(encoded).hexdigest()}, native


def resolve(records: dict[str, dict], path: str) -> dict:
    """Resolve only already-pinned internal SDK symlinks, with a strict bound."""
    seen = set()
    for _ in range(32):
        if path in seen or path not in records:
            raise ValueError("SDK tool symlink cycle/missing entry")
        seen.add(path)
        item = records[path]
        if item["type"] != "symlink":
            if item["type"] != "file":
                raise ValueError("SDK tool must resolve to a file")
            return item
        path = posixpath.normpath(posixpath.join(posixpath.dirname(path), item["target"]))
    raise ValueError("SDK symlink depth")


def macho_dependencies(data: bytes) -> dict:
    """Read bounded Mach-O load commands, never run otool/clang/ld."""
    header = data[:32]
    if len(header) != 32 or header[:4] != b"\xcf\xfa\xed\xfe":
        return {"format": "not-thin-arm64-mach-o", "closure_verified": False}
    magic, cpu, subcpu, kind, count, size, flags, reserved = struct.unpack("<8I", header)
    if count > 4096 or size > BUFFER - 32 or len(data) < size + 32:
        raise ValueError("Mach-O metadata bound")
    commands = data[32:32 + size]
    libraries = []
    rpaths = []
    offset = 0
    for _ in range(count):
        if offset + 8 > len(commands):
            raise ValueError("Mach-O command bounds")
        command, amount = struct.unpack_from("<2I", commands, offset)
        if amount < 8 or offset + amount > len(commands):
            raise ValueError("Mach-O command length")
        if command in (0xC, 0x80000018, 0x8000001F, 0x20, 0x80000023, 0x8000001C):
            start = struct.unpack_from("<I", commands, offset + 8)[0]
            if start >= amount:
                raise ValueError("Mach-O name offset")
            name = commands[offset + start:offset + amount].split(b"\0", 1)[0].decode()
            (rpaths if command == 0x8000001C else libraries).append(name)
        offset += amount
    return {"format": "mach-o-64", "cpu_type": cpu, "libraries": libraries,
            "rpaths": rpaths, "closure_verified": False,
            "gap": "system dyld cache/OS closure identity has not been verified"}


def main() -> None:
    """Emit a private bounded inventory and minimal public SDK pins."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", required=True, type=Path)
    parser.add_argument("--private-inventory", required=True, type=Path)
    parser.add_argument("--public-pins", required=True, type=Path)
    args = parser.parse_args()
    if args.sdk.is_symlink() or not args.sdk.is_absolute():
        raise ValueError("explicit physical SDK root required")
    records, summary, native = inventory(args.sdk)
    by_path = {item["path"]: item for item in records}
    selected = {}
    for path in ("bin/clang", "bin/clang++", "bin/wasm-ld", "bin/llvm-ar", "bin/llvm-ranlib", "bin/llvm-strip"):
        target = resolve(by_path, path)
        commands = native.get(target["path"], {"format": "not-thin-arm64-mach-o"})
        selected[path] = {"resolved_path": target["path"], "size": target["size"], "sha256": target["sha256"],
                          "native_load_commands_sha256": hashlib.sha256(canonical(commands)).hexdigest(),
                          "native_system_libraries": sorted(Path(name).name for name in commands.get("libraries", []) if name.startswith("/usr/lib/")),
                          "native_closure_verified": False}
    sysroot = [item for item in records if item["path"].startswith("share/wasi-sysroot/")]
    public = {"format": "tobkiri.producer-sdk-pin/2", "sdk": "wasi-sdk-34.0-arm64-macos",
              "archive_sha256": "9c59398106b417f8f14913380fdf0097a8cc0ff4af9eb3ce0065a859e88d49e9",
              "algorithm": "SHA256(canonical UTF-8 JSON array sorted by UTF-8 relative path; keys sorted; compact separators; directory empty digest; symlink target text digest; ordinary file content digest; mode included)",
              **summary, "sysroot_sha256": hashlib.sha256(canonical(sysroot)).hexdigest(),
              "sysroot_entries": len(sysroot), "selected": selected,
              "tool_execution": False, "system_native_closure_verified": False,
              "path_override": "denied; recipe role locators must match these exact logical paths and hashes"}
    if summary["metadata_bytes"] <= MAX_INVENTORY:
        args.private_inventory.write_bytes(canonical(records))
    else:
        args.private_inventory.write_bytes(canonical({**summary, "inventory_omitted": True,
                                                    "reason": "full inventory exceeds private receipt budget; aggregate only"}))
    args.public_pins.write_text(json.dumps(public, indent=2, sort_keys=True) + "\n")
    print(json.dumps({**summary, "sysroot_entries": len(sysroot), "tool_execution": False}))


if __name__ == "__main__":
    main()
