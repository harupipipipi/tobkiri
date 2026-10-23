"""Current-source validation and labelled harness tests, never guest evidence.

No test builds, composes, instantiates or executes a Wasm component. Inert bytes
and explicit compiler/runtime doubles cannot establish a new two-component
replay, native Core acceptance, or execution provenance.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import stat
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import replay_gateway_tool_caller_current as replay

ORACLE = ROOT / "tests/fixtures/issue1520/gateway-tool-caller.current-source.json"
HISTORICAL_ORACLE = ROOT / "tests/fixtures/issue1520/gateway-tool-caller.source.json"


def _pins() -> dict[str, Any]:
    """Build structural pin fixtures, not independently reviewed artifact pins."""
    return {
        **{
            name: {"artifact_sha256": "1" * 64, "receipt_sha256": "2" * 64}
            for name in replay.COMPONENTS
        },
        "source_observations_sha256": replay.CURRENT_ORACLE_SHA256,
        "wac_sha256": "3" * 64,
        "composition_wac_sha256": replay.digest(replay.WAC_SOURCE),
        "runner_source_bindings_sha256": replay.digest(
            replay.reference.canonical(replay.runner_bindings()),
        ),
    }


@pytest.fixture
def source() -> dict[str, Any]:
    """Validate the actual separately pinned current source-only observations."""
    _, document = replay.prior.validate_observations(
        ORACLE, replay.CURRENT_ORACLE_SHA256,
    )
    return document


def test_current_runner_keeps_exact_historical_execution_policy() -> None:
    assert replay.WAC_SOURCE is replay.prior.WAC_SOURCE
    assert replay.WAC_SOURCE.count(b" = new local:") == 2
    assert replay.STORE_LIMITS == replay.prior.STORE_LIMITS == {
        "memory_size": 128 * 1024 * 1024, "table_elements": 65536,
        "instances": 128, "tables": 64, "memories": 4,
    }
    assert replay.STORE_LIMITS is not replay.prior.STORE_LIMITS
    assert replay.DEFAULT_FUEL == 2_000_000_000
    assert replay.DEFAULT_TIMEOUT == 30
    result = replay._base_result()
    assert all(result[key] is False for key in replay.NO_AUTHORITY)
    assert result["external_process_supervisor_required"] is True
    assert result["compilation_wall_time_bounded"] is False
    assert result["replay_succeeded"] is False
    assert result["format"] != replay.prior._base_result()["format"]


def test_runner_closure_includes_all_current_and_historical_local_helpers() -> None:
    bindings = replay.runner_bindings()
    assert set(bindings) == {
        *replay.current_evidence.runner_bindings(),
        "replay_gateway_tool_caller_current.py",
    }
    assert len(bindings) == 16
    for name, digest in bindings.items():
        assert digest == replay.digest((ROOT / "scripts/wasm" / name).read_bytes())


@pytest.mark.parametrize("field", tuple(_pins()))
def test_missing_pin_identity_is_rejected(field: str) -> None:
    pins = _pins()
    replay.validate_pins(pins)
    pins.pop(field)
    with pytest.raises(ValueError, match="pins must bind current"):
        replay.validate_pins(pins)


@pytest.mark.parametrize("value", [None, True, 123, "A" * 64, "0" * 63])
def test_invalid_exact_pin_hashes_are_rejected(value: Any) -> None:
    pins = _pins()
    pins["caller"]["artifact_sha256"] = value
    with pytest.raises(ValueError, match="exact SHA-256"):
        replay.validate_pins(pins)


@pytest.mark.parametrize("kind", ["extra", "nested-extra", "source", "graph", "runner"])
def test_pins_cannot_weaken_current_source_or_graph(kind: str) -> None:
    pins = _pins()
    replay.validate_pins(pins)
    if kind == "extra":
        pins["allow_historical"] = True
    elif kind == "nested-extra":
        pins["provider"]["skip_source"] = True
    elif kind == "source":
        pins["source_observations_sha256"] = (
            replay.prior.REVIEWED_PINS["source_observations_sha256"]
        )
    elif kind == "graph":
        pins["composition_wac_sha256"] = "0" * 64
    else:
        pins["runner_source_bindings_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        replay.validate_pins(pins)


@pytest.fixture
def current_receipts(tmp_path: Path) -> Any:
    """Use inert component headers with fabricated, current structural receipts."""
    paths, pins, documents = {}, _pins(), {}
    for name, kind in replay.COMPONENTS.items():
        directory = tmp_path / name
        directory.mkdir()
        staged = directory / "stage"
        plan = replay.current_evidence._stage(kind, staged)
        binary = b"\x00asm\x0d\x00\x01\x00INERT HEADER TEST DATA " + name.encode()
        artifact = directory / "component.wasm"
        artifact.write_bytes(binary)
        manifest = replay.current_evidence._manifest(staged, plan)
        receipt = {
            "format": "tobkiri.component-build/2", "language": "python",
            "build_mode": "hash-bound-custom-python313-producer-trial",
            "production_enabled": False, "guest_runtime_identity_verified": False,
            "compiler_executable_sha256": "4" * 64,
            "producer_trial_receipt_sha256": "5" * 64,
            "artifact_sha256": replay.digest(binary), "artifact_bytes": len(binary),
            "source_manifest": manifest,
            "source_manifest_sha256": replay.digest(json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode()),
            "build_plan_sha256": replay.digest((staged / "plan.json").read_bytes()),
            "world": plan["world"], "imports": sorted(plan["allowed_imports"]),
            "exports": sorted(plan["outer_exports"]),
        }
        raw = replay.reference.json_bytes(receipt)
        artifact.with_suffix(".build.json").write_bytes(raw)
        pins[name] = {
            "artifact_sha256": replay.digest(binary),
            "receipt_sha256": replay.digest(raw),
        }
        paths[name], documents[name] = artifact, receipt
    return SimpleNamespace(paths=paths, pins=pins, documents=documents)


def _validate(fixture: Any, oracle: Path = ORACLE) -> dict[str, Any]:
    return replay.validate_evidence(
        fixture.paths["caller"], fixture.paths["provider"], oracle, fixture.pins,
    )


def test_both_current_receipts_and_exact_thirty_source_cases_are_required(
    current_receipts: Any, source: Any,
) -> None:
    historical_pins = copy.deepcopy(replay.prior.REVIEWED_PINS)
    result = _validate(current_receipts)
    assert replay.prior.REVIEWED_PINS == historical_pins
    assert result["source"] == source
    assert len(result["source"]["cases"]) == 30
    assert result["source"]["counts"] == {
        "success": 16, "provider-invalid": 7, "provider-nonfinite": 7,
    }
    assert set(result["components"]) == {"caller", "provider"}
    assert "wasm_execution_performed" not in result


def test_historical_source_cannot_substitute_for_the_current_reference(
    current_receipts: Any,
) -> None:
    _validate(current_receipts)
    with pytest.raises(ValueError, match="source observations differs"):
        _validate(current_receipts, HISTORICAL_ORACLE)


@pytest.mark.parametrize("name", tuple(replay.COMPONENTS))
@pytest.mark.parametrize(
    "field", ["build_plan_sha256", "source_manifest", "imports", "exports"],
)
def test_current_receipt_mutations_fail_after_a_valid_baseline(
    current_receipts: Any, name: str, field: str,
) -> None:
    _validate(current_receipts)
    current_receipts.documents[name][field] = "changed"
    raw = replay.reference.json_bytes(current_receipts.documents[name])
    current_receipts.paths[name].with_suffix(".build.json").write_bytes(raw)
    current_receipts.pins[name]["receipt_sha256"] = replay.digest(raw)
    with pytest.raises(ValueError, match="complete reviewed stage plan"):
        _validate(current_receipts)


@pytest.mark.parametrize("name", tuple(replay.COMPONENTS))
def test_historical_build_plan_is_not_current_proof_even_with_new_receipt_hash(
    current_receipts: Any, name: str,
) -> None:
    _validate(current_receipts)
    directory = "2026-10-20-gateway" if name == "caller" else "2026-10-20"
    old = json.loads((
        ROOT / "tools/python313-producer/trials" / directory / "component.build.json"
    ).read_bytes())
    # Only the inert test bytes match: the historical plan/manifest remain old.
    current = current_receipts.documents[name]
    old.update({key: current[key] for key in ("artifact_sha256", "artifact_bytes")})
    raw = replay.reference.json_bytes(old)
    current_receipts.paths[name].with_suffix(".build.json").write_bytes(raw)
    current_receipts.pins[name]["receipt_sha256"] = replay.digest(raw)
    with pytest.raises(ValueError, match="complete reviewed stage plan"):
        _validate(current_receipts)


def _completed(case: dict[str, Any]) -> dict[str, Any]:
    """Return a labelled successful harness record, never persisted guest proof."""
    return {
        **replay.prior._case_record(case), "actual": case["expected"],
        **replay.prior.comparison_flags(case, case["expected"]),
        "case_completed": True, "instantiation_attempted": True,
        "guest_calls_attempted": replay.prior.CALL_ORDER.copy(),
        "guest_calls_returned": replay.prior.CALL_ORDER.copy(),
        "guest_runtime": {
            name: replay.prior.EXPECTED_RUNTIME.copy()
            for name in replay.prior.LIFECYCLES
        },
        "lifecycle": {
            name: {"init": True, "shutdown": True} for name in replay.prior.LIFECYCLES
        },
    }


@pytest.fixture
def composition_harness(
    source: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Replace executable/compiler/guest entry points with explicit inert doubles."""
    state = SimpleNamespace(
        source=source, events=[], failure=None, validations=0,
        surface=copy.deepcopy(replay.prior.expected_surface()),
        result=replay._base_result(),
    )
    wac = tmp_path / "inert-wac"
    wac.write_bytes(b"INERT WAC TEST DATA; NOT AN EXECUTABLE")
    state.wac = wac
    pins = {"wac_sha256": replay.digest(wac.read_bytes())}
    paths = [tmp_path / name for name in ("caller", "provider", "oracle")]
    evidence = {
        "components": {
            name: {
                "binary": f"INERT {name} TEST DATA".encode(),
                "receipt_bytes": b"INERT RECEIPT TEST DATA",
                "stage_sha256": "test-stage", "plan_sha256": "test-plan",
                "source_manifest_sha256": "test-manifest",
            } for name in replay.COMPONENTS
        },
        "source": source, "source_bytes": replay.reference.json_bytes(source),
    }

    def validate(*args: Any) -> dict[str, Any]:
        assert list(args[:3]) == paths and args[3] == pins
        state.validations += 1
        state.events.append(f"validate-{state.validations}")
        if state.validations == 2 and state.failure == "late-evidence-raise":
            raise ValueError("late current evidence validation")
        result = copy.deepcopy(evidence)
        if state.validations == 2 and state.failure == "late-evidence-diff":
            result["source_bytes"] += b"changed"
        return result

    def compose(command: list[str], **kwargs: Any) -> Any:
        state.events.append("compose")
        workspace = state.workspace = kwargs["cwd"]
        assert kwargs == {
            "check": True, "capture_output": True, "text": True, "cwd": workspace,
        }
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
        assert (workspace / "replay.wac").read_bytes() == replay.prior.WAC_SOURCE
        assert (workspace / "wac").read_bytes() == wac.read_bytes()
        assert stat.S_IMODE((workspace / "wac").stat().st_mode) == 0o700
        for name in replay.COMPONENTS:
            assert (workspace / f"{name}.wasm").read_bytes() == (
                evidence["components"][name]["binary"]
            )
        assert command == [
            str(workspace / "wac"), "compose", str(workspace / "replay.wac"),
            "--dep", f"local:caller={workspace / 'caller.wasm'}",
            "--dep", f"local:provider={workspace / 'provider.wasm'}",
            "--deps-dir", str(workspace / "no-ambient-dependencies"),
            "-o", str(workspace / "composed.wasm"),
        ]
        assert not (workspace / "no-ambient-dependencies").exists()
        if state.failure == "compose":
            raise RuntimeError("inert composition failure")
        (workspace / "composed.wasm").write_bytes(b"INERT COMPOSED TEST DATA")
        return SimpleNamespace(stderr="inert diagnostic")

    def engine(config: Any) -> Any:
        state.events.append("engine")
        state.config = vars(config).copy()
        if state.failure == "engine":
            raise RuntimeError("inert engine failure")
        return object()

    class Component:
        def __init__(self, engine: Any, binary: bytes) -> None:
            del engine
            state.events.append("compile")
            assert binary == b"INERT COMPOSED TEST DATA"
            self.type = SimpleNamespace(
                imports=lambda engine: state.surface["imports"],
                exports=lambda engine: state.surface["exports"],
            )

    class Linker:
        def __init__(self, engine: Any) -> None:
            del engine
            assert state.result["typed_surface_validated"] is True
            state.events.append("linker")

        def define_unknown_imports_as_traps(self, component: Any) -> None:
            del component
            state.events.append("traps")

    def run_case(
        engine: Any, component: Any, linker: Any, case: Any, fuel: int, timeout: float,
    ) -> dict[str, Any]:
        del engine, component
        assert isinstance(linker, Linker) and "traps" in state.events
        assert fuel == replay.DEFAULT_FUEL and timeout == replay.DEFAULT_TIMEOUT
        count = len(state.result["observations"])
        if count == 2 and state.failure == "case":
            raise RuntimeError("inert unexpected case failure")
        state.events.append(f"case:{case['id']}")
        result = _completed(case)
        if count == 0 and state.failure == "case-record":
            replay.prior.record_failure(
                result, RuntimeError("inert guest trap"), "call",
            )
        return result

    wasm, component = ModuleType("wasmtime"), ModuleType("wasmtime.component")
    wasm.Config, wasm.Engine = lambda: SimpleNamespace(), engine
    component.Component, component.Linker = Component, Linker
    monkeypatch.setitem(sys.modules, "wasmtime", wasm)
    monkeypatch.setitem(sys.modules, "wasmtime.component", component)
    monkeypatch.setattr(replay, "version", lambda name: "48.0.0")
    monkeypatch.setattr(replay, "validate_evidence", validate)
    monkeypatch.setattr(replay.subprocess, "run", compose)
    monkeypatch.setattr(replay.prior, "_type_shape", lambda value, engine: value)
    monkeypatch.setattr(replay.prior, "_replay_case", run_case)
    state.run = lambda: replay._compose_and_replay(
        *paths, pins, wac, result=state.result,
    )
    return state


def test_inert_composition_uses_exact_private_graph_and_unchanged_limits(
    composition_harness: Any,
) -> None:
    state = composition_harness
    result = state.run()
    assert result["replay_succeeded"] is True  # Inert harness only, never persisted.
    assert len(result["observations"]) == 30
    assert result["typed_surface"] == replay.prior.expected_surface()
    assert all(result[key] is False for key in replay.NO_AUTHORITY)
    assert result["source_observations_sha256"] == replay.CURRENT_ORACLE_SHA256
    assert result["store_limits"] == replay.prior.STORE_LIMITS
    assert state.config == {
        "consume_fuel": True, "epoch_interruption": True,
        "memory_reservation": 128 * 1024 * 1024,
    }
    assert state.events == [
        "validate-1", "compose", "engine", "compile", "linker", "traps",
        *(f"case:{case['id']}" for case in state.source["cases"]), "validate-2",
    ]
    assert not state.workspace.exists()


@pytest.mark.parametrize(
    "kind", ["wasi", "residual-provider", "extra-export", "nested"],
)
def test_exact_surface_failure_precedes_linking_or_calls(
    composition_harness: Any, kind: str,
) -> None:
    state = composition_harness
    if kind == "wasi":
        state.surface["imports"]["wasi:cli/environment@0.2.0"] = {}
    elif kind == "residual-provider":
        state.surface["imports"][replay.prior.PROVIDER] = {}
    elif kind == "extra-export":
        state.surface["exports"]["_python_init"] = ("func", (), None)
    else:
        state.surface["exports"][replay.prior.CALLER]["error"] = (
            "record", (("message", "string"), ("code", "string")),
        )
    with pytest.raises(ValueError, match="exact reviewed"):
        state.run()
    assert state.result["typed_surface_validated"] is False
    assert state.result["observations"] == []
    assert "linker" not in state.events and "traps" not in state.events
    assert state.result["wasm_execution_performed"] is False


@pytest.mark.parametrize(
    "failure", ["late-evidence-raise", "late-evidence-diff", "case"],
)
def test_late_failures_preserve_partial_or_complete_observations(
    composition_harness: Any, failure: str,
) -> None:
    state = composition_harness
    state.failure = failure
    with pytest.raises((ValueError, RuntimeError)):
        state.run()
    assert len(state.result["observations"]) == (2 if failure == "case" else 30)
    assert state.result["replay_succeeded"] is False
    assert state.result["wasm_execution_attempted"] is True
    assert state.result["wasm_execution_performed"] is True


@pytest.mark.parametrize("kind", ["runner", "wac"])
def test_post_run_source_and_tool_drift_revoke_success(
    composition_harness: Any, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    state = composition_harness
    if kind == "runner":
        before = replay.runner_bindings()
        values = iter([before, {**before, "unexpected-helper": "changed"}])
        monkeypatch.setattr(replay, "runner_bindings", lambda: next(values))
    else:
        original_validate = replay.validate_evidence

        def validate(*args: Any) -> Any:
            result = original_validate(*args)
            if state.validations == 2:
                state.wac.write_bytes(b"changed tool")
            return result

        monkeypatch.setattr(replay, "validate_evidence", validate)
    with pytest.raises(ValueError, match="changed during replay"):
        state.run()
    assert len(state.result["observations"]) == 30
    assert state.result["replay_succeeded"] is False
    assert state.result["wasm_execution_performed"] is True


@pytest.mark.parametrize("failure", ["compose", "engine"])
def test_pre_guest_failure_cleans_workspace_without_execution_claim(
    composition_harness: Any, failure: str,
) -> None:
    state = composition_harness
    state.failure = failure
    with pytest.raises(RuntimeError, match="inert"):
        state.run()
    assert not state.workspace.exists()
    assert state.result["observations"] == []
    assert state.result["wasm_execution_performed"] is False


def test_case_failure_is_retained_without_retry_or_false_success(
    composition_harness: Any,
) -> None:
    state = composition_harness
    state.failure = "case-record"
    result = state.run()
    assert len(result["observations"]) == 30
    assert result["observations"][0]["runner_failures"][0]["message"] == (
        "inert guest trap"
    )
    assert result["replay_succeeded"] is False
    assert len([event for event in state.events if event.startswith("case:")]) == 30


@pytest.mark.parametrize(
    "argument,value",
    [("fuel", True), ("fuel", 0), ("fuel", 2_000_000_001),
     ("timeout", True), ("timeout", 0), ("timeout", float("nan")),
     ("timeout", float("inf")), ("timeout", 31)],
)
def test_invalid_runtime_bounds_fail_before_evidence_or_tool_use(
    argument: str, value: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        replay, "validate_evidence",
        lambda *args: pytest.fail("read evidence too soon"),
    )
    with pytest.raises(ValueError, match="invalid guest"):
        replay._compose_and_replay(
            Path("caller"), Path("provider"), ORACLE, {}, Path("wac"),
            **{argument: value},
        )


def _arguments(pins: Path, output: Path, expected: str) -> list[str]:
    return [
        "current-replay", "--caller", "unused", "--provider", "unused",
        "--oracle", str(ORACLE), "--pins", str(pins), "--pins-sha256", expected,
        "--wac", "unused", "--output", str(output),
    ]


def test_main_requires_external_exact_pins_document_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pins, output = tmp_path / "pins.json", tmp_path / "result.json"
    pins.write_bytes(b"{}")
    monkeypatch.setattr(sys, "argv", _arguments(pins, output, "0" * 64))
    monkeypatch.setattr(
        replay, "_compose_and_replay", lambda *args, **kwargs: pytest.fail("executed"),
    )
    assert replay.main() == 1
    result = json.loads(output.read_bytes())
    assert "exact reviewed SHA-256" in result["runner_failure"]["message"]
    assert result["wasm_execution_performed"] is False


def test_main_late_failure_keeps_returned_call_custody(
    source: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pins, output = tmp_path / "pins.json", tmp_path / "result.json"
    pins.write_bytes(b"{}")

    def fail(*args: Any, result: dict[str, Any], **kwargs: Any) -> None:
        result["observations"] = [_completed(source["cases"][0])]
        result.update(replay.prior.summarize(source["cases"], result["observations"]))
        raise RuntimeError("late current evidence failure")

    monkeypatch.setattr(replay, "_compose_and_replay", fail)
    monkeypatch.setattr(
        sys, "argv", _arguments(pins, output, replay.digest(pins.read_bytes())),
    )
    assert replay.main() == 1
    result = json.loads(output.read_bytes())
    assert result["wasm_execution_performed"] is True
    assert len(result["observations"]) == 1
    assert result["replay_succeeded"] is False
    assert result["runner_failure"]["message"] == "late current evidence failure"
    assert all(result[key] is False for key in replay.NO_AUTHORITY)


@pytest.mark.parametrize("kind", ["existing", "dangling-symlink"])
def test_main_never_overwrites_an_existing_or_symlink_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    pins, output = tmp_path / "pins.json", tmp_path / "result.json"
    if kind == "existing":
        output.write_bytes(b"preserved historical receipt")
    else:
        output.symlink_to(tmp_path / "missing")
    monkeypatch.setattr(sys, "argv", _arguments(pins, output, "0" * 64))
    with pytest.raises(ValueError, match="must be a new file"):
        replay.main()
    assert output.is_symlink() if kind == "dangling-symlink" else (
        output.read_bytes() == b"preserved historical receipt"
    )
