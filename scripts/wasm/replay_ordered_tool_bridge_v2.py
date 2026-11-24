"""Replay the real typed v2 guest under an external process supervisor.

This development-only probe supplies no Core effects or authority. Fuel, guest
memory limits and epoch interruption do not bound component compilation: invoke
this script only inside a separate wall-time and process-resource supervisor.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any, Callable

from stage_ordered_tool_bridge_v2 import (
    PACK,
    bounded_read,
    reviewed_inputs,
    stage,
    strict_json,
)

sys.path.insert(0, str(PACK))
from ordered_json_v2 import loads

INTERFACE = "local:ai-tool-bridge-ordered-python/bridge@2.0.0"
LIFECYCLE = "tobkiri:core/lifecycle@1.0.0"
EXPECTED_GUEST_RUNTIME = {"python": "3.13.16", "unicode": "15.1.0"}
ALLOWED_IMPORTS = {
    f"tobkiri:core/{name}@1.0.0"
    for name in ("types", "flow", "clock", "random", "audit")
}
DEFAULT_FUEL = 2_000_000_000
DEFAULT_TIMEOUT = 30
INVALID_MESSAGE = "outside ordered-finite-json-v2 domain"
NONFINITE_MESSAGE = "provider output contains a non-finite number"
STORE_LIMITS = {
    "memory_size": 128 * 1024 * 1024,
    "table_elements": 65536,
    "instances": 64,
    "tables": 64,
    "memories": 4,
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_evidence(
    artifact: Path,
    oracle: Path,
    *,
    artifact_limit: int = 512 * 1024 * 1024,
) -> tuple[bytes, bytes, bytes, dict[str, Any]]:
    """Bind receipt and bytes to a fresh plan, not claim component execution.

    The v2 staging validator checks the exact reviewed observation bytes, all
    nineteen reference-source bindings and every frozen case identity. The
    receipt must additionally match every staged source, including new v2 WIT
    and wrapper bindings. Wasmtime must still compile and execute the artifact.
    """
    binary = bounded_read(artifact, artifact_limit)
    if not binary:
        raise ValueError("component must not be empty")
    receipt_bytes = bounded_read(
        artifact.with_suffix(".build.json"), 16 * 1024 * 1024
    )
    receipt = strict_json(receipt_bytes)
    if type(receipt) is not dict:
        raise ValueError("build receipt must be an object")
    frozen_bytes = bounded_read(oracle, 16 * 1024 * 1024)
    reviewed_oracle, _ = reviewed_inputs()
    if frozen_bytes != reviewed_oracle:
        raise ValueError("replay oracle is not the exact reviewed v2 observations")
    frozen = strict_json(frozen_bytes)
    if type(frozen) is not dict:
        raise ValueError("source observations must be an object")
    with tempfile.TemporaryDirectory(prefix="ordered-v2-replay-review-") as temporary:
        staged = Path(temporary) / "stage"
        plan = stage(staged)
        manifest = [
            {
                **item,
                "bytes": len(bounded_read(staged / item["source"], 16 * 1024 * 1024)),
            }
            for item in plan["sources"]
        ]
        plan_sha = _digest((staged / "plan.json").read_bytes())
        provenance = strict_json((staged / "provenance.json").read_bytes())
        original = next(
            item for item in manifest if item["destination"] == "original_bridge.py"
        )
        if original["sha256"] != provenance["output"]["sha256"]:
            raise ValueError("original bridge is not bound to extraction provenance")
    manifest_sha = _digest(
        json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    if (
        receipt.get("format") != "tobkiri.component-build/2"
        or receipt.get("language") != "python"
        or receipt.get("artifact_sha256") != _digest(binary)
        or type(receipt.get("artifact_bytes")) is not int
        or receipt.get("artifact_bytes") != len(binary)
        or receipt.get("build_plan_sha256") != plan_sha
        or receipt.get("source_manifest") != manifest
        or receipt.get("source_manifest_sha256") != manifest_sha
        or receipt.get("world") != plan["world"]
        or receipt.get("imports") != sorted(plan["allowed_imports"])
        or receipt.get("exports") != sorted(plan["outer_exports"])
    ):
        raise ValueError("build receipt differs from the complete reviewed v2 plan")
    # Recheck the source snapshot after staging, not only before reading it.
    if reviewed_inputs()[0] != frozen_bytes:
        raise ValueError("reviewed observations changed during receipt validation")
    return binary, receipt_bytes, frozen_bytes, frozen


def comparison_flags(
    case: dict[str, Any], actual: dict[str, Any]
) -> dict[str, bool]:
    """Compare observation values only; this helper is not execution evidence."""
    classification = case["classification"]
    admitted = False
    boundary = False
    if classification == "success":
        admitted = actual == {"status": "ok", "output_json": case["output_json"]}
    elif classification == "original-invalid":
        admitted = actual == {
            "status": "error",
            "code": "invalid",
            "message": INVALID_MESSAGE,
        }
    elif classification == "excluded-not-parity":
        boundary = actual == {
            "status": "error",
            "code": "internal",
            "message": NONFINITE_MESSAGE,
        }
    else:
        raise ValueError("unknown reviewed case classification")
    return {
        "admitted_parity": admitted,
        "nonfinite_boundary_verified": boundary,
    }


def record_runner_failure(
    record: dict[str, Any], exc: Exception, *, phase: str
) -> None:
    """Revoke all success flags, preserving earlier failures and returned data."""
    failure = {"phase": phase, "type": type(exc).__name__, "message": str(exc)}
    failures = record.setdefault("runner_failures", [])
    if not failures and "actual" in record:
        record["result_before_runner_failure"] = record["actual"]
    failures.append(failure)
    record["admitted_parity"] = False
    record["nonfinite_boundary_verified"] = False
    record["case_completed"] = False
    record["actual"] = {"status": "runner-failure", **failures[0]}


def summarize(
    cases: list[dict[str, Any]], observations: list[dict[str, Any]]
) -> dict[str, bool]:
    """Require the complete ordered case set and lifecycle for aggregate claims."""
    complete = (
        bool(cases)
        and len({case["id"] for case in cases}) == len(cases)
        and [case["id"] for case in cases]
        == [item.get("id") for item in observations]
        and all(
            item.get("classification") == case["classification"]
            for case, item in zip(cases, observations)
        )
    )
    lifecycles = complete and all(
        item.get("lifecycle") == {"init": True, "shutdown": True}
        for item in observations
    )
    completed = complete and all(
        item.get("case_completed") is True and not item.get("runner_failures")
        for item in observations
    )
    admitted = complete and all(
        item.get("admitted_parity") is True
        and comparison_flags(case, item.get("actual", {}))["admitted_parity"]
        and item.get("case_completed") is True
        and not item.get("runner_failures")
        and item.get("lifecycle") == {"init": True, "shutdown": True}
        for case, item in zip(cases, observations)
        if case["classification"] != "excluded-not-parity"
    )
    boundaries = complete and all(
        item.get("nonfinite_boundary_verified") is True
        and item.get("admitted_parity") is False
        and comparison_flags(case, item.get("actual", {}))[
            "nonfinite_boundary_verified"
        ]
        and item.get("case_completed") is True
        and not item.get("runner_failures")
        and item.get("lifecycle") == {"init": True, "shutdown": True}
        for case, item in zip(cases, observations)
        if case["classification"] == "excluded-not-parity"
    )
    runtimes = complete and all(
        item.get("guest_runtime") == EXPECTED_GUEST_RUNTIME for item in observations
    )
    calls = complete and all(
        item.get("guest_calls_returned")
        == ["init", "runtime-info", "normalize", "shutdown"]
        for item in observations
    )
    return {
        "wasm_execution_attempted": any(
            bool(item.get("guest_calls_attempted")) for item in observations
        ),
        # A trap may execute some instructions without returning. Do not turn
        # such an attempt, or a failed Store setup, into a completed-call claim.
        "wasm_execution_performed": any(
            bool(item.get("guest_calls_returned")) for item in observations
        ),
        "complete_case_set": complete,
        "all_guest_calls_returned": calls,
        "all_lifecycles_completed": lifecycles,
        "all_cases_completed": completed,
        "all_admitted_parity": admitted,
        "all_nonfinite_boundaries_verified": boundaries,
        "guest_matches_reviewed_producer": runtimes,
        "replay_succeeded": (
            complete
            and lifecycles
            and completed
            and admitted
            and boundaries
            and runtimes
            and calls
        ),
    }


class _EpochTimer:
    """Join each timer before another store uses the shared engine's epoch."""

    def __init__(self, timeout: float, increment: Callable[[], None]) -> None:
        self._lock = threading.Lock()
        self._active = True
        self._expired = False
        self._error: Exception | None = None
        self._increment = increment
        self._timeout = timeout
        self._deadline: float | None = None
        self._timer = threading.Timer(timeout, self._expire)
        self._timer.daemon = True

    def _expire(self) -> None:
        with self._lock:
            if self._active:
                self._expired = True
                try:
                    self._increment()
                except Exception as exc:
                    self._error = exc

    def start(self) -> None:
        self._deadline = time.monotonic() + self._timeout
        self._timer.start()

    def finish(self) -> None:
        # If the callback already owns the lock, wait for its increment. If
        # cancellation wins, the callback cannot increment a later case's epoch.
        with self._lock:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                self._expired = True
            self._active = False
        self._timer.cancel()
        self._timer.join()
        if self._error is not None:
            raise RuntimeError("epoch interruption callback failed") from self._error
        if self._expired:
            raise TimeoutError("guest case exceeded its epoch deadline")


def _export(instance: Any, store: Any, interface: str, name: str) -> Any:
    shape = instance.get_export_index(store, interface)
    if shape is None:
        raise ValueError(f"missing typed interface: {interface}")
    index = instance.get_export_index(store, name, shape)
    if index is None:
        raise ValueError(f"missing typed export: {interface}/{name}")
    function = instance.get_func(store, index)
    if function is None:
        raise ValueError(f"export is not a typed function: {interface}/{name}")
    return function


def _call_and_cleanup(
    call: Callable[[], Any],
    cleanup: Callable[[], None],
    on_return: Callable[[Any], None],
    cleanup_failures: list[Exception],
) -> Any:
    """Keep lifted return custody and the first failure without skipping cleanup."""
    result = call()
    try:
        on_return(result)
    except BaseException:
        try:
            cleanup()
        except Exception as exc:
            cleanup_failures.append(exc)
        raise
    # A trap has no successful return to clean up. Every actual returned value,
    # including a typed error, requires canonical ABI post-return cleanup.
    cleanup()
    return result


def _invoke(
    function: Any,
    store: Any,
    *arguments: str,
    on_return: Callable[[Any], None],
    cleanup_failures: list[Exception],
) -> Any:
    return _call_and_cleanup(
        lambda: function(store, *arguments),
        lambda: function.post_return(store),
        on_return,
        cleanup_failures,
    )


def _replay_case(
    engine: Any,
    component: Any,
    linker: Any,
    case: dict[str, Any],
    fuel: int,
    timeout: float,
) -> dict[str, Any]:
    from wasmtime import Store
    from wasmtime.component import Record

    record: dict[str, Any] = {
        "id": case["id"],
        "classification": case["classification"],
        "profile_exclusion_not_parity": (
            case["classification"] == "excluded-not-parity"
        ),
        "admitted_parity": False,
        "nonfinite_boundary_verified": False,
        "case_completed": False,
        "lifecycle": {"init": False, "shutdown": False},
        "guest_calls_attempted": [],
        "guest_calls_returned": [],
    }
    phase = "store-setup"
    timer = None
    try:
        with Store(engine) as store:
            store.set_limits(**STORE_LIMITS)
            store.set_fuel(fuel)
            store.set_epoch_deadline(1)
            timer = _EpochTimer(timeout, engine.increment_epoch)
            timer.start()
            shutdown = None
            cleanup_failures: list[Exception] = []

            def call(name: str, function: Any, *arguments: str) -> Any:
                def capture(result: Any) -> None:
                    record["guest_calls_returned"].append(name)
                    if name != "normalize":
                        return
                    # Preserve the lifted result before post-return. Parsing or
                    # parity classification occurs only after cleanup succeeds.
                    if type(result) is str:
                        actual = {"status": "ok", "output_json": result}
                    elif isinstance(result, Record):
                        if (
                            set(vars(result)) != {"code", "message"}
                            or type(result.code) is not str
                            or type(result.message) is not str
                        ):
                            raise ValueError(
                                "guest returned a malformed typed Core error"
                            )
                        actual = {
                            "status": "error",
                            "code": result.code,
                            "message": result.message,
                        }
                    else:
                        raise ValueError(
                            "guest did not return typed string/Core error"
                        )
                    record["actual"] = actual

                record["guest_calls_attempted"].append(name)
                return _invoke(
                    function,
                    store,
                    *arguments,
                    on_return=capture,
                    cleanup_failures=cleanup_failures,
                )

            try:
                phase = "instantiate"
                instance = linker.instantiate(store, component)
                initialize = _export(instance, store, LIFECYCLE, "init")
                shutdown = _export(instance, store, LIFECYCLE, "shutdown")
                runtime_info = _export(instance, store, INTERFACE, "runtime-info")
                normalize = _export(instance, store, INTERFACE, "normalize")
                phase = "lifecycle-init"
                if call("init", initialize, "{}") is not None:
                    raise ValueError("guest lifecycle initialization failed")
                record["lifecycle"]["init"] = True
                phase = "runtime-info"
                runtime = call("runtime-info", runtime_info)
                if type(runtime) is not str:
                    raise ValueError("runtime-info did not return a typed string")
                record["guest_runtime"] = loads(runtime)
                if record["guest_runtime"] != EXPECTED_GUEST_RUNTIME:
                    raise ValueError("guest runtime differs from reviewed producer")
                phase = "normalize"
                result = call(
                    "normalize", normalize, case["operation"], case["input_json"]
                )
                if type(result) is str:
                    loads(result)
                record.update(comparison_flags(case, record["actual"]))
            except Exception as exc:
                record_runner_failure(record, exc, phase=phase)
                for cleanup_error in cleanup_failures:
                    record_runner_failure(
                        record, cleanup_error, phase=f"{phase}-post-return"
                    )
                cleanup_failures.clear()
            finally:
                # Attempt shutdown even after a normalize/runtime-info failure;
                # never overwrite that failure if cleanup happens to succeed.
                if shutdown is not None and record["lifecycle"]["init"]:
                    try:
                        if call("shutdown", shutdown) is not None:
                            raise ValueError(
                                "guest shutdown returned an unexpected value"
                            )
                        record["lifecycle"]["shutdown"] = True
                    except Exception as exc:
                        record_runner_failure(record, exc, phase="lifecycle-shutdown")
                        for cleanup_error in cleanup_failures:
                            record_runner_failure(
                                record,
                                cleanup_error,
                                phase="lifecycle-shutdown-post-return",
                            )
                        cleanup_failures.clear()
                try:
                    record["fuel_consumed"] = fuel - store.get_fuel()
                except Exception as exc:
                    record_runner_failure(record, exc, phase="fuel-accounting")
                try:
                    timer.finish()
                except Exception as exc:
                    record_runner_failure(record, exc, phase="epoch-deadline")
                timer = None
            phase = "store-cleanup"
    except Exception as exc:
        record_runner_failure(record, exc, phase=phase)
    finally:
        if timer is not None:
            try:
                timer.finish()
            except Exception as exc:
                record_runner_failure(record, exc, phase="epoch-deadline")
    record["case_completed"] = (
        not record.get("runner_failures")
        and record["lifecycle"] == {"init": True, "shutdown": True}
    )
    return record


def replay(
    artifact: Path,
    oracle: Path,
    *,
    fuel: int = DEFAULT_FUEL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Execute actual Wasmtime 48 calls; external compilation bounds are required."""
    if type(fuel) is not int or not 0 < fuel <= DEFAULT_FUEL:
        raise ValueError("fuel must be a positive integer no greater than 2000000000")
    if type(timeout) not in (float, int) or not 0 < timeout <= DEFAULT_TIMEOUT:
        raise ValueError("guest timeout must be positive and at most 30 seconds")
    if version("wasmtime") != "48.0.0":
        raise ValueError("replay requires wasmtime 48.0.0")
    from wasmtime import Config, Engine
    from wasmtime.component import Component, Linker

    binary, receipt_bytes, frozen_bytes, frozen = validate_evidence(artifact, oracle)
    config = Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    engine = Engine(config)
    # Epoch/fuel interruption cannot bound compilation or native host allocation.
    # The external process supervisor must cover this call and the whole process.
    component = Component(engine, binary)
    imports = set(component.type.imports(engine))
    exports = set(component.type.exports(engine))
    if imports != ALLOWED_IMPORTS:
        raise ValueError("component contains unexpected imports")
    if exports != {INTERFACE, LIFECYCLE}:
        raise ValueError("component does not expose only the final v2 typed ABI")
    linker = Linker(engine)
    linker.define_unknown_imports_as_traps(component)
    observations = [
        _replay_case(engine, component, linker, case, fuel, timeout)
        for case in frozen["cases"]
    ]
    return {
        "format": "tobkiri.ordered-python-tool-bridge-replay/2",
        "protocol": "ordered-finite-json-v2",
        "abi": "core-typed-development",
        "execution_observation_scope": (
            "execution is observed only after an actual guest function returns; "
            "traps alone are attempts, not completed-call proof"
        ),
        "core_runtime_acceptance": False,
        "native_core_acceptance": False,
        "full_host_replay_performed": False,
        "legacy_http_error_parity_claimed": False,
        "production_enabled": False,
        "compilation_wall_time_bounded": False,
        "external_process_supervisor_required": True,
        "guest_call_timeout_seconds": timeout,
        "guest_timeout_scope": (
            "entire case including instantiation and both lifecycle calls"
        ),
        "fuel_per_call": fuel,
        "fuel_scope": "entire case including instantiation and both lifecycle calls",
        "store_limits": STORE_LIMITS.copy(),
        "host_import_policy": (
            "canonical minimal imports trap if called; no host effects"
        ),
        "artifact_sha256": _digest(binary),
        "oracle_sha256": _digest(frozen_bytes),
        "build_receipt_sha256": _digest(receipt_bytes),
        "original_reference_runtime": frozen["runtime"],
        "reviewed_expected_guest_runtime": EXPECTED_GUEST_RUNTIME.copy(),
        "replay_sha256": _digest(Path(__file__).read_bytes()),
        "imports": sorted(imports),
        "exports": sorted(exports),
        "admitted_parity_scope": (
            "18 exact normalized strings and 12 typed INVALID mappings; "
            "three nonfinite INTERNAL boundaries are not admitted parity"
        ),
        "observations": observations,
        **summarize(frozen["cases"], observations),
    }


def main() -> int:
    """Write fresh replay evidence only after a real, externally supervised run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.output.exists() or arguments.output.is_symlink():
        raise ValueError("replay output must be a new file")
    result = replay(arguments.artifact, arguments.oracle)
    with arguments.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=True, indent=2) + "\n")
    return 0 if result["replay_succeeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
