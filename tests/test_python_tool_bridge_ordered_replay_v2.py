"""Isolated receipt/comparison/timer tests, never guest or native execution proof.

Synthetic artifact bytes exercise only the receipt validator. These tests never
instantiate Wasmtime, substitute a guest, emulate Core authority, or write a
replay evidence artifact. Actual execution requires the separately supervised
CLI with the real component.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import threading
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import replay_ordered_tool_bridge_v2 as replay
import stage_ordered_tool_bridge_v2 as staging

ORACLE = staging.PACK / "integer-v2.observations.json"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_receipt(artifact: Path, receipt: dict[str, Any]) -> None:
    artifact.with_suffix(".build.json").write_text(json.dumps(receipt))


@pytest.fixture
def receipt_fixture(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """Make inert structural validation inputs, not a component or a replay."""
    staged = tmp_path / "staged"
    plan = staging.stage(staged)
    artifact = tmp_path / "structural-validator-only.wasm"
    artifact.write_bytes(b"not a Wasm component; isolated receipt validator fixture")
    manifest = [
        {**item, "bytes": (staged / item["source"]).stat().st_size}
        for item in plan["sources"]
    ]
    receipt = {
        "format": "tobkiri.component-build/2",
        "language": "python",
        "artifact_sha256": _digest(artifact.read_bytes()),
        "artifact_bytes": artifact.stat().st_size,
        "build_plan_sha256": _digest((staged / "plan.json").read_bytes()),
        "source_manifest": manifest,
        "source_manifest_sha256": _digest(
            json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ),
        "world": plan["world"],
        "imports": sorted(plan["allowed_imports"]),
        "exports": sorted(plan["outer_exports"]),
    }
    _write_receipt(artifact, receipt)
    return artifact, receipt


def test_receipt_validation_is_not_component_execution(
    receipt_fixture: tuple[Path, dict[str, Any]],
) -> None:
    artifact, _ = receipt_fixture
    binary, _, oracle_bytes, oracle = replay.validate_evidence(artifact, ORACLE)
    assert binary == artifact.read_bytes()
    assert not binary.startswith(b"\x00asm")
    assert oracle_bytes == ORACLE.read_bytes()
    assert oracle["wasm_execution_performed"] is False
    assert oracle["native_core_acceptance"] is False
    assert len(oracle["source_bindings"]) == 19


@pytest.mark.parametrize(
    "destination",
    [
        "app_v2.py",
        "caller_adapter.py",
        "caller_adapter_v2.py",
        "ordered_json.py",
        "ordered_json_v2.py",
        "original_bridge.py",
        "provenance.json",
        "inventory.json",
        "wit/pack-v2.wit",
        "wit/deps/core.wit",
        "reviewed-inputs.json",
        "reviewed-inputs-v2.json",
    ],
)
def test_every_staged_source_is_bound_even_after_manifest_hash_is_reforged(
    receipt_fixture: tuple[Path, dict[str, Any]], destination: str
) -> None:
    artifact, receipt = receipt_fixture
    entry = next(
        item
        for item in receipt["source_manifest"]
        if item["destination"] == destination
    )
    entry["sha256"] = "0" * 64
    receipt["source_manifest_sha256"] = _digest(
        json.dumps(
            receipt["source_manifest"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )
    _write_receipt(artifact, receipt)
    with pytest.raises(ValueError, match="complete reviewed v2 plan"):
        replay.validate_evidence(artifact, ORACLE)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "extra",
        "missing",
        "reorder",
        "source-bytes",
        "source-path",
        "plan",
        "artifact",
        "artifact-size",
        "v1-export",
        "extra-import",
        "world",
    ],
)
def test_receipt_requires_exact_manifest_plan_artifact_and_abi(
    receipt_fixture: tuple[Path, dict[str, Any]], mutation: str
) -> None:
    artifact, receipt = receipt_fixture
    manifest = receipt["source_manifest"]
    if mutation == "duplicate":
        manifest.append(copy.deepcopy(manifest[0]))
    elif mutation == "extra":
        manifest.append(
            {"source": "x", "destination": "x", "sha256": "0" * 64, "bytes": 0}
        )
    elif mutation == "missing":
        manifest.pop()
    elif mutation == "reorder":
        manifest.reverse()
    elif mutation == "source-bytes":
        manifest[0]["bytes"] += 1
    elif mutation == "source-path":
        manifest[0]["source"] = "elsewhere.py"
    elif mutation == "plan":
        receipt["build_plan_sha256"] = "0" * 64
    elif mutation == "artifact":
        artifact.write_bytes(b"changed")
    elif mutation == "artifact-size":
        receipt["artifact_bytes"] += 1
    elif mutation == "v1-export":
        receipt["exports"] = [replay.INTERFACE.replace("2.0.0", "1.0.0")]
    elif mutation == "extra-import":
        receipt["imports"].append("wasi:cli/environment@0.2.0")
    else:
        receipt["world"] = "different-world"
    _write_receipt(artifact, receipt)
    with pytest.raises(ValueError, match="complete reviewed v2 plan"):
        replay.validate_evidence(artifact, ORACLE)


@pytest.mark.parametrize(
    "mutation",
    ["empty", "missing", "id", "output", "classification", "bindings", "protocol"],
)
def test_source_oracle_changes_fail_even_when_case_counts_are_kept(
    receipt_fixture: tuple[Path, dict[str, Any]],
    tmp_path: Path,
    mutation: str,
) -> None:
    artifact, _ = receipt_fixture
    oracle = json.loads(ORACLE.read_bytes())
    if mutation == "empty":
        oracle["cases"] = []
    elif mutation == "missing":
        oracle["cases"].pop()
    elif mutation == "id":
        oracle["cases"][0]["id"] = "invented"
    elif mutation == "output":
        oracle["cases"][0]["output_json"] = "{}"
    elif mutation == "classification":
        oracle["cases"][0]["classification"] = "original-invalid"
    elif mutation == "bindings":
        oracle["source_bindings"][0]["sha256"] = "0" * 64
    else:
        oracle["protocol"] = "ordered-finite-json-v1"
    path = tmp_path / "tampered-oracle.json"
    path.write_text(json.dumps(oracle))
    with pytest.raises(ValueError, match="exact reviewed v2 observations"):
        replay.validate_evidence(artifact, path)


def test_v1_oracle_is_never_silently_used_for_v2(
    receipt_fixture: tuple[Path, dict[str, Any]],
) -> None:
    artifact, _ = receipt_fixture
    with pytest.raises(ValueError, match="exact reviewed v2 observations"):
        replay.validate_evidence(artifact, staging.PACK / "oracle.json")


@pytest.mark.parametrize("target", ["artifact", "receipt", "oracle"])
def test_evidence_symlinks_are_rejected(
    receipt_fixture: tuple[Path, dict[str, Any]], tmp_path: Path, target: str
) -> None:
    artifact, _ = receipt_fixture
    oracle = ORACLE
    if target == "artifact":
        link = tmp_path / "component-link.wasm"
        link.symlink_to(artifact)
        artifact = link
    elif target == "receipt":
        path = artifact.with_suffix(".build.json")
        captured = tmp_path / "captured-receipt.json"
        path.rename(captured)
        path.symlink_to(captured)
    else:
        oracle = tmp_path / "oracle-link.json"
        oracle.symlink_to(ORACLE)
    with pytest.raises(ValueError, match="invalid or oversized"):
        replay.validate_evidence(artifact, oracle)


def test_duplicate_receipt_keys_are_rejected(
    receipt_fixture: tuple[Path, dict[str, Any]],
) -> None:
    artifact, _ = receipt_fixture
    artifact.with_suffix(".build.json").write_text(
        '{"source_manifest":[],"source_manifest":[]}'
    )
    with pytest.raises(ValueError, match="duplicate evidence key"):
        replay.validate_evidence(artifact, ORACLE)


def test_success_comparison_preserves_exact_json_strings() -> None:
    case = {"classification": "success", "output_json": '{"b":1.0,"a":-0.0}'}
    assert replay.comparison_flags(
        case, {"status": "ok", "output_json": case["output_json"]}
    ) == {"admitted_parity": True, "nonfinite_boundary_verified": False}
    for changed in ('{"a":-0.0,"b":1.0}', '{"b":1,"a":0}', '{ "b":1.0,"a":-0.0}'):
        assert not replay.comparison_flags(
            case, {"status": "ok", "output_json": changed}
        )["admitted_parity"]


@pytest.mark.parametrize("code", ["invalid", "internal", "limit-exceeded"])
def test_original_errors_are_only_fixed_typed_invalid(code: str) -> None:
    flags = replay.comparison_flags(
        {"classification": "original-invalid"},
        {"status": "error", "code": code, "message": replay.INVALID_MESSAGE},
    )
    assert flags["admitted_parity"] is (code == "invalid")
    assert flags["nonfinite_boundary_verified"] is False


@pytest.mark.parametrize(
    ("code", "message", "matched"),
    [
        ("internal", replay.NONFINITE_MESSAGE, True),
        ("invalid", replay.NONFINITE_MESSAGE, False),
        ("limit-exceeded", replay.NONFINITE_MESSAGE, False),
        ("internal", "private provider data leaked", False),
    ],
)
def test_nonfinite_internal_is_sanitized_and_never_admitted_parity(
    code: str, message: str, matched: bool
) -> None:
    flags = replay.comparison_flags(
        {"classification": "excluded-not-parity"},
        {"status": "error", "code": code, "message": message},
    )
    assert flags == {
        "admitted_parity": False,
        "nonfinite_boundary_verified": matched,
    }


@pytest.mark.parametrize(
    "phase", ["post-return", "lifecycle-shutdown", "fuel-accounting"]
)
def test_late_failures_revoke_success_and_keep_the_first_failure(phase: str) -> None:
    record = {
        "actual": {"status": "ok", "output_json": "{}"},
        "admitted_parity": True,
        "nonfinite_boundary_verified": True,
        "case_completed": True,
    }
    replay.record_runner_failure(record, RuntimeError("first failure"), phase=phase)
    replay.record_runner_failure(
        record, RuntimeError("cleanup also failed"), phase="store-cleanup"
    )
    assert record["admitted_parity"] is False
    assert record["nonfinite_boundary_verified"] is False
    assert record["case_completed"] is False
    assert record["actual"]["message"] == "first failure"
    assert record["result_before_runner_failure"]["status"] == "ok"
    assert [item["phase"] for item in record["runner_failures"]] == [
        phase,
        "store-cleanup",
    ]


def test_live_cleanup_helper_keeps_a_return_when_post_return_fails() -> None:
    """Exercise actual helper ordering using plain callbacks, not a guest."""
    record: dict[str, Any] = {
        "admitted_parity": False,
        "nonfinite_boundary_verified": False,
    }
    order = []
    failure = RuntimeError("post-return failed")
    secondary = []

    def returned(value: str) -> None:
        order.append("capture")
        record["actual"] = {"status": "ok", "output_json": value}

    def cleanup() -> None:
        order.append("post-return")
        raise failure

    with pytest.raises(RuntimeError) as caught:
        replay._call_and_cleanup(lambda: '{"intents":[]}', cleanup, returned, secondary)
    assert caught.value is failure
    assert order == ["capture", "post-return"]
    assert secondary == []
    replay.record_runner_failure(record, caught.value, phase="normalize")
    assert record["result_before_runner_failure"] == {
        "status": "ok",
        "output_json": '{"intents":[]}',
    }
    assert record["actual"]["status"] == "runner-failure"
    assert record["admitted_parity"] is False
    assert record["nonfinite_boundary_verified"] is False


def test_inspection_failure_still_cleans_and_preserves_original_exception() -> None:
    original = ValueError("lifted result inspection failed")
    cleanup_error = RuntimeError("post-return also failed")
    cleanup_calls = []
    secondary = []

    def inspect(value: object) -> None:
        raise original

    def cleanup() -> None:
        cleanup_calls.append(True)
        raise cleanup_error

    with pytest.raises(ValueError) as caught:
        replay._call_and_cleanup(lambda: object(), cleanup, inspect, secondary)
    assert caught.value is original
    assert cleanup_calls == [True]
    assert secondary == [cleanup_error]
    record: dict[str, Any] = {}
    replay.record_runner_failure(record, caught.value, phase="normalize")
    replay.record_runner_failure(record, secondary[0], phase="normalize-post-return")
    assert record["actual"]["type"] == "ValueError"
    assert [item["message"] for item in record["runner_failures"]] == [
        str(original),
        str(cleanup_error),
    ]


def test_failed_call_has_no_return_to_capture_or_clean() -> None:
    events = []
    failure = RuntimeError("call did not return")

    def call() -> object:
        raise failure

    with pytest.raises(RuntimeError) as caught:
        replay._call_and_cleanup(
            call,
            lambda: events.append("cleanup"),
            lambda value: events.append("capture"),
            [],
        )
    assert caught.value is failure
    assert events == []


def _claim_inputs() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return only inert helper values; never serialize these as replay proof."""
    cases = [
        {"id": "a", "classification": "success", "output_json": "{}"},
        {"id": "b", "classification": "excluded-not-parity"},
    ]
    values = [
        {"status": "ok", "output_json": "{}"},
        {"status": "error", "code": "internal", "message": replay.NONFINITE_MESSAGE},
    ]
    records = [
        {
            "id": case["id"],
            "classification": case["classification"],
            "actual": value,
            "lifecycle": {"init": True, "shutdown": True},
            "case_completed": True,
            "guest_runtime": replay.EXPECTED_GUEST_RUNTIME.copy(),
            "guest_calls_attempted": ["init", "runtime-info", "normalize", "shutdown"],
            "guest_calls_returned": ["init", "runtime-info", "normalize", "shutdown"],
            **replay.comparison_flags(case, value),
        }
        for case, value in zip(cases, values)
    ]
    return cases, records


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "reorder",
        "wrong-id",
        "classification",
        "runtime",
        "init",
        "shutdown",
        "not-completed",
        "late-failure",
        "forged-parity-flag",
        "forged-boundary-flag",
        "nonfinite-parity",
        "missing-return",
    ],
)
def test_aggregate_claims_fail_closed(mutation: str) -> None:
    cases, records = _claim_inputs()
    assert replay.summarize(cases, records)["replay_succeeded"] is True
    if mutation == "missing":
        records.pop()
    elif mutation == "duplicate":
        records[1] = copy.deepcopy(records[0])
    elif mutation == "reorder":
        records.reverse()
    elif mutation == "wrong-id":
        records[0]["id"] = "other"
    elif mutation == "classification":
        records[0]["classification"] = "original-invalid"
    elif mutation == "runtime":
        records[0]["guest_runtime"]["python"] = "3.13.5"
    elif mutation in ("init", "shutdown"):
        records[0]["lifecycle"][mutation] = False
    elif mutation == "not-completed":
        records[0]["case_completed"] = False
    elif mutation == "late-failure":
        records[0]["runner_failures"] = [{"message": "post-return failed"}]
    elif mutation == "forged-parity-flag":
        records[0]["actual"]["output_json"] = '{"different":true}'
    elif mutation == "forged-boundary-flag":
        records[1]["actual"]["code"] = "invalid"
    elif mutation == "missing-return":
        records[0]["guest_calls_returned"].pop()
    else:
        records[1]["admitted_parity"] = True
    assert replay.summarize(cases, records)["replay_succeeded"] is False


def test_empty_case_sets_cannot_claim_vacuous_success() -> None:
    assert not any(replay.summarize([], []).values())


def test_execution_flags_distinguish_setup_failure_attempt_and_actual_return() -> None:
    records = [{"id": "a", "guest_calls_attempted": [], "guest_calls_returned": []}]
    flags = replay.summarize([], records)
    assert flags["wasm_execution_attempted"] is False
    assert flags["wasm_execution_performed"] is False
    records[0]["guest_calls_attempted"].append("init")
    flags = replay.summarize([], records)
    assert flags["wasm_execution_attempted"] is True
    assert flags["wasm_execution_performed"] is False
    records[0]["guest_calls_returned"].append("init")
    flags = replay.summarize([], records)
    assert flags["wasm_execution_performed"] is True
    assert flags["replay_succeeded"] is False


@pytest.mark.parametrize("fuel", [True, 0, -1, 2_000_000_001, 1.0])
def test_invalid_fuel_is_rejected_before_loading_wasmtime(fuel: Any) -> None:
    with pytest.raises(ValueError, match="fuel must"):
        replay.replay(Path("never-read"), Path("never-read"), fuel=fuel)


@pytest.mark.parametrize("timeout", [True, 0, -1, 31, float("nan"), float("inf")])
def test_invalid_deadline_is_rejected_before_loading_wasmtime(
    timeout: Any,
) -> None:
    with pytest.raises(ValueError, match="guest timeout must"):
        replay.replay(Path("never-read"), Path("never-read"), timeout=timeout)


def test_timer_cancellation_prevents_a_late_epoch_increment() -> None:
    increments = []
    timer = replay._EpochTimer(3600, lambda: increments.append(1))
    timer.start()
    timer.finish()
    timer._expire()
    assert increments == []
    assert not timer._timer.is_alive()


def test_delayed_timer_callback_cannot_hide_an_elapsed_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(replay.time, "monotonic", lambda: clock[0])
    increments = []
    timer = replay._EpochTimer(3600, lambda: increments.append(1))
    timer.start()
    # Keep the actual Timer asleep while the independently observed deadline
    # passes. No callback or guest execution is simulated.
    clock[0] += 3601
    with pytest.raises(TimeoutError, match="epoch deadline"):
        timer.finish()
    assert increments == []
    assert not timer._timer.is_alive()


def test_timer_finish_joins_an_already_running_callback() -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    failures = []

    def increment() -> None:
        entered.set()
        assert release.wait(2)

    timer = replay._EpochTimer(0, increment)
    timer.start()
    assert entered.wait(2)

    def finish() -> None:
        try:
            timer.finish()
        except TimeoutError as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=finish)
    thread.start()
    try:
        assert not finished.wait(0.02)
    finally:
        release.set()
        thread.join(2)
    assert finished.is_set()
    assert len(failures) == 1
    assert not timer._timer.is_alive()


def test_epoch_callback_failures_cannot_silently_pass() -> None:
    entered = threading.Event()

    def increment() -> None:
        entered.set()
        raise RuntimeError("increment failed")

    timer = replay._EpochTimer(0, increment)
    timer.start()
    assert entered.wait(2)
    with pytest.raises(RuntimeError, match="epoch interruption callback failed"):
        timer.finish()
    assert not timer._timer.is_alive()
