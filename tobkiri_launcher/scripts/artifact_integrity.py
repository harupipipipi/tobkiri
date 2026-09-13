"""Canonical v1 artifact-tree digest and payload-size implementation.

The digest is the SHA-256 of the concatenation of each regular file's
portable relative path, a NUL byte, and its exact bytes.  A root file has an
empty relative path; directories contribute no bytes and are traversed in
portable filename order.  Symlinks and non-regular entries are rejected.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def _read_file(path: Path, digest: Any) -> int:
    """Hash one regular file and return its exact payload byte count."""
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size


def _visit(
    path: Path,
    relative_parts: tuple[str, ...],
    digest: Any,
) -> int:
    """Hash one artifact entry and return its recursive payload size."""
    if path.is_symlink():
        raise RuntimeError(f"artifact may not contain a symlink: {path}")
    if path.is_file():
        digest.update("/".join(relative_parts).encode("utf-8"))
        digest.update(b"\0")
        return _read_file(path, digest)
    if not path.is_dir():
        raise RuntimeError(f"artifact entry is not a file or directory: {path}")

    size = 0
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        size += _visit(child, relative_parts + (child.name,), digest)
    return size


def artifact_digest_and_size(path: Path) -> tuple[str, int]:
    """Return the canonical SHA-256 digest and exact payload size for an artifact."""
    digest = hashlib.sha256()
    size = _visit(path, (), digest)
    return f"sha256:{digest.hexdigest()}", size


def artifact_digest(path: Path) -> str:
    """Return the canonical SHA-256 digest for an artifact."""
    return artifact_digest_and_size(path)[0]


def artifact_size(path: Path) -> int:
    """Return the canonical recursive payload size for an artifact."""
    return artifact_digest_and_size(path)[1]
