from __future__ import annotations

import importlib.util
import json
import shutil
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tobkiri_launcher/scripts/verify_packaged_python.py"
FIXTURES = ROOT / "tobkiri_runtime/tests/test_sealed_python_environment.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


VERIFY = _load("verify_packaged_python_tests", SCRIPT)
SEALED_FIXTURES = _load("sealed_python_fixture_support", FIXTURES)
BUILDER = SEALED_FIXTURES.BUILDER
TARGET = "x86_64-unknown-linux-gnu"


def _tauri_copied_resource(tmp_path: Path) -> tuple[Path, str]:
    source = SEALED_FIXTURES._fixture_sources(tmp_path / "source", TARGET)[2]
    app = tmp_path / "Tobkiri Launcher.app"
    resource = app / VERIFY.RESOURCE_RELATIVE
    shutil.copytree(source, resource)
    for path in (resource, *resource.rglob("*")):
        if path.is_dir():
            path.chmod(0o755)
    digest = BUILDER._sha256_file(resource / BUILDER.MANIFEST_FILENAME)
    return resource, digest


def test_host_seal_accepts_only_tauri_directory_delta_and_restores_exact_modes(
    tmp_path: Path,
) -> None:
    resource, digest = _tauri_copied_resource(tmp_path)

    VERIFY._preseal_tauri_directories(resource, TARGET, digest, BUILDER)

    BUILDER.validate_environment(
        resource,
        TARGET,
        expected_manifest_digest=digest,
        run_native_smoke=False,
    )
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o555
        for path in (resource, *resource.rglob("*"))
        if path.is_dir()
    )


@pytest.mark.parametrize("unsafe_mode", (0o775, 0o500))
def test_host_seal_rejects_unsafe_or_noncanonical_preseal_modes(
    tmp_path: Path,
    unsafe_mode: int,
) -> None:
    resource, digest = _tauri_copied_resource(tmp_path)
    (resource / "runtime").chmod(unsafe_mode)

    with pytest.raises(RuntimeError, match="unsafe pre-seal mode"):
        VERIFY._preseal_tauri_directories(resource, TARGET, digest, BUILDER)


def test_directory_mode_evidence_is_manifest_inventoried(tmp_path: Path) -> None:
    resource, _digest = _tauri_copied_resource(tmp_path)
    manifest = json.loads(
        (resource / BUILDER.MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    evidence = resource / BUILDER.DIRECTORY_MODES_FILENAME
    entry = next(
        item for item in manifest["files"] if item["path"] == evidence.name
    )
    assert entry["sha256"] == BUILDER._sha256_file(evidence)
    assert entry["executable"] is False


def test_post_sign_verifier_preserves_bundle_tree_identity(tmp_path: Path) -> None:
    resource, _digest = _tauri_copied_resource(tmp_path)
    app = resource.parents[3]

    before = VERIFY._bundle_tree_identity(app)
    result = VERIFY._without_bundle_mutation(app, lambda: "verified")

    assert result == "verified"
    assert VERIFY._bundle_tree_identity(app) == before
    assert not (resource.parent / "logs").exists()


def test_post_sign_verifier_rejects_added_resource_log(tmp_path: Path) -> None:
    resource, _digest = _tauri_copied_resource(tmp_path)
    app = resource.parents[3]

    def mutate_signed_resources() -> None:
        log = resource.parent / "logs" / "defaultspack-launch.jsonl"
        log.parent.mkdir()
        log.write_text("mutation\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="mutated the signed application bundle"):
        VERIFY._without_bundle_mutation(app, mutate_signed_resources)
