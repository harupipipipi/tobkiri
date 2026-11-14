#!/usr/bin/env python3
"""Compare the pinned vendor Git tree with the published crate archives."""

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

VERSION = "47.0.4"
UPSTREAM_VCS = "254cb7a02d5804d4e466198212cda9d3575ad381"
VENDOR_TREE = "a046fbaee26ff3a53ecba2ff3a5b4f529fd10164"
LICENSE_SHA256 = "268872b9816f90fd8e85db5a28d33f8150ebb8dd016653fb39ef1f94f2686bc5"
ARCHIVE_HASHES = {
    "wasmtime": "4cdaf5b8af5713146e3a62d940c71e3eb2ad1ebacfa6b55f0c8509a5b4b87718",
    "wasmtime-internal-fiber": (
        "4d126366176553e8f304e8ac2078a4cc37134fe8f19d4d4695a09c5a2f577f10"
    ),
}
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_PAYLOAD_BYTES = 7 * 1024 * 1024
MAX_FILES = 500
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(REPO), *args])


def describe(data: bytes, executable: bool) -> dict:
    return {
        "mode": "100755" if executable else "100644",
        "bytes": len(data),
        "sha256": sha256(data),
    }


def read_upstream(archives: Path, name: str) -> dict:
    prefix = f"{name}-{VERSION}"
    with (archives / f"{prefix}.crate").open("rb") as source:
        archive = source.read(MAX_ARCHIVE_BYTES + 1)
    if len(archive) > MAX_ARCHIVE_BYTES or sha256(archive) != ARCHIVE_HASHES[name]:
        raise ValueError(f"{name}: archive exceeds limit or has the wrong SHA-256")
    with gzip.GzipFile(fileobj=io.BytesIO(archive)) as source:
        payload = source.read(MAX_PAYLOAD_BYTES + 1)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"{name}: uncompressed archive exceeds limit")

    files = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as tar:
        for position, member in enumerate(tar, start=1):
            path = PurePosixPath(member.name)
            parts = path.parts
            if (
                position > MAX_FILES
                or not parts
                or parts[0] != prefix
                or ".." in parts
                or "\\" in member.name
                or path.as_posix() != member.name.rstrip("/")
            ):
                raise ValueError(f"{name}: malformed or excessive archive members")
            if member.isdir():
                continue
            if not member.isfile() or len(parts) < 2 or member.sparse is not None:
                raise ValueError(f"{name}: unexpected archive member type: {member.name}")
            relative = PurePosixPath(*parts[1:]).as_posix()
            if relative in files or not 0 <= member.size <= MAX_PAYLOAD_BYTES:
                raise ValueError(f"{name}: duplicate or oversized member: {relative}")
            source = tar.extractfile(member)
            if source is None:
                raise ValueError(f"{name}: missing ordinary member: {relative}")
            with source:
                data = source.read(MAX_PAYLOAD_BYTES + 1)
            if len(data) != member.size:
                raise ValueError(f"{name}: incomplete member: {relative}")
            if relative == ".cargo_vcs_info.json":
                verify_vcs(name, data)
            files[relative] = describe(data, bool(member.mode & 0o111))
    return files


def verify_vcs(name: str, data: bytes) -> None:
    vcs = json.loads(data)["git"]["sha1"]
    if vcs != UPSTREAM_VCS:
        raise ValueError(f"{name}: unexpected VCS revision: {vcs}")


def read_vendor(ref: str) -> dict:
    commit = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    commit = commit.decode().strip()
    tree = git("rev-parse", "--verify", f"{commit}:vendor").decode().strip()
    if tree != VENDOR_TREE:
        raise ValueError(f"unexpected vendor Git tree: {tree}")
    entries = git("ls-tree", "-lrz", commit, "--", "vendor").split(b"\0")[:-1]
    if len(entries) > MAX_FILES:
        raise ValueError("too many files in vendor tree")
    files = {}
    total = 0
    for entry in entries:
        description, path_bytes = entry.split(b"\t", 1)
        mode, kind, oid, size = description.decode("ascii").split()
        size = int(size)
        total += size
        if (
            kind != "blob"
            or mode not in {"100644", "100755"}
            or size < 0
            or total > MAX_PAYLOAD_BYTES
        ):
            raise ValueError("unexpected Git tree mode or byte count")
        path = path_bytes.decode()
        data = git("cat-file", "blob", oid)
        if len(data) != size or path in files:
            raise ValueError(f"invalid Git blob: {path}")
        if path.endswith("/.cargo_vcs_info.json"):
            verify_vcs(path, data)
        files[path] = describe(data, mode == "100755")
    return files


def inventory_hash(files: dict) -> str:
    data = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return sha256(data)


def compare(archives: Path, ref: str) -> dict:
    vendor = read_vendor(ref)
    packages = {}
    for name, expected in ARCHIVE_HASHES.items():
        prefix = f"vendor/{name}/"
        installed = {
            p[len(prefix) :]: v for p, v in vendor.items() if p.startswith(prefix)
        }
        upstream = read_upstream(archives, name)
        if any(
            entries["LICENSE"]["sha256"] != LICENSE_SHA256
            or ".cargo_vcs_info.json" not in entries
            for entries in (installed, upstream)
        ):
            raise ValueError(f"{name}: license or upstream provenance differs")
        both = installed.keys() & upstream.keys()
        delta = {
            "added": {p: installed[p] for p in installed.keys() - upstream.keys()},
            "removed": {p: upstream[p] for p in upstream.keys() - installed.keys()},
            "changed": {
                p: {"upstream": upstream[p], "vendor": installed[p]}
                for p in both
                if installed[p] != upstream[p]
            },
        }
        packages[name] = {
            "archive_sha256": expected,
            "license_sha256": LICENSE_SHA256,
            "upstream_files": len(upstream),
            "vendor_files": len(installed),
            "unchanged": sum(installed[p] == upstream[p] for p in both),
            "upstream_inventory_sha256": inventory_hash(upstream),
            "vendor_inventory_sha256": inventory_hash(installed),
            **delta,
        }
    return {
        "format": "tobkiri.wasmtime-vendor-inventory/1",
        "version": VERSION,
        "upstream_vcs": UPSTREAM_VCS,
        "vendor_tree": VENDOR_TREE,
        "vendor_inventory_sha256": inventory_hash(vendor),
        "vendor_only_files": {
            p: v
            for p, v in vendor.items()
            if not any(p.startswith(f"vendor/{name}/") for name in ARCHIVE_HASHES)
        },
        "packages": packages,
    }


def main() -> None:
    """Check or print vendor provenance for the requested commit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_dir", type=Path)
    parser.add_argument("--ref", default="HEAD", help="Git commit or branch to inspect")
    parser.add_argument("--print-index", action="store_true")
    args = parser.parse_args()
    try:
        result = compare(args.archive_dir, args.ref)
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"vendor verification failed: {exc}\n")
    if args.print_index:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        with (HERE / "source-inventory.json").open(encoding="utf-8") as source:
            expected = json.load(source)
        if result != expected:
            parser.exit(1, "vendor evidence differs from the checked-in inventory\n")
        print(f"Verified vendor tree {VENDOR_TREE} against both pinned upstream archives.")


if __name__ == "__main__":
    main()
