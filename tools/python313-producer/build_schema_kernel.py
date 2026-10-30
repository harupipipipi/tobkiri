"""Build and call the private schema extension only in supervised Linux CI.

This smoke probe is not the 64-case oracle comparison or full catalog proof.
Registry archives are supplied by the CI workflow; this script downloads none.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

LOCK_SHA256 = "7bd7deb115b32dc7012b44a960efc45d6591ac148fa5a0466c1741a523d9b065"
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
EXTENSION = "_tobkiri_schema_kernel"
INTERFACE = "tobkiri-ui:schema-probe/probe@1.0.0"
MAX_SOURCE = 4 * 1024 * 1024
MAX_ARCHIVES = 128 * 1024 * 1024

# Import happens during producer preinitialization; inspect happens only when
# the actual native DATA runner invokes the captured component's run export.
APP = '''"""One actual private-extension call after producer snapshotting."""
import json
import struct
import sys
import sysconfig

import _tobkiri_schema_kernel as kernel
import wit_world.exports

TARGET = {
    "python": list(sys.version_info[:3]),
    "pointer_bits": struct.calcsize("P") * 8,
    "soabi": sysconfig.get_config_var("SOABI"),
    "extension_file": kernel.__file__,
}


class Probe(wit_world.exports.Probe):
    def run(self) -> str:
        actual = {"value": []}
        report = kernel.inspect(
            '{"type":"object","properties":{"value":{"type":"string"}}}',
            actual,
        )
        root_is_original = report.pop("original_instance_root") is actual
        errors = report["errors"]
        if (not root_is_original or report["compiled"] is not True
                or report["valid"] is not False or len(errors) != 1
                or errors[0]["keyword"] != "type"
                or errors[0]["instance_path"] != "/value"
                or errors[0]["instance_clone"] != []
                or report["production_enabled"] is not False):
            raise ValueError("actual kernel observation differs")
        return json.dumps({
            "target": TARGET,
            "report": report,
            "input_root_is_original": root_is_original,
            "error_instance_is_original": (
                errors[0]["instance_clone"] is actual["value"]
            ),
            "production_enabled": False,
        }, sort_keys=True)

    def probe_clock(self) -> str:
        raise RuntimeError("outside the private kernel smoke scope")

    def probe_monotonic(self) -> str:
        raise RuntimeError("outside the private kernel smoke scope")

    def probe_denied_io(self, kind: str) -> str:
        raise RuntimeError("outside the private kernel smoke scope")
'''


def packages(source: Path) -> list[dict]:
    """Admit the actual source lock and only its checksum-bound registry graph."""
    import tomllib

    from build_native import fingerprint

    if fingerprint(source / "Cargo.lock", 256 * 1024)["sha256"] != LOCK_SHA256:
        raise ValueError("schema kernel source lock differs")
    manifest = tomllib.loads((source / "Cargo.toml").read_text())
    dependencies = manifest["dependencies"]
    if (
        manifest["package"]["name"] != "tobkiri-schema-kernel"
        or manifest["package"]["version"] != "0.1.0"
        or manifest["package"].get("publish") is not False
        or manifest.get("workspace") != {}
        or manifest["lib"] != {"name": EXTENSION, "crate-type": ["cdylib"]}
        or dependencies["jsonschema"]
        != {"version": "=0.60.2", "default-features": False, "features": ["pyo3"]}
        or dependencies["pyo3"].get("version") != "=0.30.1"
        or "abi3-py313" not in dependencies["pyo3"].get("features", [])
    ):
        raise ValueError("private offline limited-ABI manifest differs")
    entries = tomllib.loads((source / "Cargo.lock").read_text())["package"]
    result, roots, seen = [], [], set()
    for item in entries:
        name, version = item["name"], item["version"]
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]+", name)
            or not re.fullmatch(r"[A-Za-z0-9.+-]+", version)
            or (name, version) in seen
        ):
            raise ValueError("invalid or duplicate kernel lock package")
        seen.add((name, version))
        if "source" not in item:
            roots.append((name, version))
        elif item["source"] == REGISTRY and re.fullmatch(
            r"[0-9a-f]{64}", item.get("checksum", "")
        ):
            result.append({
                "name": name, "version": version, "sha256": item["checksum"]
            })
        else:
            raise ValueError("unrecorded schema kernel dependency source")
    if roots != [("tobkiri-schema-kernel", "0.1.0")] or not result:
        raise ValueError("schema kernel root closure differs")
    return result


def restore_registry_vendor(source: Path, archives: Path, destination: Path) -> None:
    """Restore complete crate sources; Cargo vendor filters archive members."""
    from build_native import fingerprint
    import build_rpds
    import fetch_registry
    import fetch_rpds

    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("registry restoration is restricted to Linux CI")
    build_rpds.physical(source)
    build_rpds.physical(archives)
    build_rpds.physical(destination.parent)
    if not destination.is_absolute() or destination.exists():
        raise ValueError("new physical registry destination required")
    pins = packages(source)
    size = sum(fingerprint(archives / (p["name"] + "-" + p["version"] + ".crate"),
                           fetch_rpds.MAX_CRATE_ARCHIVE)["bytes"] for p in pins)
    if size > MAX_ARCHIVES:
        raise ValueError("registry archive closure exceeds its bound")
    destination.mkdir(mode=0o700)
    totals = {"bytes": 0, "members": 0}
    for pin in pins:
        stem = pin["name"] + "-" + pin["version"]
        # The existing bounded extractor verifies the lock checksum first and
        # retains every member, including files Cargo vendor elects to omit.
        fetch_registry.unpack(archives / (stem + ".crate"),
                              destination / stem, pin, totals)
    verify_registry(source, destination, archives)


def verify_registry(source: Path, vendor: Path, archives: Path) -> dict:
    """Derive vendored bytes from original lock-checksummed crate archives."""
    from build_native import fingerprint
    import fetch_rpds
    import fetch_sdk

    expected, identities = {}, {}
    totals, downloaded = {"bytes": 0, "members": 0}, 0
    for pin in packages(source):
        stem = pin["name"] + "-" + pin["version"]
        archive = archives / (stem + ".crate")
        identity = fingerprint(archive, fetch_rpds.MAX_CRATE_ARCHIVE)
        downloaded += identity["bytes"]
        if downloaded > MAX_ARCHIVES:
            raise ValueError("schema registry archive closure exceeds its bound")
        files = fetch_rpds.registry_inventory(archive, pin, totals)
        files.pop(".cargo-checksum.json")
        checksum = vendor / stem / ".cargo-checksum.json"
        checksum_identity = fingerprint(checksum, MAX_SOURCE)
        checksums = json.loads(checksum.read_bytes())
        expected_checksums = {
            "package": pin["sha256"],
            "files": {name: entry["sha256"] for name, entry in files.items()},
        }
        if checksums != expected_checksums:
            actual_files = checksums.get("files", {})
            expected_files = expected_checksums["files"]
            if not isinstance(actual_files, dict):
                raise ValueError("vendored registry checksum files is not an object")
            difference = {
                "crate": stem,
                "package_checksum_matches": checksums.get("package") == pin["sha256"],
                "missing": sorted(expected_files.keys() - actual_files.keys())[:10],
                "extra": sorted(actual_files.keys() - expected_files.keys())[:10],
                "changed": [
                    {"path": name, "archive": expected_files[name],
                     "vendor": actual_files[name]}
                    for name in sorted(expected_files.keys() & actual_files.keys())
                    if expected_files[name] != actual_files[name]
                ][:10],
            }
            raise ValueError("vendored registry checksum derivation differs: "
                             + json.dumps(difference, sort_keys=True))
        expected.update({stem + "/" + name: entry for name, entry in files.items()})
        expected[stem + "/.cargo-checksum.json"] = {
            **checksum_identity,
            "mode": oct(checksum.stat().st_mode & 0o777),
        }
        identities[archive.name] = identity
    if fetch_sdk.inventory(vendor) != expected:
        raise ValueError("vendored schema registry differs from pinned archives")
    if {path.name for path in archives.iterdir()} != set(identities):
        raise ValueError("unexpected or missing schema registry archives")
    return {"archives": identities, "vendor_files": expected}


def verify_inputs(
    source: Path, vendor: Path, archives: Path, rust: Path, sdk: Path,
    producer: Path, validation: Path, native_data_consumer: Path,
) -> dict:
    """Reuse original producer/tool admission and bind every prototype source."""
    from build_native import fingerprint
    import build_rpds
    import fetch_sdk
    import runtime_inputs

    for path in (source, vendor, archives, rust, sdk, producer, validation):
        build_rpds.physical(path)
    if (
        not native_data_consumer.is_absolute()
        or native_data_consumer.resolve(strict=True) != native_data_consumer
    ):
        raise ValueError("physical native DATA consumer required")
    source_files = fetch_sdk.inventory(source)
    if (
        set(source_files) != {"Cargo.toml", "Cargo.lock", "README.md", "src/lib.rs"}
        or any("link" in item for item in source_files.values())
        or sum(item["bytes"] for item in source_files.values()) > MAX_SOURCE
    ):
        raise ValueError("schema kernel source closure differs")
    registry = verify_registry(source, vendor, archives)
    build_rpds.verify_rust(rust, runtime_inputs.report(rust / "rust-manifest.json"))
    manifest = sdk / "sdk-manifest.json"
    fingerprint(manifest, 16 * 1024 * 1024)
    build_rpds.verify_sdk(sdk, json.loads(manifest.read_bytes()))
    build_rpds.verify_validation(
        validation, runtime_inputs.report(validation / "validation-inputs.json")
    )
    cli = runtime_inputs.report(producer / "cli-build.json")
    source_pin = [
        pin for name, pin in cli["predecessors"].items()
        if Path(name).name == "cli-source.json"
    ]
    if (
        cli.get("producer_cli") is not True
        or cli.get("inputs_unchanged") is not True
        or fingerprint(producer / "work/target/release/componentize-py")
        != cli["binary"]
        or len(source_pin) != 1
        or fingerprint(producer / "cli-source.json") != source_pin[0]
    ):
        raise ValueError("saved original producer identity differs")
    return {
        "source_files": source_files,
        "registry": registry,
        "native_data_consumer": fingerprint(native_data_consumer),
        "receipts": {
            str(path): fingerprint(path) for path in (
                rust / "rust-manifest.json", manifest,
                validation / "validation-inputs.json",
                producer / "cli-build.json",
                producer / "cli-source.json",
            )
        },
    }


def probe() -> None:
    """Use the saved producer, then invoke the real native DATA consumer."""
    sys.path[:0] = ["/validation-deps", "/"]
    from composition import compose
    import driver
    from native_driver import TEST, validate_report
    from rpds_driver import custom_sections, read
    from wasmtime import Config, Engine, MemoryType, Module, TableType
    from wasmtime.component import Component

    work = Path("/work")
    messages = [json.loads(line) for line in read(
        work / "cargo-artifacts.jsonl", maximum=4 * 1024 * 1024
    ).splitlines()]
    artifacts = [item for item in messages if (
        item.get("reason") == "compiler-artifact"
        and item.get("manifest_path") == "/kernel/Cargo.toml"
        and item.get("target", {}).get("name") == EXTENSION
        and item["target"].get("kind") == ["cdylib"]
        and item["target"].get("crate_types") == ["cdylib"]
        and item["target"].get("src_path") == "/kernel/src/lib.rs"
        and item.get("profile", {}).get("test") is False
    )]
    if (
        [item.get("success") for item in messages
         if item.get("reason") == "build-finished"] != [True]
        or len(artifacts) != 1
        or len(artifacts[0].get("filenames", [])) != 1
    ):
        raise ValueError("Cargo did not report one successful kernel cdylib")
    extension_path = Path(artifacts[0]["filenames"][0])
    if extension_path.parent != work / "target/wasm32-wasip2/release":
        raise ValueError("kernel output escaped its selected target")
    extension = read(extension_path)
    sections = custom_sections(extension)
    limited = [path for path in (work / "target/wasm32-wasip2/release/build").glob(
        "pyo3-*/output"
    ) if "cargo:rustc-cfg=Py_LIMITED_API" in read(path, maximum=1024 * 1024).decode()]
    if "dylink.0" not in sections or not limited:
        raise ValueError("actual extension lacks dynamic or limited-API evidence")
    def inspection_engine() -> Engine:
        # Engine consumes Config. Each static ABI/boundary inspection needs
        # its own config; these engines never execute the guest request.
        config = Config()
        config.parallel_compilation = False
        return Engine(config)

    with inspection_engine() as engine, Module(engine, extension) as module:
        imports = [{"module": item.module, "name": item.name}
                   for item in module.imports]
        exports = [item.name for item in module.exports]
        if (
            "PyInit_" + EXTENSION not in exports
            or not any(isinstance(item.type, MemoryType) for item in module.imports)
            or not any(isinstance(item.type, TableType) for item in module.imports)
        ):
            raise ValueError("actual extension Python/shared memory/table ABI missing")
    stage = work / "stage"
    stage.mkdir(mode=0o700)
    for name in ("app.py", "probe.wit"):
        shutil.copyfile(Path("/probe-source") / name, stage / name)
    captured = stage / (EXTENSION + ".abi3.so")
    captured.write_bytes(extension)
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": "/work/home", "TMPDIR": "/work/tmp",
        "VIRTUAL_ENV": "/empty-venv", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0", "RAYON_NUM_THREADS": "1", "TOKIO_WORKER_THREADS": "1",
    }
    command = ["/producer", "-d", "probe.wit", "-w", "ui-schema-probe"]
    subprocess.run([*command, "bindings", "."], cwd=stage, env=environment,
                   check=True, timeout=180)
    subprocess.run([
        *command, "componentize", "--stub-wasi", "app", "-o", "component.wasm",
    ], cwd=stage, env=environment, check=True, timeout=600)
    if read(captured) != extension or any(
        read(stage / name) != read(Path("/probe-source") / name)
        for name in ("app.py", "probe.wit")
    ):
        raise ValueError("captured kernel/probe source changed during preinit")
    inner_bytes = read(stage / "component.wasm")
    with inspection_engine() as engine, Component(engine, inner_bytes) as inner:
        driver.boundary(inner, engine, inner=True)
    del inner_bytes
    composition = compose(Path("/wac"), stage / "component.wasm", stage, stage,
                          "ui-schema-probe", [INTERFACE], environment)
    component = stage / "outer.wasm"
    component_identity = "sha256:" + hashlib.sha256(read(component)).hexdigest()
    with inspection_engine() as engine, Component(engine, read(component)) as outer:
        boundary = driver.boundary(outer, engine, inner=False)
    output = work / "native-data-observation.json"
    consumer_identity = hashlib.sha256(read(
        Path("/native-data-consumer"), maximum=1024 * 1024 * 1024
    )).hexdigest()
    with (work / "native-data-observation.log").open("xb") as log:
        subprocess.run([
            "/native-data-consumer", "--exact", TEST, "--ignored", "--nocapture",
            "--test-threads=1",
        ], env={
            **environment, "TOBKIRI_NATIVE_DATA_CI": "1",
            "TOBKIRI_NATIVE_COMPONENT": str(component),
            "TOBKIRI_NATIVE_COMPONENT_SHA256": component_identity,
            "TOBKIRI_NATIVE_OPERATION": "run", "TOBKIRI_NATIVE_REPORT": str(output),
        }, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    report = json.loads(read(output))
    observed = validate_report(report, component_identity, "run")
    result = (
        json.loads(report["value"]) if isinstance(report.get("value"), str) else None
    )
    if (
        report.get("error") is not None or observed["post_return"] is not True
        or not isinstance(result, dict)
        or result.get("input_root_is_original") is not True
        or result.get("error_instance_is_original") is not False
        or result.get("production_enabled") is not False
        or result.get("report", {}).get("compiled") is not True
        or result["report"].get("valid") is not False
        or result.get("target", {}).get("python") != [3, 13, 16]
        or result["target"].get("pointer_bits") != 32
        or not isinstance(result["target"].get("soabi"), str)
        or "313" not in result["target"]["soabi"]
        or "wasm32" not in result["target"]["soabi"]
        or not result["target"].get("extension_file", "").endswith(
            EXTENSION + ".abi3.so"
        )
        or "sha256:" + hashlib.sha256(read(component)).hexdigest() != component_identity
    ):
        raise ValueError("real native DATA kernel call differs; inspect native report")
    (work / "probe.json").write_text(json.dumps({
        "format": "tobkiri.schema-kernel-probe/1", "passed": True,
        "extension_sha256": hashlib.sha256(extension).hexdigest(),
        "component_sha256": component_identity,
        "actual_limited_api": True,
        "limited_api_outputs": [str(path) for path in limited],
        "cargo_artifact": artifacts[0], "custom_sections": sections,
        "extension_imports": imports, "extension_exports": exports,
        "composition": composition, "boundary": boundary,
        "native_data_consumer_sha256": consumer_identity, "native_report": report,
        "result": result, "original_64_cases_compared": False,
        "full_catalog_executed": False, "production_enabled": False,
    }, indent=2) + "\n")


def run(
    source: Path, vendor: Path, archives: Path, rust: Path, sdk: Path,
    producer: Path, validation: Path, native_data_consumer: Path, destination: Path,
) -> None:
    """Verify source and tools, build offline, then run an actual bounded probe."""
    from build_native import GIB, HERE, fingerprint, sandbox_command
    import build_rpds
    from build_wasi import run_stage
    import fetch_sdk

    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("schema kernel build and execution are restricted to Linux CI")
    if (
        not destination.is_absolute() or not destination.parent.is_dir()
        or destination.parent.resolve(strict=True) != destination.parent
        or shutil.disk_usage(destination.parent).free < 8 * GIB
    ):
        raise ValueError("new physical destination and original disk floor required")
    arguments = (
        source, vendor, archives, rust, sdk, producer, validation,
        native_data_consumer,
    )
    inputs = verify_inputs(*arguments)
    destination.mkdir(mode=0o700)
    work = destination / "work"
    work.mkdir(mode=0o700)
    for name in ("cargo-home", "home", "tmp", "empty-venv"):
        (work / name).mkdir(mode=0o700)
    config = destination / "build-config"
    config.mkdir(mode=0o700)
    (config / "pyo3-config.txt").write_text(build_rpds.PYO3_CONFIG)
    (config / "cargo.toml").write_text(build_rpds.CARGO_CONFIG)
    recipe = destination / "build.sh"
    recipe.write_text(build_rpds.RECIPE.replace("cd /rpds\n", "cd /kernel\n"))
    probe_source = destination / "probe-source"
    probe_source.mkdir(mode=0o700)
    (probe_source / "app.py").write_text(APP)
    shutil.copyfile(HERE / "schema_probe/probe.wit", probe_source / "probe.wit")
    replay = destination / "probe.sh"
    replay.write_text("set -eu\n/usr/bin/python3 -I -B /kernel-driver.py --probe\n")
    helpers = (
        (Path(__file__).resolve(), "/kernel-driver.py"),
        (HERE / "rpds_probe/driver.py", "/rpds_driver.py"),
        (HERE / "schema_probe/driver.py", "/driver.py"),
        (HERE / "schema_probe/native_driver.py", "/native_driver.py"),
        (HERE.parents[1] / "sdk/python/composition.py", "/composition.py"),
    )
    private = {str(path): fingerprint(path) for path in (
        recipe, replay, config / "cargo.toml", config / "pyo3-config.txt",
        *(path for path, _ in helpers),
    )}
    probe_files = fetch_sdk.inventory(probe_source)
    receipt = {
        "format": "tobkiri.schema-kernel-build/1", "outcome": "pending",
        "inputs": inputs, "configuration": private, "probe_source": probe_files,
        "target": "wasm32-wasip2", "feature": "pyo3/abi3-py313", "stages": [],
        "guest_executed": False, "original_64_cases_compared": False,
        "full_catalog_executed": False, "production_enabled": False,
    }
    output = destination / "schema-kernel-build.json"
    try:
        command = sandbox_command(None, work, recipe, (
            (source, "/kernel"), (vendor, "/vendor"),
            (rust / "toolchain", "/rust"), (sdk / "sdk", "/sdk"),
            (config, "/build-config"),
        ), address_space_bytes=4 * GIB)
        receipt["stages"].append(run_stage(
            "schema-kernel-abi3-build", command, work, destination / "build"
        ))
        command = sandbox_command(None, work, replay, (
            *helpers, (probe_source, "/probe-source"),
            (validation / "wasmtime", "/validation-deps"),
            (validation / "wac-cli", "/wac"),
            (producer / "work/target/release/componentize-py", "/producer"),
            (native_data_consumer, "/native-data-consumer"),
            (work / "empty-venv", "/empty-venv"),
        ), address_space_bytes=8 * GIB)
        receipt["stages"].append(run_stage(
            "schema-kernel-original-producer-native-data", command, work,
            destination / "probe",
        ))
        observation = json.loads((work / "probe.json").read_bytes())
        if (
            observation.get("passed") is not True
            or observation.get("actual_limited_api") is not True
            or observation.get("native_data_consumer_sha256")
            != inputs["native_data_consumer"]["sha256"]
            or verify_inputs(*arguments) != inputs
            or any(fingerprint(Path(path)) != pin for path, pin in private.items())
            or fetch_sdk.inventory(probe_source) != probe_files
        ):
            raise ValueError("actual observation or readonly input identity differs")
        receipt.update(outcome="success", guest_executed=True,
                       observation=fingerprint(work / "probe.json"),
                       extension_sha256=observation["extension_sha256"])
    except BaseException:
        receipt["outcome"] = "failed"
        raise
    finally:
        output.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        if sys.platform != "linux" or Path(__file__) != Path("/kernel-driver.py"):
            raise ValueError("private probe entry is reserved for the CI namespace")
        probe()
    else:
        parser = argparse.ArgumentParser()
        for name in (
            "source", "vendor", "archives", "rust", "sdk", "producer", "validation",
            "native-data-consumer", "destination",
        ):
            parser.add_argument("--" + name, type=Path, required=True)
        run(**vars(parser.parse_args()))
