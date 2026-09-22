from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "tobkiri_launcher" / "scripts" / "prepare_viewer_runtime.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("prepare_viewer_runtime", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_target_prefers_explicit_then_tauri_environment(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "host_target", lambda: "host-target")

    environment = {module.TAURI_TARGET_ENV: "env-target"}
    assert module.resolve_target("explicit-target", environment) == "explicit-target"
    assert module.resolve_target(None, environment) == "env-target"
    assert module.resolve_target(None, {}) == "host-target"


def test_prepare_dev_stages_repo_venv_uv_into_trusted_bundle(tmp_path, monkeypatch):
    module = _load_module()
    target = "x86_64-unknown-linux-gnu"
    source = tmp_path / ".venv" / "bin" / "uv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"uv")
    verified: list[Path] = []

    def fake_verify(path: Path) -> str:
        verified.append(path)
        return "uv 0.11.14"

    monkeypatch.setattr(module, "verify_uv_binary", fake_verify)

    destination = module.prepare_dev(tmp_path, target)

    assert destination == tmp_path / "tobkiri_runtime" / "bundled" / "uv"
    assert destination.read_bytes() == b"uv"
    assert os.access(destination, os.X_OK)
    assert verified == [source.resolve(), destination]
    assert not list(destination.parent.glob(".*.tmp"))


def test_resolve_dev_uv_source_prefers_explicit_path_over_repo_venv(tmp_path):
    module = _load_module()
    target = "x86_64-pc-windows-msvc"
    explicit = tmp_path / "managed" / "uv.exe"
    repo_uv = tmp_path / ".venv" / "Scripts" / "uv.exe"
    explicit.parent.mkdir(parents=True)
    repo_uv.parent.mkdir(parents=True)
    explicit.write_bytes(b"explicit")
    repo_uv.write_bytes(b"repo")

    resolved = module.resolve_dev_uv_source(
        tmp_path,
        target,
        {module.UV_PATH_ENV: str(explicit)},
    )

    assert resolved == explicit.resolve()


def test_relative_explicit_uv_path_is_resolved_from_repo_root(tmp_path):
    module = _load_module()
    explicit = tmp_path / "tools" / "uv"
    explicit.parent.mkdir(parents=True)
    explicit.write_bytes(b"uv")

    resolved = module.resolve_dev_uv_source(
        tmp_path,
        "x86_64-unknown-linux-gnu",
        {module.UV_PATH_ENV: "tools/uv"},
    )

    assert resolved == explicit.resolve()


def test_prepare_dev_reuses_valid_existing_trusted_bundle(tmp_path, monkeypatch):
    module = _load_module()
    destination = tmp_path / "tobkiri_runtime" / "bundled" / "uv"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")
    verified: list[Path] = []

    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        module,
        "verify_uv_binary",
        lambda path: verified.append(path) or "uv 0.11.14",
    )

    result = module.prepare_dev(tmp_path, "x86_64-unknown-linux-gnu")

    assert result == destination
    assert verified == [destination]
    assert destination.read_bytes() == b"existing"


def test_prepare_dev_removes_staged_uv_when_post_copy_verification_changes(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    source = tmp_path / ".venv" / "bin" / "uv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"uv")
    versions = iter(("uv 0.11.14", "uv 0.11.15"))
    monkeypatch.setattr(module, "verify_uv_binary", lambda _path: next(versions))

    with pytest.raises(RuntimeError, match="reported a different version"):
        module.prepare_dev(tmp_path, "x86_64-unknown-linux-gnu")

    destination = tmp_path / "tobkiri_runtime" / "bundled" / "uv"
    assert not destination.exists()


def test_prepare_dev_fails_with_actionable_repo_path_when_uv_is_missing(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match=r"\.venv/bin/uv"):
        module.prepare_dev(tmp_path, "x86_64-unknown-linux-gnu")


def test_prepare_release_builds_pack_shell_then_runs_verified_resource_preparer(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    manifest = tmp_path / "pack-shell" / "Cargo.toml"
    preparer_path = tmp_path / ".github" / "scripts" / "prepare_tauri_resources.py"
    manifest.parent.mkdir(parents=True)
    preparer_path.parent.mkdir(parents=True)
    manifest.write_text("[package]\nname='pack-shell'\n", encoding="utf-8")
    preparer_path.write_text("# test\n", encoding="utf-8")

    calls: list[tuple[list[str], Path | None]] = []
    fake_preparer = SimpleNamespace(
        UV_PINNED_VERSION="0.11.14",
        UV_SHA256_BY_TARGET={"x86_64-pc-windows-msvc": "sha"},
        seal_pack_shell_binary=lambda root, target: calls.append(
            (["seal-pack-shell", os.fspath(root), target], root)
        ),
    )

    monkeypatch.setattr(module, "load_resource_preparer", lambda _root: fake_preparer)

    def fake_run(command, *, cwd=None, capture_output=False):
        del capture_output
        calls.append(([os.fspath(part) for part in command], cwd))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module, "run_command", fake_run)

    module.prepare_release(tmp_path, "x86_64-pc-windows-msvc")

    assert calls[0][0][:4] == ["cargo", "build", "--locked", "--release"]
    assert calls[0][0][4:6] == ["--target", "x86_64-pc-windows-msvc"]
    assert calls[1] == (
        ["seal-pack-shell", os.fspath(tmp_path), "x86_64-pc-windows-msvc"],
        tmp_path,
    )
    assert calls[2][0][0] == os.fspath(module.sys.executable)
    assert "--uv-version" in calls[2][0]
    assert "0.11.14" in calls[2][0]
    assert "--require-runtime-tools" in calls[2][0]


def test_prepare_release_rejects_target_without_pinned_checksum(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(
        module,
        "load_resource_preparer",
        lambda _root: SimpleNamespace(
            UV_PINNED_VERSION="0.11.14",
            UV_SHA256_BY_TARGET={},
        ),
    )

    with pytest.raises(RuntimeError, match="No pinned uv checksum"):
        module.prepare_release(tmp_path, "aarch64-pc-windows-msvc")


def test_prepare_release_removes_read_only_dev_uv_before_verified_stage(tmp_path, monkeypatch):
    module = _load_module()
    target = "aarch64-apple-darwin"
    destination = tmp_path / "tobkiri_runtime" / "bundled" / "uv"
    destination.parent.mkdir(parents=True)
    destination.write_text("dev uv", encoding="utf-8")
    destination.chmod(0o555)
    manifest = tmp_path / "pack-shell" / "Cargo.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("[package]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")

    fake_preparer = SimpleNamespace(
        UV_PINNED_VERSION="0.0.0",
        UV_SHA256_BY_TARGET={target: "fixture"},
        seal_pack_shell_binary=lambda _root, _target: None,
    )
    calls = []
    monkeypatch.setattr(module, "load_resource_preparer", lambda _root: fake_preparer)
    monkeypatch.setattr(module, "run_command", lambda command, **kwargs: calls.append(list(command)))

    module.prepare_release(tmp_path, target)

    assert not destination.exists()
    assert len(calls) == 2
    assert "prepare_tauri_resources.py" in str(calls[1][1])
