"""V2 source staging tests, not a build, guest replay or native acceptance."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import prepare_ordered_tool_bridge as preparation
import stage_ordered_tool_bridge as v1
import stage_ordered_tool_bridge_v2 as staging

PREFIX = "packs/local.ai-tool-bridge-ordered-python"
OBSERVATIONS = f"{PREFIX}/integer-v2.observations.json"
EXPECTED_FILES = {
    "original_bridge.py", "inventory.json", "provenance.json",
    "caller_adapter.py", "caller_adapter_v2.py", "app_v2.py",
    "ordered_json.py", "ordered_json_v2.py", "wit/pack-v2.wit",
    "wit/deps/core.wit", "reviewed-inputs.json", "reviewed-inputs-v2.json",
}


def digest(data: bytes) -> str:
    """Return the exact byte identity used by source plans."""
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy only the real reviewed inputs before exercising destructive cases."""
    root = tmp_path / "repository"
    report = json.loads((ROOT / OBSERVATIONS).read_bytes())
    inventory = json.loads((ROOT / PREFIX / "python/inventory.json").read_bytes())
    paths = {
        *(item["path"] for item in report["source_bindings"]),
        *(item["path"] for item in inventory["sources"]),
        OBSERVATIONS,
        staging.STAGER,
        f"{PREFIX}/python/app_v2.py",
        f"{PREFIX}/wit/pack-v2.wit",
    }
    for name in paths:
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_bytes())
    for module in (staging, v1, preparation):
        monkeypatch.setattr(module, "ROOT", root)
    for module in (staging, v1):
        monkeypatch.setattr(module, "PACK", root / PREFIX)
    return root


def revise_report(
    repository: Path, report: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update only a fixture pin to exercise checks beyond the immutable digest."""
    data = (json.dumps(report, indent=2) + "\n").encode()
    (repository / OBSERVATIONS).write_bytes(data)
    monkeypatch.setattr(staging, "OBSERVATIONS_SHA256", digest(data))


def test_stage_exact_source_set_and_all_hashes(
    repository: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "stage"
    plan = staging.stage(output)
    assert json.loads((output / "plan.json").read_bytes()) == plan
    assert {item["source"] for item in plan["sources"]} == EXPECTED_FILES
    assert len(plan["sources"]) == len(EXPECTED_FILES)
    assert {
        path.relative_to(output).as_posix()
        for path in output.rglob("*") if path.is_file()
    } == EXPECTED_FILES | {"plan.json"}
    for item in plan["sources"]:
        assert item["source"] == item["destination"]
        assert digest((output / item["source"]).read_bytes()) == item["sha256"]
    for name in (
        "original_bridge.py", "inventory.json", "provenance.json",
        "caller_adapter.py", "caller_adapter_v2.py", "app_v2.py",
    ):
        assert (output / name).read_bytes() == (
            repository / PREFIX / "python" / name
        ).read_bytes()
    assert (output / "reviewed-inputs.json").read_bytes() == (
        repository / PREFIX / "oracle.freeze.json"
    ).read_bytes()
    provenance = json.loads((output / "provenance.json").read_bytes())
    assert digest((output / "original_bridge.py").read_bytes()) == (
        provenance["output"]["sha256"]
    )
    core = repository / "crates/tobkiri-wit/wit/core/core.wit"
    assert (output / "wit/deps/core.wit").read_bytes() == core.read_bytes()


def test_stage_separates_source_observations_from_unexecuted_guest(
    repository: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "stage"
    staging.stage(output)
    raw, companion = staging.reviewed_inputs()
    assert raw == (repository / OBSERVATIONS).read_bytes()
    assert companion == (output / "reviewed-inputs-v2.json").read_bytes()
    evidence = json.loads(companion)
    assert evidence["source_observations_sha256"] == digest(raw)
    assert evidence["source_observations_scope"] == "source-adapter-only"
    assert evidence["guest_source_scope"] == "source-stage-only-not-execution"
    assert evidence["frozen_case_counts"] == staging.COUNTS
    assert evidence["admitted_source_parity_count"] == 30
    assert evidence["build_performed"] is False
    assert all(evidence[key] is False for key in staging.NO_EXECUTION)
    script = evidence["staging_script"]
    assert script["path"] == staging.STAGER
    assert script["sha256"] == digest((repository / staging.STAGER).read_bytes())
    assert len(evidence["guest_source_bindings"]) == 10
    for source in evidence["guest_source_bindings"]:
        data = (repository / source["path"]).read_bytes()
        assert source["bytes"] == len(data)
        assert source["sha256"] == digest(data)
        assert (output / source["destination"]).read_bytes() == data
    # Reference results and Host code are external, not executable guest inputs.
    assert not any(
        word in path.name
        for path in output.rglob("*")
        for word in ("oracle", "observations", "broker", "freeze", "observe")
    )
    assert b"output_json" not in companion
    assert b"input_json" not in companion


def test_versioned_plan_matches_existing_sdk_without_building(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "stage"
    plan = staging.stage(output)
    assert plan["format"] == "tobkiri.python-build/3"
    assert plan["entrypoint"] == "app_v2"
    assert plan["wit"] == "wit/pack-v2.wit"
    assert plan["outer_exports"] == [
        "tobkiri:core/lifecycle@1.0.0",
        "local:ai-tool-bridge-ordered-python/bridge@2.0.0",
    ]
    assert plan["allowed_imports"] == [
        f"tobkiri:core/{name}@1.0.0"
        for name in ("types", "flow", "clock", "random", "audit")
    ]
    monkeypatch.syspath_prepend(str(ROOT / "sdk/python"))
    spec = importlib.util.spec_from_file_location(
        "ordered_v2_sdk_structure", ROOT / "sdk/python/build_component.py",
    )
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    verified, raw, plan_hash, sources = sdk._verify_plan(output / "plan.json")
    assert verified == plan
    assert plan_hash == digest(raw)
    assert {str(name) for name, _ in sources} == EXPECTED_FILES


def test_stage_deterministic_and_does_not_change_any_source(
    repository: Path, tmp_path: Path,
) -> None:
    before = {
        path.relative_to(repository): path.read_bytes()
        for path in repository.rglob("*") if path.is_file()
    }
    first, second = tmp_path / "one", tmp_path / "two"
    assert staging.stage(first) == staging.stage(second)
    assert (first / "plan.json").read_bytes() == (second / "plan.json").read_bytes()
    assert before == {
        path.relative_to(repository): path.read_bytes()
        for path in repository.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("target", [
    OBSERVATIONS,
    f"{PREFIX}/oracle.json",
    f"{PREFIX}/oracle.freeze.json",
    f"{PREFIX}/ordered_json.py",
    f"{PREFIX}/ordered_json_v2.py",
    f"{PREFIX}/python/provenance.json",
    f"{PREFIX}/python/inventory.json",
    f"{PREFIX}/python/caller_adapter.py",
    f"{PREFIX}/python/caller_adapter_v2.py",
    f"{PREFIX}/python/original_bridge.py",
    f"{PREFIX}/python/app_v2.py",
    f"{PREFIX}/wit/pack-v2.wit",
    "crates/tobkiri-wit/wit/core/core.wit",
    "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/runtime/bridge.py",
    "tobkiri_runtime/ecosystem/rumi_ai_gateway_pack/runtime/gateway.py",
])
@pytest.mark.parametrize("change", ["tamper", "missing", "symlink"])
def test_missing_dirty_or_linked_source_fails_before_output(
    repository: Path, tmp_path: Path, target: str, change: str,
) -> None:
    path = repository / target
    if change == "tamper":
        path.write_bytes(path.read_bytes() + b"\n# changed\n")
    else:
        retained = path.with_name(path.name + ".retained")
        path.rename(retained)
        if change == "symlink":
            path.symlink_to(retained)
    output = tmp_path / "stage"
    with pytest.raises(ValueError):
        staging.stage(output)
    assert not output.exists()


def test_missing_stager_source_fails_before_output(
    repository: Path, tmp_path: Path,
) -> None:
    (repository / staging.STAGER).unlink()
    with pytest.raises(ValueError):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_optional_local_wit_dependency_is_not_a_build_input(
    repository: Path, tmp_path: Path,
) -> None:
    local = repository / PREFIX / "wit/deps/core.wit"
    local.parent.mkdir()
    local.write_bytes(b"not the canonical Core WIT")
    output = tmp_path / "stage"
    staging.stage(output)
    assert (output / "wit/deps/core.wit").read_bytes() == (
        repository / "crates/tobkiri-wit/wit/core/core.wit"
    ).read_bytes()


def test_missing_output_parent_is_not_created(
    repository: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "absent" / "stage"
    with pytest.raises(ValueError, match="existing trusted parent"):
        staging.stage(output)
    assert not output.parent.exists()


@pytest.mark.parametrize("target", ["source", "destination"])
def test_symlink_parent_rejected(
    repository: Path, tmp_path: Path, target: str,
) -> None:
    parent = repository / PREFIX / "python" if target == "source" else tmp_path
    linked = parent.with_name(parent.name + "-linked")
    if target == "source":
        parent.rename(linked)
        parent.symlink_to(linked, target_is_directory=True)
        output = tmp_path / "stage"
    else:
        linked.symlink_to(parent, target_is_directory=True)
        output = linked / "stage"
    with pytest.raises(ValueError, match="symlink"):
        staging.stage(output)
    assert not output.exists()


@pytest.mark.parametrize("kind", ["file", "directory", "dangling-symlink"])
def test_existing_output_never_replaced(
    repository: Path, tmp_path: Path, kind: str,
) -> None:
    output = tmp_path / "stage"
    if kind == "directory":
        output.mkdir()
        (output / "keep").write_text("unchanged")
    elif kind == "file":
        output.write_text("unchanged")
    else:
        output.symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError, match="fresh|symlink"):
        staging.stage(output)
    if kind == "directory":
        assert (output / "keep").read_text() == "unchanged"
    elif kind == "file":
        assert output.read_text() == "unchanged"
    else:
        assert output.is_symlink()


@pytest.mark.parametrize("key", staging.NO_EXECUTION)
def test_observation_cannot_claim_execution_even_with_rehashed_fixture(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str,
) -> None:
    report = json.loads((repository / OBSERVATIONS).read_bytes())
    report[key] = True
    revise_report(repository, report, monkeypatch)
    with pytest.raises(ValueError, match="proof scope"):
        staging.stage(tmp_path / "stage")


@pytest.mark.parametrize("change", ["count", "parity", "classification", "binding"])
def test_observation_reviewed_identity_is_not_just_a_hash(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    report = json.loads((repository / OBSERVATIONS).read_bytes())
    if change == "count":
        report["admitted_source_parity_count"] = 33
    elif change == "parity":
        report["cases"][0]["admitted_source_parity"] = False
    elif change == "classification":
        report["cases"][0]["classification"] = "original-invalid"
    else:
        report["source_bindings"].pop()
        report["source_bindings_sha256"] = digest(
            staging._canonical(report["source_bindings"]),
        )
    revise_report(repository, report, monkeypatch)
    with pytest.raises(ValueError):
        staging.stage(tmp_path / "stage")


def test_duplicate_observation_keys_rejected(
    repository: Path, tmp_path: Path,
) -> None:
    path = repository / OBSERVATIONS
    path.write_bytes(b'{"protocol":"duplicate",' + path.read_bytes()[1:])
    with pytest.raises(ValueError, match="duplicate"):
        staging.stage(tmp_path / "stage")


def test_source_changed_during_capture_fails_before_output(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = staging._read
    changed = repository / PREFIX / "python/app_v2.py"
    tampered = False

    def capture(path: Path) -> bytes:
        nonlocal tampered
        data = original(path)
        if path == changed and not tampered:
            tampered = True
            path.write_bytes(data + b"\n# changed during capture\n")
        return data

    monkeypatch.setattr(staging, "_read", capture)
    with pytest.raises(ValueError, match="changed during staging"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()
