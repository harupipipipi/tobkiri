"""Replay an actual two-Python-component DEVELOPMENT-only composition.

The fixed WAC graph wires the caller's bridge@2 import to the actual provider.
Residual canonical Core imports trap. This is not Core/Gate acceptance, native
Host replay, caller activation, full Gateway behavior, or authority evidence.
Invoke only inside a separate OS process supervisor: guest fuel and epoch
deadlines do not bound compilation. This script adds no new supervisor policy.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

import observe_gateway_tool_caller as reference
import replay_ordered_tool_bridge_v2 as provider_replay
import stage_gateway_tool_caller as caller_stage
import stage_ordered_tool_bridge_v2 as provider_stage

CALLER = caller_stage.INTERFACE
PROVIDER = caller_stage.BRIDGE_INTERFACE
LIFECYCLES = {"provider": "provider-lifecycle", "caller": "caller-lifecycle"}
ALLOWED_IMPORTS = provider_replay.ALLOWED_IMPORTS
EXPECTED_EXPORTS = {CALLER, PROVIDER, *LIFECYCLES.values()}
EXPECTED_RUNTIME = provider_replay.EXPECTED_GUEST_RUNTIME
EXPECTED_PROVIDER_SHA256 = (
    "0f28f4a87158b18a24fe045a9be1c24e8d9405549a0af1b56546b2017ac92a46"
)
REVIEWED_PINS = {
    "caller": {
        "artifact_sha256": "1a438458ba804cf9b62c7125602b1499f159263387f56f2470c51ce4191e521c",
        "receipt_sha256": "ace0bb3411cce88aab0d8b2907905e63964f2c94d9b0ae3b1a07c7b6b984e652",
    },
    "provider": {
        "artifact_sha256": EXPECTED_PROVIDER_SHA256,
        "receipt_sha256": "17da81e4225c83aab657697457dbfa86b49f81b7d0ef60c3bf6e5fcdb9757cff",
    },
    "source_observations_sha256": "a1707056a40f6eae725c5d82fc11d202f099fd4ab04c81d2c8bf905b13422d38",
    "wac_sha256": "7cd490edbd452221ef89e6750fc43eacc9b71b1242095eb577cb9a3bf0d3beda",
}
DEFAULT_TIMEOUT = 30.0
DEFAULT_FUEL = provider_replay.DEFAULT_FUEL
STORE_LIMITS = provider_replay.STORE_LIMITS.copy()
WAC_SOURCE = b'''package local:gateway-tool-caller-development-replay;
let provider = new local:provider { ... };
let caller = new local:caller {
  "local:ai-tool-bridge-ordered-python/bridge@2.0.0": provider["local:ai-tool-bridge-ordered-python/bridge@2.0.0"],
  ...
};
export caller["local:ai-gateway-tool-caller-python/caller@1.0.0"];
export provider["local:ai-tool-bridge-ordered-python/bridge@2.0.0"];
export provider["tobkiri:core/lifecycle@1.0.0"] as "provider-lifecycle";
export caller["tobkiri:core/lifecycle@1.0.0"] as "caller-lifecycle";
'''
CALL_ORDER = ["provider.init", "caller.init", "provider.runtime-info",
              "caller.runtime-info", "caller.operation", "caller.shutdown",
              "provider.shutdown"]
NO_AUTHORITY = {
    "core_runtime_acceptance": False, "native_core_acceptance": False,
    "full_host_replay_performed": False, "legacy_http_error_parity_claimed": False,
    "production_enabled": False, "caller_activated": False,
    "all_typed_error_arms_executed": False, "retry_parity_claimed": False,
    "guest_import_call_counts_observed": False,
    "partial_stream_mutations_observed": False,
    "gate_runtime_acceptance": False,
}


def digest(data: bytes) -> str:
    """Return the exact byte identity used by evidence validation."""
    return hashlib.sha256(data).hexdigest()


def _read(path: Path, limit: int = 16 * 1024 * 1024) -> bytes:
    return provider_stage.bounded_read(path, limit)


def _pin(data: bytes, expected: Any, label: str) -> None:
    if (type(expected) is not str or re.fullmatch(r"[0-9a-f]{64}", expected) is None
            or digest(data) != expected):
        raise ValueError(f"{label} differs from its exact reviewed SHA-256")


def _manifest(staged: Path, plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**item, "bytes": len(_read(staged / item["source"]))}
            for item in plan["sources"]]


def validate_caller(artifact: Path, pins: dict[str, str]) -> dict[str, Any]:
    """Bind caller bytes/receipt to a reconstructed exact source-stage plan."""
    binary = _read(artifact, 512 * 1024**2)
    receipt_bytes = _read(artifact.with_suffix(".build.json"))
    _pin(binary, pins.get("artifact_sha256"), "caller artifact")
    _pin(receipt_bytes, pins.get("receipt_sha256"), "caller receipt")
    receipt = provider_stage.strict_json(receipt_bytes)
    if type(receipt) is not dict:
        raise ValueError("caller receipt must be an object")
    before = caller_stage.reviewed_inputs()
    with tempfile.TemporaryDirectory(prefix="gateway-caller-review-") as temporary:
        staged = Path(temporary) / "stage"
        plan = caller_stage.stage(staged)
        manifest = _manifest(staged, plan)
        plan_sha = digest(_read(staged / "plan.json"))
        staged_evidence = _read(staged / "source-stage.json")
    expected = {
        "format": "tobkiri.component-build/2", "language": "python",
        "artifact_sha256": digest(binary), "artifact_bytes": len(binary),
        "build_plan_sha256": plan_sha, "source_manifest": manifest,
        "source_manifest_sha256": digest(json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()),
        "world": plan["world"], "imports": sorted(plan["allowed_imports"]),
        "exports": sorted(plan["outer_exports"]),
    }
    if (not binary or type(receipt.get("artifact_bytes")) is not int
            or any(receipt.get(key) != value for key, value in expected.items())):
        raise ValueError("caller receipt differs from complete reviewed stage plan")
    if before != caller_stage.reviewed_inputs():
        raise ValueError("caller source changed during evidence validation")
    return {"binary": binary, "receipt_bytes": receipt_bytes,
            "stage_sha256": digest(staged_evidence), "plan_sha256": plan_sha,
            "source_manifest_sha256": expected["source_manifest_sha256"]}


def validate_observations(path: Path, sha256: str) -> tuple[bytes, dict[str, Any]]:
    """Accept only pinned source observations with current exact source bindings."""
    raw = _read(path)
    _pin(raw, sha256, "source observations")
    frozen = provider_stage.strict_json(raw)
    if type(frozen) is not dict:
        raise ValueError("source observations must be an object")
    bindings = reference.source_bindings()
    inputs = reference.case_inputs()
    cases = frozen.get("cases")
    if not isinstance(cases, list) or len(cases) != len(inputs):
        raise ValueError("source observations do not contain complete reviewed cases")
    identities = [{key: case.get(key) for key in ("id", "operation", "input_json")}
                  for case in cases]
    provider_oracle, _ = provider_stage.reviewed_inputs()
    if (frozen.get("format") != "tobkiri.gateway-tool-caller-source-observations/1"
            or frozen.get("protocol") != "ordered-finite-json-v2"
            or frozen.get("runtime") != reference.EXPECTED_RUNTIME
            or frozen.get("source_bindings") != bindings
            or frozen.get("source_bindings_sha256") != digest(reference.canonical(bindings))
            or identities != inputs
            or frozen.get("case_inputs_sha256") != digest(reference.canonical(inputs))
            or frozen.get("provider_33_oracle_sha256") != digest(provider_oracle)
            or any(frozen.get(key) is not False for key in reference.NO_EXECUTION)):
        raise ValueError("source observations differ from reviewed source identities")
    for case in cases:
        kind = case.get("classification")
        if kind not in {"success", "provider-invalid", "provider-nonfinite"}:
            raise ValueError("unknown source observation classification")
        expected = case.get("expected")
        if kind == "success":
            if (type(case.get("output_json")) is not str
                    or digest(case["output_json"].encode()) != case.get("output_sha256")
                    or expected != {"status": "ok", "output_json": case["output_json"]}):
                raise ValueError("invalid observed successful source result")
        else:
            code = "invalid" if kind == "provider-invalid" else "internal"
            message = (provider_replay.INVALID_MESSAGE if kind == "provider-invalid"
                       else provider_replay.NONFINITE_MESSAGE)
            if expected != {"status": "error", "code": code, "message": message}:
                raise ValueError("typed error boundary differs from reviewed mapping")
    counts = {key: sum(case["classification"] == key for case in cases)
              for key in ("success", "provider-invalid", "provider-nonfinite")}
    if counts != {"success": 16, "provider-invalid": 7, "provider-nonfinite": 7}:
        raise ValueError("source observation classification accounting changed")
    if frozen.get("counts") != counts:
        raise ValueError("source observation counts are inconsistent")
    return raw, frozen


def validate_evidence(
    caller: Path, provider: Path, oracle: Path, pins: dict[str, Any],
) -> dict[str, Any]:
    """Validate both independent component identities before any execution."""
    if (type(pins) is not dict or set(pins) != {
        "caller", "provider", "source_observations_sha256", "wac_sha256",
    } or any(type(pins.get(name)) is not dict or set(pins[name]) != {
        "artifact_sha256", "receipt_sha256",
    } for name in ("caller", "provider"))):
        raise ValueError("reviewed pins must contain exact caller/provider/observer/WAC identities")
    if pins != REVIEWED_PINS:
        raise ValueError("pins differ from the independently reviewed development inputs")
    caller_evidence = validate_caller(caller, pins["caller"])
    provider_binary = _read(provider, 512 * 1024**2)
    provider_receipt = _read(provider.with_suffix(".build.json"))
    _pin(provider_binary, EXPECTED_PROVIDER_SHA256, "preserved provider artifact")
    _pin(provider_binary, pins["provider"]["artifact_sha256"], "provider artifact")
    _pin(provider_receipt, pins["provider"]["receipt_sha256"], "provider receipt")
    binary, receipt, provider_oracle, _ = provider_replay.validate_evidence(
        provider, provider_stage.PACK / "integer-v2.observations.json",
    )
    if binary != provider_binary or receipt != provider_receipt:
        raise ValueError("provider bytes changed during evidence validation")
    source_bytes, source = validate_observations(
        oracle, pins["source_observations_sha256"],
    )
    return {"caller": caller_evidence,
            "provider": {"binary": binary, "receipt_bytes": receipt},
            "source_bytes": source_bytes, "source": source,
            "provider_33_oracle_sha256": digest(provider_oracle)}


def comparison_flags(case: dict[str, Any], actual: dict[str, Any]) -> dict[str, bool]:
    """Compare exact strings or typed boundary custody, never claim Host parity."""
    matched = actual == case["expected"]
    return {"source_success_parity": matched and case["classification"] == "success",
            "typed_error_custody": matched and case["classification"] != "success",
            "expected_result_matched": matched}


def record_failure(record: dict[str, Any], exc: Exception, phase: str) -> None:
    """Retain primary, post-return and cleanup errors without manufacturing success."""
    failure = {"phase": phase, "type": type(exc).__name__, "message": str(exc)}
    failures = record.setdefault("runner_failures", [])
    if not failures and "actual" in record:
        record["result_before_runner_failure"] = record["actual"]
    failures.append(failure)
    record.update(source_success_parity=False, typed_error_custody=False,
                  expected_result_matched=False, case_completed=False)
    record["actual"] = {"status": "runner-failure", **failures[0]}


def _typed_result(value: Any, record_type: type) -> dict[str, str]:
    if type(value) is str:
        return {"status": "ok", "output_json": value}
    if (isinstance(value, record_type) and set(vars(value)) == {"code", "message"}
            and type(value.code) is str and type(value.message) is str):
        return {"status": "error", "code": value.code, "message": value.message}
    raise ValueError("caller did not return typed string/Core error")


def _case_record(case: dict[str, Any]) -> dict[str, Any]:
    return {"id": case["id"], "classification": case["classification"],
            "source_success_parity": False, "typed_error_custody": False,
            "expected_result_matched": False, "case_completed": False,
            "lifecycle": {name: {"init": False, "shutdown": False}
                          for name in LIFECYCLES},
            "guest_calls_attempted": [], "guest_calls_returned": [],
            "guest_runtime": {}, "lifecycle_results": {},
            "instantiation_attempted": False}


def _run_case_in_store(
    record: dict[str, Any], store: Any, instance: Any, case: dict[str, Any],
    record_type: type,
) -> None:
    """Run both lifecycles, preserving actual successful-return ownership."""
    phase = "exports"
    shutdown = {}
    acquired = set()
    cleanup_failures: list[Exception] = []

    def call(label: str, function: Any, *args: str) -> Any:
        def capture(value: Any) -> None:
            record["guest_calls_returned"].append(label)
            if label.endswith((".init", ".shutdown")):
                record["lifecycle_results"][label] = (
                    {"status": "ok"} if value is None
                    else _typed_result(value, record_type)
                )
                if label.endswith(".init") and value is None:
                    # Acquisition happened even if canonical post-return fails.
                    acquired.add(label.split(".")[0])
            if label == "caller.operation":
                record["actual"] = _typed_result(value, record_type)

        record["guest_calls_attempted"].append(label)
        return provider_replay._invoke(function, store, *args, on_return=capture,
                                       cleanup_failures=cleanup_failures)

    def retain(exc: Exception, label: str) -> None:
        record_failure(record, exc, label)
        for error in cleanup_failures:
            record_failure(record, error, f"{label}-post-return")
        cleanup_failures.clear()

    try:
        initialize = {}
        for name, interface in LIFECYCLES.items():
            initialize[name] = provider_replay._export(instance, store, interface, "init")
            shutdown[name] = provider_replay._export(instance, store, interface, "shutdown")
        runtimes = {name: provider_replay._export(instance, store, interface, "runtime-info")
                    for name, interface in (("provider", PROVIDER), ("caller", CALLER))}
        operation = provider_replay._export(instance, store, CALLER, case["operation"])
        for name in LIFECYCLES:
            phase = f"{name}.init"
            if call(phase, initialize[name], "{}") is not None:
                raise ValueError(f"{name} lifecycle init returned an error")
            record["lifecycle"][name]["init"] = True
        for name in LIFECYCLES:
            phase = f"{name}.runtime-info"
            value = call(phase, runtimes[name])
            if type(value) is not str:
                raise ValueError(f"{name} runtime-info did not return a typed string")
            record["guest_runtime"][name] = provider_replay.loads(value)
            if record["guest_runtime"][name] != EXPECTED_RUNTIME:
                raise ValueError(f"{name} runtime differs from reviewed producer")
        phase = "caller.operation"
        value = call(phase, operation, case["input_json"])
        if type(value) is str:
            provider_replay.loads(value)
        record.update(comparison_flags(case, record["actual"]))
    except Exception as exc:
        retain(exc, phase)
    finally:
        for name in reversed(LIFECYCLES):
            if name in acquired and name in shutdown:
                phase = f"{name}.shutdown"
                try:
                    if call(phase, shutdown[name]) is not None:
                        raise ValueError(f"{name} shutdown returned an unexpected value")
                    record["lifecycle"][name]["shutdown"] = True
                except Exception as exc:
                    retain(exc, phase)


def _replay_case(
    engine: Any, component: Any, linker: Any, case: dict[str, Any],
    fuel: int, timeout: float,
) -> dict[str, Any]:
    from wasmtime import Store
    from wasmtime.component import Record

    record = _case_record(case)
    phase = "store-setup"
    timer = None
    try:
        with Store(engine) as store:
            store.set_limits(**STORE_LIMITS)
            store.set_fuel(fuel)
            store.set_epoch_deadline(1)
            timer = provider_replay._EpochTimer(timeout, engine.increment_epoch)
            timer.start()
            try:
                phase = "instantiate"
                record["instantiation_attempted"] = True
                instance = linker.instantiate(store, component)
                _run_case_in_store(record, store, instance, case, Record)
            except Exception as exc:
                record_failure(record, exc, phase)
            finally:
                try:
                    record["fuel_consumed"] = fuel - store.get_fuel()
                except Exception as exc:
                    record_failure(record, exc, "fuel-accounting")
                try:
                    timer.finish()
                except Exception as exc:
                    record_failure(record, exc, "epoch-deadline")
                timer = None
            phase = "store-cleanup"
    except Exception as exc:
        record_failure(record, exc, phase)
    finally:
        if timer is not None:
            try:
                timer.finish()
            except Exception as exc:
                record_failure(record, exc, "epoch-deadline")
    record["case_completed"] = (
        not record.get("runner_failures") and record["guest_calls_returned"] == CALL_ORDER
        and all(value == {"init": True, "shutdown": True}
                for value in record["lifecycle"].values())
    )
    return record


def summarize(cases: list[dict[str, Any]], observations: list[dict[str, Any]]) -> dict[str, bool]:
    """Require full ordered identities, both runtimes/lifecycles, and exact results."""
    complete = (bool(cases) and len({case["id"] for case in cases}) == len(cases)
                and [(case["id"], case["classification"]) for case in cases]
                == [(item.get("id"), item.get("classification")) for item in observations])
    lifecycles = complete and all(item.get("lifecycle") == {
        name: {"init": True, "shutdown": True} for name in LIFECYCLES
    } for item in observations)
    runtimes = complete and all(item.get("guest_runtime") == {
        name: EXPECTED_RUNTIME for name in LIFECYCLES
    } for item in observations)
    calls = complete and all(item.get("guest_calls_returned") == CALL_ORDER
                            and item.get("guest_calls_attempted") == CALL_ORDER
                            for item in observations)
    completed = complete and all(item.get("case_completed") is True
                                 and not item.get("runner_failures")
                                 for item in observations)
    matched = complete and all(
        item.get("expected_result_matched") is True
        and comparison_flags(case, item.get("actual", {}))["expected_result_matched"]
        for case, item in zip(cases, observations)
    )
    succeeded = bool(complete and lifecycles and runtimes and calls and completed and matched)
    return {"wasm_execution_attempted": any(bool(item.get("guest_calls_attempted"))
                                            for item in observations),
            "wasm_execution_performed": any(bool(item.get("guest_calls_returned"))
                                            for item in observations),
            "complete_case_set": complete, "all_lifecycles_completed": lifecycles,
            "both_guest_runtimes_verified": runtimes, "all_guest_calls_returned": calls,
            "all_cases_completed": completed, "all_expected_results_matched": matched,
            "replay_succeeded": succeeded}


def _base_result() -> dict[str, Any]:
    return {"format": "tobkiri.gateway-tool-caller-development-replay/1",
            "scope": "development-two-component-not-native-core-acceptance",
            "protocol": "ordered-finite-json-v2", **NO_AUTHORITY,
            "observations": [], **summarize([], []),
            "compilation_bounded_by_guest_fuel_or_epoch": False,
            "compilation_wall_time_bounded": False,
            "execution_observation_scope": (
                "execution flags report explicit export calls attempted/returned, "
                "not instantiation starts or instruction-level execution"
            ),
            "store_limit_scope": (
                "memory_size is per linear memory, not an aggregate process bound; "
                "native compilation requires separate external supervision"
            ),
            "external_process_supervisor_required": True,
            "host_import_policy": "all five residual canonical Core imports trap",
            "bridge_implementation": "actual composed Python provider component",
            "source_reference_scope": "source helper semantics, not full Host error parity"}


def runner_bindings() -> dict[str, str]:
    """Capture the runner/helper source closure before and after execution."""
    names = (
        "replay_gateway_tool_caller.py", "replay_ordered_tool_bridge_v2.py",
        "observe_gateway_tool_caller.py", "stage_gateway_tool_caller.py",
        "prepare_gateway_tool_caller.py", "stage_ordered_tool_bridge_v2.py",
        "stage_ordered_tool_bridge.py", "prepare_ordered_tool_bridge.py",
        "freeze_ordered_tool_bridge.py",
    )
    return {name: digest(_read(Path(__file__).with_name(name))) for name in names}


def expected_surface() -> dict[str, Any]:
    """Describe the exact reachable instantiated ABI, not all WIT declarations.

    Both pinned producer components already omit unused risk, pending and
    verdict declarations before this composition. They are reachable only through
    approvals or gate-mediator, neither present in this minimal graph. Match the
    producer's exact five-member referenced types instance, not a generic subset
    of canonical WIT. Canonical WIT remains unchanged; omissions are not optional.
    """
    error_code = ("enum", ("denied", "approval-required", "cancelled", "not-found",
                           "invalid", "limit-exceeded", "conflict", "unavailable",
                           "internal"))
    error = ("record", (("code", error_code), ("message", "string")))
    handle = ("record", (("id", "u64"),))
    def result(ok: Any) -> tuple[Any, ...]:
        return ("result", ok, error)

    def function(params: list[tuple[str, Any]], output: Any = None) -> tuple[Any, ...]:
        return ("func", tuple(params), output)

    common = {"error": error, "json": "string"}
    lifecycle = {**common, "init": function([("config", "string")], result(None)),
                 "shutdown": function([])}
    bridge = {"error": error, "runtime-info": function([], "string"),
              "normalize": function([("operation", "string"), ("payload", "string")],
                                    result("string"))}
    caller = {"error": error, "runtime-info": function([], "string"),
              **{name: function([("payload", "string")], result("string"))
                 for name in ("generate-intents", "stream-intents", "stream-event-intents")}}
    imports = {
        "types": {**common, "error-code": error_code, "stream-handle": handle,
                  "callback-handle": handle},
        "flow": {**common, "stream-handle": handle, "callback-handle": handle,
                 "pull": function([("s", handle), ("max", "u32")], result(("list", "string"))),
                 "cancel-stream": function([("s", handle)]),
                 "invoke-callback": function([("c", handle), ("args", "string")], result("string")),
                 "is-cancelled": function([], "bool"),
                 "call-port": function([("shape", "string"), ("op", "string"),
                                        ("args", "string")], result("string")),
                 "call-port-stream": function([("shape", "string"), ("op", "string"),
                                               ("args", "string")], result(handle)),
                 "run-flow": function([("implements", "string"), ("input", "string")], result(handle)),
                 "emit": function([("output-shape", "string"), ("event", "string")], result(None))},
        "clock": {"error": error, "now-ms": function([], "u64"),
                  "monotonic-ns": function([], "u64"),
                  "sleep-ms": function([("ms", "u32")], result(None))},
        "random": {"bytes": function([("n", "u32")], ("list", "u8"))},
        "audit": {**common,
                  "append": function([("kind", "string"), ("payload", "string")], result(None)),
                  "read": function([("after", "u64"), ("max", "u32")], result(("list", "string")))},
    }
    return {
        "imports": {f"tobkiri:core/{name}@1.0.0": value for name, value in imports.items()},
        "exports": {CALLER: caller, PROVIDER: bridge,
                    **{name: lifecycle for name in LIFECYCLES.values()}},
    }


def _type_shape(value: Any, engine: Any) -> Any:
    from wasmtime import component as types

    if value is None:
        return None
    for cls, label in ((types.String, "string"), (types.U32, "u32"),
                       (types.U64, "u64"), (types.U8, "u8"), (types.Bool, "bool")):
        if type(value) is cls:
            return label
    if isinstance(value, types.ComponentInstanceType):
        return {name: _type_shape(item, engine)
                for name, item in value.exports(engine).items()}
    if isinstance(value, types.FuncType):
        return ("func", tuple((name, _type_shape(item, engine))
                              for name, item in value.params),
                _type_shape(value.result, engine))
    if isinstance(value, types.RecordType):
        return ("record", tuple((name, _type_shape(item, engine))
                                for name, item in value.fields))
    if isinstance(value, types.EnumType):
        return ("enum", tuple(value.names))
    if isinstance(value, types.VariantType):
        return ("variant", tuple((name, _type_shape(item, engine))
                                 for name, item in value.cases))
    if isinstance(value, types.ResultType):
        return ("result", _type_shape(value.ok, engine), _type_shape(value.err, engine))
    if isinstance(value, types.ListType):
        return ("list", _type_shape(value.element, engine))
    raise ValueError(f"unexpected component type/resource: {type(value).__name__}")


def validate_surface(surface: dict[str, Any]) -> None:
    """Reject every extra/missing member or changed nested WIT signature."""
    if surface != expected_surface():
        raise ValueError("composition typed surface differs from exact reviewed WIT")


def record_surface_diagnostics(result: dict[str, Any], surface: dict[str, Any]) -> None:
    """Retain exact bounded introspection before enforcing the unchanged matcher."""
    encoded = reference.canonical(surface)
    result.update(typed_surface_sha256=digest(encoded), typed_surface_bytes=len(encoded),
                  typed_surface_validated=False)
    if len(encoded) > 256 * 1024:
        raise ValueError("observed typed surface exceeds diagnostic byte bound")
    result["typed_surface"] = surface


def _compose_and_replay(
    caller: Path, provider: Path, oracle: Path, pins: dict[str, Any], wac: Path,
    *, fuel: int = DEFAULT_FUEL, timeout: float = DEFAULT_TIMEOUT,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute only inside the separately reviewed external OS supervisor."""
    if type(fuel) is not int or not 0 < fuel <= DEFAULT_FUEL:
        raise ValueError("invalid guest fuel bound")
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= DEFAULT_TIMEOUT):
        raise ValueError("invalid guest epoch timeout")
    if version("wasmtime") != "48.0.0":
        raise ValueError("composed replay requires Wasmtime 48.0.0")
    if result is None:
        result = _base_result()
    runner_before = runner_bindings()
    evidence = validate_evidence(caller, provider, oracle, pins)
    wac_bytes = _read(wac, 256 * 1024**2)
    _pin(wac_bytes, pins["wac_sha256"], "WAC executable")
    from wasmtime import Config, Engine
    from wasmtime.component import Component, Linker

    with tempfile.TemporaryDirectory(prefix="gateway-two-component-") as temporary:
        workspace = Path(temporary)
        for name in ("caller", "provider"):
            (workspace / f"{name}.wasm").write_bytes(evidence[name]["binary"])
        (workspace / "replay.wac").write_bytes(WAC_SOURCE)
        # Execute captured, pinned tool bytes, not a path that can change after
        # its digest check. This remains DEVELOPMENT evidence, not authentication.
        executable = workspace / "wac"
        executable.write_bytes(wac_bytes)
        executable.chmod(0o700)
        command = [str(executable), "compose", str(workspace / "replay.wac"),
                   "--dep", f"local:caller={workspace / 'caller.wasm'}",
                   "--dep", f"local:provider={workspace / 'provider.wasm'}",
                   "--deps-dir", str(workspace / "no-ambient-dependencies"),
                   "-o", str(workspace / "composed.wasm")]
        composed = subprocess.run(command, check=True, capture_output=True, text=True,
                                  cwd=workspace)
        binary = _read(workspace / "composed.wasm", 512 * 1024**2)
    result.update(
        composition_sha256=digest(binary), composition_bytes=len(binary),
        composition_wac_sha256=digest(WAC_SOURCE), component_pins=pins,
        runner_source_bindings=runner_before,
        replay_sha256=runner_before["replay_gateway_tool_caller.py"],
    )
    config = Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    # DEVELOPMENT fixture only. The prior default-reservation ENOMEM replay is
    # retained separately, not overwritten. The default 4 GiB virtual reservation
    # per memory cannot fit both guests under the unchanged 8 GiB process cap.
    # A smaller reservation uses explicit bounds checks; no guards, growth/trap
    # settings, compiler safety flags, Store caps or OS limits are changed.
    # https://docs.rs/wasmtime/48.0.0/wasmtime/struct.Config.html#method.memory_reservation
    config.memory_reservation = STORE_LIMITS["memory_size"]
    result["engine_memory_reservation_bytes"] = STORE_LIMITS["memory_size"]
    result["engine_memory_reservation_scope"] = (
        "development fixture virtual reservation only; unchanged Store/OS caps "
        "and default guards, memory-growth, trap and compiler safety settings"
    )
    engine = Engine(config)
    component = Component(engine, binary)  # Only external supervisor bounds compile.
    imports = set(component.type.imports(engine))
    exports = set(component.type.exports(engine))
    result.update(imports=sorted(imports), exports=sorted(exports))
    if imports != ALLOWED_IMPORTS or exports != EXPECTED_EXPORTS:
        raise ValueError("composition imports/exports differ from exact reviewed graph")
    surface = {"imports": {name: _type_shape(item, engine)
                           for name, item in component.type.imports(engine).items()},
               "exports": {name: _type_shape(item, engine)
                           for name, item in component.type.exports(engine).items()}}
    record_surface_diagnostics(result, surface)
    validate_surface(surface)
    result["typed_surface_validated"] = True
    linker = Linker(engine)
    linker.define_unknown_imports_as_traps(component)
    observations = result["observations"]
    for case in evidence["source"]["cases"]:
        observations.append(_replay_case(engine, component, linker, case, fuel, timeout))
        result.update(summarize(evidence["source"]["cases"], observations))
        # Post-run evidence and source closure checks have not succeeded yet.
        result["replay_succeeded"] = False
    # Revalidate frozen closures after execution to reject source/evidence drift.
    after = validate_evidence(caller, provider, oracle, pins)
    if any(evidence[key] != after[key] for key in evidence):
        raise ValueError("reviewed evidence changed during composed replay")
    if runner_bindings() != runner_before:
        raise ValueError("runner source closure changed during replay")
    result.update(
        observations=observations, **summarize(evidence["source"]["cases"], observations),
        component_pins=pins, composition_sha256=digest(binary),
        composition_bytes=len(binary), composition_wac_sha256=digest(WAC_SOURCE),
        imports=sorted(imports), exports=sorted(exports),
        caller_source_stage_sha256=evidence["caller"]["stage_sha256"],
        caller_plan_sha256=evidence["caller"]["plan_sha256"],
        caller_source_manifest_sha256=evidence["caller"]["source_manifest_sha256"],
        provider_33_oracle_sha256=evidence["provider_33_oracle_sha256"],
        original_reference_runtime=evidence["source"]["runtime"],
        expected_guest_runtime=EXPECTED_RUNTIME, guest_timeout_seconds=timeout,
        guest_timeout_scope="entire case including instantiation and four lifecycle calls",
        fuel_per_case=fuel, store_limits=STORE_LIMITS,
        replay_sha256=runner_before["replay_gateway_tool_caller.py"],
        runner_source_bindings=runner_before, typed_surface=surface,
        composition_command=["wac", "compose", "replay.wac", "--dep",
                             "local:caller=caller.wasm", "--dep",
                             "local:provider=provider.wasm", "--deps-dir",
                             "no-ambient-dependencies", "-o", "composed.wasm"],
        composition_tool_stderr=composed.stderr,
    )
    return result


def main() -> int:
    """Write new development evidence inside a separate process supervisor."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("caller", "provider", "oracle", "pins", "wac", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("replay output must be a new file")
    result = _base_result()
    try:
        pins = provider_stage.strict_json(_read(args.pins))
        result = _compose_and_replay(args.caller, args.provider, args.oracle,
                                    pins, args.wac, result=result)
    except Exception as exc:
        result["replay_succeeded"] = False
        result["runner_failure"] = {"type": type(exc).__name__, "message": str(exc)}
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False) + "\n")
    return 0 if result["replay_succeeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
