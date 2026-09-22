"""Regression tests for final generated closure re-sealing."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
BUILDER_PATH = ROOT / ".github/scripts/build_sealed_python_environment.py"
FIXTURE_PATH = ROOT / "tobkiri_runtime/tests/test_sealed_python_environment.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load test dependency: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUILDER = _load(BUILDER_PATH, "sealed_application_closure_builder")
FIXTURES = _load(FIXTURE_PATH, "sealed_application_closure_fixtures")


def _packaged_application_closure(base: Path) -> Path:
    app = base / "packaged-app"
    artifact_id = "shell.fixture.default.linux-x86_64"
    digest = "sha256:" + "7" * 64
    artifact_ref = "fixture-shell"
    files = {
        "bundled/presentation_catalog.json": {
            "shell_providers": [{
                "provider_id": "shell.fixture.default",
                "artifact_variants": [{"artifact_id": artifact_id, "sha256": digest}],
            }]
        },
        "bundled/presentation_release.v4.json": {"artifact_id": artifact_id},
        "bundled/shell_artifact_index.v4.json": {
            "artifact_id": artifact_id,
            "sha256": digest,
            "path": "bundled/presentation-artifacts/fixture/fixture-shell",
        },
        "bundled/shell_profile_lock.v4.json": {"artifact_id": artifact_id},
        "ecosystem/defaultspack/pack.v4.json": {"pack_id": "defaultspack"},
        "ecosystem/defaultspack/contracts.v4.json": {"contracts": []},
        "ecosystem/defaultspack/artifact-index.v4.json": {"artifacts": []},
        "ecosystem/defaultspack/executables.v4.json": {"executables": []},
        "ecosystem/defaultspack/v4/defaults.profile.v4.json": {"profile_id": "defaults"},
        "ecosystem/defaultspack/v4/bundle.lock.json": {
            "schema": BUILDER.PACKAGED_APPLICATION_BUNDLE_LOCK_SCHEMA,
            "entries": [],
        },
        "ecosystem/defaultspack/v4/shell.fixture.default.shell.v1.json": {
            "provider_id": "shell.fixture.default",
            "availability": "verified",
            "artifact_digest": digest,
            "launch": {"variants": [{
                "artifact_id": artifact_id,
                "artifact_digest": digest,
                "artifact_ref": artifact_ref,
            }]},
        },
    }
    for relative, value in files.items():
        path = app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    presentation = app / "bundled/presentation-artifacts/fixture/fixture-shell"
    presentation.parent.mkdir(parents=True, exist_ok=True)
    presentation.write_bytes(b"presentation shell\n")
    platform = app / "ecosystem/defaultspack/platform-artifacts" / artifact_ref
    platform.parent.mkdir(parents=True, exist_ok=True)
    platform.write_bytes(b"runtime shell\n")
    for index in range(28):
        extra = platform.parent / f"closure-{index:02d}.json"
        extra.write_text(f'{{"index":{index}}}\n', encoding="utf-8")
    bundle_root = app / "ecosystem/defaultspack/v4"
    lock_path = bundle_root / "bundle.lock.json"
    entries = []
    for path in sorted(bundle_root.rglob("*")):
        if not path.is_file() or path.name == lock_path.name:
            continue
        relative = path.relative_to(bundle_root).as_posix()
        kind = "profile" if path.name == "defaults.profile.v4.json" else "shell"
        entries.append(
            {
                "path": relative,
                "kind": kind,
                "digest": f"sha256:{BUILDER._sha256_file(path)}",
            }
        )
    lock_path.write_text(
        json.dumps(
            {
                "schema": BUILDER.PACKAGED_APPLICATION_BUNDLE_LOCK_SCHEMA,
                "entries": entries,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    for relative in (
        "app.py",
        "ecosystem/defaultspack/defaultspack/desktop_app.py",
        "core_runtime/host_broker/computer_host_helper.py",
    ):
        source = ROOT / "tobkiri_runtime" / relative
        destination = app / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    return app


def _add_locked_catalog(application: Path) -> tuple[Path, Path]:
    """Add one lock-bound catalog to the synthetic packaged closure."""
    bundle_root = application / "ecosystem/defaultspack/v4"
    relative = "packs/fixture.executables.v4.json"
    catalog = bundle_root / relative
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_bytes(b'{"fixture_catalog":true}\n')
    lock_path = bundle_root / "bundle.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["entries"].append(
        {
            "path": relative,
            "kind": "executable_catalog",
            "digest": f"sha256:{BUILDER._sha256_file(catalog)}",
        }
    )
    lock_path.write_text(json.dumps(lock, sort_keys=True) + "\n", encoding="utf-8")
    return catalog, lock_path


def _run_reseal(sealed: Path, application: Path, target: str) -> subprocess.CompletedProcess[str]:
    digest = BUILDER._sha256_file(sealed / BUILDER.MANIFEST_FILENAME)
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(BUILDER_PATH),
            "--target",
            target,
            "--output-root",
            str(sealed),
            "--base-root",
            str(sealed),
            "--expected-base-manifest-sha256",
            digest,
            "--rebase-application-source",
            str(application),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _embedded_sealed_application(
    tmp_path: Path,
    target: str,
) -> tuple[Path, Path]:
    application = _packaged_application_closure(tmp_path / "outer")
    initial = FIXTURES._fixture_sources(tmp_path / "sealed", target)[2]
    initial.chmod(0o755)
    sealed = application / "python-runtime"
    os.replace(initial, sealed)
    sealed.chmod(BUILDER.IMMUTABLE_DIRECTORY_MODE)
    return application, sealed


def test_application_reseal_cli_replaces_stale_pre_generation_copy(tmp_path: Path) -> None:
    target = "x86_64-unknown-linux-gnu"
    application, sealed = _embedded_sealed_application(tmp_path, target)
    old_manifest = BUILDER._sha256_file(sealed / BUILDER.MANIFEST_FILENAME)

    assert len(BUILDER.validate_packaged_application_closure(application)) == 41

    result = _run_reseal(sealed, application, target)

    assert result.returncode == 0, result.stderr
    new_manifest = BUILDER._sha256_file(sealed / BUILDER.MANIFEST_FILENAME)
    assert new_manifest != old_manifest
    assert f"TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256={new_manifest}" in result.stdout
    BUILDER.validate_environment(sealed, target, run_native_smoke=False)
    BUILDER.verify_packaged_application_closure(application, sealed)


@pytest.mark.parametrize("mutation", ("missing", "tampered", "extra", "symlink", "lock_drift"))
def test_packaged_closure_binds_every_locked_executable_catalog(
    tmp_path: Path, mutation: str
) -> None:
    """The sealed closure rejects catalog omissions and lock/tree divergence."""
    application = _packaged_application_closure(tmp_path / "outer")
    catalog, lock_path = _add_locked_catalog(application)
    if mutation == "missing":
        catalog.unlink()
    elif mutation == "tampered":
        catalog.write_bytes(b'{"fixture_catalog":false}\n')
    elif mutation == "extra":
        extra = catalog.with_name("extra.executables.v4.json")
        extra.write_bytes(b'{"extra_catalog":true}\n')
    elif mutation == "symlink":
        catalog.unlink()
        outside = tmp_path / "outside-catalog.json"
        outside.write_bytes(b"outside\n")
        catalog.symlink_to(outside)
    else:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["entries"][-1]["digest"] = "sha256:" + "0" * 64
        lock_path.write_text(json.dumps(lock, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(BUILDER.SealedEnvironmentError):
        BUILDER.validate_packaged_application_closure(application)


def test_packaged_closure_reseal_preserves_catalog_outer_inner_identity(
    tmp_path: Path,
) -> None:
    """Re-sealing copies the lock-bound catalog byte-for-byte into ``app``."""
    target = "x86_64-unknown-linux-gnu"
    application, sealed = _embedded_sealed_application(tmp_path, target)
    catalog, _lock_path = _add_locked_catalog(application)
    result = _run_reseal(sealed, application, target)

    assert result.returncode == 0, result.stderr
    inner_catalog = sealed / "app" / "ecosystem/defaultspack/v4/packs/fixture.executables.v4.json"
    assert inner_catalog.read_bytes() == catalog.read_bytes()
    BUILDER.verify_packaged_application_closure(application, sealed)


def test_application_reseal_stage_is_outside_source_and_sealed_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "x86_64-unknown-linux-gnu"
    application, sealed = _embedded_sealed_application(tmp_path, target)
    digest = BUILDER._sha256_file(sealed / BUILDER.MANIFEST_FILENAME)
    original = BUILDER._copy_application_snapshot
    observed: list[Path] = []

    def record_snapshot(source: Path, destination: Path, spec):
        observed.append(destination)
        assert not BUILDER._paths_overlap(source.resolve(), destination.parent.resolve())
        assert not BUILDER._paths_overlap(sealed.resolve(), destination.parent.resolve())
        return original(source, destination, spec)

    monkeypatch.setattr(BUILDER, "_copy_application_snapshot", record_snapshot)
    BUILDER.replace_environment_application_closure(
        sealed,
        application,
        target,
        expected_base_manifest_digest=digest,
    )

    assert observed
    assert not list(application.parent.glob(".python-runtime.application-reseal.*"))


def test_application_reseal_failure_preserves_original_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "x86_64-unknown-linux-gnu"
    application, sealed = _embedded_sealed_application(tmp_path, target)
    digest = BUILDER._sha256_file(sealed / BUILDER.MANIFEST_FILENAME)

    def fail_after_snapshot(*_args, **_kwargs):
        raise BUILDER.SealedEnvironmentError("injected re-seal failure")

    monkeypatch.setattr(
        BUILDER,
        "_rebuild_environment_application_closure_from_snapshot",
        fail_after_snapshot,
    )
    with pytest.raises(BUILDER.SealedEnvironmentError, match="injected"):
        BUILDER.replace_environment_application_closure(
            sealed,
            application,
            target,
            expected_base_manifest_digest=digest,
        )

    assert BUILDER._sha256_file(sealed / BUILDER.MANIFEST_FILENAME) == digest
    BUILDER.validate_environment(sealed, target, run_native_smoke=False)
    assert not list(application.parent.glob(".python-runtime.application-reseal.*"))


@pytest.mark.parametrize(
    ("layout", "names"),
    (
        ("temp_inside_source", ("source", "temp")),
        ("source_inside_temp", ("temp", "source")),
        ("destination_inside_source", ("source", "destination")),
    ),
)
def test_application_reseal_rejects_overlapping_roots(
    tmp_path: Path,
    layout: str,
    names: tuple[str, str],
) -> None:
    outer = tmp_path / layout
    outer.mkdir()
    inner = outer / "nested"
    inner.mkdir()

    with pytest.raises(BUILDER.SealedEnvironmentError, match="roots overlap"):
        BUILDER._assert_disjoint_reseal_roots(
            **{names[0]: outer, names[1]: inner}
        )


def test_application_reseal_rejects_symlinked_ancestor_alias(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "source-alias"
    try:
        alias.symlink_to(source, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(BUILDER.SealedEnvironmentError, match="alias"):
        BUILDER._absolute_unaliased_directory(alias)


def test_application_snapshot_rejects_deep_recursion_before_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    current = source
    for _index in range(BUILDER.APPLICATION_CLOSURE_MAX_DEPTH + 1):
        current /= "d"
        current.mkdir()

    with pytest.raises(BUILDER.SealedEnvironmentError, match="maximum safe depth"):
        BUILDER._copy_application_snapshot(
            source,
            tmp_path / "snapshot",
            BUILDER.target_spec("x86_64-unknown-linux-gnu"),
        )


def test_application_snapshot_rejects_concurrent_source_addition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "first.py").write_text("FIRST = 1\n", encoding="utf-8")
    original = BUILDER._copy_snapshot_file
    mutated = False

    def copy_and_mutate(*args, **kwargs):
        nonlocal mutated
        original(*args, **kwargs)
        if not mutated:
            (source / "appeared.py").write_text("APPEARED = 1\n", encoding="utf-8")
            mutated = True

    monkeypatch.setattr(BUILDER, "_copy_snapshot_file", copy_and_mutate)
    with pytest.raises(BUILDER.SealedEnvironmentError, match="changed during"):
        BUILDER._copy_application_snapshot(
            source,
            tmp_path / "snapshot",
            BUILDER.target_spec("x86_64-unknown-linux-gnu"),
        )
    assert not (tmp_path / "snapshot/appeared.py").exists()


@pytest.mark.parametrize(
    "mutation",
    ("build_required", "null_digest", "empty_variants", "missing_platform_artifact"),
)
def test_application_reseal_cli_rejects_incomplete_generated_binding(
    tmp_path: Path, mutation: str
) -> None:
    target = "x86_64-unknown-linux-gnu"
    sealed = FIXTURES._fixture_sources(tmp_path / "sealed", target)[2]
    sealed.parent.chmod(0o755)
    application = _packaged_application_closure(tmp_path / "outer")
    definition_path = application / (
        "ecosystem/defaultspack/v4/shell.fixture.default.shell.v1.json"
    )
    definition = json.loads(definition_path.read_text(encoding="utf-8"))
    if mutation == "build_required":
        definition["availability"] = "build_required"
    elif mutation == "null_digest":
        definition["artifact_digest"] = None
    elif mutation == "empty_variants":
        definition["launch"]["variants"] = []
    else:
        (application / "ecosystem/defaultspack/platform-artifacts/fixture-shell").unlink()
    if mutation != "missing_platform_artifact":
        definition_path.write_text(
            json.dumps(definition, sort_keys=True) + "\n", encoding="utf-8"
        )
    old_manifest = BUILDER._sha256_file(sealed / BUILDER.MANIFEST_FILENAME)

    result = _run_reseal(sealed, application, target)

    assert result.returncode == 1
    assert BUILDER._sha256_file(sealed / BUILDER.MANIFEST_FILENAME) == old_manifest
