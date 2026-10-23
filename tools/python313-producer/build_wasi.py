"""Replay only the three recorded WASI library stages in remote isolation."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from build_native import HERE, fingerprint, sandbox_command, verify_tree
import fetch_sdk

STAGES = (
    ("wasi-zlib", "b2d0bd3dcaac908cc7a6df5ee12173657d7ac15a7cb212fd85dc3ef87a91381e",
     ("wasi/deps/lib/libz.a", "wasi/deps/include/zlib.h", "wasi/deps/include/zconf.h")),
    ("wasi-sqlite", "a21a43fea47d58860b8b042ecb4e649fcd1088ce0d2e9ac3740f96a559ae2a6e",
     ("wasi/deps/lib/libsqlite3.a", "wasi/deps/include/sqlite3.h",
      "wasi/deps/include/sqlite3ext.h")),
    ("wasi-cpython", "e275a275e259240dee7d81bf4f3d16d40c49da9012a10252ef0bcdf79444783d",
     ("wasi/libpython3.13.a", "wasi/libpython3.13.so")),
)


def run_stage(name: str, command: list[str], work: Path, evidence: Path) -> dict:
    """Keep the original supervisor and physical-cleanup requirement per stage."""
    evidence.mkdir(mode=0o700)
    (evidence / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    supervisor = HERE / "trials/2026-10-20-current-caller/supervise.py"
    result = subprocess.run([
        sys.executable, "-I", "-B", str(supervisor), "--work", str(work),
        "--receipt", str(evidence / "supervision.json"),
        "--log", str(evidence / "build.log"), "--", *command,
    ], check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    if result.returncode != 0:
        with (evidence / "build.log").open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 8192))
            print(log.read(8192).decode("utf-8", errors="replace"))
        raise RuntimeError(f"isolated {name} build failed")
    supervision = json.loads((evidence / "supervision.json").read_text())
    if supervision["outcome"] != "success":
        raise RuntimeError(f"{name} physical cleanup not confirmed")
    return supervision


def mounts(source: Path, native: Path, sdk: Path, work: Path, stage: str) -> tuple:
    """Completed build inputs become read-only before a subsequent compiler runs."""
    paths = [(source / "dependencies", "/source/dependencies"), (sdk, "/sdk"),
             (native / "work/native2", "/work/native2")]
    if stage == "wasi-sqlite":
        paths += [(work / name, "/work/" + name) for name in STAGES[0][2]]
    elif stage == "wasi-cpython":
        paths.append((work / "wasi/deps", "/work/wasi/deps"))
    elif stage != "wasi-zlib":
        raise ValueError("unrecorded WASI stage")
    return tuple(paths)


def run(source: Path, native: Path, destination: Path) -> None:
    """Build from the actual preceding native receipt, without historical pin edits."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("this resource-intensive stage is restricted to Linux CI")
    if any(not p.is_absolute() or p.resolve() != p for p in (source, native)):
        raise ValueError("physical source and native paths required")
    if (not destination.is_absolute() or not destination.parent.is_dir()
            or destination.parent.resolve() != destination.parent):
        raise ValueError("new absolute destination under physical parent required")
    destination.mkdir(mode=0o700)
    source_identity = verify_tree(source)
    native_receipt = json.loads((native / "native-build.json").read_text())
    native_python = native / "work/native2/python"
    if (native_receipt["outcome"] != "success"
            or native_receipt["source_after"] != source_identity
            or fingerprint(native_python) != native_receipt["python"]):
        raise ValueError("native build input differs from its successful receipt")
    # Full native build tree remains read-only in every child namespace.
    # Its current inventory also catches accidental host-side changes.
    native_inputs = fetch_sdk.inventory(native / "work/native2")
    sdk, sdk_inputs = fetch_sdk.acquire(destination / "sdk-input")
    sdk_manifest = fingerprint(destination / "sdk-input/sdk-manifest.json")
    work = destination / "work"
    work.mkdir(mode=0o700)
    receipt = {
        "format": "tobkiri.python313-wasi-rebuild/1", "outcome": "pending",
        "source": source_identity,
        "native_receipt": fingerprint(native / "native-build.json"),
        "native_python": native_receipt["python"], "sdk_manifest": sdk_manifest,
        "sdk_archive_sha256": fetch_sdk.SHA256, "stages": [],
        "guest_execution": False, "componentization": False,
        "producer_cli": False, "production_enabled": False,
        "historical_binary_identity_claimed": False,
    }
    receipt_path = destination / "wasi-build.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    artifacts = {}
    try:
        for name, digest, outputs in STAGES:
            recipe = HERE / f"trials/2026-10-19/{name}.sh"
            if fingerprint(recipe)["sha256"] != digest:
                raise ValueError("recorded WASI recipe changed")
            command = sandbox_command(
                source, work, recipe, mounts(source, native, sdk, work, name))
            supervision = run_stage(name, command, work, destination / name)
            for prior, identity in artifacts.items():
                if fingerprint(work / prior) != identity:
                    raise ValueError("completed WASI input changed")
            current = {output: fingerprint(work / output) for output in outputs}
            artifacts.update(current)
            receipt["stages"].append({"name": name, "recipe_sha256": digest,
                                      "supervision": supervision, "outputs": current})
            receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        # A magic header is only an output sanity check, never Wasm validation.
        with (work / "wasi/libpython3.13.so").open("rb") as library:
            if library.read(8) != b"\x00asm\x01\x00\x00\x00":
                raise ValueError("shared library is not core Wasm encoding")
        if (verify_tree(source) != source_identity
                or fetch_sdk.inventory(sdk) != sdk_inputs
                or fetch_sdk.inventory(native / "work/native2") != native_inputs):
            raise ValueError("read-only build input changed")
        receipt["inputs_unchanged"] = True
        receipt["outcome"] = "success"
    except BaseException:
        receipt["outcome"] = "failed"
        raise
    finally:
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    run(args.source, args.native, args.destination)
