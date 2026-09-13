from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "verify_packaged_python_dmg.py"
ROOT = SCRIPT.parents[2]
BUILDER_SCRIPT = ROOT / ".github/scripts/build_sealed_python_environment.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_packaged_python_dmg", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "verify_dmg_sealed_python_builder", BUILDER_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {BUILDER_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _fake_tools(root: Path, mode: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    state = root / "hdiutil-state.json"
    state.write_text(json.dumps({"commands": []}), encoding="utf-8")
    victim = root / "external-victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    _write_executable(
        bin_dir / "hdiutil",
        """#!/usr/bin/env python3
import json
import os
import plistlib
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_HDIUTIL_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
state["commands"].append(sys.argv[1:])
state_path.write_text(json.dumps(state), encoding="utf-8")
if sys.argv[1] == "detach":
    raise SystemExit(0)
mountpoint = Path(sys.argv[sys.argv.index("-mountpoint") + 1])
mode = os.environ["FAKE_HDIUTIL_MODE"]
if mode == "root_symlink":
    mountpoint.rmdir()
    mountpoint.symlink_to(Path(os.environ["FAKE_EXTERNAL_VICTIM"]), target_is_directory=True)
elif mode in {"root_replace", "wrong_device"}:
    mountpoint.rename(mountpoint.with_name(mountpoint.name + ".original"))
    mountpoint.mkdir()
elif mode == "ancestor_swap":
    parent = mountpoint.parent
    parent.rename(parent.with_name(parent.name + ".original"))
    parent.mkdir()
device = "/dev/disk0" if mode == "wrong_device" else "/dev/disk999"
payload = {"system-entities": [{"dev-entry": device, "mount-point": str(mountpoint)}]}
sys.stdout.buffer.write(plistlib.dumps(payload))
""",
    )
    _write_executable(bin_dir / "codesign", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HDIUTIL_STATE", os.fspath(state))
    monkeypatch.setenv("FAKE_HDIUTIL_MODE", mode)
    monkeypatch.setenv("FAKE_EXTERNAL_VICTIM", os.fspath(victim))
    return state


def _fake_mount(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[object, Path, Path]:
    state = _fake_tools(tmp_path, mode, monkeypatch)
    parent = tmp_path / "mount-parent"
    parent.mkdir()
    dmg = tmp_path / "fixture.dmg"
    dmg.write_bytes(b"fixture")
    mount = MODULE.MountedDmg(dmg, temporary_parent=parent)
    return mount, state, tmp_path / "external-victim"


def _close_failed_fake_mount(mount: object) -> None:
    mount.close()


def test_var_system_alias_is_canonicalized_before_mountpoint_creation() -> None:
    canonical = MODULE.canonical_temporary_parent(Path("/var"))
    assert canonical == Path("/var").resolve(strict=True)
    assert canonical.resolve(strict=True) == canonical


def test_unapproved_symlinked_temporary_parent_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(MODULE.DmgVerificationError, match="unapproved symlink"):
        MODULE.canonical_temporary_parent(alias)


def test_attach_plist_requires_exact_canonical_mountpoint(tmp_path: Path) -> None:
    canonical = tmp_path / "mount"
    canonical.mkdir()
    payload = plistlib.dumps(
        {
            "system-entities": [
                {"dev-entry": "/dev/disk42s1", "mount-point": str(canonical)}
            ]
        }
    )
    assert MODULE._device_from_attach_plist(payload, canonical) == Path("/dev/disk42s1")

    alias = tmp_path / "alias"
    alias.symlink_to(canonical, target_is_directory=True)
    with pytest.raises(MODULE.DmgVerificationError, match="exactly one"):
        MODULE._device_from_attach_plist(payload, alias)


@pytest.mark.parametrize("mode", ["root_symlink", "root_replace"])
def test_fake_hdiutil_rejects_root_swap_and_never_detaches_unbound_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    mount, state_path, victim = _fake_mount(tmp_path, mode, monkeypatch)
    try:
        with pytest.raises((MODULE.DmgVerificationError, FileNotFoundError)):
            mount.attach()
    finally:
        _close_failed_fake_mount(mount)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert all(command[0] != "detach" for command in state["commands"])
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS block devices")
def test_fake_hdiutil_rejects_wrong_mounted_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount, state_path, victim = _fake_mount(tmp_path, "wrong_device", monkeypatch)
    try:
        with pytest.raises(MODULE.DmgVerificationError, match="wrong device identity"):
            mount.attach()
    finally:
        _close_failed_fake_mount(mount)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert all(command[0] != "detach" for command in state["commands"])
    assert (victim / "keep.txt").is_file()


def test_fake_hdiutil_rejects_ancestor_swap_without_touching_external_victim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount, state_path, victim = _fake_mount(tmp_path, "ancestor_swap", monkeypatch)
    try:
        with pytest.raises(
            MODULE.DmgVerificationError, match="parent path identity changed"
        ):
            mount.attach()
    finally:
        _close_failed_fake_mount(mount)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert all(command[0] != "detach" for command in state["commands"])
    assert (victim / "keep.txt").is_file()


def test_primary_error_wins_over_cleanup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dmg = tmp_path / "fixture.dmg"
    dmg.write_bytes(b"fixture")
    repository = tmp_path / "repository"
    repository.mkdir()

    class FailingMount:
        def __init__(self, _dmg: Path) -> None:
            pass

        def attach(self) -> None:
            raise MODULE.DmgVerificationError("primary attach failure")

        def cleanup(self) -> None:
            raise MODULE.DmgVerificationError("cleanup failure")

        def close(self) -> None:
            pass

    executable_path = tmp_path / "tool"
    _write_executable(executable_path, "#!/bin/sh\nexit 0\n")
    metadata = executable_path.lstat()
    executable = MODULE.Executable(
        executable_path,
        MODULE.Identity.from_stat(metadata),
    )
    monkeypatch.setattr(MODULE, "MountedDmg", FailingMount)
    monkeypatch.setattr(MODULE, "_resolve_executable", lambda _name: executable)
    monkeypatch.setattr(MODULE, "_bind_executable", lambda _path, _label: executable)
    args = MODULE.argparse.Namespace(
        dmg=dmg,
        repo_root=repository,
        target="aarch64-apple-darwin",
        expected_manifest_sha256="a" * 64,
    )

    with pytest.raises(MODULE.DmgVerificationError, match="primary attach failure"):
        MODULE.verify_dmg(args)
    assert "cleanup also failed: cleanup failure" in capsys.readouterr().err


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("hdiutil") is None,
    reason="requires a real macOS disk image mount",
)
def test_actual_read_only_dmg_mount_is_identity_bound_and_cleanup_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    app = source / "Fixture.app"
    sealed_root = app / "Contents/Resources/app/python-runtime"
    sealed_root.mkdir(parents=True)
    (sealed_root / BUILDER.MANIFEST_FILENAME).write_text(
        "fixture", encoding="utf-8"
    )
    dmg = tmp_path / "fixture.dmg"
    subprocess.run(
        [
            "hdiutil",
            "create",
            "-quiet",
            "-srcfolder",
            os.fspath(source),
            "-format",
            "UDZO",
            os.fspath(dmg),
        ],
        check=True,
    )
    monkeypatch.setenv("TMPDIR", tempfile_parent := tempfile_parent_value())
    mount = MODULE.MountedDmg(dmg)
    assert os.fspath(mount.parent).startswith("/private/")
    mount_path = mount.path
    try:
        mount.attach()
        mounted_app = mount.application_bundle()
        assert mounted_app.name == "Fixture.app"
        mount.verify_mounted()
        mounted_sealed_root = mounted_app / "Contents/Resources/app/python-runtime"
        before = tuple(
            (path.relative_to(mounted_app).as_posix(), path.lstat().st_mode)
            for path in (mounted_app, *sorted(mounted_app.rglob("*")))
        )
        with BUILDER._native_smoke_workspace(mounted_sealed_root) as workspace:
            environment = BUILDER._native_smoke_environment(
                mounted_sealed_root,
                BUILDER.target_spec("aarch64-apple-darwin"),
                workspace,
            )
            for key in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP"):
                assert Path(environment[key]).is_relative_to(workspace.path)
            assert not workspace.path.is_relative_to(mounted_app)
        after = tuple(
            (path.relative_to(mounted_app).as_posix(), path.lstat().st_mode)
            for path in (mounted_app, *sorted(mounted_app.rglob("*")))
        )
        assert after == before
        mount.cleanup()
        mount.cleanup()
        assert not mount_path.exists()
    finally:
        if mount.device is not None and not mount.detached:
            subprocess.run(["hdiutil", "detach", str(mount.device)], check=False)
        mount.close()
    assert tempfile_parent.startswith("/var/")


def tempfile_parent_value() -> str:
    """Return macOS's legal /var alias to exercise the CI failure path."""
    candidate = os.environ.get("TMPDIR", "/var/tmp")
    if candidate.startswith("/var/"):
        return candidate
    return "/var/tmp"
