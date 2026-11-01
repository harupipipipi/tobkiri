"""Bounded source-only provider staging; no producer or Wasm guest is executed."""

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
import prepare_gateway_finalization_providers as preparation
import stage_gateway_finalization_providers as staging


def digest(value: bytes) -> str:
    """Return source identity only, not a behavioral execution observation."""
    return hashlib.sha256(value).hexdigest()


@pytest.fixture(params=tuple(staging.SOURCE_MODULES))
def provider(request: pytest.FixtureRequest) -> str:
    """Select one independently staged provider, never their merged graph."""
    return request.param


@pytest.fixture
def repository(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Copy exact real source inputs before any negative mutation test."""
    root = tmp_path / "repository"
    prefix = staging.PREFIXES[provider]
    inventory = json.loads((ROOT / prefix / "python/inventory.json").read_bytes())
    paths = {
        *(item["path"] for item in inventory["sources"]),
        *staging.DESTINATIONS[provider].values(),
        staging.STAGER, staging.EXTRACTOR, f"{prefix}/README.md",
    }
    for name in paths:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    monkeypatch.setattr(staging, "ROOT", root)
    monkeypatch.setattr(preparation, "ROOT", root)
    return root


def test_exact_nine_staged_sources_are_separate_and_unchanged(
    provider: str, repository: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "stage"
    plan = staging.stage(provider, output)
    expected = set(staging.DESTINATIONS[provider])
    assert len(expected) == len(plan["sources"]) == 9
    assert json.loads((output / "plan.json").read_bytes()) == plan
    assert [item["source"] for item in plan["sources"]] == sorted(expected)
    assert {path.relative_to(output).as_posix() for path in output.rglob("*")
            if path.is_file()} == expected | {"plan.json", "source-stage.json"}
    for item in plan["sources"]:
        name = item["source"]
        data = (output / name).read_bytes()
        assert item["destination"] == staging._producer_destination(name)
        assert digest(data) == item["sha256"] == staging.REVIEWED_SHA256[provider][name]
        assert data == (repository / staging.DESTINATIONS[provider][name]).read_bytes()
    provenance = json.loads((output / "python/provenance.json").read_bytes())
    for reference in provenance["staged_references"]:
        assert (output / reference["stage_path"]).read_bytes() == (
            repository / reference["path"]
        ).read_bytes()
    assert not (output / "no-ambient-dependencies").exists()
    assert not (output / "wit_world").exists()
    assert not any(path.suffix in {".wasm", ".xz", ".pyc"}
                   for path in output.rglob("*"))
    other = ("stream_normalizer" if provider == "usage-cost" else "usage_cost")
    assert not any(other in path.name for path in output.rglob("*"))
    assert not any(token in path.name.lower() for path in output.rglob("*")
                   for token in ("oracle", "fixture", "host", "broker"))


def test_stage_evidence_is_source_only_and_exactly_bound(
    provider: str, repository: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "stage"
    staging.stage(provider, output)
    report = json.loads((output / "source-stage.json").read_bytes())
    assert report["scope"] == "source-stage-only-not-execution"
    assert report["provider"] == provider
    assert report["provider_interface"] == staging.INTERFACES[provider]
    assert report["protocol"] == "ordered-finite-json-v2"
    assert report["build_plan_sha256"] == digest((output / "plan.json").read_bytes())
    assert all(report[key] is False for key in staging.NO_EXECUTION)
    assert len(report["source_bindings"]) == 9
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
        "required_typed_provider_imports": [], "source_dependency_count": 3,
        "ambient_dependency_directory": False,
        "composition_dependency_directory": "no-ambient-dependencies",
        "other_provider_implementation_included": False,
    }
    del report["build_plan_sha256"]
    assert report == json.loads(staging.reviewed_inputs(provider))
    assert b"expected_answer" not in staging.reviewed_inputs(provider)
    assert b"output_json" not in staging.reviewed_inputs(provider)


def test_sdk_verifies_format3_plan_without_running_a_producer(
    provider: str, repository: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("source staging must not execute a producer")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.syspath_prepend(str(ROOT / "sdk/python"))
    spec = importlib.util.spec_from_file_location(
        "finalization_provider_sdk_source_verifier",
        ROOT / "sdk/python/build_component.py",
    )
    assert spec is not None and spec.loader is not None
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    output = tmp_path / "stage"
    plan = staging.stage(provider, output)
    assert plan["format"] == "tobkiri.python-build/3" and plan["stub_wasi"] is True
    assert (plan["wit"], plan["world"], plan["entrypoint"]) == (
        "wit/pack.wit", "pack", "app",
    )
    assert plan["outer_exports"] == [
        "tobkiri:core/lifecycle@1.0.0", staging.INTERFACES[provider],
    ]
    assert plan["allowed_imports"] == [
        f"tobkiri:core/{name}@1.0.0"
        for name in ("types", "flow", "clock", "random", "audit")
    ]
    verified, raw, plan_hash, captured = sdk._verify_plan(output / "plan.json")
    assert verified == plan and digest(raw) == plan_hash
    assert {str(name) for name, _ in captured} == {
        staging._producer_destination(name) for name in staging.DESTINATIONS[provider]
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
    provider: str, repository: Path, tmp_path: Path,
) -> None:
    before = {str(path.relative_to(repository)): path.read_bytes()
              for path in repository.rglob("*") if path.is_file()}
    limits = sys.get_int_max_str_digits(), sys.getrecursionlimit()
    first, second = tmp_path / "one", tmp_path / "two"
    assert staging.stage(provider, first) == staging.stage(provider, second)
    assert {str(path.relative_to(first)): path.read_bytes()
            for path in first.rglob("*") if path.is_file()} == {
        str(path.relative_to(second)): path.read_bytes()
        for path in second.rglob("*") if path.is_file()
    }
    assert before == {str(path.relative_to(repository)): path.read_bytes()
                      for path in repository.rglob("*") if path.is_file()}
    assert (sys.get_int_max_str_digits(), sys.getrecursionlimit()) == limits


MUTATION_PATHS = [
    (provider, relative)
    for provider, prefix in staging.PREFIXES.items()
    for relative in sorted({
        *staging.DESTINATIONS[provider].values(), staging.EXTRACTOR,
        *(item["path"] for item in json.loads(
            (ROOT / prefix / "python/inventory.json").read_bytes(),
        )["sources"]),
    })
]


@pytest.mark.parametrize("provider,relative", MUTATION_PATHS, indirect=["provider"])
@pytest.mark.parametrize("change", ["tamper", "missing", "symlink"])
def test_original_dependency_adapter_and_wit_changes_fail_before_writes(
    provider: str, repository: Path, tmp_path: Path, relative: str, change: str,
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
        staging.stage(provider, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("relative", [
    ".hidden.py", "python/hidden.py", "python/.hidden.py", "python/sitecustomize.py",
    "python/__pycache__/app.pyc", "wit/additional.wit", "wit/deps/hidden.wit",
    "oracle.json", "original_bridge.py", "no-ambient-dependencies/hidden.wit",
])
def test_hidden_or_undeclared_source_additions_are_rejected(
    provider: str, repository: Path, tmp_path: Path, relative: str,
) -> None:
    target = repository / staging.PREFIXES[provider] / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"undeclared source")
    with pytest.raises(ValueError, match="undeclared|unsafe"):
        staging.stage(provider, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("relative", [preparation.GATEWAY,
                                      *preparation.CODEC_PATHS, preparation.CORE_WIT])
def test_coordinated_evidence_regeneration_does_not_bypass_reviewed_pins(
    provider: str, repository: Path, tmp_path: Path, relative: str,
) -> None:
    path = repository / relative
    path.write_bytes(path.read_bytes() + b"\n")
    output = repository / staging.PREFIXES[provider] / "python"
    preparation.prepare(provider, output)
    preparation.verify(provider, output)
    with pytest.raises(ValueError, match="reviewed SHA-256"):
        staging.stage(provider, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling", "fifo"])
def test_existing_destination_is_never_modified(
    provider: str, repository: Path, tmp_path: Path, kind: str,
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
        staging.stage(provider, output)
    assert sentinel.read_bytes() == b"keep"
    if kind == "directory":
        assert (output / "sentinel").read_bytes() == b"keep"
    elif kind == "file":
        assert output.read_bytes() == b"keep"
    elif kind != "fifo":
        assert output.is_symlink()


def test_output_parent_must_exist_and_have_no_linked_ancestor(
    provider: str, repository: Path, tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="existing no-link"):
        staging.stage(provider, tmp_path / "missing" / "stage")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="existing no-link"):
        staging.stage(provider, link / "stage")
    assert not (real / "stage").exists()


@pytest.mark.parametrize("name", ["../bad", "a/../../bad", "a\\bad", "a:bad", "a\0bad"])
def test_unsafe_output_path_is_rejected(
    provider: str, repository: Path, tmp_path: Path, name: str,
) -> None:
    with pytest.raises(ValueError, match="normalized"):
        staging.stage(provider, tmp_path / name)


@pytest.mark.parametrize("name", [
    "", ".", "..", "../bad", "/bad", "a/../bad", "./bad", "a//bad", "bad/",
    "a\\bad", "a:bad", "a\0bad",
])
def test_unsafe_manifest_paths_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="normalized"):
        staging._relative(name)


@pytest.mark.parametrize("kind", ["linked-directory", "hardlink", "fifo"])
def test_linked_or_nonregular_provider_inputs_are_rejected(
    provider: str, repository: Path, tmp_path: Path, kind: str,
) -> None:
    path = repository / staging.PREFIXES[provider] / "python"
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
        staging.stage(provider, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("kind", ["oversized", "fifo"])
def test_oversized_or_nonregular_dependency_is_rejected(
    provider: str, repository: Path, tmp_path: Path, kind: str,
) -> None:
    path = repository / preparation.CORE_WIT
    if kind == "oversized":
        path.write_bytes(b" " * (preparation.MAX_SOURCE_BYTES + 1))
    else:
        path.unlink()
        os.mkfifo(path)
    with pytest.raises(ValueError, match="bounded"):
        staging.stage(provider, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("change", ["source", "hidden"])
def test_source_changes_during_capture_are_rejected(
    provider: str, repository: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    real_verify = preparation.verify
    def changing_verify(selected: str, output: Path) -> dict[str, Any]:
        result = real_verify(selected, output)
        if change == "source":
            path = repository / preparation.CORE_WIT
            path.write_bytes(path.read_bytes() + b"\n")
        else:
            (output / ".hidden.py").write_bytes(b"unreviewed source")
        return result
    monkeypatch.setattr(preparation, "verify", changing_verify)
    with pytest.raises(ValueError, match="changed during staging|undeclared"):
        staging.stage(provider, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_total_source_capture_is_bounded(
    provider: str, repository: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(staging, "MAX_TOTAL_BYTES", 16)
    with pytest.raises(ValueError, match="byte bound"):
        staging.stage(provider, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("change", ["missing", "symlink"])
def test_missing_or_linked_stager_evidence_is_rejected(
    provider: str, repository: Path, tmp_path: Path, change: str,
) -> None:
    path = repository / staging.STAGER
    content = path.read_bytes()
    path.unlink()
    if change == "symlink":
        retained = tmp_path / "stager-source"
        retained.write_bytes(content)
        path.symlink_to(retained)
    with pytest.raises(ValueError):
        staging.stage(provider, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_destination_created_during_review_is_never_overwritten(
    provider: str, repository: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "stage"
    real_review = staging._review
    def changing_review(selected: str) -> tuple[dict, dict]:
        result = real_review(selected)
        output.mkdir()
        (output / "sentinel").write_bytes(b"keep")
        return result
    monkeypatch.setattr(staging, "_review", changing_review)
    with pytest.raises(ValueError, match="fresh"):
        staging.stage(provider, output)
    assert list(output.iterdir()) == [output / "sentinel"]
    assert (output / "sentinel").read_bytes() == b"keep"


def test_link_injected_during_write_cannot_escape_fresh_stage(
    provider: str, repository: Path, tmp_path: Path,
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
        staging.stage(provider, tmp_path / "stage")
    assert list(outside.iterdir()) == [outside / "sentinel"]
    assert (outside / "sentinel").read_bytes() == b"keep"


@pytest.mark.parametrize("selected", ["", "other", "usage", None, []])
def test_unknown_provider_rejected_before_output(tmp_path: Path, selected: Any) -> None:
    with pytest.raises(ValueError, match="unknown"):
        staging.stage(selected, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_source_directory_enumeration_stops_before_unbounded_materialization(
    provider: str, repository: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbounded synthetic directory yields only cap+1 entries before rejection."""
    maximum = len(staging.SOURCE_MODULES[provider]) + 2 + 2
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
        staging.stage(provider, tmp_path / "stage")
    assert len(reads) == maximum + 1
    assert closed == [True]
    assert not (tmp_path / "stage").exists()


def test_fresh_stage_has_private_directories_and_files(
    provider: str, repository: Path, tmp_path: Path,
) -> None:
    """The stage grants no group/other access within its trusted parent."""
    output = tmp_path / "stage"
    staging.stage(provider, output)
    assert output.stat().st_mode & 0o777 == 0o700
    for path in output.rglob("*"):
        expected = 0o700 if path.is_dir() else 0o600
        assert path.stat().st_mode & 0o777 == expected
