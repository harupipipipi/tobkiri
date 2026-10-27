"""Source and labelled replay-harness tests, NOT actual guest/Core evidence.

No test builds, composes, instantiates or executes a component. Inert component
headers and explicit Store/Record/Variant doubles test validation and custody,
not artifact provenance, imported Core dispatch, billing or runtime acceptance.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import observe_gateway_finalizer as observer
import replay_gateway_finalizer as replay


@pytest.fixture
def source_cases() -> list[dict[str, Any]]:
    """Execute actual extracted pure source, not a generated-binding substitute."""
    with observer.source_modules() as modules:
        return [observer.observe_case(case, modules) for case in observer.case_inputs()]


def test_source_catalog_ten_categories_and_exact_dependency_stops(source_cases: Any) -> None:
    assert {key: sum(case["classification"] == key for case in source_cases)
            for key in observer.COUNTS} == observer.COUNTS
    for case in source_cases:
        assert case["classification"] == case["expected_kind"]
        assert case["source_reference_call_count"] == case["expected_dependency_call_count"]
        assert case["source_call_counts_are_not_component_observations"] is True
        assert case["source_error_mappings_are_not_observed_core_errors"] is True
    usage = next(case for case in source_cases if case["id"] == "stream-later-usage-overflow")
    assert [call["provider"] for call in usage["source_reference_calls"]] == [
        "stream-normalizer", "usage-cost", "usage-cost",
    ]
    assert usage["source_bridge_calls"] == []
    tool = next(case for case in source_cases if case["id"] == "stream-later-tool-invalid")
    assert tool["source_bridge_call_count"] == 2
    assert tool["original"]["dependency"]["original"]["type"] == "GlobalContractInvocationError"
    assert tool["expected"] == {"status": "core-error", "code": "invalid",
                                "message": "outside ordered-finite-json-v2 domain"}


def test_source_preserves_nonlist_intents_until_actual_original_bridge(source_cases: Any) -> None:
    case = next(case for case in source_cases if case["id"] == "stream-null-top-level-tools")
    assert json.loads(case["source_reference_calls"][-1]["payload_json"])["intents"] is None
    assert json.loads(case["source_bridge_calls"][-1]["payload_json"])["intents"] is None
    assert json.loads(case["output_json"])["tool_intents"] == []


def test_source_output_keeps_order_and_domain_core_custody_separate(source_cases: Any) -> None:
    success = source_cases[0]
    assert success["output_json"].startswith('{"status":"ok","request_id":')
    assert '"arguments":{"z":1,"a":2}' in success["output_json"]
    domain = next(case for case in source_cases if case["id"] == "generate-domain-invalid")
    assert domain["expected"] == {"status": "domain-rejected", "code": "invalid",
                                  "message": "original domain invalid"}
    assert domain["source_reference_calls"] == []


def test_source_observer_requires_actual_reference_runtime_before_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(observer, "runtime", lambda: {**observer.EXPECTED_RUNTIME,
                                                    "python": "3.13.16"})
    monkeypatch.setattr(observer, "source_bindings", lambda: pytest.fail("read sources too soon"))
    with pytest.raises(ValueError, match="CPython 3.13.5"):
        observer.observe()


def test_source_module_loading_restores_prior_import_state() -> None:
    before = {name: sys.modules.get(name) for name in (
        "ordered_json", "ordered_json_v2", "_finalizer_source_finalizer",
    )}
    with observer.source_modules() as modules:
        assert modules["finalizer"].Candidate.__module__ == "_finalizer_source_finalizer"
        assert "wit_world" not in modules
    assert {name: sys.modules.get(name) for name in before} == before


def test_source_bindings_cover_all_actual_original_inputs_and_observer() -> None:
    bindings = observer.source_bindings()
    paths = {item["path"] for item in bindings}
    assert set(observer.SOURCE_MODULES.values()) <= paths
    assert "scripts/wasm/observe_gateway_finalizer.py" in paths
    assert observer.CATALOG.relative_to(ROOT).as_posix() in paths
    for item in bindings:
        data = (ROOT / item["path"]).read_bytes()
        assert len(data) == item["bytes"] and observer.digest(data) == item["sha256"]


@pytest.fixture
def source_document(source_cases: Any) -> dict[str, Any]:
    """Structural validator fixture, NOT a CPython3.13.5 observation receipt."""
    bindings = observer.source_bindings()
    return {
        "format": "tobkiri.gateway-finalizer-source-observations/1",
        "protocol": "ordered-finite-json-v2", "runtime": observer.EXPECTED_RUNTIME,
        "source_bindings": bindings,
        "source_bindings_sha256": observer.digest(observer.canonical(bindings)),
        "case_inputs_sha256": observer.digest(observer.canonical(observer.case_inputs())),
        "counts": observer.COUNTS.copy(), "cases": source_cases,
        **observer.NO_EXECUTION,
    }


@pytest.mark.parametrize("mutation", ["runtime", "bindings", "inputs", "missing", "order",
                                      "output", "domain-to-core", "core-to-domain", "counts",
                                      "call-count", "source-claim", "production"])
def test_reforged_source_document_hash_cannot_hide_drift(
    source_document: Any, tmp_path: Path, mutation: str,
) -> None:
    document = source_document
    if mutation == "runtime":
        document["runtime"] = {**document["runtime"], "unicode": "15.0.0"}
    elif mutation == "bindings":
        document["source_bindings"][0]["sha256"] = "0" * 64
    elif mutation == "inputs":
        document["cases"][0]["input_json"] = "{}"
    elif mutation == "missing":
        document["cases"].pop()
    elif mutation == "order":
        document["cases"].reverse()
    elif mutation == "output":
        document["cases"][0]["output_json"] = "{}"
    elif mutation in ("domain-to-core", "core-to-domain"):
        old, new = (("domain-rejected", "core-error") if mutation == "domain-to-core"
                    else ("core-error", "domain-rejected"))
        next(case for case in document["cases"] if case["classification"] == old)["expected"]["status"] = new
    elif mutation == "counts":
        document["counts"]["finalized"] += 1
    elif mutation == "call-count":
        document["cases"][0]["source_reference_call_count"] += 1
    elif mutation == "source-claim":
        document["cases"][0]["source_error_mappings_are_not_observed_core_errors"] = False
    else:
        document["production_enabled"] = True
    path = tmp_path / "structural-only.json"
    raw = observer.json_bytes(document)
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        replay.validate_observations(path, replay.digest(raw))


@pytest.fixture(params=tuple(replay.INTERFACES))
def component_receipt(request: Any, tmp_path: Path) -> Any:
    """Inert header plus fabricated test fields, never a real producer receipt."""
    name = request.param
    staged = tmp_path / "stage"
    plan = replay._stage(name, staged)
    binary = b"\x00asm\x0d\x00\x01\x00INERT STRUCTURAL TEST DATA"
    artifact = tmp_path / "test-component.wasm"
    artifact.write_bytes(binary)
    manifest = replay._manifest(staged, plan)
    receipt = {
        "format": "tobkiri.component-build/2", "language": "python",
        "build_mode": "hash-bound-custom-python313-producer-trial",
        "production_enabled": False, "guest_runtime_identity_verified": False,
        "compiler_executable_sha256": "1" * 64,
        "producer_trial_receipt_sha256": "2" * 64,
        "artifact_sha256": replay.digest(binary), "artifact_bytes": len(binary),
        "source_manifest": manifest,
        "source_manifest_sha256": replay.digest(json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()),
        "build_plan_sha256": replay.digest((staged / "plan.json").read_bytes()),
        "world": plan["world"], "imports": sorted(plan["allowed_imports"]),
        "exports": sorted(plan["outer_exports"]),
    }
    raw = observer.json_bytes(receipt)
    artifact.with_suffix(".build.json").write_bytes(raw)
    pins = {"artifact_sha256": replay.digest(binary), "receipt_sha256": replay.digest(raw)}
    return name, artifact, receipt, pins


def test_all_five_receipts_bind_complete_current_plans(component_receipt: Any) -> None:
    name, artifact, _, pins = component_receipt
    evidence = replay.validate_component(name, artifact, pins)
    assert evidence["binary"] == artifact.read_bytes()
    assert "wasm_execution_performed" not in evidence


@pytest.mark.parametrize("field", ["artifact_bytes", "source_manifest", "source_manifest_sha256",
                                   "build_plan_sha256", "imports", "exports", "production_enabled",
                                   "compiler_executable_sha256", "producer_trial_receipt_sha256"])
def test_receipt_reforging_cannot_hide_source_plan_drift(
    component_receipt: Any, field: str,
) -> None:
    name, artifact, receipt, pins = component_receipt
    receipt[field] = "changed"
    raw = observer.json_bytes(receipt)
    artifact.with_suffix(".build.json").write_bytes(raw)
    pins["receipt_sha256"] = replay.digest(raw)
    with pytest.raises(ValueError):
        replay.validate_component(name, artifact, pins)


def test_all_component_input_hashes_and_sizes_are_required(component_receipt: Any) -> None:
    name, artifact, receipt, pins = component_receipt
    for index, field in ((0, "sha256"), (-1, "bytes")):
        changed = copy.deepcopy(receipt)
        changed["source_manifest"][index][field] = "changed"
        changed["source_manifest_sha256"] = replay.digest(json.dumps(
            changed["source_manifest"], sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode())
        raw = observer.json_bytes(changed)
        artifact.with_suffix(".build.json").write_bytes(raw)
        pins["receipt_sha256"] = replay.digest(raw)
        with pytest.raises(ValueError, match="complete reviewed stage plan"):
            replay.validate_component(name, artifact, pins)


@dataclass
class HarnessVariant:
    """Labelled Wasmtime lifted variant double, not an actual guest return."""
    tag: str
    payload: Any = None


class HarnessRecord:
    """Labelled lifted record double with separate domain/Core custody."""
    def __init__(self, code: Any, message: Any) -> None:
        self.code, self.message = code, message


def test_outer_result_tag_disambiguates_identical_domain_and_core_records() -> None:
    record = HarnessRecord("invalid", "same text")
    assert replay._typed_result(HarnessVariant("ok", record), HarnessRecord, HarnessVariant) == {
        "status": "domain-rejected", "code": "invalid", "message": "same text",
    }
    assert replay._typed_result(HarnessVariant("err", record), HarnessRecord, HarnessVariant) == {
        "status": "core-error", "code": "invalid", "message": "same text",
    }


@pytest.mark.parametrize("value", [None, "{}", {}, HarnessRecord("invalid", "text"),
                                   HarnessVariant("finalized", "{}"),
                                   HarnessVariant("ok", HarnessVariant("finalized", "{}")),
                                   HarnessVariant("err", HarnessRecord("domain-only", "text")),
                                   HarnessVariant("ok", HarnessRecord(1, "text")),
                                   HarnessVariant("ok", '{"n":NaN}')])
def test_lifted_result_shape_is_exact(value: Any) -> None:
    with pytest.raises(ValueError):
        replay._typed_result(value, HarnessRecord, HarnessVariant)


class HarnessFunction:
    """Function/post-return double, not generated bindings or a guest engine."""
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
def harness(source_cases: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    case = copy.deepcopy(source_cases[0])
    state = SimpleNamespace(events=[], failures={}, cleanup_failures={}, results={},
                            case=case, record=replay._case_record(case))
    interfaces = {interface: name for name, interface in replay.INTERFACES.items()}
    interfaces.update({interface: name for name, interface in replay.LIFECYCLES.items()})

    def export(instance: Any, store: Any, interface: str, name: str) -> HarnessFunction:
        del instance, store
        label, result = f"{interfaces[interface]}.{name}", None
        if name == "runtime-info":
            result = json.dumps(replay.EXPECTED_RUNTIME)
        elif interface == replay.INTERFACES["finalizer"] and name == case["operation"]:
            label, result = "finalizer.operation", HarnessVariant("ok", case["expected"].get("output_json", "{}"))
        return HarnessFunction(label, state, result)

    monkeypatch.setattr(replay.runtime_helpers, "_export", export)
    state.run = lambda: replay._run_case_in_store(
        state.record, object(), object(), state.case, HarnessRecord, HarnessVariant,
    )
    return state


def test_five_lifecycles_and_every_post_return_have_exact_order(harness: Any) -> None:
    harness.run()
    for key in ("guest_calls_attempted", "guest_calls_returned", "post_return_attempted",
                "post_return_completed"):
        assert harness.record[key] == replay.CALL_ORDER
    assert harness.record["actual"] == harness.case["expected"]
    assert harness.record["guest_runtime"] == {
        name: replay.EXPECTED_RUNTIME for name in replay.INTERFACES
    }


@pytest.mark.parametrize("name", tuple(replay.INTERFACES))
def test_each_init_post_return_failure_still_owns_shutdown(harness: Any, name: str) -> None:
    harness.cleanup_failures[f"{name}.init"] = RuntimeError("init post-return")
    harness.run()
    assert f"{name}.shutdown" in harness.record["guest_calls_attempted"]
    assert harness.record["lifecycle"][name]["init"] is False
    assert harness.record["expected_result_matched"] is False
    assert f"{name}.init" not in harness.record["post_return_completed"]


def test_primary_and_five_shutdown_failures_are_all_retained(harness: Any) -> None:
    harness.failures["finalizer.operation"] = RuntimeError("primary")
    for name in replay.INTERFACES:
        harness.failures[f"{name}.shutdown"] = RuntimeError(name)
    harness.run()
    assert [item["message"] for item in harness.record["runner_failures"]] == [
        "primary", *reversed(replay.INTERFACES),
    ]
    assert "finalizer.operation" not in harness.record["post_return_attempted"]
    assert harness.record["guest_calls_attempted"].count("finalizer.operation") == 1


def test_bad_lift_and_failed_post_return_preserve_both_errors(harness: Any) -> None:
    harness.results["finalizer.operation"] = object()
    harness.cleanup_failures["finalizer.operation"] = RuntimeError("post-return")
    harness.run()
    failures = harness.record["runner_failures"]
    assert failures[0]["type"] == "ValueError"
    assert failures[1]["phase"] == "finalizer.operation-post-return"
    assert harness.record["guest_calls_attempted"][-5:] == replay.CALL_ORDER[-5:]


@pytest.mark.parametrize("kind,tag", [("domain-rejected", "ok"), ("core-error", "err")])
def test_errors_receive_one_post_return_and_never_retry(harness: Any, kind: str, tag: str) -> None:
    harness.case.update(classification=kind,
                        expected={"status": kind, "code": "invalid", "message": "exact"})
    harness.results["finalizer.operation"] = HarnessVariant(tag, HarnessRecord("invalid", "exact"))
    harness.run()
    assert harness.record["actual"] == harness.case["expected"]
    assert harness.record["post_return_completed"].count("finalizer.operation") == 1
    assert harness.record["guest_calls_attempted"].count("finalizer.operation") == 1
    assert harness.record["domain_rejection_parity"] is (kind == "domain-rejected")
    assert harness.record["typed_error_custody"] is (kind == "core-error")


@pytest.fixture
def store_harness(harness: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Store/timer doubles cannot execute components or establish Wasm evidence."""
    state = harness
    state.store_events, state.store_failure, state.stores = [], None, []

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
                raise RuntimeError("store cleanup")
        def set_limits(self, **limits: int) -> None:
            state.store_events.append((self.index, "limits", limits))
        def set_fuel(self, fuel: int) -> None:
            state.store_events.append((self.index, "fuel", fuel))
        def set_epoch_deadline(self, deadline: int) -> None:
            state.store_events.append((self.index, "epoch", deadline))
        def get_fuel(self) -> int:
            if state.store_failure == "fuel-accounting":
                raise RuntimeError("fuel accounting")
            return replay.DEFAULT_FUEL - 9

    class Timer:
        def __init__(self, timeout: float, increment: Any) -> None:
            del increment
            assert timeout == replay.DEFAULT_TIMEOUT
            self.index = len(state.stores) - 1
        def start(self) -> None:
            state.store_events.append((self.index, "timer-start"))
        def finish(self) -> None:
            state.store_events.append((self.index, "timer-finish"))
            if state.store_failure == "epoch-deadline":
                raise TimeoutError("whole case deadline")

    def instantiate(store: Any, component: Any) -> object:
        del component
        state.store_events.append((store.index, "instantiate"))
        if state.store_failure == "instantiate":
            raise RuntimeError("instantiation failed")
        return object()

    wasm, component = ModuleType("wasmtime"), ModuleType("wasmtime.component")
    wasm.Store = Store
    component.Record, component.Variant = HarnessRecord, HarnessVariant
    monkeypatch.setitem(sys.modules, "wasmtime", wasm)
    monkeypatch.setitem(sys.modules, "wasmtime.component", component)
    monkeypatch.setattr(replay.runtime_helpers, "_EpochTimer", Timer)
    state.engine = SimpleNamespace(increment_epoch=lambda: None)
    state.linker = SimpleNamespace(instantiate=instantiate)
    state.run_store = lambda: replay._replay_case(
        state.engine, object(), state.linker, state.case, replay.DEFAULT_FUEL,
        replay.DEFAULT_TIMEOUT,
    )
    return state


def test_whole_case_timer_starts_before_instantiation_and_stores_are_fresh(store_harness: Any) -> None:
    state = store_harness
    records = [state.run_store(), state.run_store()]
    assert all(record["case_completed"] for record in records)
    assert state.stores[0] is not state.stores[1]
    for index in range(2):
        assert [entry[1] for entry in state.store_events if entry[0] == index] == [
            "enter", "limits", "fuel", "epoch", "timer-start", "instantiate",
            "timer-finish", "exit",
        ]
    assert replay.STORE_LIMITS == {
        **replay.runtime_helpers.STORE_LIMITS, "instances": 320, "memories": 8,
    }
    assert replay.STORE_LIMITS["memory_size"] == 128 * 1024 * 1024
    assert replay.DEFAULT_FUEL == 2_000_000_000 and replay.DEFAULT_TIMEOUT == 30


@pytest.mark.parametrize("phase", ["instantiate", "fuel-accounting", "epoch-deadline", "store-cleanup"])
def test_whole_case_failures_revoke_completion(store_harness: Any, phase: str) -> None:
    store_harness.store_failure = phase
    record = store_harness.run_store()
    assert record["runner_failures"][0]["phase"] == phase
    assert record["case_completed"] is False
    assert record["expected_result_matched"] is False
    if phase == "instantiate":
        assert record["instantiation_attempted"] is True
        assert record["instantiation_returned"] is False
        assert record["guest_calls_attempted"] == []
    else:
        assert record["result_before_runner_failure"] == store_harness.case["expected"]


def _completed(case: dict[str, Any]) -> dict[str, Any]:
    return {
        **replay._case_record(case), "actual": case["expected"],
        **replay.comparison_flags(case, case["expected"]),
        "case_completed": True, "instantiation_returned": True,
        **{key: replay.CALL_ORDER.copy() for key in (
            "guest_calls_attempted", "guest_calls_returned", "post_return_attempted",
            "post_return_completed")},
        "guest_runtime": {name: replay.EXPECTED_RUNTIME.copy() for name in replay.INTERFACES},
        "lifecycle": {name: {"init": True, "shutdown": True} for name in replay.INTERFACES},
    }


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "order", "classification",
                                      "actual", "failure", "incomplete", "post-return",
                                      "instantiation", *replay.INTERFACES])
def test_summary_never_promotes_incomplete_or_reforged_harness_results(
    source_cases: Any, mutation: str,
) -> None:
    observations = [_completed(case) for case in source_cases]
    assert replay.summarize(source_cases, observations)["replay_succeeded"] is True
    if mutation == "missing":
        observations.pop()
    elif mutation == "duplicate":
        observations[-1] = copy.deepcopy(observations[0])
    elif mutation == "order":
        observations.reverse()
    elif mutation == "classification":
        observations[0]["classification"] = "core-error"
    elif mutation == "actual":
        observations[0]["actual"] = {"status": "finalized", "output_json": "{}"}
    elif mutation == "failure":
        observations[0]["runner_failures"] = [{"type": "TimeoutError"}]
    elif mutation == "incomplete":
        observations[0]["case_completed"] = False
    elif mutation == "post-return":
        observations[0]["post_return_completed"].pop()
    elif mutation == "instantiation":
        observations[0]["instantiation_returned"] = False
    else:
        observations[0]["guest_runtime"][mutation]["python"] = "3.13.5"
    assert replay.summarize(source_cases, observations)["replay_succeeded"] is False


@pytest.mark.parametrize("mutation", ["extra-import", "wasi", "residual-caller", "missing-import",
                                      "extra-export", "missing-lifecycle", "domain-fields",
                                      "outcome-order", "flattened-result", "changed-param",
                                      "private-init", "core-error-order"])
def test_exact_composed_surface_rejects_all_structural_drift(mutation: str) -> None:
    shape = copy.deepcopy(replay.expected_surface())
    replay.validate_surface(shape)
    finalizer = shape["exports"][replay.INTERFACES["finalizer"]]
    if mutation == "extra-import":
        shape["imports"]["extra"] = {}
    elif mutation == "wasi":
        shape["imports"]["wasi:cli/environment@0.2.0"] = {}
    elif mutation == "residual-caller":
        shape["imports"][replay.INTERFACES["caller"]] = {}
    elif mutation == "missing-import":
        shape["imports"].pop(next(iter(shape["imports"])))
    elif mutation == "extra-export":
        shape["exports"]["extra"] = {}
    elif mutation == "missing-lifecycle":
        shape["exports"].pop(replay.LIFECYCLES["finalizer"])
    elif mutation == "domain-fields":
        finalizer["domain-error"] = ("record", (("message", "string"), ("code", "string")))
    elif mutation == "outcome-order":
        finalizer["finalization-outcome"] = ("variant", tuple(reversed(finalizer["finalization-outcome"][1])))
    elif mutation == "flattened-result":
        finalizer["generate"] = ("func", (("payload", "string"),), ("result", "string", finalizer["error"]))
    elif mutation == "changed-param":
        finalizer["stream"] = ("func", (("payload", "u64"),), finalizer["stream"][2])
    elif mutation == "private-init":
        shape["exports"]["_python_init"] = ("func", (), None)
    else:
        shape["imports"]["tobkiri:core/types@1.0.0"]["error-code"] = ("enum", ("internal", "invalid"))
    with pytest.raises(ValueError, match="exact reviewed WIT"):
        replay.validate_surface(shape)


def test_fixed_wac_graph_has_real_dependencies_and_five_lifecycle_exports() -> None:
    source = replay.WAC_SOURCE.decode()
    assert source.count(" = new local:") == 5
    for name, identity in replay.INTERFACES.items():
        assert identity in source
        assert f'as "{replay.LIFECYCLES[name]}"' in source
        assert identity not in replay.ALLOWED_IMPORTS
    assert len(replay.ALLOWED_IMPORTS) == 5 and len(replay.EXPECTED_EXPORTS) == 10
    assert '"local:ai-tool-bridge-ordered-python/bridge@2.0.0": bridge[' in source
    assert '"local:ai-usage-cost-python/usage-cost@1.0.0": usage[' in source


def test_empty_or_instantiation_only_receipts_never_claim_export_execution() -> None:
    result = replay._base_result()
    assert all(result[key] is False for key in replay.NO_AUTHORITY)
    assert result["replay_succeeded"] is False
    assert result["wasm_execution_performed"] is False
    assert result["compilation_bounded_by_guest_fuel_or_epoch"] is False
    record = {"instantiation_attempted": True, "instantiation_returned": True}
    assert replay.summarize([], [record])["wasm_execution_performed"] is False


def test_explicit_new_pins_require_runner_and_composition_identity() -> None:
    pins = {
        **{name: {"artifact_sha256": "1" * 64, "receipt_sha256": "2" * 64}
           for name in replay.INTERFACES},
        "source_observations_sha256": "3" * 64, "wac_sha256": "4" * 64,
        "runner_source_bindings_sha256": replay.digest(observer.canonical(replay.runner_bindings())),
        "composition_wac_sha256": replay.digest(replay.WAC_SOURCE),
    }
    replay.validate_pins(pins)
    for key in pins:
        changed = copy.deepcopy(pins)
        changed.pop(key)
        with pytest.raises(ValueError):
            replay.validate_pins(changed)
    pins["composition_wac_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="fixed five-component"):
        replay.validate_pins(pins)


def test_exact_reference_interpreter_observes_and_validates_current_source(tmp_path: Path) -> None:
    if observer.runtime() != observer.EXPECTED_RUNTIME:
        pytest.skip("actual source observations require CPython 3.13.5 / Unicode15.1")
    document = observer.observe()
    raw = observer.json_bytes(document)
    path = tmp_path / "actual-source-only.json"
    path.write_bytes(raw)
    returned, validated = replay.validate_observations(path, replay.digest(raw))
    assert returned == raw and validated == document
    assert all(document[key] is False for key in observer.NO_EXECUTION)


@pytest.fixture
def composition_harness(
    source_cases: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Double composition/compiler/runtime APIs without invoking any executable.

    Successful flags here describe only labelled harness records. Neither these
    inert bytes nor the in-memory result are actual composition/guest evidence.
    """
    source = {"cases": source_cases, "runtime": observer.EXPECTED_RUNTIME.copy()}
    state = SimpleNamespace(
        commands=[], events=[], failure=None, validations=0,
        surface=copy.deepcopy(replay.expected_surface()),
        result=replay._base_result(),
    )
    wac = tmp_path / "inert-wac"
    wac.write_bytes(b"INERT WAC TEST DATA; NOT AN EXECUTABLE")
    state.wac = wac
    pins = {"wac_sha256": replay.digest(wac.read_bytes())}
    artifacts = {name: tmp_path / f"unused-{name}.wasm"
                 for name in replay.INTERFACES}
    evidence = {
        "components": {name: {
            "binary": f"INERT {name} TEST DATA".encode(),
            "receipt_bytes": b"INERT RECEIPT TEST DATA",
            "stage_sha256": "test-stage", "plan_sha256": "test-plan",
            "source_manifest_sha256": "test-manifest",
            "producer_trial_receipt_sha256": "test-producer",
        } for name in replay.INTERFACES},
        "source": source, "source_bytes": observer.json_bytes(source),
    }
    oracle = tmp_path / "unused-source-observations.json"

    def validate(paths: Any, source_path: Path, identities: Any) -> dict[str, Any]:
        assert paths == artifacts and source_path == oracle and identities == pins
        state.validations += 1
        state.events.append(f"validate-evidence-{state.validations}")
        if state.validations == 2 and state.failure == "late-evidence-raise":
            raise ValueError("harness evidence validation failed after observations")
        result = copy.deepcopy(evidence)
        if state.validations == 2 and state.failure == "late-evidence-diff":
            result["source_bytes"] += b"changed"
        return result

    def compose(command: list[str], **kwargs: Any) -> Any:
        state.commands.append(command.copy())
        state.events.append("compose")
        workspace = kwargs["cwd"]
        assert kwargs == {
            "check": True, "capture_output": True, "text": True, "cwd": workspace,
        }
        assert (workspace / "replay.wac").read_bytes() == replay.WAC_SOURCE
        # The runner executes its captured pinned tool path, never the original
        # mutable path. This harness only reads it, not executes it.
        assert Path(command[0]) == workspace / "wac"
        assert Path(command[0]).read_bytes() == wac.read_bytes()
        expected = [str(workspace / "wac"), "compose",
                    str(workspace / "replay.wac")]
        for name in replay.INTERFACES:
            component_path = workspace / f"{name}.wasm"
            assert component_path.read_bytes() == evidence["components"][name]["binary"]
            expected.extend(["--dep", f"local:{name}={component_path}"])
        expected.extend(["--deps-dir", str(workspace / "no-ambient-dependencies"),
                         "-o", str(workspace / "composed.wasm")])
        assert command == expected
        ambient = Path(command[command.index("--deps-dir") + 1])
        assert ambient.parent == workspace and not ambient.exists()
        state.workspace = workspace
        if state.failure == "compose":
            raise RuntimeError("harness composition failed")
        Path(command[-1]).write_bytes(b"INERT COMPOSED TEST DATA")
        return SimpleNamespace(stderr="harness-only diagnostic")

    class Component:
        def __init__(self, engine: Any, binary: bytes) -> None:
            del engine
            state.events.append("compile")
            assert binary == b"INERT COMPOSED TEST DATA"
            self.type = SimpleNamespace(
                imports=lambda engine: state.surface["imports"],
                exports=lambda engine: state.surface["exports"],
            )

    real_validate_surface = replay.validate_surface

    def validate_surface(surface: Any) -> None:
        state.events.append("validate-surface")
        real_validate_surface(surface)
        state.events.append("surface-accepted")

    class Linker:
        def __init__(self, engine: Any) -> None:
            del engine
            assert state.result["typed_surface_validated"] is True
            state.events.append("create-linker")

        def define_unknown_imports_as_traps(self, component: Any) -> None:
            del component
            assert state.events[-1] == "create-linker"
            state.events.append("define-traps")
            state.traps_defined = True

    def run_case(engine: Any, component: Any, linker: Any, case: Any,
                 fuel: int, timeout: float) -> dict[str, Any]:
        del engine, component
        assert isinstance(linker, Linker)
        assert state.traps_defined is True
        assert state.result["typed_surface_validated"] is True
        assert fuel == replay.DEFAULT_FUEL and timeout == replay.DEFAULT_TIMEOUT
        state.events.append(f"case:{case['id']}")
        return _completed(case)

    def engine(config: Any) -> Any:
        state.engine_config = vars(config).copy()
        assert state.result["engine_memory_reservation_bytes"] == 128 * 1024 * 1024
        state.events.append("create-engine")
        return SimpleNamespace()

    wasm = ModuleType("wasmtime")
    wasm.Config, wasm.Engine = lambda: SimpleNamespace(), engine
    component = ModuleType("wasmtime.component")
    component.Component, component.Linker = Component, Linker
    monkeypatch.setitem(sys.modules, "wasmtime", wasm)
    monkeypatch.setitem(sys.modules, "wasmtime.component", component)
    monkeypatch.setattr(replay, "version", lambda name: "48.0.0")
    monkeypatch.setattr(replay, "validate_evidence", validate)
    monkeypatch.setattr(replay.subprocess, "run", compose)
    monkeypatch.setattr(replay.prior, "_type_shape", lambda value, engine: value)
    monkeypatch.setattr(replay, "validate_surface", validate_surface)
    monkeypatch.setattr(replay, "_replay_case", run_case)
    state.run = lambda: replay._compose_and_replay(
        artifacts, oracle, pins, wac, result=state.result,
    )
    return state


def test_composition_orchestration_wires_exact_five_captured_dependencies(
    composition_harness: Any,
) -> None:
    state = composition_harness
    result = state.run()
    assert len(state.commands) == 1
    command = state.commands[0]
    assert len(command) == 17 and command.count("--dep") == 5
    assert [command[index + 1].split("=", 1)[0]
            for index, item in enumerate(command) if item == "--dep"] == [
        f"local:{name}" for name in replay.INTERFACES
    ]
    assert not state.workspace.exists()
    assert result["composition_command"] == [
        "wac", "compose", "replay.wac",
        *(part for name in replay.INTERFACES
          for part in ("--dep", f"local:{name}={name}.wasm")),
        "--deps-dir", "no-ambient-dependencies", "-o", "composed.wasm",
    ]
    assert result["replay_succeeded"] is True  # Labelled harness only, not persisted.
    assert result["composition_tool_stderr"] == "harness-only diagnostic"
    assert all(result[key] is False for key in replay.NO_AUTHORITY)
    assert state.engine_config == {
        "consume_fuel": True, "epoch_interruption": True,
        "memory_reservation": 128 * 1024 * 1024,
    }


def test_composition_orchestration_accepts_exact_surface_before_traps_and_calls(
    composition_harness: Any, source_cases: Any,
) -> None:
    state = composition_harness
    state.run()
    assert state.events == [
        "validate-evidence-1", "compose", "create-engine", "compile",
        "validate-surface", "surface-accepted", "create-linker", "define-traps",
        *(f"case:{case['id']}" for case in source_cases), "validate-evidence-2",
    ]


@pytest.mark.parametrize("mutation", ["residual-import", "nested-domain-type"])
def test_composition_orchestration_surface_rejection_precedes_traps_and_calls(
    composition_harness: Any, mutation: str,
) -> None:
    state = composition_harness
    if mutation == "residual-import":
        state.surface["imports"][replay.INTERFACES["caller"]] = {}
    else:
        finalizer = state.surface["exports"][replay.INTERFACES["finalizer"]]
        finalizer["domain-error"] = ("record", (("message", "string"),))
    with pytest.raises(ValueError, match="exact reviewed"):
        state.run()
    assert state.result["typed_surface"] == state.surface
    assert state.result["typed_surface_validated"] is False
    assert state.result["observations"] == []
    assert state.result["wasm_execution_attempted"] is False
    assert state.result["wasm_execution_performed"] is False
    assert "create-linker" not in state.events and "define-traps" not in state.events
    assert not any(event.startswith("case:") for event in state.events)


@pytest.mark.parametrize("failure", ["late-evidence-raise", "late-evidence-diff"])
def test_composition_orchestration_late_evidence_failure_retains_observations(
    composition_harness: Any, source_cases: Any, failure: str,
) -> None:
    state = composition_harness
    state.failure = failure
    with pytest.raises(ValueError, match="evidence"):
        state.run()
    assert state.validations == 2
    assert state.result["observations"] == [_completed(case) for case in source_cases]
    assert state.result["wasm_execution_attempted"] is True
    assert state.result["wasm_execution_performed"] is True
    assert state.result["replay_succeeded"] is False
    assert state.result["typed_surface_validated"] is True


def test_composition_orchestration_tool_failure_never_reaches_runtime(
    composition_harness: Any,
) -> None:
    state = composition_harness
    state.failure = "compose"
    with pytest.raises(RuntimeError, match="harness composition failed"):
        state.run()
    assert state.events == ["validate-evidence-1", "compose"]
    assert not state.workspace.exists()
    assert state.result["observations"] == []
    assert state.result["replay_succeeded"] is False
    assert state.result["wasm_execution_performed"] is False
