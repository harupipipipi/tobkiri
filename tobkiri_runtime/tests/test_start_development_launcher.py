"""Regression coverage for the documented Docker-free desktop entrypoint."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def launcher_script(monkeypatch: pytest.MonkeyPatch):
    scripts = ROOT / "tobkiri_launcher/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "start_development_launcher", scripts / "start_development_launcher.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "host_target", lambda: "aarch64-apple-darwin")
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    return module


def prepare_checkout(root: Path) -> Path:
    """Create the minimum fixture for exercising build-command boundaries."""
    python = root / ".venv/bin/python3"
    python.parent.mkdir(parents=True)
    python.touch()
    app = (
        root
        / "tobkiri_launcher/src-tauri/target/aarch64-apple-darwin"
        / "debug/bundle/macos/Tobkiri Launcher Developer.app"
    )
    executable = app / "Contents/MacOS/tobkiri-launcher"
    executable.parent.mkdir(parents=True)
    executable.touch()
    return executable


def test_desktop_build_verifies_helper_and_bundle_before_returning(
    launcher_script, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = prepare_checkout(tmp_path)
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launcher_script, "run_command", run)
    assert launcher_script.build_development_launcher(tmp_path) == executable
    assert commands[0][:6] == ["npm", "exec", "--prefix", "frontend", "--", "tauri"]
    assert "src-tauri/tauri.macos.dev.conf.json" in commands[0]
    assert "--debug" in commands[0]
    assert "build_packvm_vz_helper.sh" in commands[1][1]
    assert "--entitlements" in commands[2]
    assert "--strict" in commands[3]
    assert "write-packvm-bundle-manifest" in commands[4]
    assert commands[4][-2:] == ["--expected-signing-mode", "ad-hoc"]
    assert "--strict" in commands[6]
    assert "verify-packvm-bundle" in commands[7]
    assert len(commands) == 8


def test_failed_helper_verification_stops_before_attesting_the_bundle(
    launcher_script, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_checkout(tmp_path)
    commands = []

    def run(command, **kwargs):
        parts = [str(part) for part in command]
        commands.append(parts)
        if "--verify" in parts:
            raise subprocess.CalledProcessError(1, parts)
        return subprocess.CompletedProcess(parts, 0)

    monkeypatch.setattr(launcher_script, "run_command", run)
    with pytest.raises(subprocess.CalledProcessError):
        launcher_script.build_development_launcher(tmp_path)
    assert len(commands) == 4
    assert not any("write-packvm-bundle-manifest" in parts for parts in commands)


def test_missing_launcher_output_is_not_treated_as_success(
    launcher_script, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = prepare_checkout(tmp_path)
    executable.unlink()
    monkeypatch.setattr(launcher_script, "run_command", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="was not produced"):
        launcher_script.build_development_launcher(tmp_path)


def test_desktop_rejects_a_target_directory_outside_its_binding(
    launcher_script, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "external"))
    with pytest.raises(RuntimeError, match="default Cargo target"):
        launcher_script.build_development_launcher(tmp_path)


def test_local_build_uses_dev_preparation_and_preserves_release_preparation() -> None:
    local = json.loads(
        (ROOT / "tobkiri_launcher/src-tauri/tauri.macos.dev.conf.json").read_text()
    )
    release = json.loads(
        (ROOT / "tobkiri_launcher/src-tauri/tauri.conf.json").read_text()
    )
    assert local["build"]["beforeBuildCommand"].endswith("--mode dev")
    assert local["identifier"] == "dev.tobkiri.local-launcher"
    assert local["bundle"]["resources"] is None
    assert release["build"]["beforeBuildCommand"].endswith("--mode release")
