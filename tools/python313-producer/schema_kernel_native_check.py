"""CI-only native functional precheck; never a Wasm/fuel/product acceptance gate."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

HERE = Path(__file__).resolve().parent
FORMAT = "tobkiri.schema-kernel-native-check/1"


def run(args, receipt):
    """Build frozen source, then compare real native objects with the oracle."""
    import build_rpds
    import build_schema_kernel as builder
    import fetch_schema
    import fetch_sdk
    from build_native import fingerprint

    for path in (args.source, args.vendor, args.inputs, args.cargo_home):
        build_rpds.physical(path)
    destination = args.destination
    if (not destination.is_absolute() or os.path.lexists(destination)
            or destination.parent.resolve(strict=True) != destination.parent):
        raise ValueError("new physical native precheck destination required")
    destination.mkdir(mode=0o700)
    receipt["_destination_created"] = True
    builder.packages(args.source)
    fetch_schema.verify(args.inputs)
    source = fetch_sdk.inventory(args.source)
    if (set(source) != {"Cargo.toml", "Cargo.lock", "README.md", "COPYING.jsonschema",
                        "COPYING.python", "adapter.py", "src/lib.rs",
                        "src/traversal.rs", "src/keywords.rs", "src/coerce_args.rs"}
            or any("link" in item for item in source.values())
            or sum(item["bytes"] for item in source.values()) > builder.MAX_SOURCE):
        raise ValueError("native kernel source closure differs")
    reference = json.loads(builder.fingerprint_and_read(
        args.reference, 4 * 1024 * 1024))
    if (reference.get("format") != "tobkiri.schema-kernel-reference/1"
            or reference.get("oracle_commit")
            != "54e7da008997a3eb45876cbe18459dba9902cc9d"
            or reference.get("original_schema_count") != 42
            or len(reference.get("cases", [])) != 64):
        raise ValueError("complete original 42+22 reference required")
    receipt.update(source_head=os.environ.get("GITHUB_SHA"), source_files=source,
                   reference=fingerprint(args.reference, 4 * 1024 * 1024))
    target = destination / "target"
    environment = {**os.environ, "CARGO_HOME": str(args.cargo_home),
                   "PYO3_PYTHON": sys.executable,
                   "RUSTFLAGS": "--cfg pyo3_disable_reference_pool",
                   "PYTHONDONTWRITEBYTECODE": "1"}
    environment.pop("CARGO_ENCODED_RUSTFLAGS", None)
    environment.pop("CARGO_BUILD_TARGET", None)
    command = ["cargo", "build", "--frozen", "--release", "-j1",
               "--manifest-path", str(args.source / "Cargo.toml"),
               "--target-dir", str(target), "--message-format=json",
               "--config", 'source.crates-io.replace-with="schema-vendor"',
               "--config", 'source.schema-vendor.directory='
               + json.dumps(str(args.vendor))]
    receipt["stage"] = "native-build"
    stdout, stderr = destination / "cargo.jsonl", destination / "cargo.log"
    with stdout.open("xb") as out, stderr.open("xb") as err:
        result = subprocess.run(command, cwd=destination, env=environment,
                                stdout=out, stderr=err, timeout=600, check=False)
    if result.returncode:
        with stderr.open("rb") as stream:
            stream.seek(max(0, stderr.stat().st_size - 3000))
            print(stream.read().decode(errors="replace"), flush=True)
        raise ValueError("native kernel compilation failed")
    rows = [json.loads(line) for line in builder.fingerprint_and_read(
        stdout, 4 * 1024 * 1024).splitlines()]
    artifacts = [row for row in rows if row.get("reason") == "compiler-artifact"
                 and row.get("manifest_path") == str(args.source / "Cargo.toml")
                 and row.get("target", {}).get("name") == builder.EXTENSION
                 and row["target"].get("kind") == ["cdylib"]]
    if (len(artifacts) != 1 or len(artifacts[0].get("filenames", [])) != 1
            or [row.get("success") for row in rows
                if row.get("reason") == "build-finished"] != [True]):
        raise ValueError("one actual successful native cdylib required")
    artifact = Path(artifacts[0]["filenames"][0])
    if (artifact.parent != target / "release" or artifact.suffix != ".so"
            or artifact.resolve(strict=True) != artifact):
        raise ValueError("native artifact escaped its actual target")
    artifact_identity = fingerprint(artifact, 32 * 1024 * 1024)
    module_dir = destination / "module"
    module_dir.mkdir(mode=0o700)
    extension = module_dir / (builder.EXTENSION + ".abi3.so")
    shutil.copyfile(artifact, extension)
    if fingerprint(extension, 32 * 1024 * 1024) != artifact_identity:
        raise ValueError("native extension copy differs")
    receipt["extension"] = artifact_identity

    inputs = json.loads(builder.fingerprint_and_read(
        args.inputs / "schema-inputs.json", 2 * 1024 * 1024))["inputs"]
    package_paths = [args.inputs / "packages" / (
        "rpds-py-native" if row["role"] == "native-oracle" else row["distribution"]
    ) for row in inputs]
    if len(package_paths) != 6:
        raise ValueError("complete checksum-verified native wheel selection required")
    from schema_kernel_translate import native_overlay
    selected = [path for path, row in zip(package_paths, inputs)
                if row["distribution"] == "jsonschema"]
    if len(selected) != 1:
        raise ValueError("one authenticated original jsonschema package required")
    overlay = destination / "translated-jsonschema"
    receipt["keyword_translation"] = native_overlay(selected[0], overlay)
    package_paths = [overlay if path == selected[0] else path for path in package_paths]
    from schema_stdlib_translate import native_overlay as stdlib_native_overlay
    config = sysconfig.get_paths()
    stdlib_overlay = destination / "translated-stdlib"
    receipt["stdlib_translation"] = stdlib_native_overlay(
        Path(config["stdlib"]), stdlib_overlay
    )
    stdlib = [config["stdlib"], config["platstdlib"],
              sysconfig.get_config_var("DESTSHARED")]
    control = {
        "source": str(args.source), "inputs": str(args.inputs),
        "reference": str(args.reference), "extension": str(extension),
        "module_dir": str(module_dir),
        "package_paths": [str(path) for path in package_paths],
        "stdlib_paths": [path for path in stdlib if isinstance(path, str)],
        "stdlib_overlay": str(stdlib_overlay), "profile_calls": args.profile_calls,
        "extension_identity": artifact_identity,
        "output": str(destination / "native-worker-result.json"),
    }
    _run_comparison_worker(control, destination, receipt, builder)
    receipt["stage"] = "native-source-recheck"
    if fetch_sdk.inventory(args.source) != source:
        receipt["outcome"] = "failure"
        raise ValueError("native kernel source changed during comparison")
    receipt.update(stage="native-comparison", outcome="success")


def _run_comparison_worker(control, destination, receipt, builder):
    """Require an isolated first import; retain the child's actual failure record."""
    control_path = destination / "native-worker-control.json"
    encoded = (json.dumps(control, sort_keys=True) + "\n").encode()
    if len(encoded) > 4 * 1024 * 1024:
        raise ValueError("bounded native worker control exceeded")
    descriptor = os.open(control_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
    command = [sys.executable, "-I", "-S", "-B",
               str(HERE / "schema_kernel_native_worker.py"), str(control_path)]
    environment = {name: os.environ[name] for name in (
        "GITHUB_ACTIONS", "LD_LIBRARY_PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"
    ) if name in os.environ}
    stdout = destination / "native-worker.stdout"
    stderr = destination / "native-worker.stderr"
    receipt["stage"] = "native-comparison-child"
    timed_out, returncode = False, None
    with stdout.open("xb") as out, stderr.open("xb") as err:
        try:
            result = subprocess.run(command, cwd=destination, env=environment,
                                    stdout=out, stderr=err, timeout=600, check=False)
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
    child = None
    output = Path(control["output"])
    receipt["comparison_worker"] = {
        "format": "tobkiri.schema-kernel-native-worker/1",
        "isolated": True, "no_site": True, "no_bytecode": True,
        "timeout_seconds": 600, "timed_out": timed_out, "returncode": returncode,
        "outcome": "missing-receipt",
    }
    try:
        if output.exists():
            child = json.loads(builder.fingerprint_and_read(output, 4 * 1024 * 1024))
            if (type(child) is not dict
                    or child.get("format") != "tobkiri.schema-kernel-native-worker/1"
                    or child.get("outcome") not in {"success", "failure"}):
                raise ValueError("native comparison worker receipt differs")
            receipt.update({key: value for key, value in child.items()
                            if key not in {"format", "outcome"}})
            receipt["comparison_worker"]["outcome"] = child["outcome"]
        if (timed_out or returncode != 0 or child is None
                or child["outcome"] != "success"):
            receipt["outcome"] = "failure"
            if child is None:
                receipt["stage"] = ("native-comparison-child-timeout" if timed_out
                                    else "native-comparison-child-no-receipt")
            receipt.setdefault("error", {
                "type": ("subprocess.TimeoutExpired" if timed_out
                         else "builtins.RuntimeError"),
                "message": ("native comparison worker timed out" if timed_out
                            else "native comparison worker did not exit successfully"),
            })
            raise ValueError("isolated native comparison worker failed")
    except BaseException:
        for path in (stdout, stderr):
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 3000))
                print(stream.read().decode(errors="replace"), flush=True)
        raise


def main():
    if (sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true"
            or sys.version_info[:3] != (3, 13, 16)):
        raise RuntimeError("native execution requires pinned Linux GitHub Actions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-calls", action="store_true")
    for name in ("source", "vendor", "inputs", "reference", "cargo-home",
                 "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    receipt = {"format": FORMAT, "outcome": "failure", "wasm_executed": False,
               "wasm_budget_proved": False, "full_catalog_executed": False,
               "actual_os_owner_proof": False, "production_enabled": False}
    try:
        run(args, receipt)
    except BaseException as error:
        name = type(error).__module__ + "." + type(error).__qualname__
        if not isinstance(error, Exception) and name != "pyo3_runtime.PanicException":
            raise
        receipt.setdefault("error", {"type": name, "message": str(error)[:1500]})
        raise
    finally:
        created = receipt.pop("_destination_created", False)
        if created and args.destination.is_dir():
            output = args.destination / "schema-kernel-native-check.json"
            with output.open("x") as stream:
                stream.write(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
        public_receipt = {key: value for key, value in receipt.items()
                          if key != "source_files"}
        print(json.dumps(public_receipt, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
