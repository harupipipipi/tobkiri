"""Observe original finalization source, never generated imports or a guest.

The direct source dependencies are real extracted pure helpers. Finite DATA
boundaries are an explicit reference projection: their declared Core mappings
are not observed Core errors, Host behavior, retries, billing or authority.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import platform
import sys
from types import ModuleType
from typing import Any, Iterator
import unicodedata

import stage_gateway_finalizer as finalizer_stage
import stage_gateway_finalization_providers as dependencies_stage
import stage_gateway_tool_caller as caller_stage
import stage_ordered_tool_bridge_v2 as bridge_stage

ROOT = finalizer_stage.ROOT
CATALOG = ROOT / finalizer_stage.PREFIX / "source-cases.json"
EXPECTED_RUNTIME = {
    "implementation": "CPython", "python": "3.13.5", "unicode": "15.1.0",
    "int_max_str_digits": 4300,
}
COUNTS = {"finalized": 5, "domain-rejected": 2, "core-error": 3}
NO_EXECUTION = {
    "build_performed": False, "wasm_execution_performed": False,
    "native_core_acceptance": False, "core_runtime_acceptance": False,
    "gate_runtime_acceptance": False, "full_host_replay_performed": False,
    "production_enabled": False, "finalizer_activated": False,
    "authority_granted": False, "billing_authority_claimed": False,
    "legacy_http_error_parity_claimed": False,
    "all_typed_error_arms_executed": False, "retry_parity_claimed": False,
    "guest_import_call_counts_observed": False,
}
SOURCE_MODULES = {
    "ordered_json": "packs/local.ai-tool-bridge-ordered-python/ordered_json.py",
    "ordered_json_v2": "packs/local.ai-tool-bridge-ordered-python/ordered_json_v2.py",
    "bridge": "packs/local.ai-tool-bridge-ordered-python/python/original_bridge.py",
    "caller": "packs/local.ai-gateway-tool-caller-python/python/original_gateway_tool_caller.py",
    "usage": "packs/local.ai-usage-cost-python/python/original_usage_cost.py",
    "stream": "packs/local.ai-stream-normalizer-python/python/original_stream_normalizer.py",
    "finalizer": "packs/local.ai-gateway-finalizer-python/python/original_gateway_finalizer.py",
}


def digest(data: bytes) -> str:
    """Return byte identity, not execution provenance or authentication."""
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    """Serialize finite observation metadata deterministically."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def json_bytes(value: Any) -> bytes:
    """Serialize a new reference document with no non-finite numbers."""
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode()


def runtime() -> dict[str, Any]:
    """Read the actual source-observation interpreter and Unicode version."""
    return {
        "implementation": platform.python_implementation(),
        "python": platform.python_version(),
        "unicode": unicodedata.unidata_version,
        "int_max_str_digits": sys.get_int_max_str_digits(),
    }


def _read(path: Path) -> bytes:
    return bridge_stage.bounded_read(path, 16 * 1024 * 1024)


def case_inputs() -> list[dict[str, Any]]:
    """Read the exact ten input cases, never stage their expected answers."""
    catalog = bridge_stage.strict_json(_read(CATALOG))
    if (type(catalog) is not dict or catalog.get("schema") !=
            "io.tobkiri.gateway-finalizer-source-cases.v1"):
        raise ValueError("invalid finalizer source-case catalog")
    cases = catalog.get("cases")
    if type(cases) is not list or len(cases) != 10:
        raise ValueError("source catalog must contain exactly ten cases")
    result = []
    for case in cases:
        fields = {"id", "operation", "payload", "expected_kind",
                  "expected_dependency_call_count"}
        if type(case) is not dict:
            raise ValueError("source case must be an object")
        kind = case.get("expected_kind")
        if kind != "finalized":
            fields.add("expected_error")
        if (set(case) != fields or type(case["id"]) is not str
                or not case["id"] or case["operation"] not in ("generate", "stream")
                or type(case["payload"]) is not dict or kind not in COUNTS
                or type(case["expected_dependency_call_count"]) is not int
                or not 0 <= case["expected_dependency_call_count"] <= 6):
            raise ValueError("source case identity or boundary differs")
        if kind != "finalized" and (
            type(case["expected_error"]) is not dict
            or set(case["expected_error"]) != {"code", "message"}
            or any(type(item) is not str for item in case["expected_error"].values())
        ):
            raise ValueError("source case error must retain exact code/message")
        result.append({
            **{key: value for key, value in case.items() if key != "payload"},
            "input_json": json.dumps(case["payload"], ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False),
        })
    if len({case["id"] for case in result}) != len(result):
        raise ValueError("duplicate source case")
    if {key: sum(case["expected_kind"] == key for case in result)
            for key in COUNTS} != COUNTS:
        raise ValueError("source case category accounting changed")
    return result


def source_bindings() -> list[dict[str, Any]]:
    """Verify each current source closure and bind all observation inputs."""
    documents = [
        json.loads(finalizer_stage.reviewed_inputs()),
        json.loads(caller_stage.reviewed_inputs()),
        *(json.loads(dependencies_stage.reviewed_inputs(provider))
          for provider in dependencies_stage.SOURCE_MODULES),
    ]
    bridge_observations, bridge_evidence = bridge_stage.reviewed_inputs()
    documents.extend((json.loads(bridge_observations), json.loads(bridge_evidence)))
    paths = set(SOURCE_MODULES.values()) | {
        CATALOG.relative_to(ROOT).as_posix(),
        "scripts/wasm/observe_gateway_finalizer.py",
        "scripts/wasm/stage_gateway_finalizer.py",
        "scripts/wasm/prepare_gateway_finalizer.py",
        "scripts/wasm/stage_gateway_finalization_providers.py",
        "scripts/wasm/prepare_gateway_finalization_providers.py",
        "scripts/wasm/stage_gateway_tool_caller.py",
        "scripts/wasm/prepare_gateway_tool_caller.py",
        "scripts/wasm/stage_ordered_tool_bridge_v2.py",
        "scripts/wasm/stage_ordered_tool_bridge.py",
        "scripts/wasm/prepare_ordered_tool_bridge.py",
        "scripts/wasm/freeze_ordered_tool_bridge.py",
        "packs/local.ai-tool-bridge-ordered-python/integer-v2.observations.json",
    }
    for document in documents:
        for key in ("source_bindings", "guest_source_bindings", "inventory_sources",
                    "observer_evidence"):
            entries = document.get(key, [])
            if type(entries) is list:
                paths.update(item["path"] for item in entries)
    # Inventories can bind original manifests/source files outside guest plans.
    for prefix in (finalizer_stage.PREFIX, caller_stage.PACK.relative_to(ROOT),
                   *dependencies_stage.PREFIXES.values(),
                   bridge_stage.PACK.relative_to(ROOT)):
        inventory = json.loads(_read(ROOT / prefix / "python/inventory.json"))
        paths.update(item["path"] for item in inventory["sources"])
    return [{"path": path, "bytes": len(data), "sha256": digest(data)}
            for path in sorted(paths) for data in [_read(ROOT / path)]]


@contextmanager
def source_modules() -> Iterator[dict[str, ModuleType]]:
    """Load only reviewed real source, without generated-binding packages/cache."""
    modules: dict[str, ModuleType] = {}
    previous: dict[str, ModuleType | None] = {}
    try:
        for key, path in SOURCE_MODULES.items():
            name = key if key.startswith("ordered_json") else f"_finalizer_source_{key}"
            previous[name] = sys.modules.get(name)
            module = ModuleType(name)
            sys.modules[name] = module
            exec(compile(_read(ROOT / path), str(ROOT / path), "exec"),
                 module.__dict__)
            modules[key] = module
        yield modules
    finally:
        for name, prior in previous.items():
            if prior is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior


class SourceBoundaryFailure(RuntimeError):
    """Declared finite-transport mapping, NOT an observed imported Core error."""

    def __init__(self, code: str, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.code, self.evidence = code, evidence


def observe_case(case: dict[str, Any], modules: dict[str, ModuleType]) -> dict[str, Any]:
    """Call original helpers/providers once and retain exact source custody."""
    codec, finalizer = modules["ordered_json_v2"], modules["finalizer"]
    caller, bridge = modules["caller"], modules["bridge"]
    usage, stream = modules["usage"], modules["stream"]
    calls: list[dict[str, Any]] = []
    nested: list[dict[str, Any]] = []

    def exchange(provider: str, operation: str, payload: Any,
                 function: Any, log: list[dict[str, Any]]) -> Any:
        # Preserve non-list intents unchanged until the original bridge sees them.
        encoded = codec.dumps(payload)
        entry: dict[str, Any] = {
            "provider": provider, "operation": operation, "payload_json": encoded,
        }
        log.append(entry)
        try:
            output = function(operation, codec.loads(encoded))
        except (bridge.GlobalContractInvocationError, OverflowError) as error:
            entry["original"] = {"status": "exception", "type": type(error).__name__,
                                 "message": str(error)}
            if hasattr(error, "code"):
                entry["original"]["code"] = error.code
            if provider == "bridge" and isinstance(error, bridge.GlobalContractInvocationError):
                raise SourceBoundaryFailure(
                    "invalid", "outside ordered-finite-json-v2 domain", entry,
                ) from error
            if provider == "usage-cost" and isinstance(error, OverflowError):
                raise SourceBoundaryFailure(
                    "internal", "usage cost provider arithmetic failed", entry,
                ) from error
            raise
        try:
            result = codec.dumps(output)
        except codec.NonFiniteJSONError as error:
            entry["original"] = {"status": "nonfinite-output", "repr": repr(output)}
            if provider != "usage-cost":
                raise
            raise SourceBoundaryFailure(
                "internal", "usage cost provider arithmetic failed", entry,
            ) from error
        entry["original"] = {"status": "ok", "output_json": result}
        return codec.loads(result)

    class OriginalBridgeClient:
        """A direct pure-source reference, not a generated import or Host."""

        def invoke(self, contract: str, operation: str, payload: Any) -> Any:
            if contract != caller.TOOL_BRIDGE_CONTRACT:
                raise ValueError("unexpected source bridge contract")
            return exchange("bridge", operation, payload,
                            bridge.create_tool_intent_operation(None), nested)

    class OriginalDependencies:
        """Route extracted helpers to actual original pure dependency source."""

        def invoke(self, contract: str, operation: str, payload: Any) -> Any:
            if contract == finalizer.USAGE_CONTRACT:
                return exchange("usage-cost", operation, payload,
                                usage.create_cost_operation(None), calls)
            if contract == finalizer.STREAM_NORMALIZE_CONTRACT:
                return exchange("stream-normalizer", operation, payload,
                                stream.create_stream_normalize_operation(None), calls)
            raise ValueError("unexpected original finalization contract")

        def tool_intents(self, intents: Any, request_id: str, *, streaming: bool) -> Any:
            operation = "stream-intents" if streaming else "generate-intents"
            entry: dict[str, Any] = {
                "provider": "caller", "operation": operation,
                "payload_json": codec.dumps({"request_id": request_id, "intents": intents}),
            }
            calls.append(entry)
            try:
                output = caller._tool_intents(
                    OriginalBridgeClient(), intents, request_id, streaming=streaming,
                )
            except SourceBoundaryFailure:
                entry["original"] = {"status": "source-dependency-boundary"}
                raise
            result = codec.dumps(output)
            entry["original"] = {"status": "ok", "output_json": result}
            return codec.loads(result)

    payload = codec.loads(case["input_json"])
    data = payload["candidate"]
    candidate = finalizer.Candidate(**{
        **data, "capabilities": frozenset(data["capabilities"]),
        "modalities": frozenset(data["modalities"]),
    })
    record: dict[str, Any] = dict(case)
    try:
        args = (OriginalDependencies(), payload["value"], payload["request_id"], candidate)
        if case["operation"] == "generate":
            output = finalizer._finalize_generate_result(
                *args, payload["attempts"], streaming=False, packvm_response=False,
            )
        else:
            output = finalizer._finalize_stream_result(
                *args, payload["attempt_number"], payload["attempts"], packvm_response=False,
            )
    except finalizer.GlobalContractInvocationError as error:
        expected = {"status": "domain-rejected", "code": error.code,
                    "message": str(error)}
        record["original"] = {"status": "exception", "type": type(error).__name__,
                              "code": error.code, "message": str(error)}
    except SourceBoundaryFailure as error:
        expected = {"status": "core-error", "code": error.code, "message": str(error)}
        record["original"] = {"status": "declared-terminal-transport-boundary",
                              "dependency": error.evidence}
    else:
        encoded = codec.dumps(output)
        expected = {"status": "finalized", "output_json": encoded}
        record.update(output_json=encoded, output_sha256=digest(encoded.encode()),
                      original={"status": "ok", "output_json": encoded})
    if (expected["status"] != case["expected_kind"]
            or (expected["status"] != "finalized" and
                {key: expected[key] for key in ("code", "message")} != case["expected_error"])
            or len(calls) != case["expected_dependency_call_count"]):
        raise ValueError(f"original source differs from reviewed case boundary: {case['id']}")
    record.update(
        classification=expected["status"], expected=expected,
        source_reference_calls=calls, source_reference_call_count=len(calls),
        source_bridge_calls=nested, source_bridge_call_count=len(nested),
        source_call_counts_are_not_component_observations=True,
        source_error_mappings_are_not_observed_core_errors=True,
    )
    return record


def observe() -> dict[str, Any]:
    """Observe current reviewed source only under exact CPython 3.13.5."""
    observed_runtime = runtime()
    if observed_runtime != EXPECTED_RUNTIME:
        raise ValueError("source observations require CPython 3.13.5 / Unicode15.1 / 4300")
    before, inputs = source_bindings(), case_inputs()
    with source_modules() as modules:
        records = [observe_case(case, modules) for case in inputs]
    if before != source_bindings() or inputs != case_inputs():
        raise ValueError("source changed while recording reference observations")
    return {
        "format": "tobkiri.gateway-finalizer-source-observations/1",
        "scope": "original-source-and-declared-finite-boundaries-not-host-core-parity",
        "protocol": "ordered-finite-json-v2", "runtime": observed_runtime,
        "source_bindings": before, "source_bindings_sha256": digest(canonical(before)),
        "case_inputs_sha256": digest(canonical(inputs)), "counts": COUNTS,
        "cases": records, **NO_EXECUTION,
        "source_error_scope": (
            "Original finalizer GlobalContractInvocationError retains domain code/message. "
            "Original bridge rejection declares terminal Core invalid; usage OverflowError "
            "or nonfinite source output declares terminal Core internal. These are declared "
            "finite transport mappings, not observed imported Core errors. Source calls "
            "and their stopping points are not guest call-count or partial-mutation proof."
        ),
    }


def main() -> None:
    """Write a fresh source-only observation candidate for independent review."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = observe()
    with args.output.open("xb") as stream:
        stream.write(json_bytes(result))


if __name__ == "__main__":
    main()
