"""Restore the original lock's registry sources without invoking Cargo or Rust.

The sole Git dependency and Rust toolchain are separate inputs. This stage does
not claim a complete offline Cargo environment or permission to execute it.
"""
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

from build_native import fingerprint, verify_tree
from stage_sources import inspect_archive

LOCK_SHA256 = "de6970a5ccef3fd9f5f2454ed38424dde34ea79c71d210107446099119adb9df"
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
GIT_SOURCE = ("git+https://github.com/dicej/wasm-tools?rev=b072b0ca#"
              "b072b0caa8307779558d96a62bc9522abda6a7fb")
LOCAL_PACKAGES = {"componentize-py", "componentize-py-runtime",
                  "componentize-py-test-generator"}
REGISTRY_COUNT = 410
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_DOWNLOADS = 512 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 ** 3
MAX_SOURCE_MEMBERS = 200000
DISK_FLOOR = 8 * 1024 ** 3


def registry_packages(data: bytes) -> list[dict]:
    """Bind the complete historical lock before selecting any network source."""
    if len(data) > 256 * 1024 or hashlib.sha256(data).hexdigest() != LOCK_SHA256:
        raise ValueError("original Cargo.lock identity mismatch")
    lock = tomllib.loads(data.decode("utf-8"))
    packages = []
    seen = set()
    locals_seen = set()
    git_seen = []
    for entry in lock["package"]:
        name, version = entry["name"], entry["version"]
        if (not re.fullmatch(r"[A-Za-z0-9_-]+", name)
                or not re.fullmatch(r"[A-Za-z0-9.+-]+", version)
                or (name, version) in seen):
            raise ValueError("invalid or duplicate package identity")
        seen.add((name, version))
        source = entry.get("source")
        if source is None:
            locals_seen.add(name)
        elif source == GIT_SOURCE:
            git_seen.append((name, version))
        elif source == REGISTRY:
            checksum = entry.get("checksum", "")
            if not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError("registry package has no exact checksum")
            packages.append({"name": name, "version": version, "sha256": checksum})
        else:
            raise ValueError("unrecorded Cargo source")
    if (len(packages) != REGISTRY_COUNT or locals_seen != LOCAL_PACKAGES
            or git_seen != [("wit-dylib-ffi", "0.1.0")]):
        raise ValueError("original dependency closure changed")
    return packages


def capture(stream, output, expected: str, maximum: int) -> int:
    digest = hashlib.sha256()
    count = 0
    while chunk := stream.read(min(65536, maximum - count + 1)):
        count += len(chunk)
        if count > maximum:
            raise ValueError("registry archive download budget exceeded")
        digest.update(chunk)
        output.write(chunk)
    if not count or digest.hexdigest() != expected:
        raise ValueError("registry archive differs from Cargo.lock")
    return count


def unpack(archive: Path, destination: Path, package: dict, totals: dict) -> dict:
    """Audit all tar members before publishing source; no upstream code runs."""
    identity = fingerprint(archive, MAX_ARCHIVE)
    if identity["sha256"] != package["sha256"]:
        raise ValueError("registry archive changed before unpacking")
    with archive.open("rb") as stream:
        data = stream.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE or hashlib.sha256(data).hexdigest() != identity["sha256"]:
        raise ValueError("registry archive changed while reading")
    prefix = package["name"] + "-" + package["version"]
    budget = {"members": 0, "bytes": 0}
    # Reuse the reviewed source extractor: canonical names, fixed prefix,
    # regular files/directories only, no links/collisions/special modes, and
    # the existing 16 MiB file / 256 MiB archive-expansion bounds.
    entries = inspect_archive(data, prefix, budget, embedded_type_modes=True)
    if ".cargo-checksum.json" in entries or not any(v[0] is not None for v in entries.values()):
        raise ValueError("reserved or empty registry source")
    if (totals["members"] + budget["members"] > MAX_SOURCE_MEMBERS
            or totals["bytes"] + budget["bytes"] > MAX_SOURCE_BYTES):
        raise ValueError("registry source closure budget exceeded")
    destination.mkdir(mode=0o700)
    inventory = {}
    for name, (content, mode) in sorted(entries.items()):
        path = destination / name
        if content is None:
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(content)
        path.chmod(mode)
        inventory[name] = {"bytes": len(content), "mode": oct(mode),
                           "sha256": hashlib.sha256(content).hexdigest()}
    # Cargo's directory-source manifest records the archive checksum and every
    # actual extracted file. It is not a substitute for the tar verification.
    checksums = {"package": package["sha256"],
                 "files": {name: entry["sha256"] for name, entry in inventory.items()}}
    (destination / ".cargo-checksum.json").write_text(
        json.dumps(checksums, sort_keys=True) + "\n")
    totals["members"] += budget["members"]
    totals["bytes"] += budget["bytes"]
    return {**package, "archive_bytes": identity["bytes"], "files": inventory,
            "cargo_checksums": fingerprint(destination / ".cargo-checksum.json")}


def fetch(source: Path, destination: Path) -> None:
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("registry restoration is restricted to Linux CI")
    if (not destination.is_absolute() or not destination.parent.is_dir()
            or destination.parent.resolve() != destination.parent):
        raise ValueError("new absolute destination under physical parent required")
    source_identity = verify_tree(source)
    lock = source / "componentize-py/Cargo.lock"
    if fingerprint(lock, 256 * 1024)["sha256"] != LOCK_SHA256:
        raise ValueError("original Cargo.lock identity mismatch")
    packages = registry_packages(lock.read_bytes())
    if shutil.disk_usage(destination.parent).free < DISK_FLOOR + MAX_DOWNLOADS + MAX_SOURCE_BYTES:
        raise ValueError("insufficient free disk for bounded registry restoration")
    destination.mkdir(mode=0o700)
    archives = destination / "archives"
    sources = destination / "sources"
    archives.mkdir(mode=0o700)
    sources.mkdir(mode=0o700)
    receipt = {"format": "tobkiri.python313-registry-restore/1", "outcome": "pending",
               "source": source_identity, "lock_sha256": LOCK_SHA256, "packages": [],
               "upstream_execution": False, "cargo_invoked": False,
               "git_dependency_restored": False, "offline_environment_complete": False,
               "production_enabled": False}
    receipt_path = destination / "registry-manifest.json"
    totals = {"bytes": 0, "members": 0}
    downloaded = 0
    try:
        for package in packages:
            stem = package["name"] + "-" + package["version"]
            url = f"https://static.crates.io/crates/{package['name']}/{stem}.crate"
            request = Request(url, headers={"User-Agent": "Tobkiri-source-replay"})
            temporary = archives / (stem + ".partial")
            with urlopen(request, timeout=60) as response, temporary.open("xb") as output:
                if urlsplit(response.geturl()).scheme != "https":
                    raise ValueError("registry redirect must remain HTTPS")
                downloaded += capture(response, output, package["sha256"],
                                      min(MAX_ARCHIVE, MAX_DOWNLOADS - downloaded))
            archive = archives / (stem + ".crate")
            temporary.rename(archive)
            receipt["packages"].append(unpack(archive, sources / stem, package, totals))
            if len(receipt["packages"]) % 50 == 0:
                print(json.dumps({"registry_packages_verified": len(receipt["packages"])}),
                      flush=True)
        if verify_tree(source) != source_identity:
            raise ValueError("original staged source changed")
        receipt["outcome"] = "success"
    except BaseException:
        receipt["outcome"] = "failed"
        raise
    finally:
        receipt.update(downloaded_bytes=downloaded, expanded=totals)
        receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"registry_packages_verified": len(receipt["packages"]),
                      "upstream_execution": False, "offline_environment_complete": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    fetch(args.source, args.destination)
