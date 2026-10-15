"""Verify original staged files and install captured compiler bundle bytes.

Run only as an original native CPython build tool after stages are complete.
This script never compiles, downloads, invokes Cargo, or executes a guest.
Physical locators are external; the semantic receipt/seed contains logical paths.
"""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
import tempfile

from capture_snapshot import canonical, strict_document, guest_path, read_file

PACKAGES = (
    "bundled.tar.zst", "libc++.so.zst", "libc++abi.so.zst", "libc.so.zst",
    "libcomponentize_py_runtime_async.so.zst", "libcomponentize_py_runtime_sync.so.zst",
    "libpython3.14.so.zst", "libwasi-emulated-getpid.so.zst",
    "libwasi-emulated-mman.so.zst", "libwasi-emulated-process-clocks.so.zst",
    "libwasi-emulated-signal.so.zst", "python-lib.tar.zst",
    "wasi_snapshot_preview1.reactor.wasm.zst",
)
STAGES = ("native-cpython", "c-libraries", "wasi-cpython", "runtime-sync",
          "runtime-async", "packaging")


def exact_keys(value: object, keys: set[str]) -> None:
    """Reject missing and extra fields at every receipt schema boundary."""
    if type(value) is not dict or set(value) != keys:
        raise ValueError("stage schema fields")


def sha(value: str) -> None:
    """Require a canonical SHA-256 spelling without a placeholder value."""
    if (type(value) is not str or len(value) != 64 or
            any(ch not in "0123456789abcdef" for ch in value)):
        raise ValueError("stage SHA-256 required")


def validate_structure(receipt: dict) -> None:
    """Validate the semantic stage contract independently of host locators."""
    exact_keys(receipt, {"format", "recipe_sha256", "sources", "patches", "dependency_lock_sha256",
                         "environment", "tools", "target", "stages", "packages"})
    if receipt["format"] != "tobkiri.original-python-stage/2":
        raise ValueError("stage format")
    for key in ("recipe_sha256", "dependency_lock_sha256"):
        sha(receipt[key])
    if receipt["target"] != {"role": "shared-target", "profile": "release", "jobs": 2,
                            "incremental": False, "debug": 0}:
        raise ValueError("one fixed shared target/profile required")
    if type(receipt["environment"]) is not dict or set(receipt["environment"]) != {
            "LANG", "LC_ALL", "TZ", "SOURCE_DATE_EPOCH", "PYTHONHASHSEED"}:
        raise ValueError("stage environment; PATH/ambient overrides denied")
    environment = receipt["environment"]
    if (environment["LANG"] != "C.UTF-8" or environment["LC_ALL"] != "C.UTF-8"
            or environment["TZ"] != "UTC" or environment["PYTHONHASHSEED"] != "0"
            or not environment["SOURCE_DATE_EPOCH"].isdigit()
            or str(int(environment["SOURCE_DATE_EPOCH"])) != environment["SOURCE_DATE_EPOCH"]
            or int(environment["SOURCE_DATE_EPOCH"]) >= 2**63):
        raise ValueError("stage deterministic environment")
    if type(receipt["stages"]) is not list or tuple(row.get("name") for row in receipt["stages"]) != STAGES:
        raise ValueError("original sequential stage order")
    for row in receipt["stages"]:
        exact_keys(row, {"name", "commands", "input_sha256", "output_sha256", "exit_codes"})
        sha(row["input_sha256"])
        sha(row["output_sha256"])
        if (not row["commands"] or len(row["commands"]) != len(row["exit_codes"])
                or any(type(code) is not int or code != 0 for code in row["exit_codes"])):
            raise ValueError("stage command results")
        for argv in row["commands"]:
            if (type(argv) is not list or not argv or any(type(arg) is not str for arg in argv)
                    or any(arg.startswith("/") or "\0" in arg for arg in argv)):
                raise ValueError("logical command templates only; no physical paths")
            # All Cargo invocations happen in these pre-outer stages, with the
            # one logical target role. No package hook ever starts Cargo.
            if argv[0] == "${CARGO}":
                required = {"--locked", "--target=wasm32-wasip2", "--target-dir=${SHARED_TARGET}", "--release", "-j2"}
                if not required.issubset(argv) or "CARGO_TARGET_DIR" in " ".join(argv):
                    raise ValueError("staged Cargo target/flags")
    if type(receipt["packages"]) is not dict or set(receipt["packages"]) != set(PACKAGES):
        raise ValueError("complete original package output set required")
    for name, item in receipt["packages"].items():
        exact_keys(item, {"role", "path", "size", "sha256"})
        guest_path(item["path"])
        sha(item["sha256"])
        if item["role"] != "stage" or type(item["size"]) is not int or not 0 < item["size"] <= 64 * 1024 * 1024:
            raise ValueError("package size/role")
    for group in ("sources", "patches", "tools"):
        if type(receipt[group]) is not dict or not receipt[group]:
            raise ValueError("complete source/patch/tool inventories required")
        for role, entries in receipt[group].items():
            guest_path(role)
            if type(entries) is not list or not entries:
                raise ValueError("stage inventory required")
            prior = ""
            for item in entries:
                exact_keys(item, {"path", "size", "sha256"})
                guest_path(item["path"])
                sha(item["sha256"])
                if item["path"] <= prior or type(item["size"]) is not int or item["size"] < 0:
                    raise ValueError("stage inventory order/size")
                prior = item["path"]


def verify_files(receipt: dict, locators: dict[str, str]) -> dict[str, bytes]:
    """Read each declared file once; capture package bytes for later installation."""
    captured = {}
    observed = {}
    for group in ("sources", "patches", "tools"):
        for role, entries in receipt[group].items():
            if role not in locators:
                raise ValueError("missing physical role locator")
            for item in entries:
                key = (role, item["path"])
                if key in observed:
                    if observed[key] != (item["size"], item["sha256"]):
                        raise ValueError("conflicting file declaration")
                    continue
                data = read_file(Path(locators[role]) / item["path"], 512 * 1024 * 1024)
                actual = (len(data), hashlib.sha256(data).hexdigest())
                if actual != (item["size"], item["sha256"]):
                    raise ValueError("actual staged source/patch/tool file differs")
                observed[key] = actual
    total = 0
    for name, item in receipt["packages"].items():
        if item["role"] not in locators:
            raise ValueError("missing package role locator")
        data = read_file(Path(locators[item["role"]]) / item["path"], 64 * 1024 * 1024)
        if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("actual staged package differs; cache labels are not proof")
        total += len(data)
        if total > 128 * 1024 * 1024:
            raise ValueError("package capture total")
        captured[name] = data
    return captured


def verify(recipe: dict, receipt: dict, locators: dict) -> dict[str, bytes]:
    """Deny missing pins, then verify actual outputs against the pinned recipe."""
    if recipe.get("status") != "verified" or not recipe.get("system_native_closure_verified"):
        raise ValueError("Unavailable: full source/dependency/native closure pins pending (BUILD-01)")
    validate_structure(receipt)
    if hashlib.sha256(canonical(recipe)).hexdigest() != receipt["recipe_sha256"]:
        raise ValueError("stage recipe digest")
    for key in ("sources", "patches", "tools", "dependency_lock_sha256", "environment"):
        if recipe[key] != receipt[key]:
            raise ValueError("receipt provenance differs from recipe")
    return verify_files(receipt, locators)


def install(captured: dict[str, bytes], output: Path) -> None:
    """Write only previously captured, verified bytes; never reopen stage sources."""
    if set(captured) != set(PACKAGES) or output.is_symlink() or not output.is_dir():
        raise ValueError("package install boundary")
    for name, data in captured.items():
        fd, temporary = tempfile.mkstemp(prefix=".verified-stage-", dir=output)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o444)
            os.replace(temporary, output / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def main() -> None:
    """Verify an explicit pinned semantic receipt and install compiler bundles."""
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("recipe", "receipt", "locators", "output"):
        parser.add_argument("--" + key, required=True, type=Path)
    parser.add_argument("--receipt-sha256", required=True)
    args = parser.parse_args()
    receipt_bytes = read_file(args.receipt, 4 * 1024 * 1024)
    if hashlib.sha256(receipt_bytes).hexdigest() != args.receipt_sha256:
        raise ValueError("explicit semantic receipt pin differs")
    recipe = strict_document(read_file(args.recipe, 4 * 1024 * 1024))
    receipt = strict_document(receipt_bytes)
    locators = strict_document(read_file(args.locators, 64 * 1024))
    install(verify(recipe, receipt, locators), args.output)


if __name__ == "__main__":
    main()
