"""Actual five-component Gateway finalization DEVELOPMENT replay only.

Invoke only inside a separately approved OS process supervisor. Guest fuel and
whole-case epoch deadlines include instantiation, but cannot bound compilation.
Explicit independently reviewed pins bind new receipts, sources and WAC; the
historical two-component trial and its frozen artifact pins remain untouched.
"""

from __future__ import annotations

import argparse
from importlib.metadata import version
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

import observe_gateway_finalizer as reference
import replay_gateway_tool_caller as prior
import replay_ordered_tool_bridge_v2 as runtime_helpers
import stage_gateway_finalizer as finalizer_stage
import stage_gateway_finalization_providers as dependencies_stage
import stage_gateway_tool_caller as caller_stage
import stage_ordered_tool_bridge_v2 as bridge_stage

INTERFACES = {
    "bridge": bridge_stage.INTERFACE,
    "caller": caller_stage.INTERFACE,
    "usage-cost": dependencies_stage.INTERFACES["usage-cost"],
    "stream-normalizer": dependencies_stage.INTERFACES["stream-normalizer"],
    "finalizer": finalizer_stage.INTERFACE,
}
LIFECYCLES = {name: f"{name}-lifecycle" for name in INTERFACES}
ALLOWED_IMPORTS = runtime_helpers.ALLOWED_IMPORTS
EXPECTED_EXPORTS = {*INTERFACES.values(), *LIFECYCLES.values()}
EXPECTED_RUNTIME = runtime_helpers.EXPECTED_GUEST_RUNTIME
DEFAULT_FUEL = runtime_helpers.DEFAULT_FUEL
DEFAULT_TIMEOUT = 30.0
STORE_LIMITS = {**runtime_helpers.STORE_LIMITS, "instances": 320, "memories": 8}
CALL_ORDER = [
    *(f"{name}.init" for name in INTERFACES),
    *(f"{name}.runtime-info" for name in INTERFACES),
    "finalizer.operation", *(f"{name}.shutdown" for name in reversed(INTERFACES)),
]
NO_AUTHORITY = {
    **reference.NO_EXECUTION,
    "caller_activated": False, "partial_stream_mutations_observed": False,
    "full_gateway_migration_claimed": False,
}
# Execution flags are populated solely from actual returned export calls below.
for _key in ("build_performed", "wasm_execution_performed"):
    NO_AUTHORITY.pop(_key)
WAC_SOURCE = b'''package local:gateway-finalizer-development-replay;
let bridge = new local:bridge { ... };
let caller = new local:caller {
  "local:ai-tool-bridge-ordered-python/bridge@2.0.0": bridge["local:ai-tool-bridge-ordered-python/bridge@2.0.0"],
  ...
};
let usage = new local:usage-cost { ... };
let normalizer = new local:stream-normalizer { ... };
let finalizer = new local:finalizer {
  "local:ai-gateway-tool-caller-python/caller@1.0.0": caller["local:ai-gateway-tool-caller-python/caller@1.0.0"],
  "local:ai-usage-cost-python/usage-cost@1.0.0": usage["local:ai-usage-cost-python/usage-cost@1.0.0"],
  "local:ai-stream-normalizer-python/stream-normalizer@1.0.0": normalizer["local:ai-stream-normalizer-python/stream-normalizer@1.0.0"],
  ...
};
export bridge["local:ai-tool-bridge-ordered-python/bridge@2.0.0"];
export caller["local:ai-gateway-tool-caller-python/caller@1.0.0"];
export usage["local:ai-usage-cost-python/usage-cost@1.0.0"];
export normalizer["local:ai-stream-normalizer-python/stream-normalizer@1.0.0"];
export finalizer["local:ai-gateway-finalizer-python/finalizer@1.0.0"];
export bridge["tobkiri:core/lifecycle@1.0.0"] as "bridge-lifecycle";
export caller["tobkiri:core/lifecycle@1.0.0"] as "caller-lifecycle";
export usage["tobkiri:core/lifecycle@1.0.0"] as "usage-cost-lifecycle";
export normalizer["tobkiri:core/lifecycle@1.0.0"] as "stream-normalizer-lifecycle";
export finalizer["tobkiri:core/lifecycle@1.0.0"] as "finalizer-lifecycle";
'''
digest = reference.digest


def _read(path: Path, limit: int = 16 * 1024 * 1024) -> bytes:
    return bridge_stage.bounded_read(path, limit)


def _pin(data: bytes, expected: Any, label: str) -> None:
    if (type(expected) is not str or re.fullmatch(r"[0-9a-f]{64}", expected) is None
            or digest(data) != expected):
        raise ValueError(f"{label} differs from its exact reviewed SHA-256")


def _manifest(staged: Path, plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**item, "bytes": len(_read(staged / item["source"]))}
            for item in plan["sources"]]


def _reviewed(name: str) -> bytes:
    if name == "bridge":
        return reference.canonical([digest(data) for data in bridge_stage.reviewed_inputs()])
    if name == "caller":
        return caller_stage.reviewed_inputs()
    if name == "finalizer":
        return finalizer_stage.reviewed_inputs()
    return dependencies_stage.reviewed_inputs(name)


def _stage(name: str, path: Path) -> dict[str, Any]:
    if name == "bridge":
        return bridge_stage.stage(path)
    if name == "caller":
        return caller_stage.stage(path)
    if name == "finalizer":
        return finalizer_stage.stage(path)
    return dependencies_stage.stage(name, path)


def validate_component(name: str, artifact: Path, pins: dict[str, str]) -> dict[str, Any]:
    """Reconstruct the exact source plan behind each pinned actual build receipt."""
    if name not in INTERFACES:
        raise ValueError("unknown composition component")
    binary = _read(artifact, 512 * 1024**2)
    receipt_bytes = _read(artifact.with_suffix(".build.json"))
    _pin(binary, pins.get("artifact_sha256"), f"{name} artifact")
    _pin(receipt_bytes, pins.get("receipt_sha256"), f"{name} receipt")
    if not binary.startswith(b"\x00asm\x0d\x00\x01\x00"):
        raise ValueError("artifact is not a WebAssembly component")
    receipt = bridge_stage.strict_json(receipt_bytes)
    if type(receipt) is not dict:
        raise ValueError("build receipt must be an object")
    before = _reviewed(name)
    with tempfile.TemporaryDirectory(prefix=f"finalizer-{name}-review-") as temporary:
        staged = Path(temporary) / "stage"
        plan = _stage(name, staged)
        manifest = _manifest(staged, plan)
        plan_sha = digest(_read(staged / "plan.json"))
    # Source-case inputs/answers cannot become a guest lookup table.
    if any("source-cases" in item["source"] or "observe_gateway_finalizer" in item["source"]
           for item in manifest):
        raise ValueError("source observations must never enter a guest plan")
    expected = {
        "format": "tobkiri.component-build/2", "language": "python",
        "artifact_sha256": digest(binary), "artifact_bytes": len(binary),
        "build_plan_sha256": plan_sha, "source_manifest": manifest,
        "source_manifest_sha256": digest(json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()),
        "world": plan["world"], "imports": sorted(plan["allowed_imports"]),
        "exports": sorted(plan["outer_exports"]), "production_enabled": False,
        "guest_runtime_identity_verified": False,
        "build_mode": "hash-bound-custom-python313-producer-trial",
    }
    if (type(receipt.get("artifact_bytes")) is not int
            or any(receipt.get(key) != value for key, value in expected.items())):
        raise ValueError(f"{name} receipt differs from complete reviewed stage plan")
    if (receipt.get("production_enabled") is not False
            or receipt.get("guest_runtime_identity_verified") is not False):
        raise ValueError("build receipt must not imply runtime or production acceptance")
    for field in ("compiler_executable_sha256", "producer_trial_receipt_sha256"):
        if (type(receipt.get(field)) is not str
                or re.fullmatch(r"[0-9a-f]{64}", receipt[field]) is None):
            raise ValueError("build receipt lacks exact producer provenance")
    if before != _reviewed(name):
        raise ValueError(f"{name} source changed during receipt validation")
    return {
        "binary": binary, "receipt_bytes": receipt_bytes,
        "stage_sha256": digest(before), "plan_sha256": plan_sha,
        "source_manifest_sha256": expected["source_manifest_sha256"],
        "producer_trial_receipt_sha256": receipt["producer_trial_receipt_sha256"],
    }


def validate_observations(path: Path, sha256: str) -> tuple[bytes, dict[str, Any]]:
    """Reject changed source, incomplete cases or conflated domain/Core outcomes."""
    raw = _read(path)
    _pin(raw, sha256, "source observations")
    frozen = bridge_stage.strict_json(raw)
    if type(frozen) is not dict:
        raise ValueError("source observations must be an object")
    bindings, inputs = reference.source_bindings(), reference.case_inputs()
    cases = frozen.get("cases")
    if type(cases) is not list or len(cases) != len(inputs):
        raise ValueError("source observations do not contain the ten reviewed cases")
    identities = [{key: case.get(key) for key in expected}
                  for case, expected in zip(cases, inputs, strict=True)]
    if (frozen.get("format") != "tobkiri.gateway-finalizer-source-observations/1"
            or frozen.get("protocol") != "ordered-finite-json-v2"
            or frozen.get("runtime") != reference.EXPECTED_RUNTIME
            or frozen.get("source_bindings") != bindings
            or frozen.get("source_bindings_sha256") != digest(reference.canonical(bindings))
            or identities != inputs
            or frozen.get("case_inputs_sha256") != digest(reference.canonical(inputs))
            or any(frozen.get(key) is not False for key in reference.NO_EXECUTION)):
        raise ValueError("source observations differ from reviewed source identities")
    for case in cases:
        kind, expected = case.get("classification"), case.get("expected")
        if kind != case["expected_kind"]:
            raise ValueError("source classification differs from reviewed boundary")
        if kind == "finalized":
            if (type(case.get("output_json")) is not str
                    or digest(case["output_json"].encode()) != case.get("output_sha256")
                    or expected != {"status": "finalized", "output_json": case["output_json"]}):
                raise ValueError("invalid observed successful source result")
            runtime_helpers.loads(case["output_json"])
        elif expected != {"status": kind, **case["expected_error"]}:
            raise ValueError("domain/Core error code/message mapping changed")
        if (case.get("source_reference_call_count") != case["expected_dependency_call_count"]
                or type(case.get("source_reference_calls")) is not list
                or len(case["source_reference_calls"]) != case["source_reference_call_count"]
                or case.get("source_call_counts_are_not_component_observations") is not True
                or case.get("source_error_mappings_are_not_observed_core_errors") is not True):
            raise ValueError("source call evidence accounting changed")
    counts = {key: sum(case["classification"] == key for case in cases)
              for key in reference.COUNTS}
    if frozen.get("counts") != counts or counts != reference.COUNTS:
        raise ValueError("source observation category accounting changed")
    return raw, frozen


def runner_bindings() -> dict[str, str]:
    """Bind the complete local runner/helper closure, not an execution claim."""
    names = (
        "replay_gateway_finalizer.py", "observe_gateway_finalizer.py",
        "replay_gateway_tool_caller.py", "observe_gateway_tool_caller.py",
        "replay_ordered_tool_bridge_v2.py", "stage_gateway_finalizer.py",
        "prepare_gateway_finalizer.py", "stage_gateway_finalization_providers.py",
        "prepare_gateway_finalization_providers.py", "stage_gateway_tool_caller.py",
        "prepare_gateway_tool_caller.py", "stage_ordered_tool_bridge_v2.py",
        "stage_ordered_tool_bridge.py", "prepare_ordered_tool_bridge.py",
        "freeze_ordered_tool_bridge.py",
    )
    return {name: digest(_read(Path(__file__).with_name(name))) for name in names}


def validate_pins(pins: dict[str, Any]) -> None:
    """Require explicit new identities; never silently recycle historical pins."""
    fields = {*INTERFACES, "source_observations_sha256", "wac_sha256",
              "runner_source_bindings_sha256", "composition_wac_sha256"}
    if (type(pins) is not dict or set(pins) != fields
            or any(type(pins[name]) is not dict or set(pins[name]) !=
                   {"artifact_sha256", "receipt_sha256"} for name in INTERFACES)):
        raise ValueError("pins must bind all five components, observer, runner and WAC")
    values = [pins[key] for key in fields - set(INTERFACES)]
    values.extend(value for name in INTERFACES for value in pins[name].values())
    if any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
           for value in values):
        raise ValueError("all reviewed identities must be exact SHA-256 values")
    _pin(WAC_SOURCE, pins["composition_wac_sha256"], "fixed five-component WAC graph")
    _pin(reference.canonical(runner_bindings()), pins["runner_source_bindings_sha256"],
         "runner source closure")


def validate_evidence(artifacts: dict[str, Path], oracle: Path,
                      pins: dict[str, Any]) -> dict[str, Any]:
    """Validate all five actual artifacts before composition or execution."""
    validate_pins(pins)
    if set(artifacts) != set(INTERFACES):
        raise ValueError("exactly five component artifact paths are required")
    components = {name: validate_component(name, artifacts[name], pins[name])
                  for name in INTERFACES}
    source_bytes, source = validate_observations(oracle, pins["source_observations_sha256"])
    return {"components": components, "source_bytes": source_bytes, "source": source}


def expected_surface() -> dict[str, Any]:
    """Describe exact reachable WIT, including domain-vs-Core result nesting."""
    old = prior.expected_surface()
    error = old["exports"][prior.PROVIDER]["error"]
    lifecycle = old["exports"][prior.LIFECYCLES["provider"]]
    provider = old["exports"][prior.PROVIDER]
    usage = {key: value for key, value in provider.items() if key != "normalize"}
    usage["calculate"] = provider["normalize"]
    domain = ("record", (("code", "string"), ("message", "string")))
    outcome = ("variant", (("finalized", "string"), ("domain-rejected", domain)))
    operation = ("func", (("payload", "string"),), ("result", outcome, error))
    finalizer = {
        "error": error, "domain-error": domain, "finalization-outcome": outcome,
        "runtime-info": ("func", (), "string"),
        "generate": operation, "stream": operation,
    }
    return {"imports": old["imports"], "exports": {
        INTERFACES["bridge"]: provider,
        INTERFACES["caller"]: old["exports"][prior.CALLER],
        INTERFACES["usage-cost"]: usage,
        INTERFACES["stream-normalizer"]: provider,
        INTERFACES["finalizer"]: finalizer,
        **{name: lifecycle for name in LIFECYCLES.values()},
    }}


def validate_surface(surface: dict[str, Any]) -> None:
    """Refuse any missing, extra, reordered or changed nested typed surface."""
    if surface != expected_surface():
        raise ValueError("composition typed surface differs from exact reviewed WIT")


def _error_record(value: Any, record_type: type) -> dict[str, str]:
    if (not isinstance(value, record_type) or set(vars(value)) != {"code", "message"}
            or type(value.code) is not str or type(value.message) is not str):
        raise ValueError("result did not contain an exact code/message record")
    return {"code": value.code, "message": value.message}


def _typed_result(value: Any, record_type: type, variant_type: type) -> dict[str, str]:
    # Wasmtime48 tags the outer result because both success and error can be
    # Record. Its inner variant is untagged (string vs Record are disjoint).
    if (not isinstance(value, variant_type) or set(vars(value)) != {"tag", "payload"}
            or value.tag not in ("ok", "err")):
        raise ValueError("finalizer must return the tagged outer result")
    if value.tag == "err":
        error = _error_record(value.payload, record_type)
        codes = expected_surface()["imports"]["tobkiri:core/types@1.0.0"]["error-code"][1]
        if error["code"] not in codes:
            raise ValueError("unknown canonical Core error code")
        return {"status": "core-error", **error}
    if type(value.payload) is str:
        runtime_helpers.loads(value.payload)
        return {"status": "finalized", "output_json": value.payload}
    return {"status": "domain-rejected", **_error_record(value.payload, record_type)}


def comparison_flags(case: dict[str, Any], actual: dict[str, Any]) -> dict[str, bool]:
    """Compare exact ordered DATA or distinct domain/Core code/message custody."""
    matched = actual == case["expected"]
    return {
        "source_success_parity": matched and case["classification"] == "finalized",
        "domain_rejection_parity": matched and case["classification"] == "domain-rejected",
        "typed_error_custody": matched and case["classification"] == "core-error",
        "expected_result_matched": matched,
    }


def record_failure(record: dict[str, Any], exc: Exception, phase: str) -> None:
    """Retain primary and cleanup failures without manufacturing completion."""
    failure = {"phase": phase, "type": type(exc).__name__, "message": str(exc)}
    failures = record.setdefault("runner_failures", [])
    if not failures and "actual" in record:
        record["result_before_runner_failure"] = record["actual"]
    failures.append(failure)
    record.update(source_success_parity=False, domain_rejection_parity=False,
                  typed_error_custody=False, expected_result_matched=False,
                  case_completed=False)
    record["actual"] = {"status": "runner-failure", **failures[0]}


def _case_record(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": case["id"], "classification": case["classification"],
        "source_success_parity": False, "domain_rejection_parity": False,
        "typed_error_custody": False, "expected_result_matched": False,
        "case_completed": False, "instantiation_attempted": False,
        "instantiation_returned": False,
        "lifecycle": {name: {"init": False, "shutdown": False} for name in INTERFACES},
        "guest_calls_attempted": [], "guest_calls_returned": [],
        "post_return_attempted": [], "post_return_completed": [],
        "guest_runtime": {}, "lifecycle_results": {},
    }


def _run_case_in_store(record: dict[str, Any], store: Any, instance: Any,
                       case: dict[str, Any], record_type: type,
                       variant_type: type) -> None:
    """Own all five lifecycles and every actual successful-return post-return."""
    phase, shutdown, acquired = "exports", {}, set()
    cleanup_failures: list[Exception] = []

    def call(label: str, function: Any, *args: str) -> Any:
        def capture(value: Any) -> None:
            record["guest_calls_returned"].append(label)
            if label.endswith((".init", ".shutdown")):
                record["lifecycle_results"][label] = (
                    {"status": "ok"} if value is None
                    else {"status": "core-error", **_error_record(value, record_type)}
                )
                if label.endswith(".init") and value is None:
                    acquired.add(label.split(".")[0])
            if label == "finalizer.operation":
                record["actual"] = _typed_result(value, record_type, variant_type)

        def cleanup() -> None:
            record["post_return_attempted"].append(label)
            function.post_return(store)
            record["post_return_completed"].append(label)

        record["guest_calls_attempted"].append(label)
        return runtime_helpers._call_and_cleanup(
            lambda: function(store, *args), cleanup, capture, cleanup_failures,
        )

    def retain(exc: Exception, label: str) -> None:
        record_failure(record, exc, label)
        for error in cleanup_failures:
            record_failure(record, error, f"{label}-post-return")
        cleanup_failures.clear()

    try:
        initialize = {}
        for name, interface in LIFECYCLES.items():
            initialize[name] = runtime_helpers._export(instance, store, interface, "init")
            shutdown[name] = runtime_helpers._export(instance, store, interface, "shutdown")
        runtimes = {name: runtime_helpers._export(instance, store, interface, "runtime-info")
                    for name, interface in INTERFACES.items()}
        operation = runtime_helpers._export(instance, store, INTERFACES["finalizer"],
                                            case["operation"])
        for name in INTERFACES:
            phase = f"{name}.init"
            if call(phase, initialize[name], "{}") is not None:
                raise ValueError(f"{name} lifecycle init returned an error")
            record["lifecycle"][name]["init"] = True
        for name in INTERFACES:
            phase = f"{name}.runtime-info"
            value = call(phase, runtimes[name])
            if type(value) is not str:
                raise ValueError(f"{name} runtime-info must be a typed string")
            record["guest_runtime"][name] = runtime_helpers.loads(value)
            if record["guest_runtime"][name] != EXPECTED_RUNTIME:
                raise ValueError(f"{name} runtime differs from reviewed producer")
        phase = "finalizer.operation"
        call(phase, operation, case["input_json"])
        record.update(comparison_flags(case, record["actual"]))
    except Exception as exc:
        retain(exc, phase)
    finally:
        for name in reversed(INTERFACES):
            if name in acquired and name in shutdown:
                phase = f"{name}.shutdown"
                try:
                    if call(phase, shutdown[name]) is not None:
                        raise ValueError(f"{name} shutdown returned an unexpected value")
                    record["lifecycle"][name]["shutdown"] = True
                except Exception as exc:
                    retain(exc, phase)


def _replay_case(engine: Any, component: Any, linker: Any, case: dict[str, Any],
                 fuel: int, timeout: float) -> dict[str, Any]:
    from wasmtime import Store
    from wasmtime.component import Record, Variant

    record, phase, timer = _case_record(case), "store-setup", None
    try:
        with Store(engine) as store:
            store.set_limits(**STORE_LIMITS)
            store.set_fuel(fuel)
            store.set_epoch_deadline(1)
            timer = runtime_helpers._EpochTimer(timeout, engine.increment_epoch)
            timer.start()
            try:
                phase = "instantiate"
                record["instantiation_attempted"] = True
                instance = linker.instantiate(store, component)
                record["instantiation_returned"] = True
                _run_case_in_store(record, store, instance, case, Record, Variant)
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
        not record.get("runner_failures") and record["instantiation_returned"]
        and all(record[key] == CALL_ORDER for key in (
            "guest_calls_attempted", "guest_calls_returned",
            "post_return_attempted", "post_return_completed"))
        and all(value == {"init": True, "shutdown": True}
                for value in record["lifecycle"].values())
    )
    return record


def summarize(cases: list[dict[str, Any]], observations: list[dict[str, Any]]) -> dict[str, bool]:
    """Require complete identities, all five runtimes/lifecycles and cleanup."""
    complete = (bool(cases) and len({case["id"] for case in cases}) == len(cases)
                and [(case["id"], case["classification"]) for case in cases]
                == [(item.get("id"), item.get("classification")) for item in observations])
    lifecycles = complete and all(item.get("lifecycle") == {
        name: {"init": True, "shutdown": True} for name in INTERFACES
    } for item in observations)
    runtimes = complete and all(item.get("guest_runtime") == {
        name: EXPECTED_RUNTIME for name in INTERFACES
    } for item in observations)
    calls = complete and all(all(item.get(key) == CALL_ORDER for key in (
        "guest_calls_attempted", "guest_calls_returned")) for item in observations)
    cleanup = complete and all(all(item.get(key) == CALL_ORDER for key in (
        "post_return_attempted", "post_return_completed")) for item in observations)
    completed = complete and all(
        item.get("case_completed") is True and item.get("instantiation_returned") is True
        and not item.get("runner_failures") for item in observations)
    matched = complete and all(
        item.get("expected_result_matched") is True
        and comparison_flags(case, item.get("actual", {}))["expected_result_matched"]
        for case, item in zip(cases, observations))
    return {
        "wasm_execution_attempted": any(bool(item.get("guest_calls_attempted"))
                                        for item in observations),
        "wasm_execution_performed": any(bool(item.get("guest_calls_returned"))
                                        for item in observations),
        "complete_case_set": complete, "all_lifecycles_completed": lifecycles,
        "all_five_guest_runtimes_verified": runtimes,
        "all_guest_calls_returned": calls, "all_post_returns_completed": cleanup,
        "all_cases_completed": completed, "all_expected_results_matched": matched,
        "replay_succeeded": bool(complete and lifecycles and runtimes and calls
                                 and cleanup and completed and matched),
    }


def _base_result() -> dict[str, Any]:
    return {
        "format": "tobkiri.gateway-finalizer-development-replay/1",
        "scope": "development-five-component-not-native-core-acceptance",
        "protocol": "ordered-finite-json-v2", **NO_AUTHORITY,
        "observations": [], **summarize([], []), "build_performed": False,
        "compilation_bounded_by_guest_fuel_or_epoch": False,
        "compilation_wall_time_bounded": False,
        "external_process_supervisor_required": True,
        "host_import_policy": "all five residual canonical Core imports trap",
        "dependencies": "actual composed Python bridge/caller/usage/stream components",
        "source_reference_scope": "original source and declared finite boundaries, not Host parity",
        "store_limit_difference_from_two_component_fixture": {
            "instances": {"two_producer_fixture": 128, "five_producer_fixture": 320},
            "memories": {"two_producer_fixture": 4, "five_producer_fixture": 8},
        },
        "store_limit_scope": (
            "320 instances is 5x the single-provider allowance of 64; 8 memories is "
            "a bounded five-producer fixture allowance, not a measured minimum. "
            "128 MiB memory_size is per linear memory, not an aggregate process cap. "
            "Tables, table elements, fuel, epoch, guards and external OS limits are unchanged."
        ),
        "execution_observation_scope": (
            "Execution flags report explicit export calls attempted/returned; "
            "instantiation flags are separate, not instruction-level observations."
        ),
    }


def _compose_and_replay(artifacts: dict[str, Path], oracle: Path, pins: dict[str, Any],
                        wac: Path, *, fuel: int = DEFAULT_FUEL,
                        timeout: float = DEFAULT_TIMEOUT,
                        result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compose/run only inside the independently approved external supervisor."""
    if type(fuel) is not int or not 0 < fuel <= DEFAULT_FUEL:
        raise ValueError("invalid guest fuel bound")
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= DEFAULT_TIMEOUT):
        raise ValueError("invalid guest epoch timeout")
    if version("wasmtime") != "48.0.0":
        raise ValueError("composed replay requires Wasmtime 48.0.0")
    if result is None:
        result = _base_result()
    before = runner_bindings()
    evidence = validate_evidence(artifacts, oracle, pins)
    wac_bytes = _read(wac, 256 * 1024**2)
    _pin(wac_bytes, pins["wac_sha256"], "WAC executable")
    from wasmtime import Config, Engine
    from wasmtime.component import Component, Linker

    with tempfile.TemporaryDirectory(prefix="gateway-five-component-") as temporary:
        workspace = Path(temporary)
        for name in INTERFACES:
            (workspace / f"{name}.wasm").write_bytes(evidence["components"][name]["binary"])
        (workspace / "replay.wac").write_bytes(WAC_SOURCE)
        executable = workspace / "wac"
        executable.write_bytes(wac_bytes)
        executable.chmod(0o700)
        command = [str(executable), "compose", str(workspace / "replay.wac")]
        for name in INTERFACES:
            command.extend(["--dep", f"local:{name}={workspace / (name + '.wasm')}"])
        command.extend(["--deps-dir", str(workspace / "no-ambient-dependencies"),
                        "-o", str(workspace / "composed.wasm")])
        composed = subprocess.run(command, check=True, capture_output=True, text=True,
                                  cwd=workspace)
        binary = _read(workspace / "composed.wasm", 512 * 1024**2)
    result.update(
        composition_sha256=digest(binary), composition_bytes=len(binary),
        composition_wac_sha256=digest(WAC_SOURCE), component_pins=pins,
        runner_source_bindings=before, replay_sha256=before["replay_gateway_finalizer.py"],
    )
    config = Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    config.memory_reservation = STORE_LIMITS["memory_size"]
    result["engine_memory_reservation_bytes"] = STORE_LIMITS["memory_size"]
    result["engine_memory_reservation_scope"] = (
        "128 MiB fixture virtual reservation, retaining default guards, bounds checks, "
        "growth, trap and compiler safety settings and unchanged external OS limits"
    )
    engine = Engine(config)
    component = Component(engine, binary)
    imports, exports = component.type.imports(engine), component.type.exports(engine)
    result.update(imports=sorted(imports), exports=sorted(exports))
    surface = {
        "imports": {name: prior._type_shape(item, engine) for name, item in imports.items()},
        "exports": {name: prior._type_shape(item, engine) for name, item in exports.items()},
    }
    prior.record_surface_diagnostics(result, surface)
    if set(imports) != ALLOWED_IMPORTS or set(exports) != EXPECTED_EXPORTS:
        raise ValueError("composition imports/exports differ from exact reviewed graph")
    validate_surface(surface)
    result["typed_surface_validated"] = True
    linker = Linker(engine)
    linker.define_unknown_imports_as_traps(component)
    observations = result["observations"]
    for case in evidence["source"]["cases"]:
        observations.append(_replay_case(engine, component, linker, case, fuel, timeout))
        result.update(summarize(evidence["source"]["cases"], observations))
        result["replay_succeeded"] = False  # Post-run source revalidation still pending.
    after = validate_evidence(artifacts, oracle, pins)
    if evidence != after or runner_bindings() != before or _read(wac, 256 * 1024**2) != wac_bytes:
        raise ValueError("reviewed evidence or runner changed during composed replay")
    result.update(
        **summarize(evidence["source"]["cases"], observations),
        component_source_evidence={name: {
            key: value for key, value in item.items() if key not in ("binary", "receipt_bytes")
        } for name, item in evidence["components"].items()},
        source_observations_sha256=digest(evidence["source_bytes"]),
        original_reference_runtime=evidence["source"]["runtime"],
        expected_guest_runtime=EXPECTED_RUNTIME, guest_timeout_seconds=timeout,
        guest_timeout_scope="entire case including instantiation and all ten lifecycle calls",
        fuel_per_case=fuel, store_limits=STORE_LIMITS,
        composition_command=["wac", "compose", "replay.wac",
                             *(part for name in INTERFACES
                               for part in ("--dep", f"local:{name}={name}.wasm")),
                             "--deps-dir", "no-ambient-dependencies", "-o", "composed.wasm"],
        composition_tool_stderr=composed.stderr,
    )
    return result


def main() -> int:
    """Write a fresh receipt, preserving failures without claiming acceptance."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (*INTERFACES, "oracle", "pins", "wac", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--pins-sha256", required=True,
                        help="Independently reviewed SHA-256 of the exact pins JSON")
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("replay output must be a new file")
    result = _base_result()
    try:
        raw = _read(args.pins)
        _pin(raw, args.pins_sha256, "explicit reviewed pins document")
        pins = bridge_stage.strict_json(raw)
        result["pins_document_sha256"] = digest(raw)
        artifacts = {name: getattr(args, name.replace("-", "_")) for name in INTERFACES}
        _compose_and_replay(artifacts, args.oracle, pins, args.wac, result=result)
    except Exception as exc:
        result["replay_succeeded"] = False
        result["runner_failure"] = {"type": type(exc).__name__, "message": str(exc)}
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(reference.json_bytes(result).decode())
    return 0 if result["replay_succeeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
