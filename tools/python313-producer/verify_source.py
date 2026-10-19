"""Verify selected source bytes and apply the reviewed patch only in memory.

No download, extraction, subprocess, build, or upstream Python import is performed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
PATCH_SHA256 = "160cbaa6ceaf8b3707cb0d8d8eec630ea585ee52e08af5360553397638bcdeaa"
ARCHIVE_SHA256 = "f4b1bfb3c79b5bb11b8d228a12504163b4c0dab4d679828d8f5f26b6cb6ab35d"
COMMIT = "c0949b19d464f5d70bc1051195a3ae0e6a012df9"
MANIFEST_SHA256 = "a475ccecb674ab1166a8e7c871fb73830044d513b68b1be213364f7bc08c46d1"
MAX_FILE = 16 * 1024 * 1024
MAX_ARCHIVE = 100 * 1024 * 1024
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n")


def digest(data: bytes) -> str:
    """Return a SHA-256 hex digest."""
    return hashlib.sha256(data).hexdigest()


def read_regular(root: Path, name: str, limit: int = MAX_FILE) -> bytes:
    """Read a bounded regular file without accepting symlink path components."""
    parts = PurePosixPath(name)
    if parts.is_absolute() or ".." in parts.parts or str(parts) != name:
        raise ValueError(f"unsafe path: {name}")
    path = root
    if root.is_symlink():
        raise ValueError("source root is a symlink")
    for part in parts.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"symlink source: {name}")
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"missing, nonregular, or oversized file: {name}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"oversized file: {name}")
    return data


def apply_in_memory(patch: bytes, sources: dict[str, bytes]) -> dict[str, bytes]:
    """Apply strict unified hunks, requiring exact positions and context."""
    lines = patch.decode("utf-8").splitlines(keepends=True)
    outputs: dict[str, bytes] = {}
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- a/"):
            raise ValueError("expected old-file header")
        name = lines[index][6:].rstrip("\n")
        if name not in sources or name in outputs:
            raise ValueError("unknown or duplicate patch file")
        index += 1
        if index >= len(lines) or lines[index] != f"+++ b/{name}\n":
            raise ValueError("mismatched new-file header")
        index += 1
        old = sources[name].decode("utf-8").splitlines(keepends=True)
        result: list[str] = []
        cursor = 0
        hunks = 0
        while index < len(lines) and lines[index].startswith("@@"):
            match = HUNK.fullmatch(lines[index])
            if not match:
                raise ValueError("invalid hunk header")
            old_start, old_count, new_start, new_count = (
                int(value) if value is not None else 1 for value in match.groups()
            )
            position = old_start - 1 if old_count else old_start
            if not cursor <= position <= len(old):
                raise ValueError("invalid hunk position")
            result.extend(old[cursor:position])
            cursor = position
            if len(result) != (new_start - 1 if new_count else new_start):
                raise ValueError("invalid output position")
            index += 1
            removed = added = 0
            while index < len(lines) and lines[index][:1] in (" ", "+", "-"):
                if lines[index].startswith("--- a/"):
                    break
                kind, content = lines[index][0], lines[index][1:]
                if kind in (" ", "-"):
                    if cursor >= len(old) or old[cursor] != content:
                        raise ValueError("hunk context mismatch")
                    cursor += 1
                    removed += 1
                if kind in (" ", "+"):
                    result.append(content)
                    added += 1
                index += 1
            if (removed, added) != (old_count, new_count):
                raise ValueError("hunk count mismatch")
            hunks += 1
        if not hunks:
            raise ValueError("file without hunks")
        result.extend(old[cursor:])
        outputs[name] = "".join(result).encode("utf-8")
    return outputs


def verify(source: Path, archive: Path) -> dict[str, object]:
    """Check archive provenance, selected originals, and all patched hashes."""
    manifest_bytes = read_regular(HERE, "source-manifest.json")
    if digest(manifest_bytes) != MANIFEST_SHA256:
        raise ValueError("manifest digest mismatch")
    manifest = json.loads(manifest_bytes)
    if (manifest["componentize_py_commit"] != COMMIT
            or manifest["cpython_release"] != "3.13.16"
            or manifest["cpython_archive_sha256"] != ARCHIVE_SHA256):
        raise ValueError("unexpected source identity")
    patch = read_regular(HERE, "python313-source.patch")
    if digest(patch) != PATCH_SHA256 or manifest["patch_sha256"] != PATCH_SHA256:
        raise ValueError("patch digest mismatch")
    archive_data = read_regular(archive.parent, archive.name, MAX_ARCHIVE)
    if digest(archive_data) != ARCHIVE_SHA256:
        raise ValueError("CPython archive digest mismatch")
    sources = {}
    for name, entry in manifest["files"].items():
        data = read_regular(source, name)
        if digest(data) != entry["sha256"]:
            raise ValueError(f"source digest mismatch: {name}")
        sources[name] = data
    # Read only regular selected archive members. Never extract paths or links.
    import io
    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:xz") as tar:
        for name, entry in manifest["files"].items():
            if "archive_member" not in entry:
                continue
            member = tar.getmember(entry["archive_member"])
            if not member.isfile() or member.size > MAX_FILE:
                raise ValueError(f"unsafe archive member: {name}")
            stream = tar.extractfile(member)
            if stream is None or stream.read(MAX_FILE + 1) != sources[name]:
                raise ValueError(f"archive/source mismatch: {name}")
    outputs = apply_in_memory(patch, sources)
    expected = {
        name for name, entry in manifest["files"].items()
        if entry["role"] == "patch-input"
    }
    if set(outputs) != expected:
        raise ValueError("patch file set mismatch")
    for name, data in outputs.items():
        if digest(data) != manifest["files"][name]["patched_sha256"]:
            raise ValueError(f"patched digest mismatch: {name}")
    return {
        "status": "source bytes verified; no build or upstream execution",
        "selected_files": len(sources),
        "patched_files": len(outputs),
        "patch_sha256": PATCH_SHA256,
        "cpython_archive_sha256": ARCHIVE_SHA256,
        "componentize_py_commit_claim": COMMIT,
        "componentize_py_full_tree_verified": False,
    }


def main() -> None:
    """Print a source-only verification receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--cpython-archive", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.source, args.cpython_archive), indent=2))


if __name__ == "__main__":
    main()
