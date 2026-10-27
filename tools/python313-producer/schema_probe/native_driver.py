"""Run the unchanged fullSchema controller through the real native 64MiB Store.

This replaces only observe(). The original oracle, corpus, inventories, clock
assertions, code-cache provenance and all four genuine denials remain in driver.
The injected owner is explicitly a portable CI fixture, never OS/GUI proof.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

sys.path[:0] = ["/probe-source", "/validation-deps", "/"]
TEST = "native_setup_store::entry::schema_probe::native_schema_component"
CONSUMER = Path("/native-data-consumer")
FUEL = 2_000_000_000
MEMORY = 64 * 1024 * 1024


def validate_report(report: dict, identity: str, operation: str) -> dict:
    """Validate actual execution metadata before the original controller sees it."""
    if (
        report.get("format") != "tobkiri.native-data-schema-observation/2"
        or report.get("component_sha256") != identity
        or report.get("operation") != operation
        or report.get("memory_limit_bytes") != MEMORY
        or report.get("fuel_limit") != FUEL
        or report.get("wall_seconds") != 30
        or report.get("original_deadline_preserved") is not True
        or report.get("portable_owner_fixture") is not True
        or report.get("actual_os_owner_proof") is not False
        or report.get("production_enabled") is not False
    ):
        raise ValueError("native Store identity, limits or evidence scope differs")
    elapsed = report.get("elapsed_seconds")
    if (
        type(elapsed) not in (int, float)
        or not math.isfinite(elapsed)
        or not 0 <= elapsed < 30
    ):
        raise ValueError("native call did not complete its original wall lifetime")
    observed = report.get("observation")
    if not isinstance(observed, dict):
        raise ValueError("native Store observation missing")
    if (
        type(observed.get("post_return")) is not bool
        or observed.get("post_return_failed") is not False
        or type(observed.get("unreachable_trap")) is not bool
        or report.get("genuine_unreachable") is not observed["unreachable_trap"]
        or (observed["post_return"] and observed["unreachable_trap"])
    ):
        raise ValueError("native call outcome does not match the actual typed trap")
    fuel = observed.get("fuel_remaining")
    if type(fuel) is not int or not 0 < fuel <= FUEL:
        raise ValueError("native call did not complete its original fuel lifetime")
    if (
        observed.get("store_retired") is not True
        or observed.get("deadline_exceeded") is not False
    ):
        raise ValueError("native Store destruction or original deadline failed")
    counts = [
        observed.get(name)
        for name in (
            "wall_now",
            "wall_resolution",
            "monotonic_now",
            "monotonic_resolution",
        )
    ]
    total, completed = observed.get("clock_calls"), observed.get("clock_completed")
    if (
        any(type(value) is not int or value < 0 for value in counts)
        or type(total) is not int
        or type(completed) is not int
        or sum(counts) != total
        or not 0 <= total <= 100_000
        or completed != total
    ):
        raise ValueError("native clock callbacks did not actually complete")
    for prefix in ("wall", "monotonic"):
        resolution = observed.get(prefix + "_resolution_ns")
        if observed[prefix + "_resolution"] > 0 and (
            type(resolution) is not int or resolution <= 0
        ):
            raise ValueError("native clock resolution was not observed")
    return observed


def observe(
    path: Path,
    *,
    allow_clock=False,
    allow_monotonic=False,
    allow_stage_profile=False,
    stage_profile_api=None,
    profile_output_path=None,
    operation_name="run",
    arguments=(),
    expected_denial=False,
) -> dict:
    import driver
    from wasmtime import Config, Engine
    from wasmtime.component import Component

    if (
        allow_stage_profile
        or stage_profile_api is not None
        or profile_output_path is not None
        or not allow_clock
        or not allow_monotonic
    ):
        raise ValueError(
            "native64 adapter requires the exact original two-clock corpus"
        )
    if operation_name not in {
        "run",
        "probe-clock",
        "probe-monotonic",
        "probe-denied-io",
    }:
        raise ValueError("unknown original schema operation")
    if (operation_name == "probe-denied-io") != bool(expected_denial):
        raise ValueError("denial expectation differs from the original operation")
    if arguments not in ((), ("filesystem",), ("random",), ("subscribe",), ("poll",)):
        raise ValueError("unexpected schema arguments")
    if bool(arguments) != bool(expected_denial):
        raise ValueError("unexpected schema argument arity")

    original = driver.read(path, 32 * 1024 * 1024)
    identity = "sha256:" + hashlib.sha256(original).hexdigest()
    config = Config()
    config.parallel_compilation = False
    # Preserve the full original typed boundary inspection, then destroy the
    # Python engine before compiling/invoking through the native Rust engine.
    with Engine(config) as engine, Component(engine, original) as component:
        typed = driver.boundary(
            component, engine, inner=False, allow_clock=True, allow_monotonic=True
        )
    del original

    control = json.loads(driver.read(Path("/control.json")))
    pinned = control.get("native_data_consumer")
    if not isinstance(pinned, dict):
        raise ValueError("explicit native DATA consumer identity missing")
    digest = hashlib.sha256()
    with CONSUMER.open("rb") as binary:
        for block in iter(lambda: binary.read(262144), b""):
            digest.update(block)
    if digest.hexdigest() != pinned.get("sha256"):
        raise ValueError("native DATA consumer binary changed")

    evidence = Path("/work/native-data-observations")
    evidence.mkdir(mode=0o700, exist_ok=True)
    ordinal = len(list(evidence.glob("*.json")))
    output = evidence / f"{ordinal:02d}-{operation_name}.json"
    environment = {
        **os.environ,
        "TOBKIRI_NATIVE_DATA_CI": "1",
        "TOBKIRI_NATIVE_COMPONENT": str(path),
        "TOBKIRI_NATIVE_COMPONENT_SHA256": identity,
        "TOBKIRI_NATIVE_OPERATION": operation_name,
        "TOBKIRI_NATIVE_REPORT": str(output),
    }
    if arguments:
        environment["TOBKIRI_NATIVE_ARGUMENT"] = arguments[0]
    else:
        environment.pop("TOBKIRI_NATIVE_ARGUMENT", None)
    with output.with_suffix(".log").open("xb") as log:
        process = subprocess.run(
            [
                str(CONSUMER),
                "--exact",
                TEST,
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
    if process.returncode:
        raise RuntimeError("actual native DATA consumer failed; see bounded CI log")
    # JSON escaping of the original 4MiB UTF-8 envelope can expand the observer
    # transport. This does not increase the guest/ABI/product output boundary.
    report = json.loads(driver.read(output, 32 * 1024 * 1024))
    observed = validate_report(report, identity, operation_name)
    if (
        "sha256:" + hashlib.sha256(driver.read(path, 32 * 1024 * 1024)).hexdigest()
        != identity
    ):
        raise ValueError("component changed between the original and native consumers")
    denial = None
    if expected_denial:
        if (
            report.get("genuine_unreachable") is not True
            or report.get("value") is not None
        ):
            raise ValueError(
                "native non-clock IO did not produce a genuine unreachable trap"
            )
        if observed.get("post_return") is not False or not isinstance(
            report.get("error"), str
        ):
            raise ValueError("native denial lifecycle differs")
        denial = {
            "kind": arguments[0],
            "trap": "wasm unreachable",
            "store_retired": True,
            "wasm_stack": report["error"][:4096],
        }
        result = None
    else:
        value = report.get("value")
        if (
            report.get("error") is not None
            or observed.get("post_return") is not True
            or type(value) is not str
            or len(value.encode()) > driver.OUTPUT_LIMIT
        ):
            raise ValueError(
                "native DATA call failed its original string/post-return boundary"
            )
        result = json.loads(value)
    budget = {
        "total": observed["clock_calls"],
        "limit": 100_000,
        "deadline_enforced": True,
    }

    def clock(prefix: str, import_name: str, domain: str) -> dict:
        now, resolution = observed[prefix + "_now"], observed[prefix + "_resolution"]
        return {
            "evidence_kind": "ci-native-data-clock-bridge",
            "import_name": import_name,
            "clock_domain": "CI host " + domain,
            "clock_source": "native clock_gettime(" + domain + ")",
            "resolution_source": "native clock_getres(" + domain + ")",
            "resolution_nanoseconds_ceiling": observed[prefix + "_resolution_ns"],
            "calls": {"now": now, "resolution": resolution, "total": now + resolution},
            "shared_budget": budget,
            "deadline_enforced": True,
            "linker_installed": True,
            "production_ui_clock_connected": False,
            "production_enabled": False,
        }

    return {
        "result": result,
        "fuel_consumed": FUEL - observed["fuel_remaining"],
        "post_return": observed["post_return"],
        "denial": denial,
        "clock": clock("wall", "wasi:clocks/wall-clock@0.2.4", "CLOCK_REALTIME"),
        "monotonic_clock": clock(
            "monotonic", "tobkiri-ui:clock/monotonic@1.0.0", "CLOCK_MONOTONIC"
        ),
        "clock_budget": budget,
        "clock_callbacks_completed": True,
        "native_data_consumer": {
            "memory_bytes": MEMORY,
            "component_sha256": identity,
            "actual_os_owner_proof": False,
            "production_enabled": False,
        },
        **typed,
    }


if __name__ == "__main__":
    import driver

    driver.observe = observe
    driver.main()
