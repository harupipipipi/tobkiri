from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / ".github" / "scripts" / "prepare_tauri_resources.py"
DEFAULTSPACK_ROOT = ROOT / "tobkiri_runtime" / "ecosystem" / "defaultspack"


def _load_prepare_tauri_resources():
    spec = importlib.util.spec_from_file_location("prepare_tauri_resources", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_windows_uv_archive(
    path: Path,
    *,
    target: str,
    payload: bytes,
    member: str = "uv.exe",
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, payload)


def _write_linux_uv_archive(path: Path, *, target: str, payload: bytes) -> None:
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(name=f"uv-{target}/uv")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))


def _write_pack_shell_binary(
    module,
    target_root: Path,
    target: str,
    *,
    filename: str | None = None,
    architecture: str | None = None,
    executable: bool = True,
    profile: str = "release",
) -> Path:
    binary_name = filename or module.pack_shell_binary_name(target)
    binary = target_root / target / profile / binary_name
    binary.parent.mkdir(parents=True, exist_ok=True)
    requested_architecture = architecture or target.split("-", 1)[0]
    if module.is_windows_target(target):
        payload = bytearray(128)
        payload[:2] = b"MZ"
        payload[60:64] = (64).to_bytes(4, "little")
        payload[64:68] = b"PE\0\0"
        machine = {"x86_64": 0x8664, "aarch64": 0xAA64}[requested_architecture]
        payload[68:70] = machine.to_bytes(2, "little")
    elif "apple-darwin" in target:
        machine = {"x86_64": 0x01000007, "aarch64": 0x0100000C}[requested_architecture]
        payload = bytearray(b"\xcf\xfa\xed\xfe" + machine.to_bytes(4, "little"))
        payload.extend(b"pack-shell fixture")
    else:
        payload = bytearray(64)
        payload[:6] = b"\x7fELF\x02\x01"
        machine = {"x86_64": 62, "aarch64": 183}[requested_architecture]
        payload[18:20] = machine.to_bytes(2, "little")
    binary.write_bytes(payload)
    binary.chmod(0o755 if executable else 0o644)
    return binary


def _minimal_v4_stage(tmp_path: Path) -> Path:
    """Build a small staged resource tree from the canonical v4 inputs."""
    module = _load_prepare_tauri_resources()
    stage = tmp_path / "app"
    (stage / "core_runtime/core_pack/core_control_panel/web").mkdir(parents=True)
    (stage / "core_runtime/core_pack/core_control_panel/web/index.html").write_text(
        "<!doctype html>\n",
        encoding="utf-8",
    )
    shutil.copytree(
        ROOT / "tobkiri_runtime/core_runtime",
        stage / "core_runtime",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "ecosystem.json",
            "rumi.pack.v3.json",
        ),
    )
    module.stage_canonical_host_package(ROOT / "tobkiri_runtime", stage)
    shutil.copytree(
        ROOT / "tobkiri_runtime/tobkiri_protocol",
        stage / "tobkiri_protocol",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    for relative in module.REQUIRED_RUNTIME_BOOTSTRAP_FILES:
        source = ROOT / "tobkiri_runtime" / relative
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (stage / "requirements.txt").write_text("", encoding="utf-8")
    ui_root = stage / "ecosystem/defaultspack/ui"
    ui_root.mkdir(parents=True)
    (ui_root / "shell.html").write_text("<main></main>\n", encoding="utf-8")
    (ui_root / "shell-app.js").write_text("console.log('ok');\n", encoding="utf-8")

    pack_root = stage / "ecosystem/defaultspack"
    pack_root.mkdir(parents=True, exist_ok=True)
    for filename in (
        "pack.v4.json",
        "contracts.v4.json",
        "artifact-index.v4.json",
        "executables.v4.json",
    ):
        shutil.copy2(DEFAULTSPACK_ROOT / filename, pack_root / filename)
    shutil.copytree(
        DEFAULTSPACK_ROOT / "runtime",
        pack_root / "runtime",
        ignore=shutil.ignore_patterns(*module.EXCLUDED_DIR_NAMES, "*.pyc"),
    )
    shutil.copytree(
        DEFAULTSPACK_ROOT / "domain",
        pack_root / "domain",
        ignore=shutil.ignore_patterns(*module.EXCLUDED_DIR_NAMES, "*.pyc"),
    )
    shutil.copytree(
        DEFAULTSPACK_ROOT / "defaultspack",
        pack_root / "defaultspack",
        ignore=shutil.ignore_patterns(*module.EXCLUDED_DIR_NAMES, "*.pyc"),
    )
    shutil.copytree(DEFAULTSPACK_ROOT / "v4", pack_root / "v4")
    return stage


def _stub_formal_sealed_python_tree(stage: Path, module) -> Path:
    """Add the exact formal boundary with the two venv directory domains."""
    sealed_root = stage / module.SEALED_PYTHON_RESOURCE_DIR
    for relative in module.SEALED_ROLE_TARGETS:
        source = ROOT / "tobkiri_runtime" / relative
        destination = sealed_root / "app" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    (sealed_root / "venv").mkdir(parents=True, exist_ok=True)
    (sealed_root / "runtime/lib/python3.13/venv").mkdir(parents=True, exist_ok=True)
    manifest = sealed_root / Path(module.SEALED_PYTHON_MANIFEST).name
    manifest.write_text("{}\n", encoding="utf-8")
    return sealed_root


def _stub_formal_builder(monkeypatch, module, sealed_root: Path) -> None:
    """Provide producer validation evidence without constructing CPython."""
    manifest_digest = _sha256(
        (sealed_root / Path(module.SEALED_PYTHON_MANIFEST).name).read_bytes()
    )

    class Builder:
        def validate_environment(self, root, target, **kwargs):
            assert root == sealed_root
            assert target == "aarch64-apple-darwin"
            assert kwargs == {"run_native_smoke": False}
            return manifest_digest

    monkeypatch.setattr(module, "_load_sealed_python_builder", lambda _root: Builder())


def test_stage_uv_extracts_only_after_pinned_checksum_verification(tmp_path, monkeypatch):
    module = _load_prepare_tauri_resources()
    target = "x86_64-pc-windows-msvc"
    version = "0.11.14"
    payload = b"verified uv binary"
    archive_path = tmp_path / "uv.zip"
    _write_windows_uv_archive(archive_path, target=target, payload=payload)
    checksum = _sha256(archive_path.read_bytes())

    monkeypatch.setattr(module, "UV_PINNED_VERSION", version)
    monkeypatch.setattr(module, "UV_SHA256_BY_TARGET", {target: checksum})
    monkeypatch.setattr(
        module,
        "UV_BINARY_SHA256_BY_TARGET",
        {target: _sha256(payload)},
    )
    monkeypatch.setattr(module, "download_to_temp", lambda url, attempts=3: archive_path)

    staged = module.stage_uv(tmp_path / "app", target, version)

    assert staged.read_bytes() == payload
    assert staged.name == "uv.exe"
    assert staged.stat().st_mode & 0o222 == 0
    assert staged.stat().st_nlink == 1


def test_stage_uv_only_uses_private_external_root_without_checkout_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The packaging bootstrap stages uv only under its nonce temp root."""
    module = _load_prepare_tauri_resources()
    output_root = tmp_path / "private-uv"
    calls: list[Path] = []

    def stage(source_root: Path, _target: str, _version: str) -> Path:
        calls.append(source_root)
        destination = source_root / "bundled" / "uv"
        destination.parent.mkdir()
        destination.write_bytes(b"pinned")
        return destination

    monkeypatch.setattr(module, "stage_uv", stage)
    monkeypatch.setattr(
        module,
        "build_sealed_python_resource",
        lambda *_args: pytest.fail("stage-only action continued into environment build"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            os.fspath(SCRIPT_PATH),
            "--repo-root",
            os.fspath(ROOT),
            "--target",
            "aarch64-apple-darwin",
            "--uv-version",
            "0.11.14",
            "--stage-uv-only",
            "--uv-output-root",
            os.fspath(output_root),
        ],
    )
    assert module.main() == 0
    assert calls == [output_root]
    assert output_root.stat().st_mode & 0o077 == 0


def test_formal_sealed_resource_uses_producer_snapshot_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Formal Tauri staging consumes the rootless producer output exactly."""
    module = _load_prepare_tauri_resources()
    snapshot_path = tmp_path / "sealed-python"
    snapshot_path.mkdir()
    snapshot = snapshot_path.resolve()
    calls: dict[str, object] = {}

    class Builder:
        def build_environment(self, *_args, **_kwargs):
            pytest.fail("formal Tauri staging must not rebuild sealed Python")

        def validate_environment(self, root, target, **kwargs):
            calls.update(root=root, target=target, kwargs=kwargs)
            return "a" * 64

    monkeypatch.setenv(module.PACKAGING_PYTHON_SNAPSHOT_ENV, str(snapshot))
    monkeypatch.setenv(module.PACKAGING_PYTHON_INVENTORY_SHA_ENV, "b" * 64)
    monkeypatch.setattr(module, "_load_sealed_python_builder", lambda _root: Builder())

    assert (
        module.build_sealed_python_resource(
            ROOT,
            tmp_path / "checkout-runtime",
            "aarch64-apple-darwin",
        )
        == snapshot
    )
    assert calls == {
        "root": snapshot,
        "target": "aarch64-apple-darwin",
        "kwargs": {
            "expected_manifest_digest": "b" * 64,
            "run_native_smoke": True,
        },
    }


def test_formal_snapshot_staging_preserves_sealed_directory_modes(tmp_path: Path) -> None:
    """Copied formal snapshots remain non-writable before bundle validation."""
    module = _load_prepare_tauri_resources()
    source_root = tmp_path / "checkout-runtime"
    source_root.mkdir()
    (source_root / "python-runtime").mkdir()
    (source_root / "python-runtime" / "decoy.txt").write_text("old\n", encoding="utf-8")

    snapshot = tmp_path / "sealed-python"
    nested = snapshot / "runtime" / "bin"
    nested.mkdir(parents=True)
    manifest = snapshot / "sealed-environment.v1.json"
    manifest.write_text("{}\n", encoding="utf-8")
    executable = nested / "python"
    executable.write_bytes(b"python\n")
    for path in (snapshot, snapshot / "runtime", nested):
        path.chmod(0o500)
    manifest.chmod(0o444)
    executable.chmod(0o555)

    destination = tmp_path / "staged"
    destination.mkdir()
    assert (
        module.copy_generated_resource_dirs(
            source_root,
            destination,
            sealed_python_source=snapshot,
        )
        == 2
    )
    staged_root = destination / "python-runtime"
    assert stat.S_IMODE(staged_root.stat().st_mode) == 0o500
    assert stat.S_IMODE((staged_root / "runtime").stat().st_mode) == 0o500
    assert stat.S_IMODE((staged_root / "runtime/bin").stat().st_mode) == 0o500
    assert not (staged_root / "decoy.txt").exists()


def test_stage_uv_fails_on_checksum_mismatch_before_extract(tmp_path, monkeypatch):
    module = _load_prepare_tauri_resources()
    target = "x86_64-unknown-linux-gnu"
    version = "0.11.14"
    payload = b"tampered uv binary"
    archive_path = tmp_path / "uv.tar.gz"
    _write_linux_uv_archive(archive_path, target=target, payload=payload)
    wrong_checksum = "0" * 64

    monkeypatch.setattr(module, "UV_PINNED_VERSION", version)
    monkeypatch.setattr(module, "UV_SHA256_BY_TARGET", {target: wrong_checksum})
    monkeypatch.setattr(module, "download_to_temp", lambda url, attempts=3: archive_path)

    with pytest.raises(RuntimeError, match="uv archive SHA256 mismatch"):
        module.stage_uv(tmp_path / "app", target, version)

    assert not (tmp_path / "app" / "bundled" / "uv").exists()


def test_stage_uv_rejects_archive_member_fallback_and_wrong_target(
    tmp_path,
    monkeypatch,
):
    """A target archive cannot substitute a same-basename member."""
    module = _load_prepare_tauri_resources()
    target = "x86_64-unknown-linux-gnu"
    archive_path = tmp_path / "uv.tar.gz"
    _write_linux_uv_archive(
        archive_path,
        target="aarch64-apple-darwin",
        payload=b"wrong target",
    )
    checksum = _sha256(archive_path.read_bytes())
    monkeypatch.setattr(module, "UV_SHA256_BY_TARGET", {target: checksum})
    monkeypatch.setattr(
        module,
        "UV_BINARY_SHA256_BY_TARGET",
        {target: _sha256(b"wrong target")},
    )
    monkeypatch.setattr(module, "download_to_temp", lambda url, attempts=3: archive_path)

    with pytest.raises(RuntimeError, match="exactly one member"):
        module.stage_uv(tmp_path / "app", target, "0.11.14")


def test_stage_uv_rejects_tampered_extracted_member(tmp_path, monkeypatch):
    """An archive digest alone is not the extracted-member identity."""
    module = _load_prepare_tauri_resources()
    target = "x86_64-unknown-linux-gnu"
    archive_path = tmp_path / "uv.tar.gz"
    payload = b"member tampered after the pinned binary was recorded"
    _write_linux_uv_archive(archive_path, target=target, payload=payload)
    monkeypatch.setattr(
        module,
        "UV_SHA256_BY_TARGET",
        {target: _sha256(archive_path.read_bytes())},
    )
    monkeypatch.setattr(
        module,
        "UV_BINARY_SHA256_BY_TARGET",
        {target: _sha256(b"official member")},
    )
    monkeypatch.setattr(module, "download_to_temp", lambda url, attempts=3: archive_path)

    with pytest.raises(RuntimeError, match="extracted uv SHA256 mismatch"):
        module.stage_uv(tmp_path / "app", target, "0.11.14")


def test_stage_uv_rejects_archive_symlink_member(tmp_path, monkeypatch):
    """Tar link entries are never materialized as the uv executable."""
    module = _load_prepare_tauri_resources()
    target = "x86_64-unknown-linux-gnu"
    archive_path = tmp_path / "uv.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo(name=f"uv-{target}/uv")
        info.type = tarfile.SYMTYPE
        info.linkname = "outside"
        archive.addfile(info)
    monkeypatch.setattr(
        module,
        "UV_SHA256_BY_TARGET",
        {target: _sha256(archive_path.read_bytes())},
    )
    monkeypatch.setattr(module, "download_to_temp", lambda url, attempts=3: archive_path)

    with pytest.raises(RuntimeError, match="regular file"):
        module.stage_uv(tmp_path / "app", target, "0.11.14")


def test_stage_uv_rejects_existing_symlink_destination(tmp_path, monkeypatch):
    """A pre-existing destination link is never replaced through its target."""
    module = _load_prepare_tauri_resources()
    target = "x86_64-pc-windows-msvc"
    payload = b"verified uv binary"
    archive_path = tmp_path / "uv.zip"
    _write_windows_uv_archive(archive_path, target=target, payload=payload)
    monkeypatch.setattr(
        module,
        "UV_SHA256_BY_TARGET",
        {target: _sha256(archive_path.read_bytes())},
    )
    monkeypatch.setattr(
        module,
        "UV_BINARY_SHA256_BY_TARGET",
        {target: _sha256(payload)},
    )
    monkeypatch.setattr(module, "download_to_temp", lambda url, attempts=3: archive_path)
    app_root = tmp_path / "app"
    destination = app_root / "bundled" / "uv.exe"
    destination.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"must remain unchanged")
    try:
        destination.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(RuntimeError, match="may not be a link"):
        module.stage_uv(app_root, target, "0.11.14")
    assert outside.read_bytes() == b"must remain unchanged"


def test_stage_uv_rejects_existing_hardlink_destination(tmp_path, monkeypatch):
    """A pre-existing hardlink cannot become the pinned executable."""
    module = _load_prepare_tauri_resources()
    target = "x86_64-pc-windows-msvc"
    payload = b"verified uv binary"
    archive_path = tmp_path / "uv.zip"
    _write_windows_uv_archive(archive_path, target=target, payload=payload)
    monkeypatch.setattr(
        module,
        "UV_SHA256_BY_TARGET",
        {target: _sha256(archive_path.read_bytes())},
    )
    monkeypatch.setattr(
        module,
        "UV_BINARY_SHA256_BY_TARGET",
        {target: _sha256(payload)},
    )
    monkeypatch.setattr(module, "download_to_temp", lambda url, attempts=3: archive_path)
    app_root = tmp_path / "app"
    destination = app_root / "bundled" / "uv.exe"
    destination.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"must remain unchanged")
    os.link(outside, destination)

    with pytest.raises(RuntimeError, match="may not be hardlinked"):
        module.stage_uv(app_root, target, "0.11.14")
    assert outside.read_bytes() == b"must remain unchanged"


@pytest.mark.parametrize(
    "target",
    (
        "aarch64-apple-darwin",
        "x86_64-apple-darwin",
        "x86_64-pc-windows-msvc",
        "x86_64-unknown-linux-gnu",
    ),
)
@pytest.mark.parametrize("target_dir_kind", ("default", "absolute", "relative-spaces-unicode"))
def test_stage_pack_shell_resolves_cargo_target_dir_deterministically(
    tmp_path,
    monkeypatch,
    target_dir_kind,
    target,
):
    module = _load_prepare_tauri_resources()
    repo_root = tmp_path / "repo"
    source_root = repo_root / "tobkiri_runtime"
    repo_root.mkdir()
    if target_dir_kind == "default":
        monkeypatch.delenv(module.CARGO_TARGET_DIR_ENV, raising=False)
        target_root = repo_root / "pack-shell" / "target"
    elif target_dir_kind == "absolute":
        target_root = (tmp_path / "absolute-target").resolve()
        monkeypatch.setenv(module.CARGO_TARGET_DIR_ENV, str(target_root))
    else:
        monkeypatch.setenv(module.CARGO_TARGET_DIR_ENV, "relative target-雪")
        target_root = repo_root / "relative target-雪"

    binary = _write_pack_shell_binary(module, target_root, target)

    staged = module.stage_pack_shell(repo_root, source_root, target)

    assert staged == (
        source_root / "bundled" / module.pack_shell_binary_name(target)
    ).resolve()
    assert staged.read_bytes() == binary.read_bytes()
    digest_path = module.pack_shell_digest_path(binary)
    assert digest_path.read_text(encoding="ascii") == f"{_sha256(binary.read_bytes())}\n"


def test_stage_pack_shell_replaces_stale_digest_deterministically(tmp_path, monkeypatch):
    module = _load_prepare_tauri_resources()
    repo_root = tmp_path / "repo"
    source_root = repo_root / "tobkiri_runtime"
    target = "aarch64-apple-darwin"
    target_root = repo_root / "cargo-target"
    repo_root.mkdir()
    monkeypatch.setenv(module.CARGO_TARGET_DIR_ENV, str(target_root))
    binary = _write_pack_shell_binary(module, target_root, target)
    digest_path = module.pack_shell_digest_path(binary)
    digest_path.write_text(f"{'0' * 64}\n", encoding="ascii")

    first = module.stage_pack_shell(repo_root, source_root, target)
    first_digest = digest_path.read_bytes()
    second = module.stage_pack_shell(repo_root, source_root, target)

    assert first == second
    assert first_digest == digest_path.read_bytes()
    assert first_digest == f"{_sha256(binary.read_bytes())}\n".encode("ascii")


@pytest.mark.parametrize("case", ("symlink", "directory"))
def test_stage_pack_shell_rejects_unsafe_digest_destination(
    tmp_path,
    monkeypatch,
    case,
):
    module = _load_prepare_tauri_resources()
    repo_root = tmp_path / "repo"
    source_root = repo_root / "tobkiri_runtime"
    target = "aarch64-apple-darwin"
    target_root = repo_root / "cargo-target"
    repo_root.mkdir()
    monkeypatch.setenv(module.CARGO_TARGET_DIR_ENV, str(target_root))
    binary = _write_pack_shell_binary(module, target_root, target)
    digest_path = module.pack_shell_digest_path(binary)
    if case == "symlink":
        outside = tmp_path / "outside-digest"
        outside.write_text("outside\n", encoding="ascii")
        try:
            digest_path.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
    else:
        digest_path.mkdir()

    with pytest.raises(RuntimeError, match="digest destination is unsafe"):
        module.stage_pack_shell(repo_root, source_root, target)


@pytest.mark.parametrize("case", ("missing", "wrong", "symlink"))
def test_stage_pack_shell_rejects_missing_wrong_or_symlinked_binary(
    tmp_path,
    monkeypatch,
    case,
):
    module = _load_prepare_tauri_resources()
    repo_root = tmp_path / "repo"
    source_root = repo_root / "tobkiri_runtime"
    target = "aarch64-apple-darwin"
    target_root = repo_root / "cargo-target"
    repo_root.mkdir()
    monkeypatch.setenv(module.CARGO_TARGET_DIR_ENV, "cargo-target")
    expected = target_root / target / "release" / "pack-shell"

    if case == "wrong":
        _write_pack_shell_binary(
            module,
            target_root,
            target,
            filename="not-pack-shell",
        )
    elif case == "symlink":
        expected.parent.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside-pack-shell"
        outside.write_bytes(b"outside fixture")
        try:
            expected.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises((FileNotFoundError, RuntimeError)):
        module.stage_pack_shell(repo_root, source_root, target)


def test_stage_pack_shell_rejects_target_path_traversal(tmp_path, monkeypatch):
    module = _load_prepare_tauri_resources()
    repo_root = tmp_path / "repo"
    source_root = repo_root / "tobkiri_runtime"
    repo_root.mkdir()
    monkeypatch.delenv(module.CARGO_TARGET_DIR_ENV, raising=False)

    with pytest.raises(ValueError, match="path component"):
        module.stage_pack_shell(repo_root, source_root, "../escape")


@pytest.mark.parametrize(
    "case",
    (
        "parent-traversal",
        "file-root",
        "symlink-root",
        "dangling-symlink-root",
        "non-executable",
        "wrong-arch",
    ),
)
def test_stage_pack_shell_rejects_unsafe_target_roots_and_artifacts(tmp_path, monkeypatch, case):
    module = _load_prepare_tauri_resources()
    repo_root = tmp_path / "repo"
    source_root = repo_root / "tobkiri_runtime"
    repo_root.mkdir()
    target = "aarch64-apple-darwin"

    if case == "parent-traversal":
        monkeypatch.setenv(module.CARGO_TARGET_DIR_ENV, "../outside")
    elif case == "file-root":
        target_root = repo_root / "cargo-target"
        target_root.write_bytes(b"not a directory")
        monkeypatch.setenv(module.CARGO_TARGET_DIR_ENV, str(target_root))
    elif case in {"symlink-root", "dangling-symlink-root"}:
        outside = tmp_path / "outside"
        if case == "symlink-root":
            outside.mkdir()
        target_root = repo_root / "cargo-target"
        try:
            target_root.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        monkeypatch.setenv(module.CARGO_TARGET_DIR_ENV, str(target_root))
    else:
        target_root = repo_root / "cargo-target"
        monkeypatch.setenv(module.CARGO_TARGET_DIR_ENV, str(target_root))
        _write_pack_shell_binary(
            module,
            target_root,
            target,
            executable=case != "non-executable",
            architecture="x86_64" if case == "wrong-arch" else None,
        )

    with pytest.raises((ValueError, RuntimeError)):
        module.stage_pack_shell(repo_root, source_root, target)


def test_stage_pack_shell_does_not_search_wrong_target_or_profile(tmp_path, monkeypatch):
    module = _load_prepare_tauri_resources()
    repo_root = tmp_path / "repo"
    source_root = repo_root / "tobkiri_runtime"
    target_root = repo_root / "cargo-target"
    repo_root.mkdir()
    monkeypatch.setenv(module.CARGO_TARGET_DIR_ENV, str(target_root))
    _write_pack_shell_binary(module, target_root, "x86_64-apple-darwin", profile="release")
    _write_pack_shell_binary(module, target_root, "aarch64-apple-darwin", profile="debug")

    with pytest.raises(FileNotFoundError):
        module.stage_pack_shell(repo_root, source_root, "aarch64-apple-darwin")


def test_validate_bundle_accepts_canonical_v4_stage_without_legacy_authority(tmp_path):
    module = _load_prepare_tauri_resources()
    stage = _minimal_v4_stage(tmp_path)

    module.validate_bundle(stage, False, None, repository_root=ROOT)

    assert not list(stage.rglob("ecosystem.json"))
    assert not list(stage.rglob("rumi.pack.v3.json"))


def test_validate_bundle_accepts_only_formally_validated_python_venv_domains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two producer-owned venv directories are accepted after validation."""
    module = _load_prepare_tauri_resources()
    stage = _minimal_v4_stage(tmp_path)
    sealed_root = _stub_formal_sealed_python_tree(stage, module)
    _stub_formal_builder(monkeypatch, module, sealed_root)

    module.validate_bundle(
        stage,
        False,
        "aarch64-apple-darwin",
        repository_root=ROOT,
    )


@pytest.mark.parametrize(
    "relative",
    (
        "foreign/.venv",
        "foreign/venv",
        "foreign/virtualenv",
        "nested/python-runtime/venv",
        "python-runtime-evil/venv",
    ),
)
def test_validate_bundle_rejects_venv_names_outside_formal_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    """Path/name lookalikes never inherit the formal Python boundary."""
    module = _load_prepare_tauri_resources()
    stage = _minimal_v4_stage(tmp_path)
    sealed_root = _stub_formal_sealed_python_tree(stage, module)
    _stub_formal_builder(monkeypatch, module, sealed_root)
    (stage / relative).mkdir(parents=True)

    with pytest.raises(RuntimeError, match="Forbidden generated bundle directories"):
        module.validate_bundle(
            stage,
            False,
            "aarch64-apple-darwin",
            repository_root=ROOT,
        )


def test_validate_bundle_does_not_allow_an_unsealed_python_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed formal validator cannot be bypassed by the directory policy."""
    module = _load_prepare_tauri_resources()
    stage = _minimal_v4_stage(tmp_path)
    _stub_formal_sealed_python_tree(stage, module)

    class Builder:
        def validate_environment(self, *_args, **_kwargs):
            raise RuntimeError("sealed manifest inventory is invalid")

    monkeypatch.setattr(module, "_load_sealed_python_builder", lambda _root: Builder())

    with pytest.raises(RuntimeError, match="sealed manifest inventory"):
        module.validate_bundle(
            stage,
            False,
            "aarch64-apple-darwin",
            repository_root=ROOT,
        )


def test_formal_python_boundary_rejects_root_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing the validated root invalidates its producer evidence."""
    module = _load_prepare_tauri_resources()
    stage = tmp_path / "app"
    sealed_root = stage / module.SEALED_PYTHON_RESOURCE_DIR
    sealed_root.mkdir(parents=True)
    (sealed_root / Path(module.SEALED_PYTHON_MANIFEST).name).write_text(
        "{}\n", encoding="utf-8"
    )
    _stub_formal_builder(monkeypatch, module, sealed_root)

    boundary = module.validate_sealed_python_resource(
        stage,
        "aarch64-apple-darwin",
        ROOT,
    )
    replacement = tmp_path / "replaced-python-runtime"
    sealed_root.rename(replacement)
    sealed_root.mkdir()
    (sealed_root / Path(module.SEALED_PYTHON_MANIFEST).name).write_text(
        "{}\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="bundle boundary changed"):
        boundary.assert_unchanged()


@pytest.mark.parametrize("case", ("missing", "tampered", "symlink"))
def test_validate_bundle_rejects_sealed_role_closure_drift(tmp_path, case):
    """The staged app includes each direct canonical role target byte-for-byte."""
    module = _load_prepare_tauri_resources()
    stage = _minimal_v4_stage(tmp_path)
    target = stage / "app.py"
    if case == "missing":
        target.unlink()
    elif case == "tampered":
        target.write_bytes(target.read_bytes() + b"\n# drift\n")
    else:
        target.unlink()
        target.symlink_to(ROOT / "tobkiri_runtime/app.py")

    with pytest.raises((FileNotFoundError, RuntimeError)):
        module.validate_bundle(stage, False, None, repository_root=ROOT)


def test_staged_bootstrap_import_and_resource_manifest_are_self_contained(tmp_path):
    module = _load_prepare_tauri_resources()
    stage = _minimal_v4_stage(tmp_path)

    module.validate_bundle(stage, False, None, repository_root=ROOT)
    manifest_path = module.write_runtime_resource_manifest(stage)
    module.verify_runtime_resource_manifest(stage)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in manifest["entries"]}
    assert "core_runtime/__init__.py" in paths
    assert "core_runtime/bootstrap/runtime.py" in paths
    assert "tobkiri_host/runtime.py" in paths
    assert "tobkiri_host/composition.py" in paths
    assert "tobkiri_host/extension_sdk.py" in paths
    assert "tobkiri_host/platform_backends.py" in paths
    assert "tobkiri_host/tauri_roles.py" in paths
    assert "tobkiri_host/canonical-files.v1.json" in paths
    assert not any(path.endswith((".pyc", ".pyo")) for path in paths)
    assert not list(stage.rglob("__pycache__"))

    relocated = tmp_path / "isolated" / "app"
    shutil.copytree(stage, relocated)
    try:
        for path in sorted(relocated.rglob("*"), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        relocated.chmod(0o555)
        module.verify_runtime_resource_manifest(relocated)
        module.verify_staged_bootstrap_import(relocated)
        module.verify_staged_bootstrap_import(relocated)
        assert not list(relocated.rglob("__pycache__"))
    finally:
        relocated.chmod(0o755)
        for path in relocated.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)


def test_runtime_manifest_uses_packaged_sealed_application_path_domain(tmp_path):
    """Python generation matches the Rust path binding from the real artifact."""
    module = _load_prepare_tauri_resources()
    fixture = json.loads(
        (
            ROOT
            / "tobkiri_launcher/schemas/runtime-resource-path-binding.v1.json"
        ).read_text(encoding="utf-8")
    )
    stage = tmp_path / "app"
    target = stage / fixture["outer_path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"artifact-layout-fixture\n")

    outer, application = module.sealed_application_resource_paths(
        fixture["sealed_path"]
    )
    manifest = module.build_runtime_resource_manifest(stage)
    paths = [entry["path"] for entry in manifest["entries"]]

    assert outer == fixture["outer_path"]
    assert application == fixture["application_path"]
    assert paths == [fixture["outer_path"]]
    assert fixture["application_path"] not in paths


@pytest.mark.parametrize(
    "value",
    (
        "app/../defaultspack_entry.py",
        "app//defaultspack_entry.py",
        "app\\defaultspack_entry.py",
        "application/defaultspack_entry.py",
        "app/é.py",
    ),
)
def test_runtime_resource_path_mapping_rejects_ambiguous_domains(value):
    module = _load_prepare_tauri_resources()
    with pytest.raises(RuntimeError):
        module.sealed_application_resource_paths(value)


def test_runtime_resource_manifest_rejects_case_collision_and_hardlink(tmp_path):
    module = _load_prepare_tauri_resources()
    ambiguity_keys: set[str] = set()
    module.assert_runtime_resource_path_unambiguous("Entry.py", ambiguity_keys)
    with pytest.raises(RuntimeError, match="ambiguous by ASCII case"):
        module.assert_runtime_resource_path_unambiguous("entry.py", ambiguity_keys)

    if os.name != "nt":
        stage = tmp_path / "app"
        stage.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("shared\n", encoding="utf-8")
        os.link(outside, stage / "entry.py")
        with pytest.raises(RuntimeError, match="hardlinked"):
            module.build_runtime_resource_manifest(stage)


@pytest.mark.parametrize("case", ("missing", "tampered", "symlink", "unlisted"))
def test_validate_bundle_rejects_host_package_drift(tmp_path, case):
    module = _load_prepare_tauri_resources()
    stage = _minimal_v4_stage(tmp_path)
    target = stage / "tobkiri_host/runtime.py"

    if case == "missing":
        target.unlink()
    elif case == "tampered":
        target.write_bytes(target.read_bytes() + b"\n# tampered\n")
    elif case == "symlink":
        target.unlink()
        target.symlink_to(ROOT / "tobkiri_runtime/tobkiri_host/runtime.py")
    else:
        (stage / "tobkiri_host/unlisted.py").write_text("pass\n", encoding="utf-8")

    with pytest.raises((FileNotFoundError, RuntimeError)):
        module.validate_bundle(stage, False, None, repository_root=ROOT)


@pytest.mark.parametrize(
    ("relative", "is_directory"),
    (("core_runtime/__pycache__", True), ("core_runtime/bootstrap.pyc", False)),
)
def test_staging_and_manifest_reject_python_bytecode(
    tmp_path, relative, is_directory
):
    module = _load_prepare_tauri_resources()
    stage = _minimal_v4_stage(tmp_path)
    target = stage / relative
    if is_directory:
        target.mkdir(parents=True)
    else:
        target.write_bytes(b"bytecode")

    with pytest.raises(RuntimeError, match="generated Python bytecode"):
        module.validate_bundle(stage, False, None, repository_root=ROOT)
    with pytest.raises(RuntimeError, match="generated Python bytecode"):
        module.write_runtime_resource_manifest(stage)


@pytest.mark.parametrize("case", ("missing", "extra", "tampered", "symlink"))
def test_runtime_resource_manifest_rejects_unsafe_or_changed_tree(tmp_path, case):
    module = _load_prepare_tauri_resources()
    stage = _minimal_v4_stage(tmp_path)
    module.write_runtime_resource_manifest(stage)
    target = stage / "core_runtime/bootstrap/runtime.py"

    if case == "missing":
        target.unlink()
    elif case == "extra":
        (stage / "unlisted-resource.txt").write_text("unlisted\n", encoding="utf-8")
    elif case == "tampered":
        target.write_bytes(target.read_bytes() + b"\n# tampered\n")
    else:
        target.unlink()
        target.symlink_to(ROOT / "tobkiri_runtime/core_runtime/bootstrap/runtime.py")

    with pytest.raises((FileNotFoundError, RuntimeError)):
        module.verify_runtime_resource_manifest(stage)


def test_validate_bundle_rejects_legacy_authority_document(tmp_path):
    module = _load_prepare_tauri_resources()
    stage = _minimal_v4_stage(tmp_path)
    legacy = stage / "ecosystem/defaultspack/ecosystem.json"
    legacy.write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="legacy authority"):
        module.validate_bundle(stage, False, None, repository_root=ROOT)


@pytest.mark.parametrize("case", ("missing", "symlink", "tamper", "path", "unlisted"))
def test_validate_bundle_v4_preflight_rejects_drift_and_unsafe_resources(tmp_path, case):
    module = _load_prepare_tauri_resources()
    stage = _minimal_v4_stage(tmp_path)
    pack_root = stage / "ecosystem/defaultspack"

    if case == "missing":
        (pack_root / "pack.v4.json").unlink()
    elif case == "symlink":
        target = pack_root / "v4/packs/defaultspack.pack.v4.json"
        target.unlink()
        target.symlink_to(DEFAULTSPACK_ROOT / "v4/packs/defaultspack.pack.v4.json")
    elif case == "tamper":
        runtime = pack_root / "runtime/conversation.py"
        runtime.write_bytes(runtime.read_bytes() + b"\n# tampered\n")
    elif case == "path":
        lock_path = pack_root / "v4/bundle.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["entries"][0]["path"] = "../escape.json"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
    else:
        (pack_root / "v4/unlisted.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises((FileNotFoundError, RuntimeError)):
        module.validate_bundle(stage, False, None, repository_root=ROOT)
