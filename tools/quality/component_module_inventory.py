"""Bounded, read-only inventory of exact embedded core-module bytes.

This checks binary framing, not Wasm validity or immutable artifact custody.
Sharing estimates do not transform components, admit a catalog, execute guests,
or establish that any replacement preserves types, state or authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import BinaryIO

COMPONENT_HEADER = b"\0asm\x0d\0\x01\0"
MODULE_HEADER = b"\0asm\x01\0\0\0"
MAX_COMPONENT_BYTES = 32 * 1024 * 1024
CURRENT_CATALOG_INPUT_LIMIT = 64 * 1024 * 1024
MAX_COMPONENTS = 64
MAX_DEPTH = 16
MAX_SECTIONS = 16_384
MAX_MODULES = 4096
CHUNK_BYTES = 65_536


def _u32(stream: BinaryIO, end: int) -> int:
    value = 0
    for index in range(5):
        if stream.tell() >= end:
            raise ValueError("truncated section length")
        raw = stream.read(1)
        if not raw:
            raise ValueError("truncated section length")
        byte = raw[0]
        if index == 4 and byte > 15:
            raise ValueError("section length overflows u32")
        value |= (byte & 127) << (7 * index)
        if byte < 128:
            return value
    raise ValueError("section length exceeds five bytes")


def _hash_region(stream: BinaryIO, start: int, size: int) -> str:
    stream.seek(start)
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = stream.read(min(remaining, CHUNK_BYTES))
        if not chunk:
            raise ValueError("truncated binary region")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _scan(
    stream: BinaryIO,
    start: int,
    end: int,
    location: tuple[int, ...],
    modules: list[dict],
    counters: list[int],
) -> None:
    if len(location) > MAX_DEPTH:
        raise ValueError("component nesting exceeds inventory bound")
    stream.seek(start)
    if end - start < 8 or stream.read(8) != COMPONENT_HEADER:
        raise ValueError("expected component binary header")
    section = 0
    while stream.tell() < end:
        counters[0] += 1
        if counters[0] > MAX_SECTIONS:
            raise ValueError("section count exceeds inventory bound")
        raw = stream.read(1)
        if not raw or raw[0] > 11:
            raise ValueError("unknown or truncated component section")
        kind = raw[0]
        size = _u32(stream, end)
        offset = stream.tell()
        limit = offset + size
        if limit > end:
            raise ValueError("section escapes its containing component")
        if kind == 1:
            if len(modules) >= MAX_MODULES:
                raise ValueError("module count exceeds inventory bound")
            if size < 8 or stream.read(8) != MODULE_HEADER:
                raise ValueError("embedded core module has invalid header")
            modules.append({
                "section": [*location, section],
                "offset": offset,
                "bytes": size,
                "sha256": _hash_region(stream, offset, size),
            })
        elif kind == 4:
            _scan(stream, offset, limit, (*location, section), modules, counters)
        stream.seek(limit)
        section += 1


def inspect_component(path: Path) -> dict:
    """Read one regular input with bounded buffers, without executing it."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("component input must be a regular file")
    if not 8 <= before.st_size <= MAX_COMPONENT_BYTES:
        raise ValueError("component input exceeds original per-file bound")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("component changed while opening")
        modules: list[dict] = []
        counters = [0]
        digest = _hash_region(stream, 0, before.st_size)
        _scan(stream, 0, before.st_size, (), modules, counters)
        after = os.fstat(stream.fileno())
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, key) != getattr(after, key) for key in keys):
            raise ValueError("component changed while reading")
    return {
        "path": str(path), "bytes": before.st_size,
        "sha256": digest, "modules": modules,
    }


def inventory(paths: list[Path]) -> dict:
    """Report exact-byte repetition, never a successful runtime admission."""
    if not 1 <= len(paths) <= MAX_COMPONENTS:
        raise ValueError("component count exceeds inventory bound")
    if len({str(path.resolve()) for path in paths}) != len(paths):
        raise ValueError("duplicate component input")
    components = []
    total_modules = 0
    for path in paths:
        inspected = inspect_component(path)
        total_modules += len(inspected["modules"])
        if total_modules > MAX_MODULES:
            raise ValueError("total module count exceeds inventory bound")
        components.append(inspected)
    unique: dict[str, dict] = {}
    for index, component in enumerate(components):
        for module in component["modules"]:
            entry = unique.setdefault(module["sha256"], {
                "sha256": module["sha256"], "bytes": module["bytes"],
                "occurrences": 0, "components": set(),
            })
            if entry["bytes"] != module["bytes"]:
                raise ValueError("module digest has inconsistent byte length")
            entry["occurrences"] += 1
            entry["components"].add(index)
    shared = [entry for entry in unique.values()
              if len(entry["components"]) > 1]
    source_bytes = sum(component["bytes"] for component in components)
    potential = sum(entry["bytes"] * (entry["occurrences"] - 1)
                    for entry in shared)
    return {
        "schema": "tobkiri.component-module-inventory.v1",
        "scope": "framing and hashes only; no Wasm validation, transformation, "
                 "admission, execution or production parity",
        "component_bytes": source_bytes,
        "current_catalog_input_limit": CURRENT_CATALOG_INPUT_LIMIT,
        "within_current_catalog_input_limit": (
            source_bytes <= CURRENT_CATALOG_INPUT_LIMIT
        ),
        "shared_core_module_bytes": sum(entry["bytes"] for entry in shared),
        "potential_repeated_module_bytes": potential,
        "extraction_lower_bound_before_type_import_overhead": (
            source_bytes - potential
        ),
        "extraction_preserves_abi_state_authority": "unverified",
        "shared_modules": [
            {**entry, "components": sorted(entry["components"])}
            for entry in sorted(shared, key=lambda item: item["sha256"])
        ],
        "components": components,
    }


def main() -> None:
    """Print diagnostic JSON; this command never changes its inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("components", type=Path, nargs="+")
    args = parser.parse_args()
    print(json.dumps(inventory(args.components), indent=2))


if __name__ == "__main__":
    main()
