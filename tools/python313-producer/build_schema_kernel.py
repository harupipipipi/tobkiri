"""Build and call the private schema extension only in supervised Linux CI.

The original 64-case selfcheck comparison is required; full catalog remains separate.
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

LOCK_SHA256 = "222ef5c4c4b48fbad400c604b0135efe9a56d7ecd1c9cec90ef1021d639b93d6"
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
EXTENSION = "_tobkiri_schema_kernel"
INTERFACE = "tobkiri-ui:schema-probe/probe@1.0.0"
MAX_SOURCE = 4 * 1024 * 1024
MAX_ARCHIVES = 128 * 1024 * 1024

# Only module imports happen during producer preinitialization. All object/type
# observations happen when the actual native DATA runner invokes run.
APP = '''"""One actual private-extension call after producer snapshotting."""
import json
import numbers
import struct
import sys
import sysconfig

from captured_loader import install
from wheel_data import FILES, DISTRIBUTIONS

_FINDER = install(FILES, DISTRIBUTIONS, __file__.rsplit("/", 1)[0])
import _tobkiri_schema_kernel as kernel
import jsonschema
import jsonschema_specifications
import wit_world.exports
from schema_adapter import install_standard_classes
from schema_inputs import CASES_JSON
from schema_api_checks import exercise_api
from schema_kernel_observation import meta_nodes, node_digest, observe

install_standard_classes()

TARGET = {
    "python": list(sys.version_info[:3]),
    "pointer_bits": struct.calcsize("P") * 8,
    "soabi": sysconfig.get_config_var("SOABI"),
    "extension_file": kernel.__file__,
}
_FINDER.seal()


class Probe(wit_world.exports.Probe):
    def run(self) -> str:
        try:
            return self.observe()
        except Exception as error:
            frames, current = [], error.__traceback__
            while current is not None and len(frames) < 32:
                frames.append({
                    "module": current.tb_frame.f_globals.get("__name__"),
                    "function": current.tb_frame.f_code.co_name,
                    "line": current.tb_lineno,
                })
                current = current.tb_next
            return json.dumps({
                "unexpected_exception": {
                    "type": type(error).__module__ + "." + type(error).__qualname__,
                    "message": str(error), "frames": frames,
                },
                "production_enabled": False,
            }, sort_keys=True)

    def observe(self) -> str:
        schema = {"type": "object", "properties": {"value": {"type": "string"}}}
        actual = {"value": []}
        retained = kernel.retain_objects(schema, actual)
        schema_is_original = retained["schema"] is schema
        instance_is_original = retained["instance"] is actual
        actual["value"].append("mutation")
        schema["properties"]["value"]["title"] = "same object"
        aliases_preserved = (
            retained["instance"]["value"] is actual["value"]
            and retained["instance"]["value"] == ["mutation"]
            and retained["schema"]["properties"]["value"]
                is schema["properties"]["value"]
            and retained["schema"]["properties"]["value"]["title"] == "same object"
        )
        cases = [
            ([], "array", True), ((), "array", False),
            ({}, "object", True), ([], "object", False),
            ("x", "string", True), (1, "string", False),
            (True, "boolean", True), (1, "boolean", False),
            (None, "null", True), (False, "null", False),
            (1, "integer", True), (True, "integer", False),
            (1.0, "integer", True), (1.2, "integer", False),
            (10 ** 80, "integer", True),
            (1, "number", True), (1.2, "number", True),
            (10 ** 80, "number", True), (True, "number", False),
            ("1", "number", False), (complex(1, 2), "number", True),
        ]
        for instance, kind, expected in cases:
            if kernel.is_original_json_type(instance, kind) is not expected:
                raise ValueError("actual primitive type observation differs: " + kind)
        unknown_rejected = False
        try:
            kernel.is_original_json_type(None, "unknown-private-type")
        except ValueError:
            unknown_rejected = True
        if (not schema_is_original or not instance_is_original
                or not aliases_preserved or not unknown_rejected
                or kernel.production_enabled is not False
                or kernel.implemented_handler_count != 36):
            raise ValueError("actual primitive object observation differs")
        case_inputs = json.loads(CASES_JSON)
        if len(case_inputs) != 64:
            raise ValueError("complete original schema comparison required")
        known_nodes = meta_nodes()
        selfchecks = []
        for case in case_inputs:
            schema = case["schema"]
            try:
                jsonschema.Draft202012Validator.check_schema(schema)
            except jsonschema.exceptions.SchemaError as error:
                actual_result = {
                    "valid": False, "error": observe(error, schema, known_nodes)
                }
            else:
                actual_result = {"valid": True, "error": None}
            selfchecks.append({"name": case["name"], "actual": actual_result})
        return json.dumps({
            "target": TARGET,
            "qualification_scope": "original-object-schema-selfcheck/1",
            "schema_is_original": schema_is_original,
            "instance_is_original": instance_is_original,
            "aliases_preserved": aliases_preserved,
            "type_cases_checked": len(cases),
            "unknown_private_type_rejected": unknown_rejected,
            "implemented_handler_count": kernel.implemented_handler_count,
            "schema_validation_executed": True,
            "selfchecks": selfchecks,
            "public_api_checks": exercise_api(),
            "type_dispatch_counts": kernel.type_dispatch_counts(),
            "meta_schema_sha256": node_digest(jsonschema.Draft202012Validator.META_SCHEMA),
            "meta_registry_sha256": {
                uri: node_digest(jsonschema_specifications.REGISTRY.contents(uri))
                for uri in sorted(jsonschema_specifications.REGISTRY)
            },
            "meta_format_checker_names": sorted(
                jsonschema.Draft202012Validator.FORMAT_CHECKER.checkers
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
        or dependencies != {"pyo3": {
            "version": "=0.30.1", "default-features": False,
            "features": ["macros", "abi3-py313"],
        }}
        or manifest["package"].get("metadata", {}).get("tobkiri", {}).get(
            "schema-engine"
        ) != "original-object-port"
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
    if (sum(p["name"] == "pyo3" and p["version"] == "0.30.1"
            for p in result) != 1 or any(p["name"] == "jsonschema" for p in result)):
        raise ValueError("primitive-only dependency closure differs")
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



def fingerprint_and_read(path: Path, maximum: int) -> bytes:
    """Read admitted source/test DATA, never execute its contents."""
    from build_native import fingerprint

    fingerprint(path, maximum)
    return path.read_bytes()


def verify_inputs(
    source: Path, vendor: Path, archives: Path, rust: Path, sdk: Path,
    producer: Path, validation: Path, native_data_consumer: Path,
    schema_inputs: Path, stdlib: Path, rpds: Path, reference: Path,
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
        set(source_files) != {
            "Cargo.toml", "Cargo.lock", "README.md", "COPYING.jsonschema", "adapter.py",
            "src/lib.rs", "src/traversal.rs", "src/keywords.rs",
        }
        or any("link" in item for item in source_files.values())
        or sum(item["bytes"] for item in source_files.values()) > MAX_SOURCE
    ):
        raise ValueError("schema kernel source closure differs")
    import build_schema
    original_dependencies = build_schema.verify_inputs(
        schema_inputs, producer, rpds, validation, stdlib
    )
    reference_data = json.loads(fingerprint_and_read(reference, 4 * 1024 * 1024))
    if (reference_data.get("format") != "tobkiri.schema-kernel-reference/1"
            or reference_data.get("oracle_commit") != "54e7da008997a3eb45876cbe18459dba9902cc9d"
            or reference_data.get("jsonschema_version") != "4.26.0"
            or reference_data.get("original_schema_count") != 42
            or reference_data.get("original_schema_inventory_sha256") !=
                "2311b39b1f946c1ba45a9d87d57fde15c28a6a79afef1a44a5b98f3603b63008"
            or len(reference_data.get("cases", [])) != 64):
        raise ValueError("complete original 42+22 source reference differs")
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
        "original_dependencies": original_dependencies,
        "reference": fingerprint(reference, 4 * 1024 * 1024),
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
    control = json.loads(read(Path("/schema-control.json"), maximum=4 * 1024 * 1024))
    reference = json.loads(read(Path("/schema-reference.json"), maximum=4 * 1024 * 1024))
    original_capture = driver.stage_inputs(stage, control)
    extra_sources = ("schema_adapter.py", "schema_kernel_observation.py", "schema_inputs.py", "schema_api_checks.py")
    for name in extra_sources:
        if (stage / name).exists():
            raise ValueError("schema kernel helper collides with captured source")
        shutil.copyfile(Path("/probe-source") / name, stage / name)
    captured = stage / (EXTENSION + ".abi3.so")
    captured.write_bytes(extension)
    captured_total = sum(path.stat().st_size for path in stage.rglob("*") if path.is_file())
    if captured_total > 64 * 1024 * 1024:
        raise ValueError("complete original schema/kernel capture exceeds 64 MiB")
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
        for name in ("app.py", "probe.wit", "exercise.py", "captured_loader.py", *extra_sources)
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
    try:
        observed = validate_report(report, component_identity, "run")
    except (ValueError, KeyError):
        diagnostic = json.dumps({
            "format": "tobkiri.schema-kernel-native-lifetime-failure/1",
            "native_report": report,
        }, sort_keys=True)
        if len(diagnostic.encode()) <= 16 * 1024:
            print(diagnostic, flush=True)
        else:
            print(json.dumps({
                "format": "tobkiri.schema-kernel-native-lifetime-failure/1",
                "report_artifact": output.name,
                "elapsed_seconds": report.get("elapsed_seconds"),
                "observation": report.get("observation"),
                "bytes": len(diagnostic.encode()),
                "sha256": hashlib.sha256(diagnostic.encode()).hexdigest(),
            }, sort_keys=True), flush=True)
        raise
    result = (
        json.loads(report["value"]) if isinstance(report.get("value"), str) else None
    )
    expected_selfchecks = [
        {"name": case["name"], "actual": case["expected"]}
        for case in reference["cases"]
    ]
    def digest(value: object) -> str:
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()

    if (
        report.get("error") is not None or observed["post_return"] is not True
        or not isinstance(result, dict)
        or result.get("qualification_scope") != "original-object-schema-selfcheck/1"
        or result.get("schema_is_original") is not True
        or result.get("instance_is_original") is not True
        or result.get("aliases_preserved") is not True
        or result.get("type_cases_checked") != 21
        or result.get("unknown_private_type_rejected") is not True
        or result.get("implemented_handler_count") != 36
        or result.get("schema_validation_executed") is not True
        or result.get("selfchecks") != expected_selfchecks
        or result.get("public_api_checks") != {
            "lazy_start_and_resume": True,
            "actual_error_objects_and_references": True,
            "outer_close_keeps_retained_callback_open": True,
            "interleaved_iterators": True,
            "pep479_and_failed_iterator_closed": True,
            "false_descend_preserves_original_early_paths": True,
            "evolve_preserves_actual_class_and_schema": True,
            "unregistered_extension_class_not_cached": True,
            "keyword_delegation_closes_retained_child": True,
            "keyword_deallocation_closes_retained_child": True,
            "keyword_cycle_collection_closes_retained_child": True,
            "unstarted_keyword_does_not_acquire_child": True,
            "default_type_dispatch_matches_original": True,
        }
        or type(result.get("type_dispatch_counts", {}).get("native_default")) is not int
        or result["type_dispatch_counts"]["native_default"] <= 0
        or type(result["type_dispatch_counts"].get("explicit_original")) is not int
        or result["type_dispatch_counts"]["explicit_original"] <= 0
        or result.get("meta_schema_sha256") != digest(reference["meta_schema"])
        or result.get("meta_registry_sha256") != {
            item["uri"]: digest(item["schema"]) for item in reference["meta_registry"]
        }
        or result.get("meta_format_checker_names") != reference["meta_format_checker_names"]
        or result.get("production_enabled") is not False
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
        if isinstance(result, dict) and isinstance(result.get("selfchecks"), list):
            actual_checks = result["selfchecks"]
            for index, expected in enumerate(expected_selfchecks):
                actual = actual_checks[index] if index < len(actual_checks) else None
                if actual != expected:
                    difference = json.dumps({
                        "format": "tobkiri.schema-kernel-first-difference/1",
                        "index": index, "expected": expected, "actual": actual,
                    }, sort_keys=True)
                    if len(difference.encode()) <= 16 * 1024:
                        print(difference, flush=True)
                    break
        diagnostic = json.dumps({
            "format": "tobkiri.schema-kernel-smoke-failure/1",
            "native_report": report,
        }, sort_keys=True)
        if len(diagnostic.encode()) <= 16 * 1024:
            print(diagnostic, flush=True)
        else:
            print(json.dumps({
                "format": "tobkiri.schema-kernel-smoke-failure/1",
                "report_artifact": output.name,
                "bytes": len(diagnostic.encode()),
                "sha256": hashlib.sha256(diagnostic.encode()).hexdigest(),
            }), flush=True)
        raise ValueError("real native DATA kernel call differs; inspect native report")
    (work / "probe.json").write_text(json.dumps({
        "format": "tobkiri.schema-kernel-probe/1", "passed": True,
        "qualification_scope": "original-object-schema-selfcheck/1",
        "implemented_handler_count": 36, "schema_validation_executed": True,
        "extension_sha256": hashlib.sha256(extension).hexdigest(),
        "component_sha256": component_identity,
        "actual_limited_api": True,
        "limited_api_outputs": [str(path) for path in limited],
        "cargo_artifact": artifacts[0], "custom_sections": sections,
        "extension_imports": imports, "extension_exports": exports,
        "composition": composition, "boundary": boundary,
        "original_source_capture": original_capture,
        "complete_capture_bytes": captured_total,
        "native_data_consumer_sha256": consumer_identity, "native_report": report,
        "result": result, "original_64_cases_compared": True,
        "full_catalog_executed": False, "production_enabled": False,
    }, indent=2) + "\n")

    print(json.dumps({
        "format": "tobkiri.schema-kernel-qualified/1",
        "scope": "original-object-schema-selfcheck/1",
        "original_64_cases_compared": True,
        "type_cases": result["type_cases_checked"],
        "fuel_remaining": observed.get("fuel_remaining"),
        "elapsed_seconds": report.get("elapsed_seconds"),
        "component_sha256": component_identity,
        "full_catalog_executed": False, "production_enabled": False,
    }, sort_keys=True), flush=True)


def run(
    source: Path, vendor: Path, archives: Path, rust: Path, sdk: Path,
    producer: Path, validation: Path, native_data_consumer: Path, destination: Path,
    schema_inputs: Path, stdlib: Path, rpds: Path, reference: Path,
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
        native_data_consumer, schema_inputs, stdlib, rpds, reference,
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
    for name in ("probe.wit", "exercise.py", "captured_loader.py"):
        shutil.copyfile(HERE / "schema_probe" / name, probe_source / name)
    shutil.copyfile(source / "adapter.py", probe_source / "schema_adapter.py")
    shutil.copyfile(HERE / "schema_kernel_api_checks.py", probe_source / "schema_api_checks.py")
    shutil.copyfile(HERE / "schema_kernel_observation.py", probe_source / "schema_kernel_observation.py")
    reference_data = json.loads(fingerprint_and_read(reference, 4 * 1024 * 1024))
    # Test inputs only. Expected results and expected registry objects are never
    # captured in the guest or substituted for its genuine registry/schema.
    case_inputs = [{"name": case["name"], "schema": case["schema"]}
                   for case in reference_data["cases"]]
    case_bytes = json.dumps(case_inputs, ensure_ascii=False, allow_nan=False,
                            separators=(",", ":")).encode()
    (probe_source / "schema_inputs.py").write_text("CASES_JSON = " + repr(case_bytes) + "\n")
    import build_schema
    import runtime_inputs
    records = runtime_inputs.report(schema_inputs / "schema-inputs.json")["inputs"]
    control = destination / "schema-control.json"
    control.write_text(json.dumps({
        "inputs": records, "extension_sha256": build_schema.EXTENSION_SHA256,
        "input_limit": 64 * 1024 * 1024,
        "stdlib": runtime_inputs.report(stdlib / "stdlib-inputs.json"),
    }, sort_keys=True) + "\n")
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
        recipe, replay, control, config / "cargo.toml", config / "pyo3-config.txt",
        *(path for path, _ in helpers),
    )}
    probe_files = fetch_sdk.inventory(probe_source)
    receipt = {
        "format": "tobkiri.schema-kernel-build/1", "outcome": "pending",
        "qualification_scope": "original-object-schema-selfcheck/1",
        "implemented_handler_count": 36, "schema_validation_executed": False,
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
            (schema_inputs, "/inputs"), (stdlib, "/stdlib"),
            (control, "/schema-control.json"), (reference, "/schema-reference.json"),
            (rpds / "work/stage/rpds.abi3.so", "/rpds.abi3.so"),
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
            or observation.get("qualification_scope") != "original-object-schema-selfcheck/1"
            or observation.get("implemented_handler_count") != 36
            or observation.get("schema_validation_executed") is not True
            or observation.get("original_64_cases_compared") is not True
            or observation.get("native_data_consumer_sha256")
            != inputs["native_data_consumer"]["sha256"]
            or verify_inputs(*arguments) != inputs
            or any(fingerprint(Path(path)) != pin for path, pin in private.items())
            or fetch_sdk.inventory(probe_source) != probe_files
        ):
            raise ValueError("actual observation or readonly input identity differs")
        receipt.update(outcome="success", guest_executed=True,
                       schema_validation_executed=True, implemented_handler_count=36,
                       original_64_cases_compared=True,
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
            "native-data-consumer", "destination", "schema-inputs", "stdlib", "rpds", "reference",
        ):
            parser.add_argument("--" + name, type=Path, required=True)
        run(**vars(parser.parse_args()))
