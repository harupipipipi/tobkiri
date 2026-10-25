"""Compile the inactive real-C poll helper from actual target headers on CI.

This stage inspects shared-module imports; it does not execute the C cases,
select a candidate libc, or establish ABI/schema/product parity.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from build_native import GIB, HERE, fingerprint, sandbox_command, verify_tree
from build_rpds import physical, verify_sdk, verify_validation
import build_wasi
from build_wasi import run_stage
import fetch_sdk
import runtime_inputs

RECIPE = r"""set -eu
umask 077
trap 'poll_helper_status=$?; if [ "$poll_helper_status" -ne 0 ]; then /usr/bin/tail -c 8192 /work/compile.log 2>/dev/null || :; fi; exit "$poll_helper_status"' 0
export PATH=/sdk/bin:/usr/bin:/bin HOME=/work/home
/sdk/bin/clang --version > /work/clang-version.txt
set -- --target=wasm32-wasip2 --sysroot=/sdk/share/wasi-sysroot \
  -fPIC -I/target-headers -DPy_LIMITED_API=0x030D0000 \
  -shared -Wl,--skip-wit-component -Wl,--unresolved-symbols=import-dynamic \
  /probe-source/_tobkiri_poll_oracle.c -o /work/_tobkiri_poll_oracle.abi3.so
/sdk/bin/clang -### "$@" > /work/clang-link-stdout.txt 2> /work/clang-link-command.txt
/sdk/bin/clang "$@" > /work/compile.log 2>&1
/usr/bin/python3 -I -B /probe-source/inspect_helper.py
"""


def verify_wasi(source: Path, wasi: Path) -> tuple[dict, Path]:
    """Require successful original stages and their exact generated target headers."""
    identity = verify_tree(source)
    report = runtime_inputs.report(wasi / "wasi-build.json")
    if (
        report["source"] != identity
        or report["inputs_unchanged"] is not True
        or len(report["stages"]) != len(build_wasi.STAGES)
    ):
        raise ValueError("original WASI build does not match verified source")
    for recorded, (name, digest, outputs) in zip(report["stages"], build_wasi.STAGES):
        if (
            recorded["name"] != name
            or recorded["recipe_sha256"] != digest
            or recorded["supervision"]["outcome"] != "success"
            or set(recorded["outputs"]) != set(outputs)
        ):
            raise ValueError("original WASI stage or cleanup differs")
        for output in outputs:
            if fingerprint(wasi / "work" / output) != recorded["outputs"][output]:
                raise ValueError("original WASI library changed")
    headers = build_wasi.target_headers(wasi / "work")
    if report.get("target_headers") != headers:
        raise ValueError("generated target headers lack their original build binding")
    sdk_receipt = wasi / "sdk-input/sdk-manifest.json"
    if fingerprint(sdk_receipt) != report["sdk_manifest"]:
        raise ValueError("original SDK manifest changed")
    verify_sdk(wasi / "sdk-input", json.loads(sdk_receipt.read_bytes()))
    return report, wasi / "work" / headers["root_relative"]


def run(source: Path, wasi: Path, validation: Path, destination: Path) -> dict:
    """Compile/inspect only in a new supervised, networkless Linux CI workspace."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("C poll helper building is restricted to Linux CI")
    for path in (source, wasi, validation):
        physical(path)
    if (
        not destination.is_absolute()
        or not destination.parent.is_dir()
        or destination.parent.resolve() != destination.parent
        or os.path.lexists(destination)
        or shutil.disk_usage(destination.parent).free < 8 * GIB
    ):
        raise ValueError("new physical private helper workspace required")
    wasi_report, headers = verify_wasi(source, wasi)
    validation_report = runtime_inputs.report(validation / "validation-inputs.json")
    verify_validation(validation, validation_report)
    sdk = wasi / "sdk-input/sdk"
    probe = HERE / "empty_poll_probe"
    snapshots = {
        str(path): fetch_sdk.inventory(path)
        for path in (sdk, headers, probe, validation / "wasmtime", HERE / "rpds_probe")
    }
    inputs = {
        str(path): fingerprint(path)
        for path in (
            wasi / "wasi-build.json",
            wasi / "sdk-input/sdk-manifest.json",
            validation / "validation-inputs.json",
        )
    }
    destination.mkdir(mode=0o700)
    work = destination / "work"
    work.mkdir(mode=0o700)
    (work / "home").mkdir(mode=0o700)
    recipe = destination / "build-helper.sh"
    recipe.write_text(RECIPE)
    report = {
        "format": "tobkiri.ui-empty-poll-c-helper/1",
        "outcome": "pending",
        "inputs": inputs,
        "target_headers": wasi_report["target_headers"],
        "input_inventories": snapshots,
        "recipe": fingerprint(recipe),
        "compiled": False,
        "guest_execution": False,
        "c_abi_verified": False,
        "all_link_providers_verified": False,
        "cold_schema_verified": False,
        "production_enabled": False,
    }
    try:
        command = sandbox_command(
            None,
            work,
            recipe,
            (
                (sdk, "/sdk"),
                (headers, "/target-headers"),
                (probe, "/probe-source"),
                (HERE / "rpds_probe", "/support/rpds_probe"),
                (validation / "wasmtime", "/validation-deps"),
            ),
        )
        report["supervision"] = run_stage(
            "ui-empty-poll-c-helper", command, work, destination / "supervision"
        )
        helper = work / "_tobkiri_poll_oracle.abi3.so"
        report["helper"] = fingerprint(helper, 32 * 1024 * 1024)
        inspection = work / "helper-inspection.json"
        fingerprint(inspection, 4 * 1024 * 1024)
        report["inspection"] = json.loads(inspection.read_bytes())
        if (
            report["inspection"]["helper"] != report["helper"]
            or report["inspection"]["shared_imports_verified"] is not True
        ):
            raise ValueError("helper inspection does not bind its actual output")
        report["compiled"] = True
        report["outcome"] = "success"
    except BaseException as error:
        report["outcome"] = "failed"
        report["failure_type"] = type(error).__name__
        raise
    finally:
        report["inputs_unchanged"] = (
            all(
                fetch_sdk.inventory(Path(path)) == before
                for path, before in snapshots.items()
            )
            and all(
                fingerprint(Path(path)) == before for path, before in inputs.items()
            )
            and verify_tree(source) == wasi_report["source"]
        )
        if not report["inputs_unchanged"]:
            report["outcome"] = "failed"
        (destination / "helper-build.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
    if not report["inputs_unchanged"]:
        raise ValueError("immutable helper input changed")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "wasi", "validation", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    result = run(**vars(parser.parse_args()))
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "outcome",
                    "compiled",
                    "guest_execution",
                    "c_abi_verified",
                    "cold_schema_verified",
                    "production_enabled",
                )
            }
        )
    )
