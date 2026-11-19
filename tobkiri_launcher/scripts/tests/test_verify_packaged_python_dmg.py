from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Iterator
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


def test_attach_retries_only_bounded_resource_unavailability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dmg = tmp_path / "fixture.dmg"
    dmg.write_bytes(b"fixture")
    parent = tmp_path / "mount-parent"
    parent.mkdir()
    mount = MODULE.MountedDmg(dmg, temporary_parent=parent)
    device = Path("/dev/disk999")
    payload = plistlib.dumps(
        {
            "system-entities": [
                {
                    "dev-entry": os.fspath(device),
                    "mount-point": os.fspath(mount.path),
                }
            ]
        }
    )
    results = iter(
        [
            subprocess.CompletedProcess(
                [],
                1,
                stdout=b"",
                stderr=b"Resource temporarily unavailable\n",
            ),
            subprocess.CompletedProcess([], 0, stdout=payload, stderr=b""),
        ]
    )
    calls: list[tuple[object, ...]] = []
    sleeps: list[float] = []
    monkeypatch.setattr(
        MODULE,
        "_run",
        lambda *args, **kwargs: calls.append((*args, kwargs)) or next(results),
    )
    monkeypatch.setattr(MODULE.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        MODULE.MountedDmg,
        "_bind_mounted_path",
        lambda self, candidate: setattr(self, "device", candidate),
    )
    try:
        mount.attach()
    finally:
        mount.device = None
        mount.close()

    assert len(calls) == 2
    assert sleeps == [MODULE._ATTACH_TRANSIENT_DELAY_SECONDS]


def test_attach_does_not_retry_other_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dmg = tmp_path / "fixture.dmg"
    dmg.write_bytes(b"fixture")
    parent = tmp_path / "mount-parent"
    parent.mkdir()
    mount = MODULE.MountedDmg(dmg, temporary_parent=parent)
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        MODULE,
        "_run",
        lambda *args, **kwargs: calls.append((*args, kwargs))
        or subprocess.CompletedProcess(
            [], 1, stdout=b"", stderr=b"invalid image\n"
        ),
    )
    monkeypatch.setattr(
        MODULE.time,
        "sleep",
        lambda _delay: pytest.fail("non-transient failure was retried"),
    )
    try:
        with pytest.raises(MODULE.DmgVerificationError, match="attach failed"):
            mount.attach()
    finally:
        mount.close()

    assert len(calls) == 1


def test_attach_stops_after_bounded_transient_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dmg = tmp_path / "fixture.dmg"
    dmg.write_bytes(b"fixture")
    parent = tmp_path / "mount-parent"
    parent.mkdir()
    mount = MODULE.MountedDmg(dmg, temporary_parent=parent)
    calls: list[tuple[object, ...]] = []
    sleeps: list[float] = []
    monkeypatch.setattr(
        MODULE,
        "_run",
        lambda *args, **kwargs: calls.append((*args, kwargs))
        or subprocess.CompletedProcess(
            [],
            1,
            stdout=b"",
            stderr=b"Resource temporarily unavailable\n",
        ),
    )
    monkeypatch.setattr(MODULE.time, "sleep", sleeps.append)
    try:
        with pytest.raises(MODULE.DmgVerificationError, match="attach failed"):
            mount.attach()
    finally:
        mount.close()

    assert len(calls) == MODULE._ATTACH_TRANSIENT_ATTEMPTS
    assert sleeps == [MODULE._ATTACH_TRANSIENT_DELAY_SECONDS] * 2


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
        expected_native_setup_component_bytes=None,
        expected_native_setup_component_sha256=None,
        expected_native_registry_packs_receipt_sha256=None,
    )

    with pytest.raises(MODULE.DmgVerificationError, match="primary attach failure"):
        MODULE.verify_dmg(args)
    assert "cleanup also failed: cleanup failure" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("digest", "size", "error"),
    [
        ("a" * 64, None, "must be supplied together"),
        (None, "1", "must be supplied together"),
        ("A" * 64, "1", "SHA-256 is invalid"),
        ("a" * 64, str(32 * 1024 * 1024 + 1), "byte count is out of range"),
        ("a" * 64, "0", "byte count is out of range"),
        ("a" * 64, " 1", "byte count is out of range"),
    ],
)
def test_expected_setup_component_requires_a_valid_pin_and_size(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    digest: str | None,
    size: str | None,
    error: str,
) -> None:
    argv = [
        os.fspath(SCRIPT),
        "--dmg", "fixture.dmg",
        "--repo-root", ".",
        "--target", "aarch64-apple-darwin",
        "--expected-manifest-sha256", "b" * 64,
    ]
    if digest is not None:
        argv.extend(("--expected-native-setup-component-sha256", digest))
    if size is not None:
        argv.extend(("--expected-native-setup-component-bytes", size))
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit, match="2"):
        MODULE.parse_args()
    assert error in capsys.readouterr().err


@pytest.mark.parametrize("digest", ["a" * 64, "A" * 64, "sha256:" + "a" * 64, "a"])
def test_registry_receipt_cli_accepts_only_bare_lowercase_sha256(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    digest: str,
) -> None:
    monkeypatch.setattr(sys, "argv", [
        os.fspath(SCRIPT),
        "--dmg", "fixture.dmg",
        "--repo-root", ".",
        "--target", "aarch64-apple-darwin",
        "--expected-manifest-sha256", "b" * 64,
        "--expected-native-registry-packs-receipt-sha256", digest,
    ])
    if digest == "a" * 64:
        args = MODULE.parse_args()
        assert args.expected_native_registry_packs_receipt_sha256 == digest
    else:
        with pytest.raises(SystemExit, match="2"):
            MODULE.parse_args()
        assert "expected native Registry Packs receipt SHA-256 is invalid" in (
            capsys.readouterr().err
        )


def test_registry_receipt_pin_is_forwarded_by_shell_wrapper(tmp_path: Path) -> None:
    dmg = tmp_path / "fixture.dmg"
    dmg.touch()
    result = subprocess.run(
        [
            "bash",
            os.fspath(SCRIPT.with_suffix(".sh")),
            "--dmg",
            os.fspath(dmg),
            "--target",
            "aarch64-apple-darwin",
            "--expected-manifest-sha256",
            "a" * 64,
            "--expected-native-registry-packs-receipt-sha256",
            "A" * 64,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "expected native Registry Packs receipt SHA-256 is invalid" in result.stderr


def _write_registry_packs(
    app: Path, *, version: int = 1, adapter_version: str = "0.3.0"
) -> str:
    directory = app / "Contents/Resources/app/native-registry-packs"
    directory.mkdir(parents=True)
    records = []
    pack_ids = [
        "local.provider-registry", "local.model-registry", "local.saved-conversation"
    ]
    if version >= 2:
        pack_ids.append("local.saved-turn-runtime")
    if version >= 3:
        pack_ids.append("local.saved-orchestration")
    if version >= 4:
        pack_ids.append("local.provider-adapter")
    for pack_id in pack_ids:
        archive = f"{pack_id}.tpk"
        contents = f"fixture archive for {pack_id}".encode()
        (directory / archive).write_bytes(contents)
        records.append({
            "id": pack_id,
            "version": adapter_version if pack_id == "local.provider-adapter" else "0.1.0",
            "archive": archive,
            "archive_bytes": len(contents),
            "archive_sha256": "sha256:" + hashlib.sha256(contents).hexdigest(),
            "manifest_sha256": "sha256:" + "a" * 64,
        })
    receipt = {"format": f"tobkiri.registry-packs/{version}", "packs": records}
    data = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
    (directory / "receipt.json").write_bytes(data)
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(
    params=[
        pytest.param((1, {}), id="three-packs"),
        pytest.param((2, {}), id="with-settlement"),
        pytest.param((3, {}), id="with-orchestration"),
        pytest.param((4, {"adapter_version": "0.1.0"}), id="with-provider-adapter-0.1.0"),
        pytest.param((4, {"adapter_version": "0.2.0"}), id="with-provider-adapter-0.2.0"),
        pytest.param((4, {}), id="with-provider-adapter-0.3.0"),
    ],
)
def registry_mount(
    tmp_path: Path, request: pytest.FixtureRequest,
) -> Iterator[tuple[MODULE.MountedDmg, Path, str]]:
    root = tmp_path.resolve() / "mounted"
    app = root / "Fixture.app"
    version, options = request.param
    receipt_digest = _write_registry_packs(app, version=version, **options)
    mount = MODULE.MountedDmg.__new__(MODULE.MountedDmg)
    mount.path = root
    mount.name = root.name
    mount.parent = root.parent
    mount.parent_fd = os.open(root.parent, MODULE._directory_flags())
    try:
        mount.parent_identity = MODULE.Identity.from_stat(os.fstat(mount.parent_fd))
        mount.mounted_fd = os.open(root, MODULE._directory_flags())
        try:
            metadata = os.fstat(mount.mounted_fd)
            mount.mounted_identity = MODULE.Identity.from_stat(metadata)
            yield mount, app, receipt_digest
        finally:
            os.close(mount.mounted_fd)
    finally:
        os.close(mount.parent_fd)


def test_registry_verifier_checks_all_archive_contents(
    registry_mount: tuple[MODULE.MountedDmg, Path, str],
) -> None:
    mount, app, receipt_digest = registry_mount
    mount.verify_native_registry_packs(app, receipt_digest)
    for archive in sorted((app / MODULE.NATIVE_REGISTRY_PACKS).glob("*.tpk")):
        data = archive.read_bytes()
        archive.write_bytes(b"x" * len(data))
        with pytest.raises(MODULE.DmgVerificationError, match="SHA-256 differs"):
            mount.verify_native_registry_packs(app, receipt_digest)
        archive.write_bytes(data + b"x")
        with pytest.raises(MODULE.DmgVerificationError, match="size differs"):
            mount.verify_native_registry_packs(app, receipt_digest)
        archive.write_bytes(data)


@pytest.mark.parametrize(
    ("entry", "change", "error"),
    [
        ("local.model-registry.tpk", "missing", "missing required entries"),
        ("extra", "extra", "extra entries"),
        ("local.saved-conversation.tpk", "symlink", "without following links"),
        ("local.provider-registry.tpk", "hardlink", "physical regular file"),
        ("local.model-registry.tpk", "fifo", "physical regular file"),
        ("receipt.json", "symlink", "without following links"),
        ("receipt.json", "hardlink", "physical regular file"),
        ("receipt.json", "oversize", "size is out of range"),
        (".", "symlink", "without following links"),
    ],
)
def test_registry_verifier_requires_the_exact_physical_files(
    registry_mount: tuple[MODULE.MountedDmg, Path, str],
    entry: str,
    change: str,
    error: str,
) -> None:
    mount, app, receipt_digest = registry_mount
    target = app / MODULE.NATIVE_REGISTRY_PACKS / entry
    external = mount.path.parent / "external"
    if change == "missing":
        target.unlink()
    elif change == "extra":
        target.touch()
    elif change == "oversize":
        target.write_bytes(b"x" * (MODULE.MAX_REGISTRY_RECEIPT_BYTES + 1))
    elif change == "fifo":
        target.unlink()
        os.mkfifo(target)
    else:
        target.rename(external)
        if change == "symlink":
            target.symlink_to(external, target_is_directory=entry == ".")
        else:
            os.link(external, target)

    with pytest.raises(MODULE.DmgVerificationError, match=error):
        mount.verify_native_registry_packs(app, receipt_digest)


def test_registry_verifier_rejects_replaced_mount_root(
    registry_mount: tuple[MODULE.MountedDmg, Path, str],
) -> None:
    mount, app, receipt_digest = registry_mount
    mount.path.rename(mount.path.with_name("original"))
    _write_registry_packs(app)
    with pytest.raises(MODULE.DmgVerificationError, match="mounted DMG root identity"):
        mount.verify_native_registry_packs(app, receipt_digest)


def test_registry_receipt_pin_is_checked_before_parsing(
    registry_mount: tuple[MODULE.MountedDmg, Path, str],
) -> None:
    mount, app, receipt_digest = registry_mount
    (app / MODULE.NATIVE_REGISTRY_PACKS / "receipt.json").write_bytes(b"invalid JSON")
    with pytest.raises(MODULE.DmgVerificationError, match="receipt SHA-256 differs"):
        mount.verify_native_registry_packs(app, receipt_digest)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("id", "local.provider-registry"),
        ("archive", "../local.model-registry.tpk"),
        ("archive_bytes", True),
        ("archive_bytes", 0),
        ("archive_bytes", 512 * 1024 * 1024 + 1),
        ("archive_sha256", "a" * 64),
        ("manifest_sha256", "sha256:" + "A" * 64),
        ("version", "01.0.0"),
        ("unknown", None),
    ],
)
def test_registry_receipt_rejects_invalid_records_even_with_matching_digest(
    registry_mount: tuple[MODULE.MountedDmg, Path, str],
    field: str,
    replacement: object,
) -> None:
    mount, app, _ = registry_mount
    path = app / MODULE.NATIVE_REGISTRY_PACKS / "receipt.json"
    receipt = json.loads(path.read_bytes())
    receipt["packs"][1][field] = replacement
    payload = json.dumps(receipt).encode()
    path.write_bytes(payload)
    with pytest.raises(MODULE.DmgVerificationError, match="receipt has an invalid"):
        mount.verify_native_registry_packs(app, hashlib.sha256(payload).hexdigest())


def test_registry_receipt_rejects_duplicate_json_keys(
    registry_mount: tuple[MODULE.MountedDmg, Path, str],
) -> None:
    mount, app, _ = registry_mount
    path = app / MODULE.NATIVE_REGISTRY_PACKS / "receipt.json"
    payload = path.read_bytes().replace(
        b'"archive_bytes": ', b'"archive_bytes": 1, "archive_bytes": ', 1
    )
    path.write_bytes(payload)
    with pytest.raises(MODULE.DmgVerificationError, match="receipt is invalid"):
        mount.verify_native_registry_packs(app, hashlib.sha256(payload).hexdigest())


def test_registry_receipt_requires_the_producer_pack_order(
    registry_mount: tuple[MODULE.MountedDmg, Path, str],
) -> None:
    mount, app, _ = registry_mount
    path = app / MODULE.NATIVE_REGISTRY_PACKS / "receipt.json"
    receipt = json.loads(path.read_bytes())
    receipt["packs"].reverse()
    payload = json.dumps(receipt).encode()
    path.write_bytes(payload)
    with pytest.raises(
        MODULE.DmgVerificationError, match="receipt has an invalid archive"
    ):
        mount.verify_native_registry_packs(app, hashlib.sha256(payload).hexdigest())


def test_registry_receipt_version_requires_its_exact_pack_set(
    registry_mount: tuple[MODULE.MountedDmg, Path, str],
) -> None:
    mount, app, receipt_digest = registry_mount
    directory = app / MODULE.NATIVE_REGISTRY_PACKS
    path = directory / "receipt.json"
    receipt = json.loads(path.read_bytes())
    if len(receipt["packs"]) > 3:
        (directory / receipt["packs"][-1]["archive"]).unlink()
        with pytest.raises(MODULE.DmgVerificationError, match="missing required entries"):
            mount.verify_native_registry_packs(app, receipt_digest)
        receipt["format"] = f'tobkiri.registry-packs/{len(receipt["packs"]) - 3}'
    else:
        (directory / "local.saved-turn-runtime.tpk").touch()
        with pytest.raises(MODULE.DmgVerificationError, match="extra entries"):
            mount.verify_native_registry_packs(app, receipt_digest)
        receipt["format"] = "tobkiri.registry-packs/2"

    payload = json.dumps(receipt).encode()
    path.write_bytes(payload)
    with pytest.raises(MODULE.DmgVerificationError, match="receipt has the wrong format"):
        mount.verify_native_registry_packs(app, hashlib.sha256(payload).hexdigest())


@pytest.mark.parametrize(
    ("receipt_version", "unapproved"),
    [(3, "0.1.1"), (3, "0.2.0"), (4, "0.1.1"), (4, "0.2.1"), (4, "0.3.1")],
)
def test_registry_receipt_rejects_unapproved_pack_releases(
    tmp_path: Path, receipt_version: int, unapproved: str,
) -> None:
    app = tmp_path / "Fixture.app"
    _write_registry_packs(app, version=receipt_version)
    path = app / MODULE.NATIVE_REGISTRY_PACKS / "receipt.json"
    receipt = json.loads(path.read_bytes())
    receipt["packs"][-1]["version"] = unapproved
    with pytest.raises(MODULE.DmgVerificationError, match="receipt has an invalid pin"):
        MODULE._registry_archive_pins(json.dumps(receipt).encode())


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
    component = app / MODULE.NATIVE_SETUP_COMPONENT
    component.parent.mkdir()
    component_bytes = b"fixed Wasm fixture"
    component.write_bytes(component_bytes)
    component_digest = hashlib.sha256(component_bytes).hexdigest()
    registry_digest = _write_registry_packs(app, version=4)
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
        mount.verify_native_setup_component(
            mounted_app, len(component_bytes), component_digest
        )
        mount.verify_native_registry_packs(mounted_app, registry_digest)
        with pytest.raises(MODULE.DmgVerificationError, match="Wasm size differs"):
            mount.verify_native_setup_component(
                mounted_app, len(component_bytes) + 1, component_digest
            )
        with pytest.raises(MODULE.DmgVerificationError, match="Wasm SHA-256 differs"):
            mount.verify_native_setup_component(
                mounted_app, len(component_bytes), "b" * 64
            )
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

    mismatch_mount = MODULE.MountedDmg(dmg)
    monkeypatch.setattr(MODULE, "MountedDmg", lambda _dmg: mismatch_mount)
    args = MODULE.argparse.Namespace(
        dmg=dmg,
        repo_root=ROOT,
        target="aarch64-apple-darwin",
        expected_manifest_sha256="a" * 64,
        expected_native_setup_component_bytes=len(component_bytes),
        expected_native_setup_component_sha256=component_digest,
        expected_native_registry_packs_receipt_sha256="b" * 64,
    )
    try:
        with pytest.raises(
            MODULE.DmgVerificationError, match="receipt SHA-256 differs"
        ):
            MODULE.verify_dmg(args)
        assert mismatch_mount.detached
        assert not mismatch_mount.path.exists()
    finally:
        if mismatch_mount.device is not None and not mismatch_mount.detached:
            subprocess.run(
                ["hdiutil", "detach", str(mismatch_mount.device)], check=False
            )


def tempfile_parent_value() -> str:
    """Return macOS's legal /var alias to exercise the CI failure path."""
    candidate = os.environ.get("TMPDIR", "/var/tmp")
    if candidate.startswith("/var/"):
        return candidate
    return "/var/tmp"
