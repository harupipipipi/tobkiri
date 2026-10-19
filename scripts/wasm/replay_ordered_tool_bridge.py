"""Replay actual Wasmtime 48 typed guest calls; not native Core acceptance."""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
import threading
import tempfile

from stage_ordered_tool_bridge import bounded_read, reviewed_inputs, stage, strict_json

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "packs/local.ai-tool-bridge-ordered-python"
sys.path.insert(0, str(PACK))
from ordered_json import loads

INTERFACE = "local:ai-tool-bridge-ordered-python/bridge@1.0.0"
EXPECTED_GUEST_RUNTIME = {"python": "3.13.16", "unicode": "15.1.0"}
ALLOWED_IMPORTS = {f"tobkiri:core/{name}@1.0.0" for name in
                   ("types", "flow", "clock", "random", "audit")}


def validate_evidence(artifact: Path, oracle: Path) -> tuple[bytes, bytes, bytes, dict]:
    """Bind actual artifact, complete reviewed source plan and exact oracle bytes."""
    binary = bounded_read(artifact, 512 * 1024 * 1024)
    if not binary:
        raise ValueError("component must not be empty")
    receipt_bytes = bounded_read(artifact.with_suffix(".build.json"), 16 * 1024 * 1024)
    receipt = strict_json(receipt_bytes)
    if type(receipt) is not dict:
        raise ValueError("build receipt must be an object")
    frozen_bytes = bounded_read(oracle, 16 * 1024 * 1024)
    reviewed_oracle, _ = reviewed_inputs()
    if frozen_bytes != reviewed_oracle:
        raise ValueError("replay oracle is not the exact reviewed frozen oracle")
    with tempfile.TemporaryDirectory(prefix="ordered-replay-review-") as temporary:
        staged = Path(temporary) / "stage"
        plan = stage(staged)
        manifest = [{**item, "bytes": len((staged / item["source"]).read_bytes())}
                    for item in plan["sources"]]
        plan_sha = hashlib.sha256((staged / "plan.json").read_bytes()).hexdigest()
        provenance = strict_json((staged / "provenance.json").read_bytes())
        original = next(item for item in manifest
                        if item["destination"] == "original_bridge.py")
        if original["sha256"] != provenance["output"]["sha256"]:
            raise ValueError("original bridge is not bound to extraction provenance")
    manifest_sha = hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if (receipt.get("format") != "tobkiri.component-build/2"
            or receipt.get("language") != "python"
            or receipt.get("artifact_sha256") != hashlib.sha256(binary).hexdigest()
            or receipt.get("artifact_bytes") != len(binary)
            or receipt.get("build_plan_sha256") != plan_sha
            or receipt.get("source_manifest") != manifest
            or receipt.get("source_manifest_sha256") != manifest_sha
            or receipt.get("world") != plan["world"]
            or receipt.get("imports") != sorted(plan["allowed_imports"])
            or receipt.get("exports") != sorted(plan["outer_exports"])):
        raise ValueError("build receipt differs from the complete reviewed source plan")
    return binary, receipt_bytes, frozen_bytes, strict_json(frozen_bytes)


def record_runner_failure(record: dict, exc: Exception) -> None:
    """Invalidate all success flags even when observation fails after a result."""
    record["admitted_parity"] = False
    record["exclusion_enforced"] = False
    record["actual"] = {"status": "runner-failure",
                        "type": type(exc).__name__, "message": str(exc)}


def replay(artifact: Path, oracle: Path, *, fuel: int = 2_000_000_000,
           timeout: int = 300) -> dict[str, object]:
    """Invoke guest exports with bounded fuel/memory and compare exact strings."""
    if version("wasmtime") != "48.0.0":
        raise ValueError("replay requires wasmtime 48.0.0")
    from wasmtime import Config, Engine, Store
    from wasmtime.component import Component, Linker, Record
    binary, receipt_bytes, frozen_bytes, frozen = validate_evidence(artifact, oracle)
    config = Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    engine = Engine(config)
    # Compilation precedes per-invocation epoch timers. A separate process
    # supervisor is required to bound compilation time and process resources.
    component = Component(engine, binary)
    imports = set(component.type.imports(engine))
    if imports != ALLOWED_IMPORTS:
        raise ValueError("component contains unexpected imports")
    exports = set(component.type.exports(engine))
    if exports != {INTERFACE, "tobkiri:core/lifecycle@1.0.0"}:
        raise ValueError("component does not expose only the final typed ABI")
    linker = Linker(engine)
    # Development pure-call probe only: no host effects are supplied. Any use
    # traps. This is deliberately NOT evidence of native Core integration.
    linker.define_unknown_imports_as_traps(component)
    observations = []
    runtimes = []
    for case in frozen["cases"]:
        store = Store(engine)
        store.set_limits(memory_size=128 * 1024 * 1024, table_elements=65536,
                         instances=64, tables=64, memories=4)
        store.set_fuel(fuel)
        store.set_epoch_deadline(1)
        timer = threading.Timer(timeout, engine.increment_epoch)
        timer.daemon = True
        timer.start()
        record = {"id": case["id"], "admitted_parity": False,
                  "profile_exclusion_not_parity": case["transport"]["status"] == "excluded"}
        try:
            instance = linker.instantiate(store, component)
            lifecycle = instance.get_export_index(store, "tobkiri:core/lifecycle@1.0.0")
            init_index = instance.get_export_index(store, "init", lifecycle)
            initialize = instance.get_func(store, init_index)
            if initialize is None:
                raise ValueError("missing typed lifecycle init")
            initialized = initialize(store, "{}")
            initialize.post_return(store)
            if initialized is not None:
                raise ValueError("guest lifecycle initialization failed")
            shape = instance.get_export_index(store, INTERFACE)

            def invoke(name: str, *args: str):
                index = instance.get_export_index(store, name, shape)
                function = instance.get_func(store, index)
                if function is None:
                    raise ValueError(f"missing typed export: {name}")
                result = function(store, *args)
                function.post_return(store)
                return result

            runtime = loads(invoke("runtime-info"))
            if runtime not in runtimes:
                runtimes.append(runtime)
            result = invoke("normalize", case["operation"], case["input_json"])
            # Wasmtime 48 lifts disjoint result<string, record> without Variant.
            if type(result) is str:
                loads(result)
                record["actual"] = {"status": "ok", "output_json": result}
                record["admitted_parity"] = (
                    case["transport"]["status"] == "ok"
                    and result == case["transport"]["output_json"])
            elif isinstance(result, Record):
                code = getattr(result, "code", None)
                message = getattr(result, "message", None)
                record["actual"] = {"status": "error", "code": code,
                                    "message": message}
                record["admitted_parity"] = (
                    case["transport"]["status"] == "invalid" and code == "invalid")
                record["exclusion_enforced"] = (
                    case["transport"]["status"] == "excluded" and code == "invalid")
            else:
                raise ValueError("guest did not return the typed string/error result")
            record["fuel_consumed"] = fuel - store.get_fuel()
        except Exception as exc:
            record_runner_failure(record, exc)
        finally:
            timer.cancel()
        observations.append(record)
    return {"format": "tobkiri.ordered-python-tool-bridge-replay/1",
            "protocol": "ordered-finite-json-v1", "abi": "core-typed-development",
            "core_runtime_acceptance": False,
            "compilation_wall_time_bounded": False,
            "guest_call_timeout_seconds": timeout,
            "fuel_per_call": fuel,
            "host_import_policy": "canonical minimal imports trap if called",
            "artifact_sha256": hashlib.sha256(binary).hexdigest(),
            "oracle_sha256": hashlib.sha256(frozen_bytes).hexdigest(),
            "build_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "original_reference_runtime": {"python": "3.13.5", "unicode": "15.1.0"},
            "reviewed_expected_guest_runtime": EXPECTED_GUEST_RUNTIME,
            "guest_matches_reviewed_producer": runtimes == [EXPECTED_GUEST_RUNTIME],
            "replay_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "guest_runtime": runtimes, "imports": sorted(imports),
            "exports": sorted(exports), "observations": observations,
            "all_admitted_parity": all(item["admitted_parity"] for item in observations
                                       if not item["profile_exclusion_not_parity"]),
            "all_exclusions_enforced": all(item.get("exclusion_enforced", False)
                for item in observations if item["profile_exclusion_not_parity"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.artifact, args.oracle)
    args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n")
    raise SystemExit(0 if result["all_admitted_parity"] and
                     result["all_exclusions_enforced"] and
                     result["guest_matches_reviewed_producer"] else 1)
