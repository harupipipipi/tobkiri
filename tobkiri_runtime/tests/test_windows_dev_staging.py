from __future__ import annotations

import importlib.util
import os
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



def test_windows_dev_copy_does_not_inherit_read_only_source(tmp_path):
    module = _load_module()
    source = tmp_path / "source.exe"
    alias = tmp_path / "source-alias.exe"
    source.write_bytes(b"trusted development bytes")
    os.link(source, alias)
    source.chmod(0o444)
    destination = tmp_path / "uv.exe"
    try:
        for _ in range(2):
            module._copy_writable_development_binary(source, destination)
            assert destination.read_bytes() == source.read_bytes()
            assert destination.stat().st_mode & 0o200
            assert destination.stat().st_nlink == 1
        assert not source.stat().st_mode & 0o200
        assert not alias.stat().st_mode & 0o200
        assert not list(tmp_path.glob(".dev-binary-*.tmp"))
    finally:
        source.chmod(0o600)


def test_windows_dev_copy_keeps_read_only_destination_unchanged(tmp_path):
    module = _load_module()
    source, destination = tmp_path / "new.exe", tmp_path / "uv.exe"
    source.write_bytes(b"new")
    destination.write_bytes(b"previous")
    destination.chmod(0o444)
    before = destination.stat()
    try:
        with pytest.raises(RuntimeError, match="read-only"):
            module._copy_writable_development_binary(source, destination)
        assert destination.read_bytes() == b"previous"
        assert destination.stat().st_mode == before.st_mode
        assert not list(tmp_path.glob(".dev-binary-*.tmp"))
    finally:
        destination.chmod(0o600)


def test_windows_dev_copy_rejects_hardlinked_destination(tmp_path):
    module = _load_module()
    source, destination = tmp_path / "new.exe", tmp_path / "uv.exe"
    source.write_bytes(b"new")
    destination.write_bytes(b"previous")
    alias = tmp_path / "other.exe"
    os.link(destination, alias)
    with pytest.raises(RuntimeError, match="Unsafe"):
        module._copy_writable_development_binary(source, destination)
    assert alias.read_bytes() == destination.read_bytes() == b"previous"


@pytest.mark.parametrize("failure", ["copy", "replace", "mutation"])
def test_windows_dev_copy_failure_preserves_destination(tmp_path, monkeypatch, failure):
    module = _load_module()
    source, destination = tmp_path / "new.exe", tmp_path / "uv.exe"
    source.write_bytes(b"new")
    destination.write_bytes(b"previous")
    error = OSError("injected primary failure")
    def broken(*_args):
        raise error
    if failure == "copy":
        monkeypatch.setattr(module.shutil, "copyfileobj", broken)
    elif failure == "replace":
        monkeypatch.setattr(module.os, "replace", broken)
    else:
        copy = module.shutil.copyfileobj
        def mutate(input_handle, output):
            copy(input_handle, output)
            source.write_bytes(b"changed content")
        monkeypatch.setattr(module.shutil, "copyfileobj", mutate)
    with pytest.raises((OSError, RuntimeError)) as caught:
        module._copy_writable_development_binary(source, destination)
    if failure != "mutation":
        assert caught.value is error
    assert destination.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".dev-binary-*.tmp"))


def test_windows_shell_snapshot_omits_read_only_mode(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "os", SimpleNamespace(**{
        name: getattr(os, name) for name in dir(os) if name != "name"
    }, name="nt"))
    source = tmp_path / "source.exe"
    source.write_bytes(b"shell")
    source.chmod(0o444)
    destination = tmp_path / "shell.exe"
    try:
        module.stage_development_shell_artifact(source, destination)
        assert destination.stat().st_mode & 0o200
        assert not source.stat().st_mode & 0o200
        assert destination.read_bytes() == b"shell"
    finally:
        source.chmod(0o600)


def test_windows_dev_copy_closes_raw_descriptor_if_fdopen_fails(tmp_path, monkeypatch):
    module = _load_module()
    source, destination = tmp_path / "new.exe", tmp_path / "uv.exe"
    source.write_bytes(b"new")
    opened = []
    def broken(descriptor, *_args, **kwargs):
        assert kwargs == {"closefd": False}
        opened.append(descriptor)
        raise OSError("fdopen failed")
    monkeypatch.setattr(module.os, "fdopen", broken)
    with pytest.raises(OSError, match="fdopen failed"):
        module._copy_writable_development_binary(source, destination)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert not list(tmp_path.glob(".dev-binary-*.tmp"))
    assert not destination.exists()


def test_windows_dev_copy_rejects_reparse_destination(tmp_path, monkeypatch):
    module = _load_module()
    source, destination = tmp_path / "new.exe", tmp_path / "uv.exe"
    source.write_bytes(b"new")
    destination.write_bytes(b"previous")
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == destination:
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=1, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(RuntimeError, match="Unsafe"):
        module._copy_writable_development_binary(source, destination)
    assert destination.read_bytes() == b"previous"


def test_windows_dev_copy_preserves_primary_error_when_cleanup_fails(tmp_path, monkeypatch):
    module = _load_module()
    source, destination = tmp_path / "new.exe", tmp_path / "uv.exe"
    source.write_bytes(b"new")
    original = OSError("original copy failure")
    def broken_copy(*_args):
        raise original
    unlink = Path.unlink
    def broken_unlink(path, *args, **kwargs):
        if path.name.startswith(".dev-binary-"):
            raise PermissionError("cleanup denied")
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(module.shutil, "copyfileobj", broken_copy)
        patch.setattr(Path, "unlink", broken_unlink)
        with pytest.raises(OSError) as caught:
            module._copy_writable_development_binary(source, destination)
        assert caught.value is original
    for temporary in tmp_path.glob(".dev-binary-*.tmp"):
        temporary.unlink()
    assert not destination.exists()


def test_windows_shell_directory_snapshot_omits_read_only_modes(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "os", SimpleNamespace(**{
        name: getattr(os, name) for name in dir(os) if name != "name"
    }, name="nt"))
    source = tmp_path / "source"
    source.mkdir()
    child = source / "shell.exe"
    child.write_bytes(b"shell")
    child.chmod(0o444)
    destination = tmp_path / "staged"
    try:
        module.stage_development_shell_artifact(source, destination)
        assert destination.stat().st_mode & 0o200
        assert (destination / "shell.exe").stat().st_mode & 0o200
        assert not child.stat().st_mode & 0o200
    finally:
        child.chmod(0o600)
