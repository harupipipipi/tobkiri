"""Separate two observed CPython preinit cache outputs from immutable inputs.

Read headers as DATA only. Never import, unmarshal, or execute a cache. Header
correspondence alone is not proof of its body; the authenticated original
producer and its retained source/input custody supply that provenance.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path, PurePosixPath
import sys

from poll_link_trace import fingerprint

# CPython v3.13.16 Lib/importlib/_bootstrap_external.py,
# Git blob fcfe312747c03540175378a07faf7d288398ab4b.
MAGIC = (3571).to_bytes(2, "little") + b"\r\n"
CACHES = {
    "__pycache__/app.cpython-313.pyc": "app.py",
    "__pycache__/componentize_py_types.cpython-313.pyc": "componentize_py_types.py",
}
LIMIT = 4 * 1024 * 1024


def inspect_outputs(
    stage: Path, before: dict, after: dict, *, require_native_hash: bool = False
) -> dict:
    """Require all prior inputs exact and only the two observed cache outputs."""
    if any(after.get(name) != identity for name, identity in before.items()):
        raise ValueError("captured or generated source changed during preinit")
    if set(after) - set(before) != set(CACHES):
        raise ValueError("preinit created an unobserved output or omitted its caches")
    result = {}
    for name, source in CACHES.items():
        cache_path = PurePosixPath(name)
        mapped_source = cache_path.parent.parent / (
            cache_path.name.removesuffix(".cpython-313.pyc") + ".py"
        )
        if mapped_source.as_posix() != source or source not in before:
            raise ValueError("cache does not correspond to a captured source path")
        path = stage / name
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("cache output may not traverse a link")
        identity = fingerprint(path, LIMIT, private=False)
        if identity != {key: after[name][key] for key in ("bytes", "sha256")}:
            raise ValueError("cache output changed before header inspection")
        with path.open("rb") as stream:
            header = stream.read(16)
        if len(header) != 16 or header[:4] != MAGIC:
            raise ValueError("cache does not have the fixed CPython31316 header")
        flags = int.from_bytes(header[4:8], "little")
        if flags & ~3:
            raise ValueError("cache header has unsupported CPython flags")
        source_path = stage / source
        source_identity = fingerprint(
            source_path, LIMIT, private=False, allow_empty=True
        )
        if source_identity != {key: before[source][key] for key in ("bytes", "sha256")}:
            raise ValueError("cache source differs from its captured bytes")
        checked = False
        if flags & 1:
            if (
                sys.version_info[:3] == (3, 13, 16)
                and importlib.util.MAGIC_NUMBER == MAGIC
            ):
                import _imp

                expected = _imp.source_hash(
                    int.from_bytes(MAGIC, "little"), source_path.read_bytes()
                )
                if header[8:16] != expected:
                    raise ValueError("cache source hash differs")
                checked = True
            elif require_native_hash:
                raise ValueError(
                    "hash cache needs the pinned native CPython31316 verifier"
                )
        else:
            expected_time = int(source_path.stat().st_mtime) & 0xFFFFFFFF
            expected_size = source_identity["bytes"] & 0xFFFFFFFF
            if (
                int.from_bytes(header[8:12], "little") != expected_time
                or int.from_bytes(header[12:16], "little") != expected_size
            ):
                raise ValueError("timestamp cache source time or size differs")
            checked = True
        if fingerprint(path, LIMIT, private=False) != identity:
            raise ValueError("cache output changed during header inspection")
        result[name] = {
            **after[name],
            "source": source,
            "magic": header[:4].hex(),
            "flags": flags,
            "kind": "hash" if flags & 1 else "timestamp",
            "source_header_checked": checked,
            "bytecode_body_independently_executed_or_validated": False,
        }
    return result
