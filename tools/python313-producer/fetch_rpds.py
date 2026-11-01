"""Hash-bound rpds/PyO3 source inputs for an isolated UI dependency experiment.

This does not modify the historical producer, its lock or the Pack source plans.
No downloaded code executes here. Compilation and guest execution are CI-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from build_native import fingerprint
import archive_inputs
import fetch_registry
from fetch_registry import REGISTRY, capture, unpack
from fetch_sdk import inventory
import stage_sources
from stage_sources import inspect_archive
import verify_source

VERSION = "2026.6.3"
COMMIT = "7277eb681f6efd67eca1bbaa32f78d78bdc044a5"
PREFIX = "rpds-" + COMMIT
URL = "https://codeload.github.com/crate-py/rpds/tar.gz/" + COMMIT
ARCHIVE_SHA256 = "ba002c2f0078bf3b7934f16d8fe620eaadc77c8e31fe640c65fded63676f9f11"
LOCK_SHA256 = "6c04984e9c05d00e24e23eeade8463ea09d7a7fba9f8f5706da9bdbc026baee4"
MANIFEST_SHA256 = "2fb9471df809676863caf7254a59bc16658dc83567bf5972620fa8a760d01a45"
MAX_SOURCE_ARCHIVE = 4 * 1024 * 1024
MAX_CRATE_ARCHIVE = 32 * 1024 * 1024
MAX_DOWNLOADS = 128 * 1024 * 1024
DISK_FLOOR = 8 * 1024**3


def packages(lock: bytes) -> list[dict]:
    """Allow only the exact upstream lock and its checksum-bound registry graph."""
    if len(lock) > 256 * 1024 or hashlib.sha256(lock).hexdigest() != LOCK_SHA256:
        raise ValueError("rpds Cargo.lock identity differs")
    data = tomllib.loads(lock.decode("utf-8"))
    result, seen, roots = [], set(), []
    for item in data["package"]:
        name, version = item["name"], item["version"]
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]+", name)
            or not re.fullmatch(r"[A-Za-z0-9.+-]+", version)
            or (name, version) in seen
        ):
            raise ValueError("invalid rpds lock package")
        seen.add((name, version))
        if item.get("source") is None:
            roots.append((name, version))
            continue
        if item["source"] != REGISTRY or not re.fullmatch(
            r"[0-9a-f]{64}", item.get("checksum", "")
        ):
            raise ValueError("rpds dependency is not the pinned registry source")
        result.append({"name": name, "version": version, "sha256": item["checksum"]})
    if roots != [("rpds-py", VERSION)] or len(result) != 18:
        raise ValueError("rpds dependency closure differs")
    return result


def extract_source(data: bytes, destination: Path) -> dict:
    """Audit the small immutable Git archive before publishing regular source files."""
    if (
        len(data) > MAX_SOURCE_ARCHIVE
        or hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256
    ):
        raise ValueError("rpds source archive identity differs")
    entries = inspect_archive(data, PREFIX, {"bytes": 0, "members": 0})
    for name, expected in (
        ("Cargo.lock", LOCK_SHA256),
        ("Cargo.toml", MANIFEST_SHA256),
    ):
        raw = entries[name][0]
        if raw is None or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("rpds source manifest or lock differs")
    if any(name == ".cargo" or name.startswith(".cargo/") for name in entries):
        raise ValueError("source-local Cargo configuration is not admitted")
    packages(entries["Cargo.lock"][0])
    destination.mkdir(mode=0o700)
    for name, (raw, mode) in sorted(entries.items()):
        path = destination / name
        if raw is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(raw)
            path.chmod(mode)
    return inventory(destination)


def source_inventory(archive: Path) -> dict:
    """Reconstruct the source tree identity from its immutable archive pin."""
    result = archive_inputs.inspect(
        archive,
        PREFIX,
        ARCHIVE_SHA256,
        maximum=MAX_SOURCE_ARCHIVE,
        max_file=verify_source.MAX_FILE,
        max_expanded=stage_sources.MAX_EXPANDED,
        max_members=stage_sources.MAX_MEMBERS,
    )
    files = result["files"]
    for name, expected in (
        ("Cargo.lock", LOCK_SHA256),
        ("Cargo.toml", MANIFEST_SHA256),
    ):
        if files.get(name, {}).get("sha256") != expected:
            raise ValueError("rpds source manifest or lock differs")
    if any(
        name == ".cargo" or name.startswith(".cargo/")
        for name in files.keys() | result["directories"]
    ):
        raise ValueError("source-local Cargo configuration is not admitted")
    return files


def checksum_bytes(package: dict, files: dict) -> bytes:
    """Use the same canonical Cargo directory-source bytes at fetch and verify."""
    return (
        json.dumps(
            {
                "package": package["sha256"],
                "files": {name: entry["sha256"] for name, entry in files.items()},
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def registry_inventory(archive: Path, package: dict, totals: dict) -> dict:
    """Derive one vendored crate and its generated Cargo checksum manifest."""
    stem = package["name"] + "-" + package["version"]
    result = archive_inputs.inspect(
        archive,
        stem,
        package["sha256"],
        maximum=MAX_CRATE_ARCHIVE,
        max_file=verify_source.MAX_FILE,
        max_expanded=stage_sources.MAX_EXPANDED,
        max_members=stage_sources.MAX_MEMBERS,
        embedded_type_modes=True,
    )
    files = result["files"]
    if (
        ".cargo-checksum.json" in files
        or ".cargo-checksum.json" in result["directories"]
        or not files
    ):
        raise ValueError("reserved or empty registry source")
    totals["members"] += result["members"]
    totals["bytes"] += result["expanded_bytes"]
    if (
        totals["members"] > fetch_registry.MAX_SOURCE_MEMBERS
        or totals["bytes"] > fetch_registry.MAX_SOURCE_BYTES
    ):
        raise ValueError("registry source closure budget exceeded")
    checksums = checksum_bytes(package, files)
    return {
        **files,
        ".cargo-checksum.json": {
            "bytes": len(checksums),
            "sha256": hashlib.sha256(checksums).hexdigest(),
            "mode": "0o644",
        },
    }


def download(url: str, destination: Path, expected: str, maximum: int) -> int:
    """Download one fixed HTTPS input; never infer URLs from arbitrary lock sources."""
    with urlopen(
        Request(url, headers={"User-Agent": "Tobkiri-source-replay"}), timeout=60
    ) as response:
        if urlsplit(response.geturl()).scheme != "https":
            raise ValueError("source redirect left HTTPS")
        with destination.open("xb") as output:
            return capture(response, output, expected, maximum)


def run(destination: Path) -> None:
    """Fetch sources on CI only, without invoking Cargo or an upstream build hook."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("rpds source acquisition is restricted to Linux CI")
    if (
        not destination.is_absolute()
        or destination.parent.resolve() != destination.parent
        or shutil.disk_usage(destination.parent).free < DISK_FLOOR
    ):
        raise ValueError("physical new destination and existing disk floor required")
    destination.mkdir(mode=0o700)
    archives = destination / "archives"
    archives.mkdir(mode=0o700)
    archive = archives / "rpds.tar.gz"
    downloaded = download(URL, archive, ARCHIVE_SHA256, MAX_SOURCE_ARCHIVE)
    source = destination / "source"
    source_files = extract_source(archive.read_bytes(), source)
    vendor = destination / "vendor"
    vendor.mkdir(mode=0o700)
    totals, records = {"bytes": 0, "members": 0}, []
    for package in packages((source / "Cargo.lock").read_bytes()):
        stem = package["name"] + "-" + package["version"]
        path = archives / (stem + ".crate")
        url = f"https://static.crates.io/crates/{package['name']}/{stem}.crate"
        downloaded += download(
            url,
            path,
            package["sha256"],
            min(MAX_CRATE_ARCHIVE, MAX_DOWNLOADS - downloaded),
        )
        if downloaded > MAX_DOWNLOADS:
            raise ValueError("rpds download closure exceeds its bound")
        record = unpack(path, vendor / stem, package, totals)
        checksum = vendor / stem / ".cargo-checksum.json"
        checksum.write_bytes(checksum_bytes(package, record["files"]))
        checksum.chmod(0o644)
        record["cargo_checksums"] = fingerprint(checksum)
        records.append(record)
    report = {
        "format": "tobkiri.ui-rpds-inputs/1",
        "outcome": "success",
        "commit": COMMIT,
        "version": VERSION,
        "archive": fingerprint(archive),
        "source_files": source_files,
        "vendor_files": inventory(vendor),
        "packages": records,
        "download_bytes": downloaded,
        "cargo_executed": False,
        "production_enabled": False,
    }
    (destination / "rpds-inputs.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {"sources_verified": True, "packages": len(records), "executed": False}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    run(**vars(parser.parse_args()))
