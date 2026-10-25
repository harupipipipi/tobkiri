"""Execute the same real C helper with the two authenticated inactive producers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from build_native import GIB, HERE, fingerprint, sandbox_command
from build_rpds import verify_validation
from build_wasi import run_stage
import fetch_sdk
from poll_comparison_inputs import (
    HELPER,
    PREFIXES,
    authenticate,
    physical,
    read_json,
    verify_bundle,
)
from poll_link_trace import compare_traces, read_trace
from poll_reference_results import compare
import runtime_inputs

PIN_PATH = HERE / "empty-poll-comparison-inputs.json"
RECIPE = "set -eu\numask 077\n/usr/bin/python3 -I -S -B /reference-driver.py\n"


def run(
    bundle: Path, authentication: Path, validation: Path, destination: Path
) -> dict:
    """Run only on CI; keep every product capability and schema policy unchanged."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("actual C comparison is restricted to Linux CI")
    if sys.version_info[:3] != (3, 13, 16):
        raise ValueError("the original CPython3.13.16 CI host is required")
    for path in (bundle, authentication, validation):
        physical(path)
    if (
        not destination.is_absolute()
        or destination.parent.resolve() != destination.parent
        or os.path.lexists(destination)
        or shutil.disk_usage(destination.parent).free < 8 * GIB
    ):
        raise ValueError(
            "new physical comparison root and existing disk floor required"
        )
    pins = read_json(PIN_PATH)
    authenticate(
        pins,
        *(
            read_json(authentication / name)
            for name in ("run.json", "job.json", "artifact.json")
        ),
    )
    saved = verify_bundle(bundle, pins)
    validation_receipt = read_json(validation / "validation-inputs.json")
    verify_validation(validation, validation_receipt)
    probe = HERE / "empty_poll_probe"
    probe_inventory = fetch_sdk.inventory(probe)
    helper_probe = [
        inventory
        for name, inventory in saved["helper"]["input_inventories"].items()
        if Path(name).name == "empty_poll_probe"
    ]
    if len(helper_probe) != 1:
        raise ValueError(
            "compiled helper lacks its one original C/probe source inventory"
        )
    probe_inputs = {}
    for name in ("app.py", "probe.wit", "_tobkiri_poll_oracle.c"):
        actual = fingerprint(probe / name, 4 * 1024 * 1024)
        if actual != {key: helper_probe[0][name][key] for key in ("bytes", "sha256")}:
            raise ValueError("compiled C or unchanged wrapper/WIT source differs")
        probe_inputs[name] = actual
    support = {
        name: fingerprint(HERE / name, 4 * 1024 * 1024)
        for name in (
            "poll_reference_driver.py",
            "poll_reference_results.py",
            "poll_link_trace.py",
        )
    }
    pin_identity = fingerprint(PIN_PATH, 4 * 1024 * 1024)
    identities = {
        str(path): fingerprint(path, 16 * 1024 * 1024)
        for path in (
            validation / "validation-inputs.json",
            *(
                authentication / name
                for name in ("run.json", "job.json", "artifact.json")
            ),
        )
    }
    destination.mkdir(mode=0o700)
    recipe = destination / "reference.sh"
    recipe.write_text(RECIPE)
    recipe_identity = fingerprint(recipe)
    report = {
        "format": "tobkiri.ui-empty-poll-comparison/1",
        "outcome": "pending",
        "supplier": pins,
        "inputs": identities,
        "probe_inputs": probe_inputs,
        "support_scripts": support,
        "recipe": recipe_identity,
        "variants": {},
        "guest_execution": False,
        "raw_c_observations_match": False,
        "final_provider_resolution_verified": False,
        "tls_storage_provider_verified": False,
        "c_abi_verified": False,
        "cold_schema_verified": False,
        "normal_pack_operation": False,
        "full_setup_gui": False,
        "production_enabled": False,
    }
    try:
        for variant, prefix in PREFIXES.items():
            stage = destination / variant
            stage.mkdir(mode=0o700)
            work = stage / "work"
            work.mkdir(mode=0o700)
            for name in ("home", "tmp", "empty-venv"):
                (work / name).mkdir(mode=0o700)
            binary = bundle / prefix / "work/target/release/componentize-py"
            binary.chmod(0o755)
            control = stage / "control.json"
            control.write_text(
                json.dumps(
                    {
                        "variant": variant,
                        "helper": pins["files"][HELPER],
                        "producer": pins["files"][
                            prefix + "/work/target/release/componentize-py"
                        ],
                        "probe_inputs": probe_inputs,
                    },
                    indent=2,
                )
                + "\n"
            )
            control_identity = fingerprint(control)
            command = sandbox_command(
                None,
                work,
                recipe,
                (
                    (probe, "/probe-source"),
                    (binary, "/producer"),
                    (bundle / HELPER, "/helper.abi3.so"),
                    (control, "/control.json"),
                    (validation / "wasmtime", "/validation-deps"),
                    (HERE / "poll_reference_driver.py", "/reference-driver.py"),
                    (HERE / "poll_reference_results.py", "/poll_reference_results.py"),
                    (HERE / "poll_link_trace.py", "/poll_link_trace.py"),
                    (work / "empty-venv", "/empty-venv"),
                ),
                address_space_bytes=8 * GIB,
            )
            observed = {"outcome": "pending"}
            report["variants"][variant] = observed
            observed["supervision"] = run_stage(
                "ui-poll-reference-" + variant, command, work, stage / "execution"
            )
            actual = runtime_inputs.report(work / "reference.json")
            component = work / "stage/component.wasm"
            trace = read_trace(work / "link-trace", pins["files"][HELPER], component)
            if (
                actual.get("guest_execution") is not True
                or actual.get("componentization") is not True
                or actual.get("trace") != trace
                or actual.get("control") != read_json(control)
                or fingerprint(control) != control_identity
                or any(
                    actual.get(name) is not False
                    for name in (
                        "final_provider_resolution_verified",
                        "c_abi_verified",
                        "cold_schema_verified",
                        "production_enabled",
                    )
                )
            ):
                raise ValueError("actual reference result or component binding differs")
            observation = actual["observation"]
            if (
                observation.get("component") != trace["final_component"]
                or any(
                    observation.get(name) is not True
                    for name in (
                        "genuine_wasip2",
                        "returned",
                        "post_return",
                        "store_retired",
                        "epoch_timer_joined",
                    )
                )
                or observation.get("private_initializer_called") is not False
                or observation.get(
                    "inherited_arguments_environment_stdio_preopens_network"
                )
                is not False
            ):
                raise ValueError("actual genuine reference lifetime is incomplete")
            observed.update(
                outcome="success",
                reference=actual,
                reference_receipt=fingerprint(work / "reference.json"),
            )
        a, b = (report["variants"][name]["reference"] for name in PREFIXES)
        if a["stage_inputs"] != b["stage_inputs"]:
            raise ValueError("paired captured and generated Python bindings differ")
        report["trace_comparison"] = compare_traces(a["trace"], b["trace"])
        report["behavior_comparison"] = compare(
            a["observation"]["result"], b["observation"]["result"]
        )
        verify_bundle(bundle, pins)
        verify_validation(validation, validation_receipt)
        if (
            fingerprint(PIN_PATH, 4 * 1024 * 1024) != pin_identity
            or fingerprint(recipe) != recipe_identity
            or fetch_sdk.inventory(probe) != probe_inventory
            or any(
                fingerprint(HERE / name, 4 * 1024 * 1024) != identity
                for name, identity in support.items()
            )
            or any(
                fingerprint(Path(name), 16 * 1024 * 1024) != identity
                for name, identity in identities.items()
            )
        ):
            raise ValueError("comparison source, controls or tools changed")
        report.update(
            outcome="success", guest_execution=True, raw_c_observations_match=True
        )
    except BaseException as error:
        report["outcome"] = "failed"
        report["failure_type"] = type(error).__name__
        raise
    finally:
        (destination / "comparison.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "authentication", "validation", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    result = run(**vars(parser.parse_args()))
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "outcome",
                    "raw_c_observations_match",
                    "c_abi_verified",
                    "production_enabled",
                )
            }
        )
    )
