"""Stage source-bound v2 inputs, without building or activating any component."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import stage_ordered_tool_bridge as v1
from stage_ordered_tool_bridge import PACK, ROOT, bounded_read, strict_json

OBSERVATIONS_SHA256 = (
    "3387411b6ac46b160e793477d270aa1c110a0d58f033937d58957ffe47a539e4"
)
INTERFACE = "local:ai-tool-bridge-ordered-python/bridge@2.0.0"
PROTOCOL = "ordered-finite-json-v2"
COUNTS = {"success": 18, "original-invalid": 12, "excluded-not-parity": 3}
NO_EXECUTION = (
    "wasm_execution_performed",
    "native_core_acceptance",
    "full_host_replay_performed",
    "production_enabled",
)
STAGER = "scripts/wasm/stage_ordered_tool_bridge_v2.py"
GUEST_ADDITIONS = {
    "app_v2.py": "0e0ba561b6ea7ce749024b2656384d14e040bca5f1505949ad51322882e29f75",
    "wit/pack-v2.wit": (
        "513454d839ac468205bc5b4ed93032625ed4f5d3064aa17f643f907e80bfeb78"
    ),
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _no_links(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink path is forbidden")


def _read(path: Path) -> bytes:
    _no_links(path.absolute())
    return bounded_read(path, 16 * 1024 * 1024)


def _record(path: str, data: bytes) -> dict[str, object]:
    return {"path": path, "bytes": len(data), "sha256": _digest(data)}


def _review() -> tuple[bytes, dict[str, bytes]]:
    """Capture reviewed sources before writing; do not execute the observer."""
    observation_path = PACK / "integer-v2.observations.json"
    raw = _read(observation_path)
    report = strict_json(raw)
    if type(report) is not dict or _digest(raw) != OBSERVATIONS_SHA256:
        raise ValueError("v2 observations differ from the reviewed SHA-256")
    if (
        report.get("format") != "tobkiri.ordered-integer-source-observations/1"
        or report.get("protocol") != PROTOCOL
        or report.get("runtime")
        != {
            "implementation": "CPython",
            "python": "3.13.5",
            "unicode": "15.1.0",
            "int_max_str_digits": 4300,
        }
        or report.get("frozen_case_counts") != COUNTS
        or report.get("admitted_source_parity_count") != 30
        or any(report.get(key) is not False for key in NO_EXECUTION)
    ):
        raise ValueError("v2 source observation identity or proof scope differs")
    bindings = report.get("source_bindings")
    if (
        type(bindings) is not list
        or len(bindings) != 19
        or _digest(_canonical(bindings)) != report.get("source_bindings_sha256")
    ):
        raise ValueError("v2 source bindings differ from reviewed observations")
    captured = {}
    for binding in bindings:
        relative = binding["path"]
        data = _read(ROOT / relative)
        if relative in captured or _record(relative, data) != binding:
            raise ValueError(f"source differs from reviewed observations: {relative}")
        captured[relative] = data

    # Inventory sources are also used by the unchanged extraction verifier.
    prefix = PACK.relative_to(ROOT).as_posix()
    inventory = strict_json(captured[f"{prefix}/python/inventory.json"])
    for binding in inventory["sources"]:
        relative = binding["path"]
        data = _read(ROOT / relative)
        if _record(relative, data) != binding:
            raise ValueError(f"inventory source differs: {relative}")
        captured[relative] = data
    oracle_raw, frozen_raw = v1.reviewed_inputs()
    oracle, frozen = strict_json(oracle_raw), strict_json(frozen_raw)
    if (
        report.get("oracle_sha256") != _digest(oracle_raw)
        or report.get("oracle_freeze_sha256") != _digest(frozen_raw)
        or report.get("case_inputs_sha256") != frozen["case_inputs_sha256"]
        or len(report.get("cases", [])) != len(oracle["cases"])
    ):
        raise ValueError("v2 observations do not bind the complete frozen input set")
    for observed, original in zip(report["cases"], oracle["cases"], strict=True):
        expected = (
            "original-invalid"
            if original["transport"]["status"] == "invalid"
            else (
                "excluded-not-parity"
                if "inner-nonfinite" in original["id"]
                else "success"
            )
        )
        if (
            any(observed.get(key) != original[key]
                for key in ("id", "operation", "input_json", "original"))
            or observed.get("v1_classification") != original["transport"]["status"]
            or observed.get("classification") != expected
            or observed.get("admitted_source_parity")
            is not (expected != "excluded-not-parity")
        ):
            raise ValueError("v2 frozen-case classification or parity claim differs")

    destinations = {
        **{
            name: f"{prefix}/python/{name}"
            for name in (
                "original_bridge.py", "inventory.json", "provenance.json",
                "caller_adapter.py", "caller_adapter_v2.py", "app_v2.py",
            )
        },
        **{
            name: f"{prefix}/{name}"
            for name in ("ordered_json.py", "ordered_json_v2.py", "wit/pack-v2.wit")
        },
        "wit/deps/core.wit": "crates/tobkiri-wit/wit/core/core.wit",
    }
    files, staged_bindings = {}, []
    for destination, relative in sorted(destinations.items()):
        data = captured.get(relative)
        if data is None:
            data = captured[relative] = _read(ROOT / relative)
        if (
            destination in GUEST_ADDITIONS
            and _digest(data) != GUEST_ADDITIONS[destination]
        ):
            raise ValueError(f"versioned guest source differs: {destination}")
        files[destination] = data
        staged_bindings.append({
            **_record(relative, data), "destination": destination,
        })
    captured[STAGER] = _read(ROOT / STAGER)
    companion = {
        "format": "tobkiri.ordered-python-v2-staged-inputs/1",
        "protocol": PROTOCOL,
        "source_observations_scope": "source-adapter-only",
        "source_observations_sha256": _digest(raw),
        "source_observations_bindings_sha256": report["source_bindings_sha256"],
        "frozen_case_counts": COUNTS,
        "admitted_source_parity_count": 30,
        "original_frozen_inputs_sha256": _digest(frozen_raw),
        "guest_source_scope": "source-stage-only-not-execution",
        "guest_source_bindings": staged_bindings,
        "staging_script": _record(STAGER, captured[STAGER]),
        "build_performed": False,
        **dict.fromkeys(NO_EXECUTION, False),
    }
    files["reviewed-inputs.json"] = frozen_raw
    files["reviewed-inputs-v2.json"] = _canonical(companion) + b"\n"
    # Reject changes during validation/capture, including inventory-only sources.
    if _read(observation_path) != raw or any(
        _read(ROOT / relative) != data for relative, data in captured.items()
    ):
        raise ValueError("source changed during staging")
    return raw, files


def reviewed_inputs() -> tuple[bytes, bytes]:
    """Return exact source observations and hash-only v2 stage bindings."""
    raw, files = _review()
    return raw, files["reviewed-inputs-v2.json"]


def stage(output: Path) -> dict[str, object]:
    """Write a fresh SDK plan; the existing parent must be trusted and stable.

    The plan binds exact source bytes, not behavior. Reference answers, observer
    code, original Host code and build tools never enter the guest source set.
    A failed fresh directory is retained for diagnosis rather than overwritten.
    """
    output = output.absolute()
    _no_links(output)
    if output.exists() or not output.parent.is_dir():
        raise ValueError("stage output must be fresh with an existing trusted parent")
    _, files = _review()
    plan = {
        "format": "tobkiri.python-build/3",
        "stub_wasi": True,
        "wit": "wit/pack-v2.wit",
        "world": "pack",
        "entrypoint": "app_v2",
        "sources": [
            {"source": name, "destination": name, "sha256": _digest(data)}
            for name, data in sorted(files.items())
        ],
        "outer_exports": ["tobkiri:core/lifecycle@1.0.0", INTERFACE],
        "allowed_imports": [
            f"tobkiri:core/{name}@1.0.0"
            for name in ("types", "flow", "clock", "random", "audit")
        ],
    }
    output.mkdir()
    for name, data in sorted(files.items()):
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    with (output / "plan.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(plan, indent=2) + "\n")
    return plan


def main() -> None:
    """Stage an inactive versioned source candidate, never invoke a producer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    stage(parser.parse_args().output)


if __name__ == "__main__":
    main()
