"""Freeze actual original-Python observations, never hand-authored expectations."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import unicodedata

from prepare_ordered_tool_bridge import prepare, verify

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "packs/local.ai-tool-bridge-ordered-python"
PREFIX = "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize"


def cases() -> list[dict[str, object]]:
    """Return input probes only; outputs must come from the exact oracle."""
    values = [
        ("empty", {}),
        ("ordinary", {"request_id": " req ", "intents": [
            {"function": {"name": "echo", "arguments": {"z": 1.0, "a": -0.0}}}]}),
        ("unicode-repr-15-1", {"request_id": {"\u1c89": 1}, "intents": [
            {"name": "echo", "arguments": {}}]}),
        ("opaque-inner-json", {"intents": [{"name": "echo", "arguments":
            '{"z":1.0,"a":-0.0,"dup":1,"dup":2,"u":"日本語"}'}]}),
        ("invalid-name", {"intents": [{"name": "a b"}]}),
        ("invalid-item", {"intents": [1]}),
        ("invalid-arguments", {"intents": [{"name": "echo", "arguments": "[1]"}]}),
        ("invalid-json", {"intents": [{"name": "echo", "arguments": "{"}]}),
        ("inner-bigint-excluded", {"intents": [{"name": "echo", "arguments":
            '{"n":9007199254740992}'}]}),
        ("inner-nonfinite-excluded", {"intents": [{"name": "echo", "arguments":
            '{"n":NaN}'}]}),
        ("inner-depth-excluded", {"intents": [{"name": "echo", "arguments":
            '{"n":' + '[' * 65 + '0' + ']' * 65 + '}'}]}),
    ]
    return [{"id": f"{suffix or 'base'}:{name}", "operation": PREFIX + suffix,
             "input": payload} for suffix in ("", ".generate", ".stream")
            for name, payload in values]


def freeze(output: Path) -> dict[str, object]:
    """Observe captured original code under the declared CPython/Unicode pair."""
    if platform.python_version() != "3.13.5" or unicodedata.unidata_version != "15.1.0":
        raise ValueError("oracle requires CPython 3.13.5 / Unicode 15.1.0")
    prepare(PACK / "python")
    verify(PACK / "python")
    sys.path[:0] = [str(PACK), str(PACK / "python")]
    from original_bridge import create_tool_intent_operation, GlobalContractInvocationError
    from ordered_json import dumps
    operation = create_tool_intent_operation(None)
    observations = []
    for case in cases():
        record = {"id": case["id"], "operation": case["operation"],
                  "input_json": dumps(case["input"])}
        try:
            result = operation("normalize", case["input"])
        except GlobalContractInvocationError as exc:
            record["original"] = {"status": "error", "code": exc.code,
                                  "message": str(exc)}
            record["transport"] = {"status": "invalid"}
        else:
            # Python's original repr is evidence even for excluded JSON values.
            record["original"] = {"status": "ok", "repr": repr(result)}
            try:
                record["transport"] = {"status": "ok", "output_json": dumps(result)}
            except (ValueError, TypeError, OverflowError, RecursionError):
                record["transport"] = {"status": "excluded"}
        observations.append(record)
    result = {"format": "tobkiri.ordered-python-tool-bridge-oracle/1",
              "protocol": "ordered-finite-json-v1",
              "python": platform.python_version(), "unicode": unicodedata.unidata_version,
              "source_provenance_sha256": hashlib.sha256(
                  (PACK / "python/provenance.json").read_bytes()).hexdigest(),
              "freeze_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "cases": observations}
    encoded = (json.dumps(result, ensure_ascii=True, indent=2) + "\n").encode()
    output.write_bytes(encoded)
    inputs = [{key: case[key] for key in ("id", "operation", "input_json")}
              for case in observations]
    binding = {
        "format": "tobkiri.ordered-tool-bridge-frozen-inputs/1",
        "protocol": result["protocol"],
        "original_reference_runtime": {"python": result["python"],
                                       "unicode": result["unicode"]},
        "oracle_sha256": hashlib.sha256(encoded).hexdigest(),
        "source_provenance_sha256": result["source_provenance_sha256"],
        "freeze_script_sha256": result["freeze_script_sha256"],
        "case_inputs_sha256": hashlib.sha256(json.dumps(
            inputs, ensure_ascii=True, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(),
        "case_count": len(inputs),
    }
    output.with_suffix(".freeze.json").write_text(json.dumps(binding, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    freeze(parser.parse_args().output)
