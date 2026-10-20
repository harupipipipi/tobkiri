"""Exact finalizer source staging, never producer/guest/typed-ABI acceptance."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import prepare_gateway_finalizer as preparation
import stage_gateway_finalizer as staging


def digest(value: bytes) -> str:
    """Return source identity only, not a behavioral execution observation."""
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Copy exact real source inputs before any negative mutation test."""
    root = tmp_path / "repository"
    prefix = staging.PREFIX
    inventory = json.loads((ROOT / prefix / "python/inventory.json").read_bytes())
    paths = {
        *(item["path"] for item in inventory["sources"]),
        *staging.DESTINATIONS.values(),
        staging.STAGER, staging.EXTRACTOR, *staging.OBSERVER_EVIDENCE,
    }
    for name in paths:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    monkeypatch.setattr(staging, "ROOT", root)
    monkeypatch.setattr(preparation, "ROOT", root)
    return root


def test_exact_thirteen_source_files_exclude_dependency_implementations(
    repository: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "stage"
    plan = staging.stage(output)
    expected = set(staging.DESTINATIONS)
    assert len(expected) == len(plan["sources"]) == 13
    assert json.loads((output / "plan.json").read_bytes()) == plan
    assert [item["source"] for item in plan["sources"]] == sorted(expected)
    assert {path.relative_to(output).as_posix() for path in output.rglob("*")
            if path.is_file()} == expected | {"plan.json", "source-stage.json"}
    for item in plan["sources"]:
        name = item["source"]
        data = (output / name).read_bytes()
        assert item["destination"] == staging._producer_destination(name)
        assert digest(data) == item["sha256"] == staging.REVIEWED_SHA256[name]
        assert data == (repository / staging.DESTINATIONS[name]).read_bytes()
    assert not (output / "no-ambient-dependencies").exists()
    assert not (output / "wit_world").exists()
    assert not any(path.suffix in {".wasm", ".xz", ".pyc"}
                   for path in output.rglob("*"))
    python_names = {path.name for path in (output / "python").iterdir()}
    assert python_names == {
        *staging.SOURCE_MODULES, "ordered_json.py", "ordered_json_v2.py",
    }
    assert not any(token in path.name.lower() for path in output.rglob("*")
                   for token in ("oracle", "fixture", "host", "broker", "source-cases"))



def test_stage_evidence_is_source_only_and_exactly_bound(
    repository: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "stage"
    staging.stage(output)
    report = json.loads((output / "source-stage.json").read_bytes())
    assert report["scope"] == "source-stage-only-not-execution"
    assert report["finalizer_interface"] == staging.INTERFACE
    assert report["protocol"] == "ordered-finite-json-v2"
    assert report["build_plan_sha256"] == digest((output / "plan.json").read_bytes())
    assert all(report[key] is False for key in staging.NO_EXECUTION)
    assert len(report["source_bindings"]) == 13
    assert report["source_bindings_sha256"] == digest(
        staging._canonical(report["source_bindings"])
    )
    for binding in report["source_bindings"]:
        data = (repository / binding["path"]).read_bytes()
        assert binding["bytes"] == len(data)
        assert binding["sha256"] == digest(data)
        assert (output / binding["destination"]).read_bytes() == data
        assert binding["producer_destination"] == staging._producer_destination(
            binding["destination"],
        )
    for key, name in (("staging_script", staging.STAGER),
                      ("extraction_script", staging.EXTRACTOR)):
        assert report[key] == staging._record(name, (repository / name).read_bytes())
    assert report["extraction_script"]["sha256"] == staging.REVIEWED_EXTRACTOR_SHA256
    assert report["dependency_closure"] == {
        "required_typed_provider_imports": list(staging.REQUIRED_INTERFACES),
        "transitive_wit_interface": staging.TRANSITIVE_WIT_INTERFACE,
        "staged_source_count": 13, "source_dependency_count": 7,
        "python_module_count": 5, "python_evidence_file_count": 2,
        "wit_file_count": 6,
        "ambient_dependency_directory": False,
        "composition_dependency_directory": "no-ambient-dependencies",
        "other_provider_implementation_included": False,
    }
    assert report["typed_abi_verification_performed"] is False
    assert report["observer_evidence_in_guest_sources"] is False
    assert report["transport_dependency_evidence"] == json.loads(
        (repository / staging.PREFIX / "python/provenance.json").read_bytes(),
    )["transport_dependencies"]
    assert len(report["transport_dependency_evidence"]) == 20
    assert {row["path"] for row in report["observer_evidence"]} == set(
        staging.OBSERVER_EVIDENCE,
    )
    for row in report["observer_evidence"]:
        assert row == staging._record(
            row["path"], (repository / row["path"]).read_bytes(),
        )
    del report["build_plan_sha256"]
    assert report == json.loads(staging.reviewed_inputs())
    assert b"expected_answer" not in staging.reviewed_inputs()
    assert b"output_json" not in staging.reviewed_inputs()


def test_sdk_verifies_format3_plan_without_running_a_producer(
    repository: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("source staging must not execute a producer")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.syspath_prepend(str(ROOT / "sdk/python"))
    spec = importlib.util.spec_from_file_location(
        "finalizer_sdk_source_verifier",
        ROOT / "sdk/python/build_component.py",
    )
    assert spec is not None and spec.loader is not None
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    output = tmp_path / "stage"
    plan = staging.stage(output)
    assert plan["format"] == "tobkiri.python-build/3" and plan["stub_wasi"] is True
    assert (plan["wit"], plan["world"], plan["entrypoint"]) == (
        "wit/pack.wit", "pack", "app",
    )
    assert plan["outer_exports"] == [
        "tobkiri:core/lifecycle@1.0.0", staging.INTERFACE,
    ]
    assert plan["allowed_imports"] == [
        *(f"tobkiri:core/{name}@1.0.0"
          for name in ("types", "flow", "clock", "random", "audit")),
        *staging.REQUIRED_INTERFACES,
    ]
    verified, raw, plan_hash, captured = sdk._verify_plan(output / "plan.json")
    assert verified == plan and digest(raw) == plan_hash
    assert {str(name) for name, _ in captured} == {
        staging._producer_destination(name) for name in staging.DESTINATIONS
    }
    lookup = {item["destination"]: item["source"] for item in plan["sources"]}
    assert all(data == (output / lookup[str(name)]).read_bytes()
               for name, data in captured)
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


def test_staging_is_deterministic_and_does_not_change_sources_or_interpreter(
    repository: Path, tmp_path: Path,
) -> None:
    before = {str(path.relative_to(repository)): path.read_bytes()
              for path in repository.rglob("*") if path.is_file()}
    limits = sys.get_int_max_str_digits(), sys.getrecursionlimit()
    first, second = tmp_path / "one", tmp_path / "two"
    assert staging.stage(first) == staging.stage(second)
    assert {str(path.relative_to(first)): path.read_bytes()
            for path in first.rglob("*") if path.is_file()} == {
        str(path.relative_to(second)): path.read_bytes()
        for path in second.rglob("*") if path.is_file()
    }
    assert before == {str(path.relative_to(repository)): path.read_bytes()
                      for path in repository.rglob("*") if path.is_file()}
    assert (sys.get_int_max_str_digits(), sys.getrecursionlimit()) == limits


MUTATION_PATHS = sorted({
    *staging.DESTINATIONS.values(), staging.EXTRACTOR, *staging.OBSERVER_EVIDENCE,
    *(item["path"] for item in json.loads(
        (ROOT / staging.PREFIX / "python/inventory.json").read_bytes(),
    )["sources"]),
})


@pytest.mark.parametrize("relative", MUTATION_PATHS)
@pytest.mark.parametrize("change", ["tamper", "missing", "symlink"])
def test_original_dependency_adapter_and_wit_changes_fail_before_writes(
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
    with pytest.raises(ValueError):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("relative", [
    ".hidden.py", "python/hidden.py", "python/.hidden.py", "python/sitecustomize.py",
    "python/__pycache__/app.pyc", "wit/additional.wit", "wit/deps/hidden.wit",
    "oracle.json", "original_bridge.py", "no-ambient-dependencies/hidden.wit",
])
def test_hidden_or_undeclared_source_additions_are_rejected(
    repository: Path, tmp_path: Path, relative: str,
) -> None:
    target = repository / staging.PREFIX / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"undeclared source")
    with pytest.raises(ValueError, match="undeclared|unsafe"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("relative", [preparation.GATEWAY,
                                      *preparation.CODEC_PATHS, preparation.CORE_WIT])
def test_coordinated_evidence_regeneration_does_not_bypass_reviewed_pins(
    repository: Path, tmp_path: Path, relative: str,
) -> None:
    path = repository / relative
    path.write_bytes(path.read_bytes() + b"\n")
    output = repository / staging.PREFIX / "python"
    preparation.prepare(output)
    preparation.verify(output)
    with pytest.raises(ValueError, match="reviewed SHA-256"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling", "fifo"])
def test_existing_destination_is_never_modified(
    repository: Path, tmp_path: Path, kind: str,
) -> None:
    output, retained = tmp_path / "stage", tmp_path / "retained"
    retained.mkdir()
    sentinel = retained / "sentinel"
    sentinel.write_bytes(b"keep")
    if kind == "directory":
        output.mkdir()
        (output / "sentinel").write_bytes(b"keep")
    elif kind == "file":
        output.write_bytes(b"keep")
    elif kind == "fifo":
        os.mkfifo(output)
    else:
        output.symlink_to(retained if kind == "symlink" else tmp_path / "missing")
    with pytest.raises(ValueError, match="fresh"):
        staging.stage(output)
    assert sentinel.read_bytes() == b"keep"
    if kind == "directory":
        assert (output / "sentinel").read_bytes() == b"keep"
    elif kind == "file":
        assert output.read_bytes() == b"keep"
    elif kind != "fifo":
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


@pytest.mark.parametrize("kind", ["linked-directory", "hardlink", "fifo"])
def test_linked_or_nonregular_provider_inputs_are_rejected(
    repository: Path, tmp_path: Path, kind: str,
) -> None:
    path = repository / staging.PREFIX / "python"
    if kind == "linked-directory":
        retained = tmp_path / "retained-python"
        path.rename(retained)
        path.symlink_to(retained, target_is_directory=True)
    elif kind == "hardlink":
        os.link(path / "app.py", tmp_path / "hardlink")
    else:
        (path / "app.py").unlink()
        os.mkfifo(path / "app.py")
    with pytest.raises(ValueError, match="unsafe"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("kind", ["oversized", "fifo"])
def test_oversized_or_nonregular_dependency_is_rejected(
    repository: Path, tmp_path: Path, kind: str,
) -> None:
    path = repository / preparation.CORE_WIT
    if kind == "oversized":
        path.write_bytes(b" " * (preparation.MAX_SOURCE_BYTES + 1))
    else:
        path.unlink()
        os.mkfifo(path)
    with pytest.raises(ValueError, match="bounded"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("change", ["source", "hidden"])
def test_source_changes_during_capture_are_rejected(
    repository: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    real_verify = preparation.verify
    def changing_verify(output: Path) -> dict[str, Any]:
        result = real_verify(output)
        if change == "source":
            path = repository / preparation.CORE_WIT
            path.write_bytes(path.read_bytes() + b"\n")
        else:
            (output / ".hidden.py").write_bytes(b"unreviewed source")
        return result
    monkeypatch.setattr(preparation, "verify", changing_verify)
    with pytest.raises(ValueError, match="changed during staging|undeclared"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_total_source_capture_is_bounded(
    repository: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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


def test_destination_created_during_review_is_never_overwritten(
    repository: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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


def test_link_injected_during_write_cannot_escape_fresh_stage(
    repository: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"keep")
    real_write = staging._write
    def injecting_write(descriptor: int, relative: str, content: bytes) -> None:
        if relative.startswith("python/"):
            os.symlink(outside, "python", dir_fd=descriptor)
        real_write(descriptor, relative, content)
    monkeypatch.setattr(staging, "_write", injecting_write)
    with pytest.raises(ValueError, match="no-link"):
        staging.stage(tmp_path / "stage")
    assert list(outside.iterdir()) == [outside / "sentinel"]
    assert (outside / "sentinel").read_bytes() == b"keep"


def test_source_directory_enumeration_stops_before_unbounded_materialization(
    repository: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbounded synthetic directory yields only cap+1 entries before rejection."""
    maximum = len(staging.SOURCE_MODULES) + 3 + 2
    reads, closed = [], []
    class UnboundedEntries:
        def __enter__(self) -> "UnboundedEntries":
            return self

        def __exit__(self, *args: Any) -> None:
            closed.append(True)

        def __iter__(self) -> "UnboundedEntries":
            return self

        def __next__(self) -> Any:
            reads.append(len(reads))
            if len(reads) > maximum + 1:
                pytest.fail("directory enumeration exceeded its entry cap")
            return type("Entry", (), {"name": f"entry-{len(reads)}"})()

    def scandir(descriptor: int) -> UnboundedEntries:
        assert type(descriptor) is int
        return UnboundedEntries()

    def forbidden_listdir(*args: Any) -> None:
        pytest.fail("listdir would materialize unbounded directory contents")

    monkeypatch.setattr(staging.os, "scandir", scandir)
    monkeypatch.setattr(staging.os, "listdir", forbidden_listdir)
    with pytest.raises(ValueError, match="undeclared inputs"):
        staging.stage(tmp_path / "stage")
    assert len(reads) == maximum + 1
    assert closed == [True]
    assert not (tmp_path / "stage").exists()


def test_fresh_stage_has_private_directories_and_files(
    repository: Path, tmp_path: Path,
) -> None:
    """The stage grants no group/other access within its trusted parent."""
    output = tmp_path / "stage"
    staging.stage(output)
    assert output.stat().st_mode & 0o777 == 0o700
    for path in output.rglob("*"):
        expected = 0o700 if path.is_dir() else 0o600
        assert path.stat().st_mode & 0o777 == expected


@pytest.mark.parametrize("change", [
    "missing-transitive-wit", "source-cases-added", "dependency-python-added",
    "dependency-python-substituted",
])
def test_unapproved_closure_cannot_be_approved_by_floating_pin_changes(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    """Shape and source paths stay fixed independently of the mutable pin map."""
    destinations = dict(staging.DESTINATIONS)
    pins = dict(staging.REVIEWED_SHA256)
    if change == "missing-transitive-wit":
        path = "wit/deps/ai-tool-bridge-ordered-python/pack.wit"
        del destinations[path]
        del pins[path]
    else:
        if change == "source-cases-added":
            path = "python/source-cases.json"
            source = f"{staging.PREFIX}/source-cases.json"
        elif change == "dependency-python-added":
            path = "python/usage_cost_adapter.py"
            source = "packs/local.ai-usage-cost-python/python/usage_cost_adapter.py"
        else:
            path = "python/gateway_finalizer_adapter.py"
            source = "packs/local.ai-usage-cost-python/python/usage_cost_adapter.py"
        destinations[path] = source
        pins[path] = digest((repository / source).read_bytes())
    monkeypatch.setattr(staging, "DESTINATIONS", destinations)
    monkeypatch.setattr(staging, "REVIEWED_SHA256", pins)
    with pytest.raises(ValueError, match="closure differs"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("change", ["duplicate", "mismatched", "missing"])
def test_wit_package_identity_checks_are_not_only_hash_checks(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    destination = "wit/deps/ai-usage-cost-python/pack.wit"
    source = repository / staging.DESTINATIONS[destination]
    data = source.read_bytes()
    declaration = b"package local:ai-usage-cost-python@1.0.0;"
    if change == "duplicate":
        data += b"\n" + declaration + b"\n"
    elif change == "mismatched":
        data = data.replace(
            declaration, b"package local:ai-gateway-tool-caller-python@1.0.0;",
        )
    else:
        data = data.replace(declaration, b"")
    source.write_bytes(data)
    monkeypatch.setattr(staging, "REVIEWED_SHA256", {
        **staging.REVIEWED_SHA256, destination: digest(data),
    })
    with pytest.raises(ValueError, match="WIT package identity"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_direct_import_identity_mismatch_fails_before_output(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = repository / staging.DESTINATIONS["wit/pack.wit"]
    data = source.read_bytes().replace(
        b"import local:ai-usage-cost-python/usage-cost@1.0.0;",
        b"import local:ai-usage-cost-python/other@1.0.0;",
    )
    source.write_bytes(data)
    monkeypatch.setattr(staging, "REVIEWED_SHA256", {
        **staging.REVIEWED_SHA256, "wit/pack.wit": digest(data),
    })
    with pytest.raises(ValueError, match="direct typed imports"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_canonical_dependency_wit_is_whole_and_transitive_import_is_retained(
    repository: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "stage"
    plan = staging.stage(output)
    caller = (output / "wit/deps/ai-gateway-tool-caller-python/pack.wit").read_bytes()
    assert caller == (repository / preparation.DEPENDENCY_WIT[0]).read_bytes()
    assert b"world pack" in caller
    assert f"import {staging.TRANSITIVE_WIT_INTERFACE};".encode() in caller
    transitive = (output / "wit/deps/ai-tool-bridge-ordered-python/pack.wit")
    assert transitive.read_bytes() == (
        repository / preparation.TRANSITIVE_WIT
    ).read_bytes()
    assert staging.TRANSITIVE_WIT_INTERFACE not in plan["allowed_imports"]
    assert set(staging.REQUIRED_INTERFACES) <= set(plan["allowed_imports"])


def test_catalog_answers_remain_external_observer_evidence_only(
    repository: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "stage"
    plan = staging.stage(output)
    source_paths = {row["source"] for row in plan["sources"]}
    assert not any("source-cases" in path for path in source_paths)
    catalog = json.loads(
        (repository / staging.PREFIX / "source-cases.json").read_bytes(),
    )
    assert len(catalog["cases"]) == 10
    evidence = (output / "source-stage.json").read_bytes()
    assert b"expected_kind" not in evidence
    assert b"expected_error" not in evidence
    assert b"expected_dependency_call_count" not in evidence
    for row in catalog["cases"]:
        assert row["id"].encode() not in evidence
    assert not any(path.name == "source-cases.json" for path in output.rglob("*"))


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_reviewed_pin_allowlist_is_exact(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    pins = dict(staging.REVIEWED_SHA256)
    if change == "missing":
        del pins["python/app.py"]
    else:
        pins["python/extra.py"] = "0" * 64
    monkeypatch.setattr(staging, "REVIEWED_SHA256", pins)
    with pytest.raises(ValueError, match="exact 13-file"):
        staging.stage(tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_output_directory_replacement_is_detected_without_outside_write(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "stage"
    moved = tmp_path / "retained-stage"
    real_write = staging._write

    def replacing_write(descriptor: int, relative: str, content: bytes) -> None:
        real_write(descriptor, relative, content)
        if relative == "source-stage.json":
            output.rename(moved)
            output.mkdir()
            (output / "sentinel").write_bytes(b"keep")

    monkeypatch.setattr(staging, "_write", replacing_write)
    with pytest.raises(ValueError, match="destination changed"):
        staging.stage(output)
    assert list(output.iterdir()) == [output / "sentinel"]
    assert (output / "sentinel").read_bytes() == b"keep"
    assert (moved / "source-stage.json").is_file()
