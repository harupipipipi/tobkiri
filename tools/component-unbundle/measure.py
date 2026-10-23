"""Measure an actual extraction; never admit artifacts or execute guests."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
from typing import BinaryIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "quality"))
from component_module_inventory import inspect_component  # noqa: E402

INPUT_LIMIT = 32 * 1024 * 1024
OUTPUT_LIMIT = 64 * 1024 * 1024
MAX_MODULES = 4096
THRESHOLD = 10 * 1024


def _read(stream: BinaryIO, size: int) -> bytes:
    raw = stream.read(size)
    if len(raw) != size:
        raise ValueError("truncated worker output")
    return raw


def _hash(stream: BinaryIO, size: int) -> str:
    if not 8 <= size <= INPUT_LIMIT:
        raise ValueError("artifact exceeds original per-input bound")
    digest = hashlib.sha256()
    while size:
        chunk = _read(stream, min(size, 65536))
        digest.update(chunk)
        size -= len(chunk)
    return digest.hexdigest()


def _worker_digest(path: Path) -> str:
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or not 8 <= before.st_size <= 128 * 1024 * 1024:
        raise ValueError("trusted worker must be a bounded regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = before.st_size
        while remaining:
            chunk = _read(stream, min(remaining, 65536))
            digest.update(chunk)
            remaining -= len(chunk)
        if stream.read(1):
            raise ValueError("trusted worker grew while hashing")
        after = os.fstat(stream.fileno())
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in keys):
        raise ValueError("trusted worker changed while hashing")
    return digest.hexdigest()


def inspect_wire(stream: BinaryIO, expected: list[dict]) -> dict:
    """Bind every extracted byte stream to the original embedded module order."""
    if _read(stream, 8) != b"TBUNB001":
        raise ValueError("unknown extraction framing")
    count, size = struct.unpack("<II", _read(stream, 8))
    if count > MAX_MODULES or count != len(expected):
        raise ValueError("extracted module count differs from original")
    result = {"bytes": size, "sha256": _hash(stream, size), "modules": []}
    total = 16 + size
    for index, original in enumerate(expected):
        size = struct.unpack("<I", _read(stream, 4))[0]
        total += 4 + size
        if total > OUTPUT_LIMIT:
            raise ValueError("extraction envelope exceeds output bound")
        digest = _hash(stream, size)
        if (size, digest) != (original["bytes"], original["sha256"]):
            raise ValueError("extracted module differs from original bytes")
        result["modules"].append({
            "import": f"unbundled-module{index}", "bytes": size,
            "sha256": digest,
        })
    if stream.read(1):
        raise ValueError("unexpected extraction trailer")
    return result


def extract(worker: Path, source: Path) -> dict:
    """Run one locally trusted build tool with no concurrent children."""
    original = inspect_component(source)
    before = source.lstat()
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as input_stream, tempfile.TemporaryFile() as output:
        opened = os.fstat(input_stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev, opened.st_ino
        ) != (before.st_dev, before.st_ino):
            raise ValueError("source identity changed before extraction")
        completed = subprocess.run(
            [str(worker)], stdin=input_stream, stdout=output, stderr=subprocess.PIPE,
            timeout=120, check=False, shell=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
        if completed.returncode:
            raise ValueError(
                "extraction worker failed: "
                + completed.stderr[:4096].decode("utf-8", errors="replace")
            )
        after = os.fstat(input_stream.fileno())
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, k) != getattr(after, k) for k in keys):
            raise ValueError("source changed during extraction")
        input_stream.seek(0)
        if _hash(input_stream, after.st_size) != original["sha256"]:
            raise ValueError("worker input differs from captured source")
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("worker output exceeds envelope bound")
        output.seek(0)
        expected = [m for m in original["modules"] if m["bytes"] > THRESHOLD]
        transformed = inspect_wire(output, expected)
    return {
        "source": str(source), "source_sha256": original["sha256"],
        "source_bytes": original["bytes"],
        "source_module_count": len(original["modules"]), "component": transformed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("components", type=Path, nargs="+")
    args = parser.parse_args()
    if not 1 <= len(args.components) <= 64:
        raise ValueError("component count exceeds inventory bound")
    if len({str(p.resolve()) for p in args.components}) != len(args.components):
        raise ValueError("duplicate component input")
    worker = args.worker.resolve(strict=True)
    worker_digest = _worker_digest(worker)
    records = []
    unique = {}
    total_modules = 0
    for source in args.components:
        record = extract(worker, source)
        total_modules += record["source_module_count"]
        if total_modules > MAX_MODULES:
            raise ValueError("total extracted module count exceeds inventory bound")
        records.append(record)
        for module in record["component"]["modules"]:
            old = unique.setdefault(module["sha256"], module["bytes"])
            if old != module["bytes"]:
                raise ValueError("module digest has inconsistent byte length")
    if _worker_digest(worker) != worker_digest:
        raise ValueError("worker changed during inspection")
    closure_bytes = sum(r["component"]["bytes"] for r in records) + sum(unique.values())
    print(json.dumps({
        "schema": "tobkiri.component-module-extraction.v1",
        "scope": "Actual validated binary transformation and original-byte binding; "
                 "no guest execution, authenticated Host admission or production adoption",
        "worker_sha256": worker_digest, "components": records,
        "source_bytes": sum(r["source_bytes"] for r in records),
        "transformed_closure_bytes": closure_bytes,
        "unique_extracted_modules": len(unique),
        "current_catalog_input_limit": OUTPUT_LIMIT,
        "within_current_catalog_input_limit": closure_bytes <= OUTPUT_LIMIT,
        "guest_execution_performed": False, "native_core_acceptance": False,
        "production_enabled": False,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
