"""Fetch the lock-pinned JSON Schema wheels and a separate native oracle wheel.

Downloaded wheel contents are handled only as data. This module does not import,
install, or execute any staged package code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tomllib
from urllib.parse import urlsplit

from build_native import HERE, fingerprint
import archive_inputs
import fetch_rpds
import fetch_sdk
import fetch_validation_tools

LOCK_PATH = HERE.parents[1] / "tobkiri_runtime" / "uv.lock"
LOCK_SHA256 = "187534b689820569d89c2ee686125251f040e6d3cc67e5941b679b45e9530a11"
REGISTRY = "https://pypi.org/simple"
LOCK_MAX_BYTES = 512 * 1024
MAX_WHEEL_BYTES = 512 * 1024
MAX_DOWNLOADS = 2 * 1024 * 1024
MAX_EXPANDED = fetch_validation_tools.MAX_EXPANDED
MAX_ENTRIES = fetch_validation_tools.MAX_ENTRIES
MAX_FILE = fetch_validation_tools.MAX_FILE
DISK_FLOOR = 8 * 1024**3
FORMAT = "tobkiri.python313-schema-inputs/1"

PACKAGES = (
    ("attrs", "26.1.0"),
    ("jsonschema", "4.26.0"),
    ("jsonschema-specifications", "2025.9.1"),
    ("referencing", "0.37.0"),
    ("packaging", "26.2"),
)
NATIVE_ORACLE = (
    "rpds-py",
    "2026.6.3",
    "rpds_py-2026.6.3-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
)


def _lock_bytes(lock_path: Path) -> tuple[bytes, dict]:
    """Read one small regular lock and enforce its immutable repository identity."""
    try:
        with lock_path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("canonical schema lock must be a regular file")
            raw = stream.read(LOCK_MAX_BYTES + 1)
    except OSError as error:
        raise ValueError("canonical schema lock is unavailable") from error
    if len(raw) > LOCK_MAX_BYTES:
        raise ValueError("canonical schema lock must be a bounded regular file")
    identity = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if identity["sha256"] != LOCK_SHA256:
        raise ValueError("canonical schema lock identity differs")
    return raw, identity


def _normalize_dependency(item: dict) -> tuple[str, str | None, str | None, str | None]:
    """Return the lock fields that select a dependency for Python 3.13.16."""
    if not isinstance(item, dict) or not isinstance(item.get("name"), str):
        raise ValueError("invalid schema dependency entry")
    source = item.get("source")
    if source is not None and source != {"registry": REGISTRY}:
        raise ValueError("schema dependency source differs from the PyPI registry")
    if set(item) - {"name", "version", "source", "marker"}:
        raise ValueError("unsupported schema dependency selector")
    version = item.get("version")
    marker = item.get("marker")
    if version is not None and not isinstance(version, str):
        raise ValueError("invalid schema dependency version")
    if marker is not None and not isinstance(marker, str):
        raise ValueError("invalid schema dependency marker")
    return item["name"], version, source.get("registry") if source else None, marker


def _dependency_rows(row: dict) -> set[tuple[str, str | None, str | None, str | None]]:
    """Return a package's exact normalized lock dependency rows."""
    dependencies = row.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise ValueError("invalid schema dependency list")
    normalized = [_normalize_dependency(item) for item in dependencies]
    if len(normalized) != len(set(normalized)):
        raise ValueError("duplicate schema dependency selection")
    return set(normalized)


def _pin_from_wheel(
    wheel: dict, distribution: str, version: str, filename: str, role: str
) -> dict:
    """Validate and normalize one exact lock-selected wheel record."""
    url, digest, size = wheel.get("url"), wheel.get("hash"), wheel.get("size")
    parsed = urlsplit(url) if isinstance(url, str) else None
    actual_filename = PurePosixPath(parsed.path).name if parsed else ""
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.netloc != "files.pythonhosted.org"
        or parsed.query
        or parsed.fragment
        or actual_filename != filename
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(digest))
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= MAX_WHEEL_BYTES
    ):
        raise ValueError(f"{distribution} wheel pin is not a supported locked wheel")
    return {
        "distribution": distribution,
        "version": version,
        "role": role,
        "filename": actual_filename,
        "url": url,
        "sha256": digest.removeprefix("sha256:"),
        "bytes": size,
    }


def _wheel_pin(row: dict, distribution: str, version: str) -> dict:
    """Extract the one universal wheel pin admitted for a selected package."""
    wheels = row.get("wheels")
    if (
        not isinstance(wheels, list)
        or len(wheels) != 1
        or not isinstance(wheels[0], dict)
    ):
        raise ValueError(f"{distribution} must have one locked universal wheel")
    expected = f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"
    return _pin_from_wheel(wheels[0], distribution, version, expected, "pure")


def _native_oracle_pin(row: dict) -> dict:
    """Select only the locked CPython 3.13 Linux x86_64 rpds oracle wheel."""
    distribution, version, filename = NATIVE_ORACLE
    wheels = row.get("wheels")
    if not isinstance(wheels, list):
        raise ValueError("rpds-py native oracle wheel list is invalid")
    candidates = []
    for wheel in wheels:
        if not isinstance(wheel, dict) or not isinstance(wheel.get("url"), str):
            raise ValueError("rpds-py native oracle wheel pin is invalid")
        name = PurePosixPath(urlsplit(wheel["url"]).path).name
        if name == filename:
            candidates.append(wheel)
    if len(candidates) != 1:
        raise ValueError(
            "rpds-py native oracle wheel selection is missing or duplicated"
        )
    return _pin_from_wheel(
        candidates[0], distribution, version, filename, "native-oracle"
    )


def _select_from_lock(raw: bytes) -> list[dict]:
    """Validate the fixed JSON Schema graph and derive six wheel pins."""
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("invalid canonical schema lock") from error
    rows = data.get("package")
    if not isinstance(rows, list):
        raise ValueError("schema lock has no package list")

    expected_versions = dict(PACKAGES)
    selected: dict[str, dict] = {}
    matching = [
        item
        for item in rows
        if isinstance(item, dict) and item.get("name") in expected_versions
    ]
    for distribution, version in PACKAGES:
        candidates = [item for item in matching if item["name"] == distribution]
        if len(candidates) != 1:
            raise ValueError(
                "schema lock package selection is duplicated or missing: "
                f"{distribution}"
            )
        row = candidates[0]
        if row.get("version") != version or row.get("source") != {"registry": REGISTRY}:
            raise ValueError(
                f"unsupported schema dependency version or source: {distribution}"
            )
        selected[distribution] = row

    rpds = [
        item
        for item in rows
        if isinstance(item, dict) and item.get("name") == "rpds-py"
    ]
    rpds_target = [item for item in rpds if item.get("version") == "2026.6.3"]
    if (
        len(rpds_target) != 1
        or rpds_target[0].get("source") != {"registry": REGISTRY}
        or rpds_target[0].get("resolution-markers")
        != [
            "python_full_version >= '3.15'",
            "python_full_version >= '3.11' and python_full_version < '3.15'",
        ]
    ):
        raise ValueError("Python 3.13 rpds-py lock selection differs")

    expected_jsonschema = {
        ("attrs", None, None, None),
        ("jsonschema-specifications", None, None, None),
        ("referencing", None, None, None),
        ("rpds-py", "0.30.0", REGISTRY, "python_full_version < '3.11'"),
        ("rpds-py", "2026.6.3", REGISTRY, "python_full_version >= '3.11'"),
    }
    expected_specifications = {("referencing", None, None, None)}
    expected_referencing = {
        ("attrs", None, None, None),
        ("rpds-py", "0.30.0", REGISTRY, "python_full_version < '3.11'"),
        ("rpds-py", "2026.6.3", REGISTRY, "python_full_version >= '3.11'"),
        ("typing-extensions", None, None, "python_full_version < '3.13'"),
    }
    if (
        _dependency_rows(selected["jsonschema"]) != expected_jsonschema
        or _dependency_rows(selected["jsonschema-specifications"])
        != expected_specifications
        or _dependency_rows(selected["referencing"]) != expected_referencing
        or selected["attrs"].get("dependencies", [])
        or selected["packaging"].get("dependencies", [])
    ):
        raise ValueError(
            "schema dependency graph differs from the locked Python 3.13 closure"
        )

    return [
        _wheel_pin(selected[distribution], distribution, version)
        for distribution, version in PACKAGES
    ] + [_native_oracle_pin(rpds_target[0])]


def selected_inputs(lock_path: Path) -> list[dict]:
    """Return exact schema and oracle wheels selected by the immutable uv lock."""
    raw, _ = _lock_bytes(Path(lock_path))
    return _select_from_lock(raw)


def _inspect_wheel(archive: Path, pin: dict) -> dict:
    """Derive a wheel's complete safe file inventory from its pinned bytes."""
    return archive_inputs.inspect_wheel(
        archive,
        pin["sha256"],
        maximum=MAX_WHEEL_BYTES,
        expected_bytes=pin["bytes"],
        max_file=MAX_FILE,
        max_expanded=MAX_EXPANDED,
        max_members=MAX_ENTRIES,
    )


def _stage_one(archive: Path, package_root: Path, pin: dict) -> dict:
    """Verify and extract one wheel without importing or installing its code."""
    derived = _inspect_wheel(archive, pin)
    fetch_validation_tools.extract_wheel(archive, package_root)
    actual = fetch_sdk.inventory(package_root)
    if actual != derived:
        raise ValueError(f"extracted {pin['distribution']} tree differs from its wheel")
    return derived


def _package_path(packages: Path, pin: dict) -> Path:
    """Return the separate data directory for one runtime or oracle wheel."""
    name = "rpds-py-native" if pin["role"] == "native-oracle" else pin["distribution"]
    return packages / name


def run(destination: Path) -> dict:
    """Fetch six pinned wheels on Linux CI and publish data-only contents."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("schema input acquisition is restricted to Linux CI")
    destination = Path(destination)
    if (
        not destination.is_absolute()
        or not destination.parent.is_dir()
        or destination.parent.resolve() != destination.parent
        or destination.exists()
        or destination.is_symlink()
        or shutil.disk_usage(destination.parent).free < DISK_FLOOR
    ):
        raise ValueError("physical new destination and existing disk floor required")

    lock_bytes, lock_identity = _lock_bytes(LOCK_PATH)
    pins = _select_from_lock(lock_bytes)
    if sum(pin["bytes"] for pin in pins) > MAX_DOWNLOADS:
        raise ValueError("schema wheel closure exceeds its download bound")

    destination.mkdir(mode=0o700)
    archives = destination / "archives"
    packages = destination / "packages"
    archives.mkdir(mode=0o700)
    packages.mkdir(mode=0o700)
    records = []
    downloaded = 0
    for pin in pins:
        archive = archives / pin["filename"]
        remaining = MAX_DOWNLOADS - downloaded
        count = fetch_rpds.download(
            pin["url"], archive, pin["sha256"], min(MAX_WHEEL_BYTES, remaining)
        )
        if count != pin["bytes"]:
            raise ValueError(f"downloaded {pin['distribution']} wheel size differs")
        downloaded += count
        package_root = _package_path(packages, pin)
        files = _stage_one(archive, package_root, pin)
        records.append(
            {
                **pin,
                "archive": fingerprint(archive, MAX_WHEEL_BYTES),
                "files": files,
            }
        )

    receipt = {
        "format": FORMAT,
        "outcome": "success",
        "lock": lock_identity,
        "inputs": records,
        "download_bytes": downloaded,
        "wheel_code_imported": False,
        "upstream_execution": False,
        "production_enabled": False,
    }
    manifest = destination / "schema-inputs.json"
    with manifest.open("x", encoding="utf-8") as output:
        json.dump(receipt, output, sort_keys=True, indent=2)
        output.write("\n")
    return receipt


def _directory_inventory(root: Path) -> dict:
    """Inventory one existing non-symlink package tree as regular files only."""
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise ValueError("schema package directory is missing or not physical")
    except OSError as error:
        raise ValueError("schema package directory is missing") from error
    return fetch_sdk.inventory(root)


def verify(directory: Path) -> dict:
    """Re-derive all wheel files from lock-pinned archives and compare staged trees."""
    directory = Path(directory)
    if not directory.is_absolute() or directory.resolve() != directory:
        raise ValueError("absolute physical schema input directory required")
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError("schema input directory is not a physical directory")
    except OSError as error:
        raise ValueError("schema input directory is unavailable") from error

    lock_bytes, lock_identity = _lock_bytes(LOCK_PATH)
    pins = _select_from_lock(lock_bytes)
    try:
        receipt_path = directory / "schema-inputs.json"
        receipt_stat = receipt_path.lstat()
        if (
            not stat.S_ISREG(receipt_stat.st_mode)
            or receipt_stat.st_size > 2 * 1024 * 1024
        ):
            raise ValueError("schema input receipt exceeds its size bound")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("schema input receipt is unavailable or invalid") from error
    receipt_keys = {
        "format",
        "outcome",
        "lock",
        "inputs",
        "download_bytes",
        "wheel_code_imported",
        "upstream_execution",
        "production_enabled",
    }
    if not isinstance(receipt, dict) or (
        set(receipt) != receipt_keys
        or receipt.get("format") != FORMAT
        or receipt.get("outcome") != "success"
        or receipt.get("lock") != lock_identity
        or receipt.get("wheel_code_imported") is not False
        or receipt.get("upstream_execution") is not False
        or receipt.get("production_enabled") is not False
    ):
        raise ValueError("schema input receipt does not match the canonical lock")

    archives_root = directory / "archives"
    packages_root = directory / "packages"
    if not stat.S_ISDIR(archives_root.lstat().st_mode) or not stat.S_ISDIR(
        packages_root.lstat().st_mode
    ):
        raise ValueError("schema archive and package directories are required")
    if set(path.name for path in archives_root.iterdir()) != {
        pin["filename"] for pin in pins
    }:
        raise ValueError("schema archive set differs from the locked wheel set")
    if set(path.name for path in packages_root.iterdir()) != {
        _package_path(packages_root, pin).name for pin in pins
    }:
        raise ValueError("schema package set differs from the locked wheel set")
    if set(path.name for path in directory.iterdir()) != {
        "archives",
        "packages",
        "schema-inputs.json",
    }:
        raise ValueError("schema input directory contains unsupported entries")

    expected_records = []
    for pin in pins:
        archive = archives_root / pin["filename"]
        identity = fingerprint(archive, MAX_WHEEL_BYTES)
        if identity != {"bytes": pin["bytes"], "sha256": pin["sha256"]}:
            raise ValueError(f"{pin['distribution']} archive differs from its lock pin")
        derived = _inspect_wheel(archive, pin)
        package_root = _package_path(packages_root, pin)
        actual = _directory_inventory(package_root)
        if actual != derived:
            raise ValueError(
                f"{pin['distribution']} tree differs from its pinned wheel"
            )
        expected_records.append({**pin, "archive": identity, "files": derived})

    expected_download = sum(pin["bytes"] for pin in pins)
    if (
        receipt.get("inputs") != expected_records
        or receipt.get("download_bytes") != expected_download
    ):
        raise ValueError(
            "schema input receipt differs from independently derived wheels"
        )
    return {
        "verified": True,
        "packages": len(expected_records),
        "files": sum(len(record["files"]) for record in expected_records),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    run(parser.parse_args().destination)
