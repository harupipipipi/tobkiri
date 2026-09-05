from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tobkiri_launcher/scripts/stage_macos_unbundled_resources.py"
FIXTURES = ROOT / "tobkiri_runtime/tests/test_sealed_python_environment.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


STAGER = _load("stage_macos_unbundled_resources_tests", SCRIPT)
SEALED_FIXTURES = _load("unbundled_sealed_fixture_support", FIXTURES)
BUILDER = SEALED_FIXTURES.BUILDER
TARGET = "x86_64-unknown-linux-gnu"


def _resource_source(tmp_path: Path) -> tuple[Path, Path, str]:
    sealed = SEALED_FIXTURES._fixture_sources(tmp_path / "fixture", TARGET)[2]
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(sealed, source / "python-runtime", copy_function=shutil.copyfile)
    for path in sorted(
        (sealed, *sealed.rglob("*")),
        key=lambda value: len(value.relative_to(sealed).parts),
        reverse=True,
    ):
        target = source / "python-runtime" / path.relative_to(sealed)
        target.chmod(stat.S_IMODE(path.lstat().st_mode))
    entries = []
    for path in sorted(source.rglob("*")):
        if path.is_file():
            payload = path.read_bytes()
            entries.append(
                {
                    "path": path.relative_to(source).as_posix(),
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
    (source / STAGER.RUNTIME_MANIFEST).write_text(
        json.dumps({"schema": STAGER.RUNTIME_SCHEMA, "entries": entries}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(
        (source / "python-runtime" / BUILDER.MANIFEST_FILENAME).read_bytes()
    ).hexdigest()
    return source, sealed, digest


def test_exact_unbundled_stage_prevents_tauri_directory_mode_drift(
    tmp_path: Path,
) -> None:
    source, _sealed, digest = _resource_source(tmp_path)
    drifted = tmp_path / "drifted/python-runtime"
    drifted.parent.mkdir()
    shutil.copytree(source / "python-runtime", drifted)
    for path in (drifted, *drifted.rglob("*")):
        if path.is_dir():
            path.chmod(0o755)
    with pytest.raises(
        Exception,
        match="root is writable|root mode changed|directory mode changed",
    ):
        BUILDER.validate_environment(
            drifted,
            TARGET,
            expected_manifest_digest=digest,
            run_native_smoke=False,
            require_sealed=True,
        )

    destination_parent = tmp_path / "exact"
    destination_parent.mkdir()
    destination = destination_parent / "app"
    for path in (source / "python-runtime", *(source / "python-runtime").rglob("*")):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
    STAGER.copy_exact_runtime_tree(source.resolve(), destination.resolve())
    STAGER.seal_staged_python(ROOT.resolve(), destination.resolve(), TARGET)

    BUILDER.validate_environment(
        destination / "python-runtime",
        TARGET,
        expected_manifest_digest=digest,
        run_native_smoke=False,
        require_sealed=True,
    )
    assert stat.S_IMODE((destination / "python-runtime").stat().st_mode) == 0o555
