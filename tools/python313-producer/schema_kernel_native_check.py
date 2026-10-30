"""CI-only native functional precheck; never a Wasm/fuel/product acceptance gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
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
    from schema_kernel_observation import first_difference

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
                        "adapter.py", "src/lib.rs", "src/traversal.rs", "src/keywords.rs"}
            or any("link" in item for item in source.values())
            or sum(item["bytes"] for item in source.values()) > builder.MAX_SOURCE):
        raise ValueError("native kernel source closure differs")
    reference = json.loads(builder.fingerprint_and_read(args.reference, 4 * 1024 * 1024))
    if (reference.get("format") != "tobkiri.schema-kernel-reference/1"
            or reference.get("oracle_commit") != "54e7da008997a3eb45876cbe18459dba9902cc9d"
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
               "--config", 'source.schema-vendor.directory=' + json.dumps(str(args.vendor))]
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
    config = sysconfig.get_paths()
    stdlib = [config["stdlib"], config["platstdlib"], sysconfig.get_config_var("DESTSHARED")]
    sys.path[:] = list(dict.fromkeys([str(HERE), str(module_dir),
                                     *(str(p) for p in package_paths),
                                     *(p for p in stdlib if isinstance(p, str))]))
    # The workflow invokes -S -B with an empty PYTHONPATH; no ambient site imports.
    receipt["stage"] = "native-comparison"
    import _tobkiri_schema_kernel as kernel
    if (Path(kernel.__file__).resolve(strict=True) != extension
            or kernel.production_enabled is not False
            or kernel.implemented_handler_count != 36):
        raise ValueError("actual source-built private extension import differs")
    from jsonschema import Draft202012Validator, exceptions
    from jsonschema_specifications import REGISTRY
    from schema_kernel_api_checks import exercise_api, exercise_unsupported_native
    from schema_kernel_observation import meta_nodes, node_digest, observe

    spec = importlib.util.spec_from_file_location("schema_adapter", args.source / "adapter.py")
    if spec is None or spec.loader is None:
        raise ValueError("actual native adapter source unavailable")
    adapter = importlib.util.module_from_spec(spec)
    sys.modules["schema_adapter"] = adapter
    spec.loader.exec_module(adapter)
    adapter.install_standard_classes()
    actual_metadata = {
        "schema": node_digest(Draft202012Validator.META_SCHEMA),
        "registry": {uri: node_digest(REGISTRY.contents(uri)) for uri in sorted(REGISTRY)},
        "formats": sorted(Draft202012Validator.FORMAT_CHECKER.checkers),
    }
    expected_metadata = {
        "schema": node_digest(reference["meta_schema"]),
        "registry": {row["uri"]: node_digest(row["schema"]) for row in reference["meta_registry"]},
        "formats": reference["meta_format_checker_names"],
    }
    difference = first_difference(expected_metadata, actual_metadata)
    if difference is not None:
        receipt["difference"] = difference
        raise ValueError("native meta registry/checker observation differs")
    known_nodes = meta_nodes()
    for index, case in enumerate(reference["cases"]):
        receipt.update(case_index=index, case_name=case["name"])
        schema = case["schema"]
        try:
            Draft202012Validator.check_schema(schema)
        except exceptions.SchemaError as error:
            actual = {"valid": False, "error": observe(error, schema, known_nodes)}
        else:
            actual = {"valid": True, "error": None}
        difference = first_difference(case["expected"], actual)
        if difference is not None:
            receipt["difference"] = difference
            raise ValueError("native schema result differs from real original reference")
    api = exercise_api()
    if len(api) != 14 or not all(value is True for value in api.values()):
        raise ValueError("all fourteen actual API groups required")
    unsupported = exercise_unsupported_native()
    counts = kernel.type_dispatch_counts()
    if any(type(counts.get(key)) is not int or counts[key] <= 0
           for key in ("native_default", "explicit_original")):
        raise ValueError("both actual type dispatch paths must execute")
    if fetch_sdk.inventory(args.source) != source:
        raise ValueError("native kernel source changed during comparison")
    receipt.update(outcome="success", native_64_success=True, api_14_success=True,
                   api_checks=api, unsupported_native=unsupported, type_dispatch_counts=counts,
                   pointer_bits=struct.calcsize("P") * 8)


def main():
    if (sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true"
            or sys.version_info[:3] != (3, 13, 16)):
        raise RuntimeError("native execution requires pinned Linux GitHub Actions")
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "vendor", "inputs", "reference", "cargo-home", "destination"):
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
        receipt["error"] = {"type": name, "message": str(error)[:1500]}
        raise
    finally:
        created = receipt.pop("_destination_created", False)
        if created and args.destination.is_dir():
            with (args.destination / "schema-kernel-native-check.json").open("x") as stream:
                stream.write(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
        print(json.dumps({key: value for key, value in receipt.items() if key != "source_files"},
                         sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
