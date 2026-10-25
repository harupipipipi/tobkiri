"""Compare the real locked JSON Schema closure in isolated native/Wasm CI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import sys

from build_native import GIB, HERE, fingerprint, sandbox_command
from build_rpds import physical, verify_validation
from build_wasi import run_stage
import fetch_rpds
import fetch_schema
import fetch_schema_stdlib
import fetch_sdk
import runtime_inputs

RPDS_HEAD = "53163ed815c62c4853bbed3d8a082c0764e3084f"
EXTENSION_SHA256 = "ace6bfa6de9f435f12380e9c11b1b7ed0a9645b721bdcdf634fb02cdbbff720b"
COMPONENT_SHA256 = "56d765060d8f2f9078a31cf2e96bdd591d737545250464afcb9bc9cdbc91c551"
INPUT_LIMIT = 64 * 1024 * 1024


def verify_clock_variant(cli: dict, source: dict, *, allow_ui_wall_clock: bool) -> None:
    """Never accept a new producer mode through the historical consumer option."""
    if (
        cli.get("ui_monotonic_clock_candidate") is not None
        or source.get("ui_monotonic_clock") is not None
    ):
        raise ValueError("monotonic producer requires its separate consuming controller")
    clock_variant = cli.get("ui_wall_clock_candidate")
    if allow_ui_wall_clock:
        if (
            not isinstance(clock_variant, dict)
            or clock_variant != source.get("ui_wall_clock")
            or clock_variant.get("feature") != "ui-wall-clock"
            or clock_variant.get("interface_prefix") != "wasi:clocks/wall-clock@"
            or clock_variant.get("functions") != ["now", "resolution"]
            or clock_variant.get("stub_wasi_retained") is not True
            or set(clock_variant.get("changed_files", {}))
            != {"Cargo.toml", "src/stubwasi.rs"}
        ):
            raise ValueError("separate explicit UI clock producer derivation required")
    elif clock_variant is not None or source.get("ui_wall_clock") is not None:
        raise ValueError("UI clock producer requires an explicit controller option")


def verify_inputs(
    inputs: Path,
    producer: Path,
    rpds: Path,
    validation: Path,
    stdlib: Path,
    *,
    allow_ui_wall_clock: bool = False,
) -> dict:
    """Bind all restored archives and actual prior artifacts before execution."""
    for path in (inputs, producer, rpds, validation, stdlib):
        physical(path)
    fetch_schema.verify(inputs)
    fetch_schema_stdlib.verify(stdlib)
    verify_validation(
        validation, runtime_inputs.report(validation / "validation-inputs.json")
    )
    if fingerprint(rpds / "work/stage/rpds.abi3.so")["sha256"] != EXTENSION_SHA256:
        raise ValueError("rpds extension differs from the actual successful ABI probe")
    if fingerprint(rpds / "work/stage/outer.wasm")["sha256"] != COMPONENT_SHA256:
        raise ValueError("rpds predecessor component differs")
    fingerprint(rpds / "work/probe.json", 4 * 1024 * 1024)
    prior = json.loads((rpds / "work/probe.json").read_bytes())
    if not (
        prior["passed"] is True
        and prior["actual_limited_api"] is True
        and prior["observation"]["post_return"] is True
        and prior["extension_sha256"] == EXTENSION_SHA256
    ):
        raise ValueError("actual rpds predecessor observation is incomplete")
    cli = runtime_inputs.report(producer / "cli-build.json")
    if (
        cli["producer_cli"] is not True
        or cli["inputs_unchanged"] is not True
        or fingerprint(producer / "work/target/release/componentize-py")
        != cli["binary"]
    ):
        raise ValueError("saved producer identity differs")
    source_pins = [
        value
        for name, value in cli["predecessors"].items()
        if Path(name).name == "cli-source.json"
    ]
    if source_pins != [fingerprint(producer / "cli-source.json")]:
        raise ValueError("saved producer source receipt differs")
    fingerprint(producer / "cli-source.json", 4 * 1024 * 1024)
    source = json.loads((producer / "cli-source.json").read_bytes())
    verify_clock_variant(cli, source, allow_ui_wall_clock=allow_ui_wall_clock)
    return {
        str(path): fingerprint(path)
        for path in (
            inputs / "schema-inputs.json",
            producer / "cli-build.json",
            producer / "cli-source.json",
            rpds / "work/probe.json",
            rpds / "rpds-build.json",
            validation / "validation-inputs.json",
            stdlib / "stdlib-inputs.json",
        )
    }


def run(
    inputs: Path,
    producer: Path,
    rpds: Path,
    validation: Path,
    stdlib: Path,
    destination: Path,
    *,
    allow_ui_wall_clock: bool = False,
) -> None:
    """Execute original wheels and the Wasm candidate only in supervised Linux CI."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("schema execution is restricted to Linux CI")
    if sys.version_info[:3] != (3, 13, 16):
        raise ValueError("native original CPython 3.13.16 reference required")
    if (
        not destination.is_absolute()
        or destination.parent.resolve() != destination.parent
        or shutil.disk_usage(destination.parent).free < fetch_schema.DISK_FLOOR
    ):
        raise ValueError("new physical destination and existing disk floor required")
    identities = verify_inputs(
        inputs,
        producer,
        rpds,
        validation,
        stdlib,
        allow_ui_wall_clock=allow_ui_wall_clock,
    )
    destination.mkdir(mode=0o700)
    work = destination / "work"
    work.mkdir(mode=0o700)
    for name in ("home", "tmp", "empty-venv"):
        (work / name).mkdir(mode=0o700)
    source = HERE / "schema_probe"
    scripts = fetch_sdk.inventory(source)
    native_prefix = Path(sys.base_prefix).resolve(strict=True)
    native_python = Path(sys.executable).resolve(strict=True)
    if not native_python.is_relative_to(native_prefix):
        raise ValueError("native interpreter is outside its explicit read-only prefix")
    records = runtime_inputs.report(inputs / "schema-inputs.json")["inputs"]
    control = destination / "control.json"
    control.write_text(
        json.dumps(
            {
                "inputs": records,
                "extension_sha256": EXTENSION_SHA256,
                "input_limit": INPUT_LIMIT,
                "stdlib": runtime_inputs.report(stdlib / "stdlib-inputs.json"),
                "allow_ui_wall_clock": allow_ui_wall_clock,
                "native_paths": [
                    "/inputs/packages/"
                    + (
                        "rpds-py-native"
                        if pin["role"] == "native-oracle"
                        else pin["distribution"]
                    )
                    for pin in records
                ],
            },
            indent=2,
        )
        + "\n"
    )
    composition = HERE.parents[1] / "sdk/python/composition.py"
    control_identity = fingerprint(control)
    composition_identity = fingerprint(composition)
    report = {
        "format": "tobkiri.ui-schema-candidate/1",
        "outcome": "pending",
        "inputs": identities,
        "probe_source": scripts,
        "native_python": {
            "version": list(sys.version_info[:3]),
            "executable": fingerprint(native_python),
        },
        "rpds_source_head": RPDS_HEAD,
        "rpds_extension_source": {
            "commit": fetch_rpds.COMMIT,
            "archive_sha256": fetch_rpds.ARCHIVE_SHA256,
            "version": fetch_rpds.VERSION,
            "pyinit": "PyInit_rpds",
        },
        "stages": [],
        "guest_executed": False,
        "native_capture_binding": False,
        "full_setup_gui": False,
        "production_enabled": False,
        "ui_wall_clock_candidate": allow_ui_wall_clock,
    }
    try:
        native_recipe = destination / "native.sh"
        native_recipe.write_text(
            "set -eu\nexport PYTHONHASHSEED=0\n"
            + shlex.quote(str(native_python))
            + " -I -S -B /probe-source/native_oracle.py\n"
        )
        command = sandbox_command(
            None,
            work,
            native_recipe,
            (
                (native_prefix, str(native_prefix)),
                (source, "/probe-source"),
                (inputs, "/inputs"),
                (control, "/control.json"),
            ),
            address_space_bytes=4 * GIB,
        )
        report["stages"].append(
            run_stage("schema-original-native", command, work, destination / "native")
        )
        native = fingerprint(work / "native.json", 4 * 1024 * 1024)
        wasm_recipe = destination / "wasm.sh"
        wasm_recipe.write_text(
            "set -eu\n/usr/bin/python3 -I -S -B /probe-source/driver.py\n"
        )
        command = sandbox_command(
            None,
            work,
            wasm_recipe,
            (
                (source, "/probe-source"),
                (inputs, "/inputs"),
                (control, "/control.json"),
                (rpds / "work/stage/rpds.abi3.so", "/rpds.abi3.so"),
                (stdlib, "/stdlib"),
                (producer / "work/target/release/componentize-py", "/producer"),
                (validation / "wasmtime", "/validation-deps"),
                (validation / "wac-cli", "/wac"),
                (composition, "/composition.py"),
                (work / "empty-venv", "/empty-venv"),
            ),
            address_space_bytes=8 * GIB,
        )
        report["stages"].append(
            run_stage(
                "schema-component-import-run", command, work, destination / "wasm"
            )
        )
        observed = runtime_inputs.report(work / "schema.json")
        if (
            observed.get("passed") is not True
            or fingerprint(work / "native.json") != native
        ):
            raise ValueError("unchanged original schema observations did not match")
        verify_inputs(
            inputs,
            producer,
            rpds,
            validation,
            stdlib,
            allow_ui_wall_clock=allow_ui_wall_clock,
        )
        if (
            fetch_sdk.inventory(source) != scripts
            or fingerprint(control) != control_identity
            or fingerprint(composition) != composition_identity
        ):
            raise ValueError("captured schema source or control changed")
        report.update(
            outcome="success",
            guest_executed=True,
            observation=fingerprint(work / "schema.json"),
            original=native,
        )
    except BaseException:
        report["outcome"] = "failed"
        raise
    finally:
        (destination / "schema-build.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("inputs", "producer", "rpds", "validation", "stdlib", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--allow-ui-wall-clock", action="store_true")
    run(**vars(parser.parse_args()))
