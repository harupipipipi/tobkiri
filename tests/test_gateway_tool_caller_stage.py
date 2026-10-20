"""Original Gateway source staging tests, never a build or guest execution."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import prepare_gateway_tool_caller as preparation
import stage_gateway_tool_caller as staging

PREFIX = "packs/local.ai-gateway-tool-caller-python"
EXPECTED_FILES = {
    "original_gateway_tool_caller.py", "gateway_tool_caller_adapter.py", "app.py",
    "inventory.json", "provenance.json", "ordered_json.py", "ordered_json_v2.py",
    "wit/pack.wit", "wit/deps/core.wit", "wit/deps/bridge-v2.wit",
}


def digest(value: bytes) -> str:
    """Return a source byte identity, not a behavioral observation."""
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every real source input before mutation tests."""
    root = tmp_path / "repository"
    inventory = json.loads((ROOT / PREFIX / "python/inventory.json").read_bytes())
    paths = {
        *(item["path"] for item in inventory["sources"]),
        *staging.DESTINATIONS.values(),
        staging.STAGER, staging.EXTRACTOR, f"{PREFIX}/README.md",
    }
    for name in paths:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    monkeypatch.setattr(staging, "ROOT", root)
    monkeypatch.setattr(staging, "PACK", root / PREFIX)
    monkeypatch.setattr(preparation, "ROOT", root)
    return root


def test_stage_captures_exact_ten_source_files_and_no_executable_dependency(
    repository: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "stage"
    plan = staging.stage(output)
    assert json.loads((output / "plan.json").read_bytes()) == plan
    assert {item["source"] for item in plan["sources"]} == EXPECTED_FILES
    assert len(plan["sources"]) == len(EXPECTED_FILES) == 10
    assert [item["source"] for item in plan["sources"]] == sorted(EXPECTED_FILES)
    assert {path.relative_to(output).as_posix() for path in output.rglob("*")
            if path.is_file()} == EXPECTED_FILES | {"plan.json", "source-stage.json"}
    for item in plan["sources"]:
        name = item["source"]
        assert name == item["destination"]
        data = (output / name).read_bytes()
        assert digest(data) == item["sha256"] == staging.REVIEWED_SHA256[name]
        assert data == (repository / staging.DESTINATIONS[name]).read_bytes()
    assert not (output / "no-ambient-dependencies").exists()
    assert not (output / "original_bridge.py").exists()
    assert not (output / "wit_world").exists()
    assert not any(path.suffix in {".wasm", ".xz", ".pyc"}
                   for path in output.rglob("*"))
    assert not any(token in path.name.lower()
                   for path in output.rglob("*")
                   for token in ("oracle", "fixture", "host", "broker"))


def test_stage_evidence_is_hash_only_and_no_execution_claims(
    repository: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "stage"
    staging.stage(output)
    report = json.loads((output / "source-stage.json").read_bytes())
    assert report["scope"] == "source-stage-only-not-execution"
    assert report["protocol"] == "ordered-finite-json-v2"
    assert report["build_plan_sha256"] == digest((output / "plan.json").read_bytes())
    assert all(report[key] is False for key in staging.NO_EXECUTION)
    assert len(report["source_bindings"]) == 10
    assert report["source_bindings_sha256"] == digest(
        staging._canonical(report["source_bindings"])
    )
    for binding in report["source_bindings"]:
        data = (repository / binding["path"]).read_bytes()
        assert binding["bytes"] == len(data)
        assert binding["sha256"] == digest(data)
        assert (output / binding["destination"]).read_bytes() == data
    for key, name in (("staging_script", staging.STAGER),
                      ("extraction_script", staging.EXTRACTOR)):
        assert report[key] == staging._record(
            name, (repository / name).read_bytes(),
        )
    assert report["extraction_script"]["sha256"] == staging.REVIEWED_EXTRACTOR_SHA256
    assert report["dependency_closure"] == {
        "required_typed_import": staging.BRIDGE_INTERFACE,
        "source_dependency_count": 4,
        "ambient_dependency_directory": False,
        "composition_dependency_directory": "no-ambient-dependencies",
        "local_bridge_implementation_included": False,
    }
    del report["build_plan_sha256"]
    assert report == json.loads(staging.reviewed_inputs())
    assert b"expected_answer" not in staging.reviewed_inputs()
    assert b"output_json" not in staging.reviewed_inputs()


def test_exact_plan_is_accepted_by_sdk_verifier_without_running_producer(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("source staging must not execute a producer")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.syspath_prepend(str(ROOT / "sdk/python"))
    spec = importlib.util.spec_from_file_location(
        "gateway_caller_sdk_source_verifier", ROOT / "sdk/python/build_component.py",
    )
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    output = tmp_path / "stage"
    plan = staging.stage(output)
    assert plan["format"] == "tobkiri.python-build/3"
    assert plan["stub_wasi"] is True
    assert (plan["wit"], plan["world"], plan["entrypoint"]) == (
        "wit/pack.wit", "pack", "app",
    )
    assert plan["outer_exports"] == [
        "tobkiri:core/lifecycle@1.0.0", staging.INTERFACE,
    ]
    assert plan["allowed_imports"] == [
        *(f"tobkiri:core/{name}@1.0.0"
          for name in ("types", "flow", "clock", "random", "audit")),
        staging.BRIDGE_INTERFACE,
    ]
    verified, raw, plan_hash, captured = sdk._verify_plan(output / "plan.json")
    assert verified == plan
    assert digest(raw) == plan_hash
    assert {str(name) for name, _ in captured} == EXPECTED_FILES
    assert all(data == (output / str(name)).read_bytes() for name, data in captured)
    assert sdk._componentize_commands(Path("componentize-py"), plan)[1] == [
        "componentize-py", "-d", "wit", "-w", "pack", "componentize",
        "--stub-wasi", "app", "-o", "component.wasm",
    ]
    import composition

    command = composition._composition_commands(
        Path("wac"), output / "component.wasm", output, output / "wit", "pack",
    )[0]
    assert command[command.index("--deps-dir") + 1] == str(
        output / "no-ambient-dependencies",
    )
    assert not (output / "no-ambient-dependencies").exists()


def test_stage_is_deterministic_and_preserves_all_input_bytes(
    repository: Path, tmp_path: Path,
) -> None:
    before = {str(path.relative_to(repository)): path.read_bytes()
              for path in repository.rglob("*") if path.is_file()}
    first, second = tmp_path / "one", tmp_path / "two"
    assert staging.stage(first) == staging.stage(second)
    assert {str(path.relative_to(first)): path.read_bytes()
            for path in first.rglob("*") if path.is_file()} == {
        str(path.relative_to(second)): path.read_bytes()
        for path in second.rglob("*") if path.is_file()
    }
    assert before == {str(path.relative_to(repository)): path.read_bytes()
                      for path in repository.rglob("*") if path.is_file()}


MUTATION_PATHS = sorted({
    *staging.DESTINATIONS.values(), staging.EXTRACTOR,
    *(item["path"] for item in json.loads(
        (ROOT / PREFIX / "python/inventory.json").read_bytes(),
    )["sources"]),
})


@pytest.mark.parametrize("relative", MUTATION_PATHS)
@pytest.mark.parametrize("change", ["tamper", "missing", "symlink"])
def test_changed_original_dependency_adapter_or_wit_fails_before_output(
    repository: Path, tmp_path: Path, relative: str, change: str,
) -> None:
    path = repository / relative
    if change == "tamper":
        path.write_bytes(path.read_bytes() + b"\n# changed\n")
    else:
        content = path.read_bytes()
        path.unlink()
        if change == "symlink":
            retained = tmp_path / "linked-content"
            retained.write_bytes(content)
            path.symlink_to(retained)
    output = tmp_path / "stage"
    with pytest.raises(ValueError):
        staging.stage(output)
    assert not output.exists()


@pytest.mark.parametrize("relative", [
    ".hidden.py", "python/hidden.py", "python/.hidden.py", "python/sitecustomize.py",
    "python/__pycache__/app.pyc", "wit/additional.wit", "wit/deps/hidden.wit",
    "original_bridge.py", "oracle.json", "no-ambient-dependencies/hidden.wit",
])
def test_hidden_or_undeclared_caller_source_additions_fail(
    repository: Path, tmp_path: Path, relative: str,
) -> None:
    target = repository / PREFIX / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"undeclared source")
    with pytest.raises(ValueError, match="undeclared|unsafe"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_coordinated_original_and_evidence_regeneration_still_requires_review(
    repository: Path, tmp_path: Path,
) -> None:
    path = repository / preparation.GATEWAY
    path.write_bytes(path.read_bytes() + b"\n# source changed\n")
    preparation.prepare(repository / PREFIX / "python")
    preparation.verify(repository / PREFIX / "python")
    with pytest.raises(ValueError, match="reviewed SHA-256"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("relative", [
    *preparation.CODEC_PATHS, preparation.CORE_WIT, preparation.BRIDGE_WIT,
])
def test_coordinated_dependency_and_evidence_regeneration_requires_review(
    repository: Path, tmp_path: Path, relative: str,
) -> None:
    path = repository / relative
    path.write_bytes(path.read_bytes() + b"\n")
    preparation.prepare(repository / PREFIX / "python")
    preparation.verify(repository / PREFIX / "python")
    with pytest.raises(ValueError, match="reviewed SHA-256"):
        staging.stage(tmp_path / "stage")


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling"])
def test_existing_destination_is_not_changed(
    repository: Path, tmp_path: Path, kind: str,
) -> None:
    output = tmp_path / "stage"
    retained = tmp_path / "retained"
    retained.mkdir()
    sentinel = retained / "sentinel"
    sentinel.write_bytes(b"keep")
    if kind == "directory":
        output.mkdir()
        (output / "sentinel").write_bytes(b"keep")
    elif kind == "file":
        output.write_bytes(b"keep")
    else:
        output.symlink_to(retained if kind == "symlink" else tmp_path / "missing")
    with pytest.raises(ValueError, match="fresh"):
        staging.stage(output)
    assert sentinel.read_bytes() == b"keep"
    if kind == "directory":
        assert (output / "sentinel").read_bytes() == b"keep"
    elif kind == "file":
        assert output.read_bytes() == b"keep"
    else:
        assert output.is_symlink()


def test_output_parent_must_exist_and_have_no_linked_ancestor(
    repository: Path, tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="existing no-link"):
        staging.stage(tmp_path / "missing" / "stage")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="existing no-link"):
        staging.stage(link / "stage")
    assert not (real / "stage").exists()


@pytest.mark.parametrize("name", ["../bad", "a/../../bad", "a\\bad", "a:bad", "a\0bad"])
def test_unsafe_output_path_is_rejected(
    repository: Path, tmp_path: Path, name: str,
) -> None:
    with pytest.raises(ValueError, match="normalized"):
        staging.stage(tmp_path / name)


@pytest.mark.parametrize("name", [
    "", ".", "..", "../bad", "/bad", "a/../bad", "./bad", "a//bad", "bad/",
    "a\\bad", "a:bad", "a\0bad",
])
def test_unsafe_manifest_paths_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="normalized"):
        staging._relative(name)


def test_linked_source_directory_is_rejected(
    repository: Path, tmp_path: Path,
) -> None:
    path = repository / PREFIX / "python"
    retained = tmp_path / "retained-python"
    path.rename(retained)
    path.symlink_to(retained, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        staging.stage(tmp_path / "stage")


def test_hardlinked_caller_source_is_rejected(
    repository: Path, tmp_path: Path,
) -> None:
    path = repository / PREFIX / "python/app.py"
    os.link(path, tmp_path / "hardlink")
    with pytest.raises(ValueError, match="unsafe"):
        staging.stage(tmp_path / "stage")


def test_oversized_and_nonregular_dependency_are_rejected(
    repository: Path, tmp_path: Path,
) -> None:
    path = repository / preparation.CORE_WIT
    path.write_bytes(b" " * (preparation.MAX_SOURCE_BYTES + 1))
    with pytest.raises(ValueError, match="bounded"):
        staging.stage(tmp_path / "stage")
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError, match="bounded"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_changed_input_during_capture_is_rejected(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_verify = preparation.verify

    def changing_verify(output: Path) -> dict:
        result = real_verify(output)
        path = repository / preparation.CORE_WIT
        path.write_bytes(path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(preparation, "verify", changing_verify)
    with pytest.raises(ValueError, match="changed during staging"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_source_capture_has_total_byte_bound(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(staging, "MAX_TOTAL_BYTES", 16)
    with pytest.raises(ValueError, match="byte bound"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("change", ["missing", "symlink"])
def test_missing_or_linked_stager_evidence_is_rejected(
    repository: Path, tmp_path: Path, change: str,
) -> None:
    path = repository / staging.STAGER
    content = path.read_bytes()
    path.unlink()
    if change == "symlink":
        retained = tmp_path / "stager-source"
        retained.write_bytes(content)
        path.symlink_to(retained)
    with pytest.raises(ValueError):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_concurrent_hidden_source_addition_is_rejected(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_verify = preparation.verify

    def changing_verify(output: Path) -> dict:
        result = real_verify(output)
        (output / ".hidden.py").write_bytes(b"unreviewed source")
        return result

    monkeypatch.setattr(preparation, "verify", changing_verify)
    with pytest.raises(ValueError, match="undeclared"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_destination_created_during_review_is_never_overwritten(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "stage"
    real_review = staging._review

    def changing_review() -> tuple[dict, dict]:
        result = real_review()
        output.mkdir()
        (output / "sentinel").write_bytes(b"keep")
        return result

    monkeypatch.setattr(staging, "_review", changing_review)
    with pytest.raises(ValueError, match="fresh"):
        staging.stage(output)
    assert list(output.iterdir()) == [output / "sentinel"]
    assert (output / "sentinel").read_bytes() == b"keep"


def test_link_injected_during_write_cannot_escape_fresh_directory(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"keep")
    real_write = staging._write

    def injecting_write(descriptor: int, relative: str, content: bytes) -> None:
        if relative.startswith("wit/"):
            os.symlink(outside, "wit", dir_fd=descriptor)
        real_write(descriptor, relative, content)

    monkeypatch.setattr(staging, "_write", injecting_write)
    with pytest.raises(ValueError, match="no-link"):
        staging.stage(tmp_path / "stage")
    assert list(outside.iterdir()) == [outside / "sentinel"]
    assert (outside / "sentinel").read_bytes() == b"keep"
