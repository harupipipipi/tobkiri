"""Strict, bounded source/stage capture; no compiler, guest or network execution."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Mapping

MAX_ENTRIES = 32_768
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_METADATA = 4 * 1024 * 1024
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def canonical(document: object) -> bytes:
    """Encode only the compiler's bounded canonical JSON subset."""
    def validate(value: object, depth: int = 0) -> None:
        if depth > 32:
            raise ValueError("canonical depth limit")
        if value is None or type(value) is bool:
            return
        if type(value) is int and abs(value) <= 9_007_199_254_740_991:
            return
        if type(value) is str:
            value.encode("utf-8", "strict")
            return
        if type(value) is list:
            for item in value:
                validate(item, depth + 1)
            return
        if type(value) is dict and all(type(key) is str for key in value):
            for key, item in value.items():
                validate(key, depth + 1)
                validate(item, depth + 1)
            return
        raise ValueError("canonical value/type limit")
    validate(document)
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(",", ":")).encode()


def strict_document(data: bytes, bound: int = MAX_METADATA) -> object:
    """Reject duplicates, floats, invalid Unicode and noncanonical spellings."""
    if len(data) > bound:
        raise ValueError("document byte limit")
    def pairs(items: list[tuple]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate document key")
            result[key] = value
        return result
    document = json.loads(data.decode("utf-8", "strict"), object_pairs_hook=pairs)
    if canonical(document) != data:
        raise ValueError("noncanonical document")
    return document


def guest_path(value: str) -> None:
    """Admit one normalized, bounded, relative UTF-8 guest path."""
    if (type(value) is not str or not value or len(value.encode()) > 4096
            or "\\" in value or "\0" in value or value.startswith("/")):
        raise ValueError("guest path")
    parts = value.split("/")
    if len(parts) > 32 or any(part in ("", ".", "..") for part in parts):
        raise ValueError("guest path components")


def physical_directory(path: Path) -> int:
    """Open every physical parent with no-follow descriptor-relative traversal."""
    if not path.is_absolute() or any(part in (".", "..") for part in path.parts):
        raise ValueError("physical absolute directory required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
                                | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_file(path: Path, bound: int = MAX_FILE) -> bytes:
    """Capture a regular file once; reject symlink parents/leaves and growth."""
    parent = physical_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
                     | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        return _read_fd(fd, bound)
    finally:
        os.close(fd)


def _read_fd(fd: int, bound: int) -> bytes:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > bound:
        raise ValueError("regular file/size required")
    chunks = []
    remaining = bound + 1
    while remaining:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    after = os.fstat(fd)
    data = b"".join(chunks)
    if (len(data) > bound or len(data) != before.st_size or
            (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ValueError("file changed during capture")
    return data


@dataclass(frozen=True)
class Node:
    """Immutable captured content with declaration-derived metadata."""
    kind: str
    data: bytes
    inode: int
    epoch: int

    def metadata(self) -> dict:
        """Return stable logical metadata without any host inode or timestamps."""
        return {"kind": self.kind, "size": len(self.data), "inode": self.inode,
                "epoch": self.epoch, "mode": "0555" if self.kind == "directory"
                else "0444"}


@dataclass(frozen=True)
class Snapshot:
    """A captured tree; every later read/list/stat is in memory."""
    nodes: Mapping[str, Node]
    roots: tuple[str, ...]

    def listing(self, path: str) -> tuple[str, ...]:
        """List captured immediate children in canonical UTF-8 order."""
        if self.nodes[path].kind != "directory":
            raise NotADirectoryError(path)
        prefix = path + "/"
        return tuple(name[len(prefix):] for name in self.nodes
                     if name.startswith(prefix) and "/" not in name[len(prefix):])


def capture(entries: list[dict], mounts: Mapping[str, Path], epoch: int) -> Snapshot:
    """Capture exactly a complete declaration; reject missing and extra entries."""
    if type(epoch) is not int or not 0 <= epoch < 2**63 or not mounts:
        raise ValueError("capture epoch/mounts")
    if len(entries) > MAX_ENTRIES or len(canonical(entries)) > MAX_METADATA:
        raise ValueError("capture metadata limit")
    expected = {}
    for entry in entries:
        if set(entry) != {"guest_path", "kind", "size", "sha256"}:
            raise ValueError("capture entry fields")
        path = entry["guest_path"]
        guest_path(path)
        if path in expected or entry["kind"] not in ("file", "directory"):
            raise ValueError("duplicate path/type")
        if (type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_FILE
                or type(entry["sha256"]) is not str or len(entry["sha256"]) != 64
                or any(ch not in "0123456789abcdef" for ch in entry["sha256"])):
            raise ValueError("capture size/digest")
        if entry["kind"] == "directory" and (entry["size"] != 0 or entry["sha256"] != EMPTY_SHA):
            raise ValueError("directory declaration")
        expected[path] = entry
    if list(expected) != sorted(expected):
        raise ValueError("capture manifest order")
    for root in mounts:
        guest_path(root)
    observed = {}
    total = 0

    def walk(fd: int, path: str, own_root: str) -> None:
        nonlocal total
        entry = expected.get(path)
        if entry is None or entry["kind"] != "directory" or path in observed:
            raise ValueError("directory declaration/collision")
        observed[path] = ("directory", b"")
        names = sorted(os.listdir(fd))
        for name in names:
            child = path + "/" + name
            guest_path(child)
            if child in mounts and child != own_root:
                continue
            entry = expected.get(child)
            if entry is None:
                raise ValueError("undeclared captured entry")
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if entry["kind"] == "directory":
                flags |= os.O_DIRECTORY
            opened = os.open(name, flags, dir_fd=fd)
            try:
                if entry["kind"] == "directory":
                    walk(opened, child, own_root)
                else:
                    data = _read_fd(opened, MAX_FILE)
                    total += len(data)
                    if (total > MAX_TOTAL or len(data) != entry["size"] or
                            hashlib.sha256(data).hexdigest() != entry["sha256"]
                            or child in observed):
                        raise ValueError("capture content/size/collision")
                    observed[child] = ("file", data)
            finally:
                os.close(opened)
        if sorted(os.listdir(fd)) != names:
            raise ValueError("directory changed during capture")

    for root, source in mounts.items():
        opened = physical_directory(source)
        try:
            walk(opened, root, root)
        finally:
            os.close(opened)
    if set(observed) != set(expected):
        raise ValueError("missing captured entry")
    for path in observed:
        parent, _, _ = path.rpartition("/")
        if parent and (parent not in observed or observed[parent][0] != "directory"):
            raise ValueError("missing captured parent")
    nodes = {path: Node(kind, data, index + 1, epoch)
             for index, (path, (kind, data)) in enumerate(sorted(observed.items()))}
    if len({node.inode for node in nodes.values()}) != len(nodes):
        raise ValueError("inode collision")
    return Snapshot(MappingProxyType(nodes), tuple(sorted(mounts)))
