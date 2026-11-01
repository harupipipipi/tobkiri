"""Stage pinned source archives without running any upstream code or commands.

Destination must be new under a trusted local parent, not an attacker-controlled
filesystem. Failed private staging directories are retained for diagnostics.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile

import verify_source as verifier

MAX_EXPANDED = 256 * 1024 * 1024
MAX_MEMBERS = 10000
ARCHIVES = {
    "componentize-py": {
        "filename": "componentize-py.tar.gz",
        "sha256": "9205d0d51daf774eefb2a7f37577952b074e40edab468955c601dfdcf9e08550",
        "prefix": f"componentize-py-{verifier.COMMIT}",
        "url": "https://codeload.github.com/bytecodealliance/componentize-py/"
               f"tar.gz/{verifier.COMMIT}",
    },
    "cpython": {
        "filename": "Python-3.13.16.tar.xz",
        "sha256": verifier.ARCHIVE_SHA256,
        "prefix": "Python-3.13.16",
        "url": "https://www.python.org/ftp/python/3.13.16/Python-3.13.16.tar.xz",
    },
    "dependencies/zlib": {
        "filename": "zlib-1.3.1.tar.gz",
        "sha256": "9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23",
        "prefix": "zlib-1.3.1",
        "url": "https://github.com/madler/zlib/releases/download/"
               "v1.3.1/zlib-1.3.1.tar.gz",
    },
    "dependencies/sqlite": {
        "filename": "sqlite-autoconf-3510200.tar.gz",
        "sha256": "fbd89f866b1403bb66a143065440089dd76100f2238314d92274a082d4f2b7bb",
        "prefix": "sqlite-autoconf-3510200",
        "url": "https://sqlite.org/2026/sqlite-autoconf-3510200.tar.gz",
    },
}


def inspect_archive(
    data: bytes, prefix: str, budget: dict[str, int], *, embedded_type_modes: bool = False
) -> dict[str, tuple[bytes | None, int]]:
    """Validate every member and capture bounded data without extracting links."""
    entries: dict[str, tuple[bytes | None, int]] = {}
    seen: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive:
            budget["members"] += 1
            if budget["members"] > MAX_MEMBERS:
                raise ValueError("archive member limit exceeded")
            name = member.name.rstrip("/")
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or "\\" in name
                    or any(part in ("", ".", "..") for part in name.split("/"))
                    or path.parts[0] != prefix):
                raise ValueError(f"unsafe archive path: {member.name}")
            if name in seen:
                raise ValueError(f"duplicate archive member: {name}")
            seen.add(name)
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"unsupported archive member: {name}")
            mode = member.mode
            if embedded_type_modes:
                # Some old crates wrote st_mode, including the file type, into
                # tar's mode field. Accept only a matching redundant type;
                # never mask setuid/setgid/sticky or contradictory type bits.
                expected_type = stat.S_IFREG if member.isfile() else stat.S_IFDIR
                if (mode & ~(0o170000 | 0o777)
                        or stat.S_IFMT(mode) not in (0, expected_type)):
                    raise ValueError(f"special archive mode: {name}")
                mode &= 0o777
            if mode & ~0o777:
                raise ValueError(f"special archive mode: {name}")
            if not 0 <= member.size <= verifier.MAX_FILE:
                raise ValueError(f"archive file limit exceeded: {name}")
            if member.isdir() and member.size:
                raise ValueError(f"directory with content: {name}")
            relative = "/".join(path.parts[1:])
            if not relative:
                if not member.isdir():
                    raise ValueError("archive root must be a directory")
                continue
            for ancestor in PurePosixPath(relative).parents:
                parent = str(ancestor)
                if parent in entries and entries[parent][0] is not None:
                    raise ValueError("archive file/directory collision")
            if member.isfile() and any(
                existing.startswith(relative + "/") for existing in entries
            ):
                raise ValueError("archive file/directory collision")
            content = None
            if member.isfile():
                budget["bytes"] += member.size
                if budget["bytes"] > MAX_EXPANDED:
                    raise ValueError("expanded archive limit exceeded")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"unreadable archive member: {name}")
                content = stream.read(member.size + 1)
                if len(content) != member.size:
                    raise ValueError(f"archive member size mismatch: {name}")
            entries[relative] = (content, mode)
    if not seen:
        raise ValueError("empty archive")
    return entries


def stage(archives: Path, cpython_archive: Path, destination: Path) -> dict:
    """Publish a new verified stage; retain failed partial work for diagnosis."""
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination must not exist")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise ValueError("destination requires an existing trusted parent")
    # Capture each compressed input once. Parsing and extraction use these bytes.
    captured = {}
    for root, identity in ARCHIVES.items():
        path = cpython_archive if root == "cpython" else (
            archives / identity["filename"]
        )
        data = verifier.read_regular(path.parent, path.name, verifier.MAX_ARCHIVE)
        if verifier.digest(data) != identity["sha256"]:
            raise ValueError(f"archive digest mismatch: {root}")
        captured[root] = data
    manifest_data = verifier.read_regular(verifier.HERE, "source-manifest.json")
    if verifier.digest(manifest_data) != verifier.MANIFEST_SHA256:
        raise ValueError("manifest digest mismatch")
    manifest = json.loads(manifest_data)
    patch = verifier.read_regular(verifier.HERE, "python313-source.patch")
    if verifier.digest(patch) != verifier.PATCH_SHA256:
        raise ValueError("patch digest mismatch")
    budget = {"members": 0, "bytes": 0}
    contents = {}
    original_modes = {}
    for root, data in captured.items():
        entries = inspect_archive(data, ARCHIVES[root]["prefix"], budget)
        for relative, (content, mode) in entries.items():
            name = f"{root}/{relative}"
            contents[name] = content
            original_modes[name] = mode
    selected = {}
    for name, entry in manifest["files"].items():
        content = contents.get(name)
        if content is None or verifier.digest(content) != entry["sha256"]:
            raise ValueError(f"selected source digest mismatch: {name}")
        selected[name] = content
    patched = verifier.apply_in_memory(patch, selected)
    expected = {
        name for name, entry in manifest["files"].items()
        if entry["role"] == "patch-input"
    }
    if set(patched) != expected:
        raise ValueError("patched file set mismatch")
    for name, data in patched.items():
        if verifier.digest(data) != manifest["files"][name]["patched_sha256"]:
            raise ValueError(f"patched digest mismatch: {name}")
        contents[name] = data
    partial = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-",
                                    dir=destination.parent))
    try:
        for name, data in sorted(contents.items()):
            path = partial / name
            if data is None:
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
            path.chmod(0o755 if original_modes[name] & 0o111 else 0o644)
        files = {}
        for path in sorted(partial.rglob("*")):
            name = path.relative_to(partial).as_posix()
            if path.is_dir():
                path.chmod(0o755)
                continue
            expected_data = contents[name]
            actual = verifier.read_regular(partial, name)
            if actual != expected_data:
                raise ValueError(f"written source mismatch: {name}")
            files[name] = {
                "sha256": verifier.digest(actual), "bytes": len(actual),
                "mode": oct(path.stat().st_mode & 0o777),
                "archive_mode": oct(original_modes[name]),
            }
        receipt = {
            "schema_version": 1,
            "status": "staged source only; no upstream execution",
            "provenance_scope": "Local archive identities from official URLs; "
                                "not independent vendor signature verification.",
            "archives": ARCHIVES,
            "patch_sha256": verifier.PATCH_SHA256,
            "source_manifest_sha256": verifier.MANIFEST_SHA256,
            "selected_pre_post": manifest["files"],
            "selected_files_verified": len(selected),
            "patched_files_verified": len(patched),
            "archive_members": budget["members"],
            "archive_expanded_bytes": budget["bytes"],
            "files": files,
        }
        receipt_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
        with (partial / "stage-manifest.json").open("xb") as stream:
            stream.write(receipt_bytes)
        if destination.exists() or destination.is_symlink():
            raise ValueError("destination appeared during staging")
        # Same-parent atomic publication, relying on the trusted-parent contract.
        os.rename(partial, destination)
        return {
            "destination": str(destination),
            "manifest_sha256": verifier.digest(receipt_bytes),
            "files": len(files), "selected_files": len(selected),
            "patched_files": len(patched),
        }
    except Exception as error:
        raise RuntimeError(
            f"staging failed; retained diagnostic tree: {partial}"
        ) from error


def main() -> None:
    """Stage explicitly supplied archives into a new trusted destination."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", required=True, type=Path)
    parser.add_argument("--cpython-archive", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(stage(args.archives, args.cpython_archive, args.destination),
                     indent=2))


if __name__ == "__main__":
    main()
