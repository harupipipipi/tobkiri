"""Bind the diagnostic artifact's exact producer, source and WIT receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

import component_inputs
from build_native import STAGED_SHA256


def read_regular(path: Path, bound: int) -> bytes:
    if not path.is_absolute() or path.resolve(strict=True) != path or path.is_symlink():
        raise ValueError("physical diagnostic input required")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > bound:
            raise ValueError("regular diagnostic input required")
        data = stream.read(bound + 1)
        after = os.fstat(stream.fileno())
    if len(data) > bound or len(data) != before.st_size or after.st_size != before.st_size:
        raise ValueError("diagnostic input changed or exceeds its bound")
    return data


def bind(artifact: Path, receipt: Path, proof: Path, plan: Path) -> dict[str, str]:
    raw = read_regular(artifact, 32 * 1024 * 1024)
    outer = json.loads(read_regular(receipt, 4 * 1024 * 1024))
    build_raw = read_regular(proof, 4 * 1024 * 1024)
    build = json.loads(build_raw)
    plan_raw = read_regular(plan, 1024 * 1024)
    actual_plan = json.loads(plan_raw)
    expected_plan = component_inputs._thread_capabilities_plan(
        component_inputs._capture_thread_capabilities_sources(component_inputs.HERE.parents[1])
    )
    identity = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    name = "harness-thread-capabilities"
    candidate = outer["components"][name]
    if (outer["format"] != "tobkiri.python313-native-thread-capabilities-diagnostic-candidate/1"
            or outer["outcome"] != "success" or outer["inputs_unchanged"] is not True
            or outer["diagnostic_only"] is not True or outer["production_enabled"] is not False
            or outer["whole_component_parity"] is not False
            or outer["native_core_replay"] is not False
            or outer["product_resident_concurrency_cancel_acceptance_claimed"] is not False
            or outer["guest_runtime_identity_verified"] is not False
            or outer["source"]["sha256"] != STAGED_SHA256
            or set(outer["components"]) != {name}
            or candidate["artifact"] != identity
            or candidate["build_receipt"] != {
                "bytes": len(build_raw), "sha256": hashlib.sha256(build_raw).hexdigest(),
            }
            or actual_plan != expected_plan
            or candidate["source"]["plan_sha256"] != hashlib.sha256(plan_raw).hexdigest()
            or build["build_plan_sha256"] != hashlib.sha256(plan_raw).hexdigest()
            or build["artifact_sha256"] != identity["sha256"]
            or build["artifact_bytes"] != identity["bytes"]
            or build["compiler_executable_sha256"] != outer["producer"]["sha256"]
            or build["producer_ci_receipt_sha256"] != outer["producer_receipt"]["sha256"]
            or build["world"] != component_inputs.THREAD_CAPABILITIES_WORLD
            or build["imports"] != component_inputs.THREAD_CAPABILITIES_IMPORTS
            or build["exports"] != component_inputs.THREAD_CAPABILITIES_EXPORTS
            or build["options"]["stub_wasi"] is not True
            or build["options"]["inherited_environment"] is not False
            or build["format"] != "tobkiri.component-build/2"
            or build["target_validation"]["exact_exports"] is not True
            or build["target_validation"]["typed_target_check"] is not True
            or build["diagnostic_only"] is not True
            or type(build.get("stdlib_initialization")) is not dict
            or build["stdlib_initialization"] != component_inputs.THREAD_CAPABILITIES_INITIALIZATION
            or build["stdlib_initialization"]["late_import_parity"] is not False
            or build["whole_component_parity"] is not False
            or build["guest_runtime_identity_verified"] is not False
            or build["product_resident_concurrency_cancel_acceptance_claimed"] is not False
            or build["production_enabled"] is not False):
        raise ValueError("diagnostic artifact, producer or closed boundary differs")
    return {
        "TOBKIRI_TEST_THREAD_COMPONENT": str(artifact),
        "TOBKIRI_TEST_THREAD_COMPONENT_SHA256": identity["sha256"],
        "TOBKIRI_TEST_THREAD_CAPABILITIES_CI": "1",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("artifact", "receipt", "proof", "plan", "github-env"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    values = bind(args.artifact, args.receipt, args.proof, args.plan)
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("unsafe diagnostic environment value")
    with args.github_env.open("a", encoding="utf-8") as stream:
        for name, value in values.items():
            stream.write(f"{name}={value}\n")
    print(json.dumps({"diagnostic_inputs_bound": True, "product_acceptance": False}))
