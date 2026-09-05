from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "run_tauri_build.py"
ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("run_tauri_build_tests", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {SCRIPT}")
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


def test_macos_environment_binds_system_xattr_before_hostile_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = tmp_path / "bin"
    hostile.mkdir()
    (hostile / "xattr").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    (hostile / "xattr").chmod(0o755)
    monkeypatch.setattr(RUNNER, "_verify_macos_xattr", lambda: None)
    monkeypatch.setattr(
        RUNNER.shutil,
        "which",
        lambda executable, path=None: (
            "/usr/bin/xattr" if executable == "xattr" else None
        ),
    )

    environment = RUNNER.build_environment(
        {"PATH": os.fspath(hostile)}, platform="darwin"
    )

    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PATH"].split(os.pathsep)[:4] == list(
        RUNNER.MACOS_SYSTEM_PATH
    )
    assert RUNNER.shutil.which("xattr", path=environment["PATH"]) == "/usr/bin/xattr"


def test_ci_e2e_policy_accepts_only_non_publishable_app_bundle() -> None:
    environment = {"TOBKIRI_MACOS_ARTIFACT_POLICY": "ci-e2e-v1"}
    RUNNER.validate_arguments(
        ("build", "--bundles", "app", "--ci"), environment
    )

    for arguments in (
        ("build", "--bundles", "dmg", "--ci"),
        ("build", "--bundles", "app"),
        ("build", "--ci"),
    ):
        with pytest.raises(RuntimeError, match="non-publishable"):
            RUNNER.validate_arguments(arguments, environment)


def test_runner_rejects_non_build_tauri_commands() -> None:
    with pytest.raises(RuntimeError, match="only the build subcommand"):
        RUNNER.validate_arguments(("dev",), {})


def test_macos_workflows_use_the_bound_runner_for_every_tauri_build() -> None:
    for relative in (
        ".github/workflows/desktop-installers.yml",
        ".github/workflows/release.yml",
    ):
        workflow = (ROOT / relative).read_text(encoding="utf-8")
        assert "cargo tauri build" not in workflow
        assert workflow.count("python -B scripts/run_tauri_build.py build") == 2
