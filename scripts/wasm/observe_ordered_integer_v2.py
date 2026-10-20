"""Observe an additive v2 source adapter, never build or execute a component."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import unicodedata

from stage_ordered_tool_bridge import PACK, ROOT, bounded_read, reviewed_inputs

sys.path[:0] = [str(PACK), str(PACK / "python")]
import caller_adapter_v2 as adapter
import ordered_json as v1
import ordered_json_v2 as v2
from original_bridge import GlobalContractInvocationError, create_tool_intent_operation


def digest(data: bytes) -> str:
    """Return a SHA-256 identity for captured bytes."""
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    """Encode observation metadata, leaving request and output strings opaque."""
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def runtime() -> dict[str, object]:
    """Report the real local interpreter, including its conversion safety limit."""
    return {
        "implementation": platform.python_implementation(),
        "python": platform.python_version(),
        "unicode": unicodedata.unidata_version,
        "int_max_str_digits": sys.get_int_max_str_digits(),
    }


def _bindings() -> list[dict[str, object]]:
    paths = [
        "packs/local.ai-tool-bridge-ordered-python/oracle.json",
        "packs/local.ai-tool-bridge-ordered-python/oracle.freeze.json",
        "packs/local.ai-tool-bridge-ordered-python/ordered_json.py",
        "packs/local.ai-tool-bridge-ordered-python/ordered_json_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/python/caller_adapter.py",
        "packs/local.ai-tool-bridge-ordered-python/python/caller_adapter_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/python/original_bridge.py",
        "packs/local.ai-tool-bridge-ordered-python/python/provenance.json",
        "packs/local.ai-tool-bridge-ordered-python/python/inventory.json",
        "scripts/wasm/freeze_ordered_tool_bridge.py",
        "scripts/wasm/prepare_ordered_tool_bridge.py",
        "scripts/wasm/stage_ordered_tool_bridge.py",
        "scripts/wasm/observe_ordered_integer_v2.py",
        "crates/tobkiri-wit/wit/core/core.wit",
        "crates/tobkiri-canon/src/codec.rs",
        "tobkiri_runtime/tobkiri_host/broker.py",
        "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/executables.v4.json",
    ]
    provenance = json.loads((PACK / "python/provenance.json").read_bytes())
    paths.extend(item["path"] for item in provenance["sources"])
    result = []
    for relative in sorted(set(paths)):
        data = bounded_read(ROOT / relative, 16 * 1024 * 1024)
        result.append({"path": relative, "bytes": len(data), "sha256": digest(data)})
    return result


def _outer_size(value: object) -> int:
    # This mirrors canonical string/record escaping for these finite metadata
    # values, not a native Core execution or a claim of arbitrary inner capacity.
    return len(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8"))


def observe() -> dict[str, object]:
    """Capture exact reference-source outputs and separate remaining exclusions."""
    observed_runtime = runtime()
    expected_runtime = {
        "implementation": "CPython", "python": "3.13.5", "unicode": "15.1.0",
        "int_max_str_digits": 4300,
    }
    if observed_runtime != expected_runtime:
        raise ValueError("observations require CPython 3.13.5 / Unicode 15.1 / 4300")
    oracle_raw, freeze_raw = reviewed_inputs()
    bindings_before = _bindings()
    oracle = json.loads(oracle_raw)
    original = create_tool_intent_operation(None)
    records = []
    for case in oracle["cases"]:
        payload = v1.loads(case["input_json"])
        record = {key: case[key] for key in ("id", "operation", "input_json")}
        record["v1_classification"] = case["transport"]["status"]
        record["admitted_source_parity"] = False
        try:
            output = original("normalize", payload)
        except GlobalContractInvocationError as exc:
            observed = {"status": "error", "code": exc.code, "message": str(exc)}
            if observed != case["original"]:
                raise ValueError("original error differs from frozen observation")
            try:
                adapter.invoke(case["operation"], case["input_json"])
            except GlobalContractInvocationError as actual:
                if (actual.code, str(actual)) != (exc.code, str(exc)):
                    raise ValueError("v2 original error differs") from actual
            else:
                raise ValueError("v2 failed to preserve original error")
            record.update(original=observed, classification="original-invalid",
                          admitted_source_parity=True)
        else:
            observed = {"status": "ok", "repr": repr(output)}
            if observed != case["original"]:
                raise ValueError("original result differs from frozen observation")
            record["original"] = observed
            try:
                actual = adapter.invoke(case["operation"], case["input_json"])
            except v2.OrderedJSONError as exc:
                if case["transport"]["status"] != "excluded":
                    raise ValueError("v2 unexpectedly excluded a v1 case") from exc
                record.update(classification="excluded-not-parity",
                              actual_error=str(exc))
            else:
                # Independent standard JSON serialization of the actual original
                # result supplies expected bytes, not a hand-authored answer.
                expected = json.dumps(
                    output, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
                if actual != expected or repr(adapter.response(actual)) != repr(output):
                    raise ValueError("v2 result differs from actual original output")
                if (case["transport"]["status"] == "ok"
                        and actual != case["transport"]["output_json"]):
                    raise ValueError("v2 changed a frozen v1 successful result")
                record.update(classification="success", output_json=actual,
                              output_sha256=digest(actual.encode("utf-8")),
                              admitted_source_parity=True)
                record["outer_response_bytes"] = _outer_size(actual)
        record["outer_request_bytes"] = _outer_size({
            "operation": case["operation"], "payload": case["input_json"],
        })
        if any(record.get(key, 0) > v1.MAX_BYTES for key in
               ("outer_request_bytes", "outer_response_bytes")):
            raise ValueError("observed case exceeds the unchanged outer byte bound")
        records.append(record)

    boundaries = []
    for digits in (4300, 4301):
        for sign in ("", "-"):
            token = sign + "9" * digits
            payload = {"intents": [{"name": "echo", "arguments": '{"n":' + token + "}"}]}
            input_json = adapter.request(payload)
            entry = {"digits": digits, "negative": bool(sign), "input_json": input_json}
            try:
                output = original("normalize", payload)
            except ValueError as exc:
                # This is not GlobalContractInvocationError or successful-output
                # exclusion. Preserve the original parser's distinct error.
                try:
                    adapter.invoke(adapter.OPERATIONS[0], input_json)
                except ValueError as actual:
                    if type(actual) is not type(exc) or str(actual) != str(exc):
                        raise ValueError("original digit-limit error changed") from actual
                else:
                    raise ValueError("v2 accepted an original digit-limit error")
                entry.update(status="original-ValueError", message=str(exc),
                             candidate_matches_original_error=True)
            else:
                expected = json.dumps(
                    output, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
                actual = adapter.invoke(adapter.OPERATIONS[0], input_json)
                if actual != expected:
                    raise ValueError("integer boundary result differs")
                entry.update(status="success", output_json=actual,
                             output_sha256=digest(actual.encode("utf-8")),
                             outer_response_bytes=_outer_size(actual))
            entry["outer_request_bytes"] = _outer_size({
                "operation": adapter.OPERATIONS[0], "payload": input_json,
            })
            if any(entry.get(key, 0) > v1.MAX_BYTES for key in
                   ("outer_request_bytes", "outer_response_bytes")):
                raise ValueError("integer boundary exceeds native outer byte bound")
            boundaries.append(entry)

    counts = {
        name: sum(item["classification"] == name for item in records)
        for name in ("success", "original-invalid", "excluded-not-parity")
    }
    if counts != {"success": 15, "original-invalid": 12, "excluded-not-parity": 6}:
        raise ValueError("unexpected complete frozen-case classification")
    inputs = [{key: case[key] for key in ("id", "operation", "input_json")}
              for case in oracle["cases"]]
    bindings = _bindings()
    if bindings != bindings_before:
        raise ValueError("source bindings changed during observation")
    return {
        "format": "tobkiri.ordered-integer-source-observations/1",
        "protocol": v2.PROTOCOL, "runtime": observed_runtime,
        "oracle_sha256": digest(oracle_raw), "oracle_freeze_sha256": digest(freeze_raw),
        "case_inputs_sha256": digest(canonical(inputs)),
        "source_bindings": bindings, "source_bindings_sha256": digest(canonical(bindings)),
        "limits": {"inner_utf8_bytes": v1.MAX_BYTES, "depth": v1.MAX_DEPTH,
                   "nodes_including_keys": v1.MAX_NODES, "integer_decimal_digits": 4300,
                   "outer_core_strict_v1_bytes": v1.MAX_BYTES},
        "frozen_case_counts": counts, "admitted_source_parity_count": 27,
        "wasm_execution_performed": False, "native_core_acceptance": False,
        "full_host_replay_performed": False, "production_enabled": False,
        "cases": records, "integer_boundaries": boundaries,
    }


def main() -> None:
    """Write fresh source evidence only; never overwrite historical observations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output
    if output.exists():
        raise ValueError("observation output must not already exist")
    report = observe()
    with output.open("xb") as stream:
        stream.write((json.dumps(report, ensure_ascii=True, indent=2) + "\n").encode())
    print(json.dumps({"output": str(output), "counts": report["frozen_case_counts"],
                      "wasm_execution_performed": False, "native_core_acceptance": False}))


if __name__ == "__main__":
    main()
