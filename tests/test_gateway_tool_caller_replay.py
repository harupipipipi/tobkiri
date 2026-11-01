"""Strict isolated DEVELOPMENT harness tests, never real component evidence.

Fake functions, stores and records below exercise runner custody only. They are
not generated imports, a provider implementation, native Core, or guest replay.
No test builds, composes or instantiates an actual component.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import observe_gateway_tool_caller as observer
import replay_gateway_tool_caller as replay

ORACLE = ROOT / "tests/fixtures/issue1520/gateway-tool-caller.source.json"
CURRENT_ORACLE = (
    ROOT / "tests/fixtures/issue1520/gateway-tool-caller.current-source.json"
)
# This separately pinned reference is source proof only. It does not
# replace the historical oracle/production runner pins or establish a new replay.
CURRENT_ORACLE_SHA256 = (
    "854fa03de651c12cb7126d8f81e4f0c62e340a5a85f71eb50784e539e207bced"
)
HISTORICAL_BINDINGS_SHA256 = (
    "32f2684e0054e1ab2c17a86f98dfb65f071e5c85fd08b7923cd16ad96948215a"
)
HISTORICAL_REPLAY = (
    ROOT / "tools/python313-producer/trials/2026-10-20-gateway/replay.json"
)
HISTORICAL_REPLAY_SHA256 = (
    "0fac9e2d7b6864f7aaaa2414ba9d3cdb474ea4853968fe041aaf2632313f18c1"
)


@pytest.fixture
def source() -> dict[str, Any]:
    """Preserved historical source expectations, not current source bindings."""
    return json.loads(ORACLE.read_bytes())


@pytest.fixture
def current_source() -> dict[str, Any]:
    """Start structural mutations from hash-pinned, currently valid source."""
    raw, document = replay.validate_observations(
        CURRENT_ORACLE, CURRENT_ORACLE_SHA256,
    )
    assert raw == CURRENT_ORACLE.read_bytes()
    assert all(document[key] is False for key in observer.NO_EXECUTION)
    return document


@pytest.fixture
def caller_receipt(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, str]]:
    """Create inert bytes for structural receipt tests, not a Wasm component."""
    stage = tmp_path / "stage"
    plan = replay.caller_stage.stage(stage)
    binary = b"structural-validation-only; NOT a component"
    artifact = tmp_path / "caller.wasm"
    artifact.write_bytes(binary)
    manifest = replay._manifest(stage, plan)
    receipt = {
        "format": "tobkiri.component-build/2", "language": "python",
        "artifact_sha256": replay.digest(binary), "artifact_bytes": len(binary),
        "source_manifest": manifest,
        "source_manifest_sha256": replay.digest(json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()),
        "build_plan_sha256": replay.digest((stage / "plan.json").read_bytes()),
        "world": plan["world"], "imports": sorted(plan["allowed_imports"]),
        "exports": sorted(plan["outer_exports"]),
    }
    receipt_bytes = json.dumps(receipt).encode()
    artifact.with_suffix(".build.json").write_bytes(receipt_bytes)
    pins = {"artifact_sha256": replay.digest(binary),
            "receipt_sha256": replay.digest(receipt_bytes)}
    return artifact, receipt, pins


def test_structural_receipt_validation_never_claims_execution(caller_receipt: Any) -> None:
    artifact, _, pins = caller_receipt
    result = replay.validate_caller(artifact, pins)
    assert result["binary"] == artifact.read_bytes()
    assert not result["binary"].startswith(b"\x00asm")
    assert "wasm_execution_performed" not in result


@pytest.mark.parametrize("field", ["format", "language", "artifact_sha256",
                                   "artifact_bytes", "source_manifest_sha256",
                                   "build_plan_sha256", "world", "imports", "exports"])
def test_caller_rejects_forged_receipt_even_with_refreshed_receipt_pin(
    caller_receipt: Any, field: str,
) -> None:
    artifact, receipt, pins = caller_receipt
    receipt[field] = "changed"
    raw = json.dumps(receipt).encode()
    artifact.with_suffix(".build.json").write_bytes(raw)
    pins["receipt_sha256"] = replay.digest(raw)
    with pytest.raises(ValueError, match="complete reviewed stage plan"):
        replay.validate_caller(artifact, pins)


@pytest.mark.parametrize("destination", sorted(replay.caller_stage.DESTINATIONS))
def test_every_caller_staged_input_identity_is_required(
    caller_receipt: Any, destination: str,
) -> None:
    artifact, receipt, pins = caller_receipt
    manifest = receipt["source_manifest"]
    next(item for item in manifest if item["destination"] == destination)["sha256"] = "0" * 64
    receipt["source_manifest_sha256"] = replay.digest(json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode())
    raw = json.dumps(receipt).encode()
    artifact.with_suffix(".build.json").write_bytes(raw)
    pins["receipt_sha256"] = replay.digest(raw)
    with pytest.raises(ValueError, match="complete reviewed stage plan"):
        replay.validate_caller(artifact, pins)


@pytest.mark.parametrize("kind", ["artifact", "receipt", "symlink-artifact",
                                  "symlink-receipt", "duplicate-keys"])
def test_changed_or_unsafe_evidence_fails(caller_receipt: Any, kind: str) -> None:
    artifact, _, pins = caller_receipt
    path = artifact if kind.endswith("artifact") else artifact.with_suffix(".build.json")
    if kind.startswith("symlink"):
        captured = path.with_suffix(".capture")
        path.rename(captured)
        path.symlink_to(captured)
    elif kind == "duplicate-keys":
        raw = b'{"format":1,"format":2}'
        path.write_bytes(raw)
        pins["receipt_sha256"] = replay.digest(raw)
    else:
        path.write_bytes(b"changed")
    with pytest.raises(ValueError):
        replay.validate_caller(artifact, pins)


def test_caller_source_change_after_staging_is_rejected(
    caller_receipt: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact, _, pins = caller_receipt
    values = iter([b"before", b"after"])
    monkeypatch.setattr(replay.caller_stage, "reviewed_inputs", lambda: next(values))
    with pytest.raises(ValueError, match="source changed"):
        replay.validate_caller(artifact, pins)


def test_historical_observations_keep_recorded_bindings_and_replay_identity(
    source: Any,
) -> None:
    """The preserved 92f38e3 proof never authenticates today's source closure."""
    replay._pin(
        ORACLE.read_bytes(), replay.REVIEWED_PINS["source_observations_sha256"],
        "historical source observations",
    )
    replay._pin(
        HISTORICAL_REPLAY.read_bytes(), HISTORICAL_REPLAY_SHA256,
        "historical two-component replay receipt",
    )
    receipt = json.loads(HISTORICAL_REPLAY.read_bytes())
    assert receipt["component_pins"] == replay.REVIEWED_PINS
    assert receipt["provider_33_oracle_sha256"] == source["provider_33_oracle_sha256"]
    assert receipt["original_reference_runtime"] == source["runtime"]
    for name, directory in (
        ("caller", "2026-10-20-gateway"), ("provider", "2026-10-20"),
    ):
        build_path = (
            ROOT / "tools/python313-producer/trials" / directory
            / "component.build.json"
        )
        build_bytes = build_path.read_bytes()
        replay._pin(
            build_bytes, receipt["component_pins"][name]["receipt_sha256"],
            f"historical {name} build receipt",
        )
        build = json.loads(build_bytes)
        assert build["artifact_sha256"] == (
            receipt["component_pins"][name]["artifact_sha256"]
        )
        if name == "caller":
            assert build["build_plan_sha256"] == receipt["caller_plan_sha256"]
            assert build["source_manifest_sha256"] == (
                receipt["caller_source_manifest_sha256"]
            )
    assert source["source_bindings_sha256"] == HISTORICAL_BINDINGS_SHA256
    assert replay.digest(observer.canonical(source["source_bindings"])) == (
        HISTORICAL_BINDINGS_SHA256
    )
    assert len(source["cases"]) == 30
    assert source["counts"] == {
        "success": 16, "provider-invalid": 7, "provider-nonfinite": 7,
    }
    assert len(source["source_bindings"]) == 37
    assert all(source[key] is False for key in observer.NO_EXECUTION)
    with pytest.raises(
        ValueError,
        match="^source observations differ from reviewed source identities$",
    ):
        replay.validate_observations(
            ORACLE, replay.REVIEWED_PINS["source_observations_sha256"],
        )


def test_current_reference_changes_only_bindings_not_historical_cases(
    source: Any, current_source: Any,
) -> None:
    """Current source-only proof is separate from the preserved actual replay."""
    assert set(current_source) == set(source)
    assert {key for key in source if source[key] != current_source[key]} == {
        "source_bindings", "source_bindings_sha256", "provider_33_oracle_sha256",
    }
    assert observer.json_bytes(current_source["cases"]) == observer.json_bytes(
        source["cases"],
    )
    assert current_source["source_bindings"] == observer.source_bindings()
    assert len(current_source["source_bindings"]) == 37
    assert CURRENT_ORACLE_SHA256 != replay.REVIEWED_PINS["source_observations_sha256"]
    with pytest.raises(
        ValueError,
        match="^source observations differs from its exact reviewed SHA-256$",
    ):
        replay.validate_observations(
            CURRENT_ORACLE, replay.REVIEWED_PINS["source_observations_sha256"],
        )


def test_exact_reference_runtime_reproduces_current_source_only_observations() -> None:
    if observer.runtime() != observer.EXPECTED_RUNTIME:
        pytest.skip("actual reference observations require exact CPython3.13.5")
    raw = observer.json_bytes(observer.observe())
    assert raw == CURRENT_ORACLE.read_bytes()
    assert replay.digest(raw) == CURRENT_ORACLE_SHA256


def test_source_observer_never_claims_all_error_arms_or_runtime_counts(source: Any) -> None:
    later = next(case for case in source["cases"] if case["id"] == "events-later-nonfinite")
    assert later["source_reference_call_count"] == 3
    assert later["source_call_counts_are_not_component_observations"] is True
    assert later["expected"]["code"] == "internal"
    assert source["retry_parity_claimed"] is False
    assert source["all_typed_error_arms_executed"] is False


@pytest.mark.parametrize(("mutation", "message"), [
    ("case-order", "source observations differ from reviewed source identities"),
    ("case-missing", "source observations do not contain complete reviewed cases"),
    ("case-id", "source observations differ from reviewed source identities"),
    ("case-input", "source observations differ from reviewed source identities"),
    ("runtime", "source observations differ from reviewed source identities"),
    ("bindings", "source observations differ from reviewed source identities"),
    ("provider-oracle", "source observations differ from reviewed source identities"),
    ("counts", "source observation counts are inconsistent"),
    ("classification", "unknown source observation classification"),
    ("typed-error", "typed error boundary differs from reviewed mapping"),
    ("execution-flag", "source observations differ from reviewed source identities"),
])
def test_reforged_observation_hash_cannot_hide_structural_drift(
    current_source: Any, tmp_path: Path, mutation: str, message: str,
) -> None:
    source = current_source
    if mutation == "case-order":
        source["cases"].reverse()
    elif mutation == "case-missing":
        source["cases"].pop()
    elif mutation == "case-id":
        source["cases"][0]["id"] = "made-up"
    elif mutation == "case-input":
        source["cases"][0]["input_json"] = "{}"
    elif mutation == "runtime":
        source["runtime"]["python"] = "3.13.16"
    elif mutation == "bindings":
        source["source_bindings"][0]["sha256"] = "0" * 64
    elif mutation == "provider-oracle":
        source["provider_33_oracle_sha256"] = "0" * 64
    elif mutation == "counts":
        source["counts"]["success"] += 1
    elif mutation == "classification":
        source["cases"][0]["classification"] = "all-host-parity"
    elif mutation == "typed-error":
        next(case for case in source["cases"] if case["classification"] == "provider-invalid")["expected"]["code"] = "unavailable"
    else:
        source["wasm_execution_performed"] = True
    raw = observer.json_bytes(source)
    path = tmp_path / "tampered.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match=f"^{message}$"):
        replay.validate_observations(path, replay.digest(raw))


def test_new_source_binding_is_rejected_with_unchanged_current_oracle(
    current_source: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert current_source["source_bindings"] == observer.source_bindings()
    bindings = observer.source_bindings()
    bindings[0]["sha256"] = "0" * 64
    monkeypatch.setattr(observer, "source_bindings", lambda: bindings)
    with pytest.raises(
        ValueError,
        match="^source observations differ from reviewed source identities$",
    ):
        replay.validate_observations(CURRENT_ORACLE, CURRENT_ORACLE_SHA256)


@pytest.mark.parametrize("target", ["caller", "provider", "oracle", "wac"])
def test_caller_cannot_replace_reviewed_artifact_receipt_or_oracle_pins(
    target: str, tmp_path: Path,
) -> None:
    pins = copy.deepcopy(replay.REVIEWED_PINS)
    if target in ("caller", "provider"):
        pins[target]["artifact_sha256"] = "0" * 64
    elif target == "oracle":
        pins["source_observations_sha256"] = "0" * 64
    else:
        pins["wac_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="independently reviewed"):
        replay.validate_evidence(tmp_path / "a", tmp_path / "b", ORACLE, pins)


class HarnessRecord:
    """Labelled lifted-record double, not an actual component result."""

    def __init__(self, code: Any, message: Any) -> None:
        self.code = code
        self.message = message


class HarnessFunction:
    """Labelled function double for primary and canonical post-return custody."""

    def __init__(self, label: str, state: Any, result: Any) -> None:
        self.label, self.state, self.result = label, state, result

    def __call__(self, store: Any, *args: str) -> Any:
        del store
        self.state.events.append(("call", self.label, args))
        if self.label in self.state.failures:
            raise self.state.failures[self.label]
        return self.state.results.get(self.label, self.result)

    def post_return(self, store: Any) -> None:
        del store
        self.state.events.append(("post-return", self.label))
        if self.label in self.state.cleanup_failures:
            raise self.state.cleanup_failures[self.label]


@pytest.fixture
def harness(source: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace export lookup only; never use Wasmtime or execute a real guest."""
    case = source["cases"][0]
    state = SimpleNamespace(events=[], failures={}, cleanup_failures={}, results={},
                            case=case, record=replay._case_record(case))
    interfaces = {replay.CALLER: "caller", replay.PROVIDER: "provider",
                  **{interface: name for name, interface in replay.LIFECYCLES.items()}}

    def export(instance: Any, store: Any, interface: str, name: str) -> HarnessFunction:
        del instance, store
        label = f"{interfaces[interface]}.{name}"
        result = None
        if name == "runtime-info":
            result = json.dumps(replay.EXPECTED_RUNTIME)
        elif name == case["operation"]:
            label, result = "caller.operation", case["expected"].get("output_json")
        return HarnessFunction(label, state, result)

    monkeypatch.setattr(replay.provider_replay, "_export", export)
    state.run = lambda: replay._run_case_in_store(state.record, object(), object(),
                                                 state.case, HarnessRecord)
    return state


def test_harness_both_lifecycles_runtime_and_operation_order(harness: Any) -> None:
    harness.run()
    record = harness.record
    assert record["guest_calls_attempted"] == replay.CALL_ORDER
    assert record["guest_calls_returned"] == replay.CALL_ORDER
    assert record["guest_runtime"] == {name: replay.EXPECTED_RUNTIME for name in replay.LIFECYCLES}
    assert [event[1] for event in harness.events if event[0] == "post-return"] == replay.CALL_ORDER
    assert record["source_success_parity"] is True
    assert "wasm_execution_performed" not in record


@pytest.mark.parametrize("name", ["provider", "caller"])
def test_harness_runtime_mismatch_still_shuts_down_both(harness: Any, name: str) -> None:
    harness.results[f"{name}.runtime-info"] = '{"python":"3.13.5","unicode":"15.1.0"}'
    harness.run()
    assert harness.record["runner_failures"][0]["phase"] == f"{name}.runtime-info"
    assert harness.record["guest_calls_attempted"][-2:] == ["caller.shutdown", "provider.shutdown"]
    assert harness.record["expected_result_matched"] is False


@pytest.mark.parametrize("name", ["provider", "caller"])
def test_harness_init_failure_shuts_down_only_acquired_guests(harness: Any, name: str) -> None:
    harness.failures[f"{name}.init"] = RuntimeError("init trap")
    harness.run()
    shutdowns = [label for label in harness.record["guest_calls_attempted"] if label.endswith("shutdown")]
    assert shutdowns == ([] if name == "provider" else ["provider.shutdown"])


@pytest.mark.parametrize("name", ["provider", "caller"])
def test_harness_init_post_return_failure_still_shuts_down_acquired_guest(
    harness: Any, name: str,
) -> None:
    harness.cleanup_failures[f"{name}.init"] = RuntimeError("init post-return failed")
    harness.run()
    assert f"{name}.shutdown" in harness.record["guest_calls_attempted"]
    assert harness.record["lifecycle"][name]["init"] is False
    assert harness.record["expected_result_matched"] is False


def test_harness_primary_and_both_shutdown_failures_are_retained(harness: Any) -> None:
    harness.failures.update({
        "caller.operation": RuntimeError("primary"),
        "caller.shutdown": RuntimeError("caller cleanup"),
        "provider.shutdown": RuntimeError("provider cleanup"),
    })
    harness.run()
    assert [item["message"] for item in harness.record["runner_failures"]] == [
        "primary", "caller cleanup", "provider cleanup",
    ]
    assert harness.record["actual"]["message"] == "primary"
    assert "caller.operation" not in [event[1] for event in harness.events if event[0] == "post-return"]


def test_harness_lifted_result_is_retained_when_post_return_fails(harness: Any) -> None:
    harness.cleanup_failures["caller.operation"] = RuntimeError("canonical cleanup")
    harness.run()
    assert harness.record["result_before_runner_failure"] == harness.case["expected"]
    assert harness.record["source_success_parity"] is False
    assert harness.record["guest_calls_attempted"][-2:] == ["caller.shutdown", "provider.shutdown"]


def test_harness_bad_lift_and_post_return_error_preserve_both(harness: Any) -> None:
    harness.results["caller.operation"] = object()
    harness.cleanup_failures["caller.operation"] = RuntimeError("canonical cleanup")
    harness.run()
    failures = harness.record["runner_failures"]
    assert failures[0]["type"] == "ValueError"
    assert failures[1]["phase"] == "caller.operation-post-return"
    assert failures[1]["message"] == "canonical cleanup"


@pytest.mark.parametrize("code,message", [("invalid", replay.provider_replay.INVALID_MESSAGE),
                                           ("internal", replay.provider_replay.NONFINITE_MESSAGE)])
def test_harness_typed_error_return_gets_cleanup_without_retry(
    harness: Any, code: str, message: str,
) -> None:
    harness.case.update(classification="provider-invalid" if code == "invalid" else "provider-nonfinite",
                        expected={"status": "error", "code": code, "message": message})
    harness.results["caller.operation"] = HarnessRecord(code, message)
    harness.run()
    assert harness.record["typed_error_custody"] is True
    assert harness.record["source_success_parity"] is False
    assert harness.record["guest_calls_attempted"].count("caller.operation") == 1
    assert [event[1] for event in harness.events if event[0] == "post-return"].count("caller.operation") == 1


@pytest.mark.parametrize("value", [None, {}, 1, HarnessRecord(1, "invalid"),
                                   HarnessRecord("invalid", 1)])
def test_lifted_result_shape_is_strict(value: Any) -> None:
    with pytest.raises(ValueError, match="typed string/Core error"):
        replay._typed_result(value, HarnessRecord)


def _completed(case: dict[str, Any]) -> dict[str, Any]:
    return {"id": case["id"], "classification": case["classification"],
            "actual": case["expected"], **replay.comparison_flags(case, case["expected"]),
            "case_completed": True, "guest_calls_attempted": replay.CALL_ORDER.copy(),
            "guest_calls_returned": replay.CALL_ORDER.copy(),
            "guest_runtime": {name: replay.EXPECTED_RUNTIME.copy() for name in replay.LIFECYCLES},
            "lifecycle": {name: {"init": True, "shutdown": True} for name in replay.LIFECYCLES}}


@pytest.mark.parametrize("mutation", ["missing", "extra", "reorder", "duplicate", "classification",
                                      "result", "failure", "incomplete", "caller-runtime",
                                      "provider-runtime", "caller-shutdown", "provider-shutdown",
                                      "attempts", "returns"])
def test_harness_complete_case_accounting_cannot_be_forged(source: Any, mutation: str) -> None:
    cases = source["cases"]
    observations = [_completed(case) for case in cases]
    assert replay.summarize(cases, observations)["replay_succeeded"] is True
    if mutation == "missing":
        observations.pop()
    elif mutation == "extra":
        observations.append(copy.deepcopy(observations[0]))
    elif mutation == "reorder":
        observations.reverse()
    elif mutation == "duplicate":
        observations[-1] = copy.deepcopy(observations[0])
    elif mutation == "classification":
        observations[0]["classification"] = "provider-invalid"
    elif mutation == "result":
        observations[0]["actual"] = {"status": "ok", "output_json": "[]"}
    elif mutation == "failure":
        observations[0]["runner_failures"] = [{"type": "TimeoutError"}]
    elif mutation == "incomplete":
        observations[0]["case_completed"] = False
    elif mutation.endswith("runtime"):
        observations[0]["guest_runtime"][mutation.split("-")[0]]["python"] = "3.13.5"
    elif mutation.endswith("shutdown"):
        observations[0]["lifecycle"][mutation.split("-")[0]]["shutdown"] = False
    else:
        key = "guest_calls_attempted" if mutation == "attempts" else "guest_calls_returned"
        observations[0][key].pop()
    assert replay.summarize(cases, observations)["replay_succeeded"] is False


def test_empty_and_attempt_only_records_never_prove_execution() -> None:
    assert replay.summarize([], [])["wasm_execution_performed"] is False
    result = replay.summarize([], [{"guest_calls_attempted": ["provider.init"]}])
    assert result["wasm_execution_attempted"] is True
    assert result["wasm_execution_performed"] is False
    assert result["replay_succeeded"] is False
    assert all(replay._base_result()[key] is False for key in replay.NO_AUTHORITY)
    assert replay._base_result()["compilation_bounded_by_guest_fuel_or_epoch"] is False


def test_fixed_wac_graph_does_not_expose_bridge_as_host_import() -> None:
    source = replay.WAC_SOURCE.decode()
    assert '"local:ai-tool-bridge-ordered-python/bridge@2.0.0": provider[' in source
    assert source.count("let provider = new local:provider") == 1
    assert source.count("let caller = new local:caller") == 1
    assert replay.PROVIDER not in replay.ALLOWED_IMPORTS
    assert len(replay.ALLOWED_IMPORTS) == 5
    assert replay.EXPECTED_EXPORTS == {replay.CALLER, replay.PROVIDER,
                                       "provider-lifecycle", "caller-lifecycle"}


@pytest.fixture
def store_harness(harness: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Inject explicit Store/Timer doubles, never a component execution engine."""
    state = harness
    state.store_events = []
    state.store_failure = None
    state.stores = []

    class Store:
        def __init__(self, engine: Any) -> None:
            del engine
            self.index = len(state.stores)
            state.stores.append(self)

        def __enter__(self) -> Any:
            state.store_events.append((self.index, "enter"))
            return self

        def __exit__(self, *args: Any) -> None:
            state.store_events.append((self.index, "exit"))
            if state.store_failure == "store-cleanup":
                raise RuntimeError("store cleanup failed")

        def set_limits(self, **limits: int) -> None:
            state.store_events.append((self.index, "limits", limits))

        def set_fuel(self, fuel: int) -> None:
            state.store_events.append((self.index, "fuel", fuel))

        def set_epoch_deadline(self, epoch: int) -> None:
            state.store_events.append((self.index, "epoch", epoch))

        def get_fuel(self) -> int:
            if state.store_failure == "fuel-accounting":
                raise RuntimeError("fuel accounting failed")
            return replay.DEFAULT_FUEL - 7

    class Timer:
        def __init__(self, timeout: float, increment: Any) -> None:
            del increment
            self.index = len(state.stores) - 1
            assert timeout == replay.DEFAULT_TIMEOUT

        def start(self) -> None:
            state.store_events.append((self.index, "timer-start"))

        def finish(self) -> None:
            state.store_events.append((self.index, "timer-finish"))
            if state.store_failure == "epoch-deadline":
                raise TimeoutError("deadline expired after result")

    def instantiate(store: Any, component: Any) -> object:
        del component
        state.store_events.append((store.index, "instantiate"))
        return object()

    wasm = ModuleType("wasmtime")
    wasm.Store = Store
    component = ModuleType("wasmtime.component")
    component.Record = HarnessRecord
    monkeypatch.setitem(sys.modules, "wasmtime", wasm)
    monkeypatch.setitem(sys.modules, "wasmtime.component", component)
    monkeypatch.setattr(replay.provider_replay, "_EpochTimer", Timer)
    state.engine = SimpleNamespace(increment_epoch=lambda: None)
    state.linker = SimpleNamespace(instantiate=instantiate)
    state.run_store = lambda: replay._replay_case(
        state.engine, object(), state.linker, state.case,
        replay.DEFAULT_FUEL, replay.DEFAULT_TIMEOUT,
    )
    return state


def test_harness_new_stores_each_case_and_timer_finishes_before_next(store_harness: Any) -> None:
    state = store_harness
    first, second = state.run_store(), state.run_store()
    assert first["case_completed"] and second["case_completed"]
    assert first["fuel_consumed"] == second["fuel_consumed"] == 7
    assert state.stores[0] is not state.stores[1]
    for index in range(2):
        assert [entry[1] for entry in state.store_events if entry[0] == index] == [
            "enter", "limits", "fuel", "epoch", "timer-start", "instantiate",
            "timer-finish", "exit",
        ]
    events = state.store_events
    assert events.index((0, "timer-finish")) < events.index((0, "exit")) < events.index((1, "enter"))
    assert replay.STORE_LIMITS == {**replay.provider_replay.STORE_LIMITS, "instances": 128}
    assert replay.DEFAULT_FUEL == replay.provider_replay.DEFAULT_FUEL


@pytest.mark.parametrize("phase", ["epoch-deadline", "fuel-accounting", "store-cleanup"])
def test_harness_late_case_failure_retains_result_and_revokes_success(
    store_harness: Any, phase: str,
) -> None:
    state = store_harness
    state.store_failure = phase
    record = state.run_store()
    assert record["result_before_runner_failure"] == state.case["expected"]
    assert record["runner_failures"][0]["phase"] == phase
    assert record["case_completed"] is False
    assert record["expected_result_matched"] is False
    assert record["guest_calls_returned"] == replay.CALL_ORDER


@pytest.mark.parametrize("mutation", ["extra-import", "residual-bridge", "wasi", "missing-import",
                                      "extra-export", "private-export", "wrong-version",
                                      "wrong-function", "extra-param", "extra-error-field",
                                      "error-enum-order", "nested-resource"])
def test_exact_composed_wit_structure_rejects_all_drift(mutation: str) -> None:
    shape = copy.deepcopy(replay.expected_surface())
    replay.validate_surface(shape)
    if mutation == "extra-import":
        shape["imports"]["extra"] = {}
    elif mutation == "residual-bridge":
        shape["imports"][replay.PROVIDER] = shape["exports"][replay.PROVIDER]
    elif mutation == "wasi":
        shape["imports"]["wasi:cli/environment@0.2.0"] = {}
    elif mutation == "missing-import":
        shape["imports"].pop(next(iter(shape["imports"])))
    elif mutation in {"extra-export", "private-export"}:
        shape["exports"]["extra" if mutation == "extra-export" else "_python_init"] = {}
    elif mutation == "wrong-version":
        shape["exports"][replay.PROVIDER.replace("2.0.0", "1.0.0")] = shape["exports"].pop(replay.PROVIDER)
    elif mutation == "wrong-function":
        shape["exports"][replay.CALLER]["generate-intents"] = ("func", (), "u64")
    elif mutation == "extra-param":
        function = shape["exports"][replay.CALLER]["stream-intents"]
        shape["exports"][replay.CALLER]["stream-intents"] = (function[0], (*function[1], ("extra", "string")), function[2])
    elif mutation == "extra-error-field":
        shape["exports"][replay.CALLER]["error"] = ("record", (("message", "string"), ("extra", "string")))
    elif mutation == "error-enum-order":
        shape["imports"]["tobkiri:core/types@1.0.0"]["error-code"] = ("enum", ("internal", "invalid"))
    else:
        shape["imports"]["tobkiri:core/flow@1.0.0"]["stream-handle"] = ("own", "resource")
    with pytest.raises(ValueError, match="exact reviewed WIT"):
        replay.validate_surface(shape)


@pytest.fixture
def composition_harness(source: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Double compiler/runtime APIs, not composition, native, or guest proof."""
    shape = replay.expected_surface()
    state = SimpleNamespace(commands=[], failure=None, validations=0, source=source,
                            surface=shape)
    wac = tmp_path / "fake-wac"
    wac.write_bytes(b"inert WAC harness bytes, not executable")
    pins = {"wac_sha256": replay.digest(wac.read_bytes())}
    evidence = {"caller": {"binary": b"inert caller", "stage_sha256": "a",
                           "plan_sha256": "b", "source_manifest_sha256": "c"},
                "provider": {"binary": b"inert provider"}, "source": source,
                "provider_33_oracle_sha256": "d"}

    def validate(*args: Any) -> dict[str, Any]:
        state.validations += 1
        if state.failure == "late-evidence" and state.validations == 2:
            raise ValueError("evidence drift after actual-return observations")
        return copy.deepcopy(evidence)

    def compose(command: list[str], **kwargs: Any) -> Any:
        state.commands.append(command)
        cwd = kwargs["cwd"]
        absent = Path(command[command.index("--deps-dir") + 1])
        assert not absent.exists() and absent.parent == cwd
        assert (cwd / "replay.wac").read_bytes() == replay.WAC_SOURCE
        assert (cwd / "caller.wasm").read_bytes() == b"inert caller"
        assert (cwd / "provider.wasm").read_bytes() == b"inert provider"
        Path(command[-1]).write_bytes(b"inert composed harness bytes")
        return SimpleNamespace(stderr="")

    class Component:
        def __init__(self, engine: Any, binary: bytes) -> None:
            del engine
            assert binary == b"inert composed harness bytes"
            self.type = SimpleNamespace(imports=lambda engine: shape["imports"],
                                        exports=lambda engine: shape["exports"])

    class Linker:
        def __init__(self, engine: Any) -> None:
            del engine

        def define_unknown_imports_as_traps(self, component: Any) -> None:
            state.traps_defined = True

    wasm = ModuleType("wasmtime")
    wasm.Config = lambda: SimpleNamespace()

    def engine(config: Any) -> Any:
        state.engine_config = vars(config).copy()
        state.reservation_before_engine = state.result.get("engine_memory_reservation_bytes")
        if state.failure == "engine-creation":
            raise RuntimeError("harness engine creation failed")
        return SimpleNamespace()

    wasm.Engine = engine
    component = ModuleType("wasmtime.component")
    component.Component, component.Linker = Component, Linker
    monkeypatch.setitem(sys.modules, "wasmtime", wasm)
    monkeypatch.setitem(sys.modules, "wasmtime.component", component)
    monkeypatch.setattr(replay, "version", lambda name: "48.0.0")
    monkeypatch.setattr(replay, "validate_evidence", validate)
    monkeypatch.setattr(replay.subprocess, "run", compose)
    monkeypatch.setattr(replay, "_type_shape", lambda value, engine: value)
    monkeypatch.setattr(replay, "_replay_case", lambda engine, component, linker, case, fuel, timeout: _completed(case))
    state.result = replay._base_result()
    state.run = lambda: replay._compose_and_replay(tmp_path / "caller", tmp_path / "provider",
                                                  ORACLE, pins, wac, result=state.result)
    return state


def test_composition_harness_uses_only_exact_private_graph_and_trapping_imports(
    composition_harness: Any,
) -> None:
    state = composition_harness
    result = state.run()
    assert result["replay_succeeded"] is True  # Harness comparison only, never persisted.
    assert state.traps_defined is True
    assert len(state.commands) == 1
    command = state.commands[0]
    assert command[1] == "compose"
    assert command[3] == command[5] == "--dep"
    assert command[4].startswith("local:caller=")
    assert command[6].startswith("local:provider=")
    assert command[7] == "--deps-dir" and command[9] == "-o"
    assert len(command) == 11
    assert all(result[key] is False for key in replay.NO_AUTHORITY)
    assert result["compilation_wall_time_bounded"] is False


def test_late_evidence_error_keeps_completed_observations(composition_harness: Any) -> None:
    state = composition_harness
    state.failure = "late-evidence"
    with pytest.raises(ValueError, match="evidence drift"):
        state.run()
    assert len(state.result["observations"]) == 30
    assert state.result["replay_succeeded"] is False
    assert state.result["wasm_execution_performed"] is True
    assert state.result["wasm_execution_attempted"] is True


def test_runner_helper_drift_fails_after_retaining_observations(
    composition_harness: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = replay.runner_bindings()
    values = iter([{**bindings, "helper": "before"},
                   {**bindings, "helper": "after"}])
    monkeypatch.setattr(replay, "runner_bindings", lambda: next(values))
    with pytest.raises(ValueError, match="runner source closure changed"):
        composition_harness.run()
    assert len(composition_harness.result["observations"]) == 30
    assert composition_harness.result["replay_succeeded"] is False


def test_main_late_failure_never_relabels_returned_calls_as_no_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Any,
) -> None:
    pins, output = tmp_path / "pins.json", tmp_path / "result.json"
    pins.write_text("{}")

    def fail(*args: Any, result: dict[str, Any], **kwargs: Any) -> None:
        result["observations"] = [_completed(source["cases"][0])]
        result.update(replay.summarize(source["cases"], result["observations"]))
        raise RuntimeError("late metadata failure")

    monkeypatch.setattr(replay, "_compose_and_replay", fail)
    monkeypatch.setattr(sys, "argv", ["replay", "--caller", "unused", "--provider", "unused",
                                     "--oracle", str(ORACLE), "--pins", str(pins),
                                     "--wac", "unused", "--output", str(output)])
    assert replay.main() == 1
    result = json.loads(output.read_bytes())
    assert result["wasm_execution_performed"] is True
    assert result["wasm_execution_attempted"] is True
    assert len(result["observations"]) == 1
    assert result["replay_succeeded"] is False
    assert result["runner_failure"]["message"] == "late metadata failure"


@pytest.mark.parametrize("name", ["provider", "caller"])
@pytest.mark.parametrize("post_return_failure", [False, True])
def test_harness_typed_init_error_is_captured_before_canonical_cleanup(
    harness: Any, name: str, post_return_failure: bool,
) -> None:
    label = f"{name}.init"
    harness.results[label] = HarnessRecord("denied", "fixture initialization denied")
    if post_return_failure:
        harness.cleanup_failures[label] = RuntimeError("init error post-return failed")
    harness.run()
    assert harness.record["lifecycle_results"][label] == {
        "status": "error", "code": "denied", "message": "fixture initialization denied",
    }
    assert harness.record["lifecycle"][name]["init"] is False
    assert f"{name}.shutdown" not in harness.record["guest_calls_attempted"]
    assert harness.record["expected_result_matched"] is False
    assert label in [event[1] for event in harness.events if event[0] == "post-return"]


def test_type_inspection_harness_preserves_names_order_and_rejects_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only labelled introspection class doubles; no native types are constructed."""
    types = ModuleType("wasmtime.component")
    names = ("String", "U32", "U64", "U8", "Bool", "ComponentInstanceType",
             "FuncType", "RecordType", "EnumType", "VariantType", "ResultType",
             "ListType")
    for name in names:
        setattr(types, name, type(name, (), {}))
    wasm = ModuleType("wasmtime")
    wasm.component = types
    monkeypatch.setitem(sys.modules, "wasmtime", wasm)
    monkeypatch.setitem(sys.modules, "wasmtime.component", types)
    enum = types.EnumType()
    enum.names = ["denied", "invalid"]
    record = types.RecordType()
    record.fields = [("code", enum), ("message", types.String())]
    result = types.ResultType()
    result.ok, result.err = types.String(), record
    function = types.FuncType()
    function.params, function.result = [("payload", types.String())], result
    instance = types.ComponentInstanceType()
    instance.exports = lambda engine: {"normalize": function}
    assert replay._type_shape(instance, None) == {
        "normalize": ("func", (("payload", "string"),), (
            "result", "string", ("record", (
                ("code", ("enum", ("denied", "invalid"))), ("message", "string"),
            )),
        )),
    }
    variant = types.VariantType()
    variant.cases = [("pass", None), ("deny", types.String())]
    assert replay._type_shape(variant, None) == ("variant", (("pass", None), ("deny", "string")))
    sequence = types.ListType()
    sequence.element = types.U8()
    assert replay._type_shape(sequence, None) == ("list", "u8")
    for name, expected in (("U32", "u32"), ("U64", "u64"), ("Bool", "bool")):
        assert replay._type_shape(getattr(types, name)(), None) == expected
    with pytest.raises(ValueError, match="unexpected component type/resource"):
        replay._type_shape(object(), None)


def test_surface_mismatch_retains_exact_bounded_diagnostics_before_any_call(
    composition_harness: Any,
) -> None:
    state = composition_harness
    state.surface["exports"][replay.CALLER]["unexpected"] = ("func", (), None)
    with pytest.raises(ValueError, match="exact reviewed WIT"):
        state.run()
    result = state.result
    assert result["typed_surface"] == state.surface
    encoded = observer.canonical(state.surface)
    assert result["typed_surface_sha256"] == replay.digest(encoded)
    assert result["typed_surface_bytes"] == len(encoded)
    assert result["typed_surface_validated"] is False
    assert result["composition_sha256"] == replay.digest(b"inert composed harness bytes")
    assert result["composition_bytes"] == len(b"inert composed harness bytes")
    assert result["observations"] == []
    assert result["replay_succeeded"] is False
    assert result["wasm_execution_attempted"] is False
    assert result["wasm_execution_performed"] is False
    assert not hasattr(state, "traps_defined")
    assert all(result[key] is False for key in replay.NO_AUTHORITY)


def test_oversized_surface_diagnostics_fail_closed_without_unbounded_output() -> None:
    surface = {"oversized": "x" * (256 * 1024)}
    result = replay._base_result()
    with pytest.raises(ValueError, match="diagnostic byte bound"):
        replay.record_surface_diagnostics(result, surface)
    encoded = observer.canonical(surface)
    assert "typed_surface" not in result
    assert result["typed_surface_sha256"] == replay.digest(encoded)
    assert result["typed_surface_bytes"] == len(encoded)
    assert result["typed_surface_validated"] is False
    assert result["replay_succeeded"] is False


def test_actual_pre_export_diagnostic_matches_exact_reachable_abi() -> None:
    path = ROOT / "tests/fixtures/issue1520/gateway-tool-caller.typed-surface.json"
    fixture = json.loads(path.read_bytes())
    assert fixture["source_report_sha256"] == "cf1a74734db7cf8709b1bdc9bfb4824383beb916e65f19e31cf8956767a4d5ef"
    assert fixture["composition_sha256"] == "2530b6a090f6584cfc6ffeef131b070af429c0b120381be463c767f47bb2fe67"
    assert fixture["composition_bytes"] == 35761422
    assert fixture["typed_surface_validated"] is False
    assert fixture["runner_failure"]["message"] == "composition typed surface differs from exact reviewed WIT"
    assert fixture["replay_succeeded"] is False
    assert fixture["wasm_execution_attempted"] is False
    assert fixture["wasm_execution_performed"] is False
    encoded = observer.canonical(fixture["typed_surface"])
    assert replay.digest(encoded) == fixture["typed_surface_sha256"] == "e4a321b6a174e02287c98d4f632cc33b5c4b0b1adf5c2242b989a20d0636a1c9"
    assert len(encoded) == fixture["typed_surface_bytes"] == 6219
    assert encoded == observer.canonical(replay.expected_surface())

    def restore_tuples(value: Any) -> Any:
        if type(value) is list:
            return tuple(restore_tuples(item) for item in value)
        if type(value) is dict:
            return {key: restore_tuples(item) for key, item in value.items()}
        return value

    replay.validate_surface(restore_tuples(fixture["typed_surface"]))
    assert set(replay.expected_surface()["imports"]["tobkiri:core/types@1.0.0"]) == {
        "error-code", "error", "json", "stream-handle", "callback-handle",
    }


@pytest.mark.parametrize("name", ["error-code", "error", "json", "stream-handle",
                                  "callback-handle"])
def test_every_reachable_canonical_type_is_still_required(name: str) -> None:
    shape = replay.expected_surface()
    del shape["imports"]["tobkiri:core/types@1.0.0"][name]
    with pytest.raises(ValueError, match="exact reviewed WIT"):
        replay.validate_surface(shape)


@pytest.mark.parametrize("name", ["risk", "pending", "verdict", "arbitrary-extra"])
def test_unused_or_arbitrary_canonical_type_additions_are_rejected(name: str) -> None:
    shape = replay.expected_surface()
    shape["imports"]["tobkiri:core/types@1.0.0"][name] = "string"
    with pytest.raises(ValueError, match="exact reviewed WIT"):
        replay.validate_surface(shape)


def test_composition_harness_reservation_with_explicit_bounded_instance_allowance(
    composition_harness: Any,
) -> None:
    state = composition_harness
    result = state.run()
    assert state.engine_config == {
        "consume_fuel": True, "epoch_interruption": True,
        "memory_reservation": 128 * 1024 * 1024,
    }
    assert state.reservation_before_engine == 128 * 1024 * 1024
    assert result["engine_memory_reservation_bytes"] == 128 * 1024 * 1024
    assert "virtual reservation only" in result["engine_memory_reservation_scope"]
    assert replay.provider_replay.STORE_LIMITS == {
        "memory_size": 128 * 1024 * 1024, "table_elements": 65536,
        "instances": 64, "tables": 64, "memories": 4,
    }
    assert replay.DEFAULT_FUEL == replay.provider_replay.DEFAULT_FUEL == 2_000_000_000
    assert replay.DEFAULT_TIMEOUT == replay.provider_replay.DEFAULT_TIMEOUT == 30
    assert result["external_process_supervisor_required"] is True
    assert result["compilation_wall_time_bounded"] is False


def test_engine_creation_failure_retains_reservation_diagnostic_without_execution(
    composition_harness: Any,
) -> None:
    state = composition_harness
    state.failure = "engine-creation"
    with pytest.raises(RuntimeError, match="harness engine creation failed"):
        state.run()
    assert state.result["engine_memory_reservation_bytes"] == 128 * 1024 * 1024
    assert state.result["observations"] == []
    assert state.result["wasm_execution_performed"] is False
    assert state.result["replay_succeeded"] is False


def test_only_instance_capacity_differs_for_two_producer_development_fixture() -> None:
    baseline = replay.provider_replay.STORE_LIMITS
    assert replay.STORE_LIMITS is not baseline
    assert baseline == {
        "memory_size": 128 * 1024 * 1024, "table_elements": 65536,
        "instances": 64, "tables": 64, "memories": 4,
    }
    assert set(replay.STORE_LIMITS) == set(baseline)
    changed = {key: (baseline[key], replay.STORE_LIMITS[key]) for key in baseline
               if baseline[key] != replay.STORE_LIMITS[key]}
    assert changed == {"instances": (64, 128)}
    result = replay._base_result()
    assert result["store_limit_difference_from_single_provider"] == {
        "instances": {"single_provider": 64, "two_producer_fixture": 128},
    }
    assert result["instance_limit_scope"] == (
        "bounded two-producer DEVELOPMENT fixture allowance: twice the "
        "single-provider instance cap, not a measured minimum"
    )
    assert replay.DEFAULT_FUEL == replay.provider_replay.DEFAULT_FUEL == 2_000_000_000
    assert replay.DEFAULT_TIMEOUT == replay.provider_replay.DEFAULT_TIMEOUT == 30
    assert all(result[key] is False for key in replay.NO_AUTHORITY)
