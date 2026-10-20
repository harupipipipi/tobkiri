"""Freeze actual original helper/source results, not Wasm or Host error parity.

The reference client calls the unchanged provider source directly. It is an
explicit source-semantics fixture, not a generated import, Core broker, or Host.
No retry, all-error-arm, authority, or runtime activation claim follows from it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
from types import ModuleType
from typing import Any
import unicodedata

import stage_gateway_tool_caller as caller_stage
import stage_ordered_tool_bridge_v2 as provider_stage

ROOT = caller_stage.ROOT
EXPECTED_RUNTIME = {
    "implementation": "CPython", "python": "3.13.5", "unicode": "15.1.0",
    "int_max_str_digits": 4300,
}
NO_EXECUTION = {
    "build_performed": False, "wasm_execution_performed": False,
    "native_core_acceptance": False, "core_runtime_acceptance": False,
    "full_host_replay_performed": False, "production_enabled": False,
    "legacy_http_error_parity_claimed": False,
    "all_typed_error_arms_executed": False, "retry_parity_claimed": False,
}


def digest(data: bytes) -> str:
    """Identify exact bytes, not their trust or execution provenance."""
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    """Serialize finite observation metadata deterministically."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def json_bytes(value: Any) -> bytes:
    """Serialize a frozen source observation document."""
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode()


def runtime() -> dict[str, Any]:
    """Read the interpreter actually observing source semantics."""
    return {
        "implementation": platform.python_implementation(),
        "python": platform.python_version(),
        "unicode": unicodedata.unidata_version,
        "int_max_str_digits": sys.get_int_max_str_digits(),
    }


def case_inputs() -> list[dict[str, str]]:
    """Define requests only; every successful expected value is observed."""
    cases = []

    def add(identifier: str, operation: str, collection: list[Any]) -> None:
        key = "events" if operation == "stream-event-intents" else "intents"
        payload = {"request_id": " req ", key: collection}
        cases.append({"id": identifier, "operation": operation,
                      "input_json": json.dumps(payload, ensure_ascii=False,
                                               separators=(",", ":"),
                                               allow_nan=False)})

    deep: Any = {"tail": [-0.0, 18446744073709551616]}
    for _ in range(80):
        deep = {"nested": [deep]}
    for prefix in ("generate", "stream"):
        operation = f"{prefix}-intents"
        values = {
            "native-bigints": [{"name": "echo", "arguments": {
                "above-u64": 18446744073709551616, "below-i64": -(2**80),
                "wide": 10**80, "$int": "inert DATA",
            }}],
            "inner-4300-digit-bigint": [{"name": "echo", "arguments":
                                       '{"n":' + "9" * 4300 + '}'}],
            "deep-values": [{"name": "echo", "arguments": deep}],
            "floats-order-negative-zero": [{"name": "echo", "arguments": {
                "z": 1.0, "negative_zero": -0.0, "a": 1e-7,
                "nested": {"b": 2, "a": 1}, "unicode": "𝔘é",
            }, "authority_granted": True, "approved": True, "executes": True}],
            "multiple-intents": [{"name": "one"}, {"name": "two", "id": "id"}],
            "empty-intents": [],
            "invalid-name": [{"name": "not a tool"}],
            "invalid-arguments": [{"name": "echo", "arguments": "{"}],
            "invalid-item": [None],
            "nonfinite-nan": [{"name": "echo", "arguments": '{"n":NaN}'}],
            "nonfinite-infinity": [{"name": "echo", "arguments": '{"n":Infinity}'}],
            "nonfinite-overflow": [{"name": "echo", "arguments": '{"n":1e400}'}],
        }
        for name, intents in values.items():
            add(f"{prefix}-{name}", operation, intents)

    def event(name: str, arguments: Any = None) -> dict[str, Any]:
        intent = {"name": name}
        if arguments is not None:
            intent["arguments"] = arguments
        return {"type": "tool_intent_delta", "tool_intent": intent}

    event_cases = {
        "singleton-index-zero": [event("one")],
        "mixed-skipped": [
            {"type": "text_delta", "text": "keep", "tool_intent": {"name": "skip"}},
            event("one"), {"type": "tool_intent_delta", "tool_intent": "skip"},
            {"type": "tool_intent_delta"}, event("two"),
        ],
        "multiple-events": [event("one"), event("two"), event("three")],
        "all-skipped": [{"type": "text_delta", "text": "keep"},
                        {"type": "tool_intent_delta", "tool_intent": None}],
        "later-invalid": [event("one"), event("invalid name"), event("never")],
        "later-nonfinite": [event("one"), event("two", '{"n":NaN}'),
                            event("source-only-third")],
    }
    for name, events in event_cases.items():
        add(f"events-{name}", "stream-event-intents", events)
    return cases


def source_bindings() -> list[dict[str, Any]]:
    """Bind reviewed source closures and observer without staging answers."""
    caller = json.loads(caller_stage.reviewed_inputs())
    provider_bytes, _ = provider_stage.reviewed_inputs()
    provider = json.loads(provider_bytes)
    paths = {
        item["path"] for item in caller["source_bindings"]
    } | {item["path"] for item in caller["inventory_sources"]}
    paths |= {item["path"] for item in provider["source_bindings"]}
    paths |= {
        "scripts/wasm/observe_gateway_tool_caller.py",
        "scripts/wasm/stage_gateway_tool_caller.py",
        "scripts/wasm/stage_ordered_tool_bridge_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/python/app_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/wit/pack-v2.wit",
    }
    bindings = []
    for path in sorted(paths):
        data = provider_stage.bounded_read(ROOT / path, 16 * 1024 * 1024)
        bindings.append({"path": path, "bytes": len(data), "sha256": digest(data)})
    return bindings


def _load(path: Path, name: str) -> ModuleType:
    # Execute only these reviewed verbatim sources; do not create __pycache__ in
    # the frozen caller tree or install a fake generated-binding package.
    module = ModuleType(name)
    exec(compile(provider_stage.bounded_read(path, 16 * 1024 * 1024),
                 str(path), "exec"), module.__dict__)
    return module


def observe() -> dict[str, Any]:
    """Run exact original source under the reviewed reference interpreter."""
    observed_runtime = runtime()
    if observed_runtime != EXPECTED_RUNTIME:
        raise ValueError("source observations require CPython 3.13.5 / Unicode15.1 / 4300")
    before = source_bindings()
    caller = _load(caller_stage.PACK / "python/original_gateway_tool_caller.py",
                   "original_caller_source_reference")
    provider = _load(provider_stage.PACK / "python/original_bridge.py",
                     "original_provider_source_reference")
    normalize = provider.create_tool_intent_operation(None)
    records = []
    for case in case_inputs():
        calls: list[dict[str, Any]] = []

        class SourceReferenceClient:
            """Direct source only: not Host error isolation or a component import."""

            def invoke(self, contract: str, operation: str, payload: Any) -> Any:
                calls.append({"contract": contract, "operation": operation,
                              "payload_json": json.dumps(payload, ensure_ascii=False,
                                                         separators=(",", ":"),
                                                         allow_nan=False)})
                return normalize(operation, payload)

        payload = json.loads(case["input_json"])
        record: dict[str, Any] = dict(case)
        try:
            if case["operation"] == "stream-event-intents":
                output = payload["events"]
                caller._attach_stream_tool_intents(SourceReferenceClient(), output,
                                                   payload["request_id"])
            else:
                output = caller._tool_intents(
                    SourceReferenceClient(), payload["intents"], payload["request_id"],
                    streaming=case["operation"] == "stream-intents",
                )
        except provider.GlobalContractInvocationError as exc:
            record.update(classification="provider-invalid",
                          original={"status": "error", "type": type(exc).__name__,
                                    "code": exc.code, "message": str(exc)},
                          expected={"status": "error", "code": "invalid",
                                    "message": "outside ordered-finite-json-v2 domain"})
        else:
            try:
                encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False)
            except ValueError:
                record.update(
                    classification="provider-nonfinite",
                    original={"status": "nonfinite-output", "repr": repr(output)},
                    expected={"status": "error", "code": "internal",
                              "message": "provider output contains a non-finite number"},
                )
            else:
                record.update(classification="success", output_json=encoded,
                              output_sha256=digest(encoded.encode()),
                              expected={"status": "ok", "output_json": encoded})
        record["source_reference_calls"] = calls
        record["source_reference_call_count"] = len(calls)
        record["source_call_counts_are_not_component_observations"] = True
        records.append(record)
    after = source_bindings()
    if before != after:
        raise ValueError("source changed while recording reference observations")
    provider_oracle, _ = provider_stage.reviewed_inputs()
    return {
        "format": "tobkiri.gateway-tool-caller-source-observations/1",
        "scope": "original-source-semantics-not-full-host-error-parity",
        "protocol": "ordered-finite-json-v2", "runtime": observed_runtime,
        "source_bindings": before, "source_bindings_sha256": digest(canonical(before)),
        "provider_33_oracle_sha256": digest(provider_oracle),
        "case_inputs_sha256": digest(canonical(case_inputs())),
        "counts": {key: sum(item["classification"] == key for item in records)
                   for key in ("success", "provider-invalid", "provider-nonfinite")},
        "cases": records, **NO_EXECUTION,
        "source_error_scope": (
            "typed expected errors are declared component boundaries, not observed "
            "Host errors; original direct source can continue after nonfinite output "
            "where the actual typed provider must stop"
        ),
    }


def main() -> None:
    """Write a new source-only candidate for independent review and freezing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    with arguments.output.open("xb") as stream:
        stream.write(json_bytes(observe()))


if __name__ == "__main__":
    main()
