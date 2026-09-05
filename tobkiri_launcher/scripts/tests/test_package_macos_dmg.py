from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import signal
import struct
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "package_macos_dmg.sh"
PUBLISHER_PATH = Path(__file__).resolve().parents[1] / "publish_macos_dmg.py"
CI_ARTIFACT_PATH = (
    Path(__file__).resolve().parents[3] / ".github/scripts/macos_ci_artifact.py"
)
FINAL_NAME = "Tobkiri Launcher_1.2.3_x64.dmg"


def _load_publisher():
    spec = importlib.util.spec_from_file_location("publish_macos_dmg", PUBLISHER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PUBLISHER = _load_publisher()


def _load_ci_artifact():
    spec = importlib.util.spec_from_file_location("macos_ci_artifact", CI_ARTIFACT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CI_ARTIFACT = _load_ci_artifact()


@pytest.fixture(autouse=True)
def _fixture_helper_is_ad_hoc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic bundle fixtures have no host codesign provenance."""
    monkeypatch.setattr(
        CI_ARTIFACT,
        "_inspect_packvm_helper_signing",
        lambda _path: {
            "signing_mode": "ad-hoc",
            "team_id": None,
            "authority": None,
        },
    )


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _create_fake_tools(root: Path, mode: str) -> tuple[Path, Path]:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    state_path = root / "hdiutil-state.json"
    state_path.write_text(
        json.dumps(
            {
                "mode": mode,
                "create_count": 0,
                "info_count": 0,
                "attached": {},
                "detach": [],
                "commands": [],
                "events": [],
                "created_path": None,
            }
        ),
        encoding="utf-8",
    )

    _write_executable(
        bin_dir / "codesign",
        """#!/bin/sh
set -eu
case "${1:-}" in
  --display)
    printf '%s\\n' 'Authority=Developer ID Application: Test'
    ;;
  -d)
    if [ "${2:-}" = "--entitlements" ]; then
      if [ "${FAKE_PACKVM_EXTRA_ENTITLEMENT:-0}" = "1" ]; then
        printf '%s\\n' '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>com.apple.security.virtualization</key><true/><key>com.apple.security.get-task-allow</key><true/></dict></plist>'
      else
        printf '%s\\n' '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>com.apple.security.virtualization</key><true/></dict></plist>'
      fi
    else
      printf '%s\\n' 'Identifier=dev.tobkiri.launcher.packvm-vz-helper' >&2
      printf '%s\\n' 'Signature=adhoc' >&2
      printf '%s\\n' '# designated => cdhash H"0123456789abcdef0123456789abcdef01234567"' >&2
    fi
    ;;
esac
exit 0
""",
    )
    _write_executable(
        bin_dir / "ditto",
        """#!/bin/sh
set -eu
cp -R "$1" "$2"
""",
    )
    _write_executable(
        bin_dir / "plutil",
        """#!/bin/sh
last_argument=''
for argument in "$@"; do
  last_argument=$argument
done
if [ "$last_argument" = "-" ]; then
  # Consume the codesign plist before exiting. Otherwise Linux can deliver
  # SIGPIPE to the producer and fail the packager's pipefail pipeline.
  cat >/dev/null
fi
if [ "${1:-}" = "-extract" ]; then
  if [ "${2:-}" = 'com\\.apple\\.security\\.virtualization' ]; then
    printf '%s\\n' 'true'
  else
    printf '%s\\n' '1.2.3'
  fi
else
  printf '%s\\n' '1.2.3'
fi
""",
    )
    _write_executable(
        bin_dir / "hdiutil",
        r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


state_path = Path(os.environ["FAKE_HDIUTIL_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
command = sys.argv[1]


def save() -> None:
    state_path.write_text(json.dumps(state), encoding="utf-8")


def fail(message: str, code: int) -> None:
    save()
    print(message, file=sys.stderr)
    raise SystemExit(code)


if command == "create":
    output = Path(sys.argv[-1])
    state["create_count"] += 1
    state["commands"].append(sys.argv[1:])
    state["events"].append("create")
    mode = state["mode"]
    if mode == "primary_error":
        workspace = output.parents[1]
        (workspace / "staging" / "external-victim").symlink_to(
            Path(os.environ["FAKE_EXTERNAL_VICTIM"]), target_is_directory=True
        )
        fail("hdiutil: create failed - permission denied", 7)
    if mode == "permanent":
        fail("hdiutil: create failed - permission denied", 7)
    if mode == "symlink":
        output.symlink_to(Path(os.environ["FAKE_EXTERNAL_VICTIM"]) / "keep.txt")
    elif mode == "directory":
        output.mkdir()
    elif mode == "fifo":
        os.mkfifo(output)
    else:
        output.write_bytes(("DMG-%d" % state["create_count"]).encode("ascii"))
    state["created_path"] = str(output)
    if mode in {"resource_busy", "rebound_device", "mount_rebinding"}:
        state["attached"][str(output)] = "/dev/disk42"
        fail("hdiutil: create failed - Resource busy", 1)
    if mode == "success_cleanup_failure":
        workspace = output.parents[1]
        (workspace / "staging" / "external-victim").symlink_to(
            Path(os.environ["FAKE_EXTERNAL_VICTIM"]), target_is_directory=True
        )
    save()
    print("created: %s" % output)
    raise SystemExit(0)

if command == "info":
    state["events"].append("info")
    state["info_count"] += 1
    print("framework : test")
    print("image-path      : /foreign/foreign.dmg")
    print("/dev/disk99 GUID_partition_scheme")
    print("================================================")
    for image_path, device in state["attached"].items():
        current_device = device
        if state["mode"] == "rebound_device" and state["info_count"] > 1:
            current_device = "/dev/disk99"
        if state["mode"] == "mount_rebinding" and state["info_count"] > 1:
            current_device = "/dev/disk43"
        print("image-path      : %s" % image_path)
        print("%s GUID_partition_scheme" % current_device)
        print("%ss1 APFS" % current_device)
        print("================================================")
    save()
    raise SystemExit(0)

if command == "detach":
    state["events"].append("detach")
    state["detach"].append(sys.argv[2])
    state["attached"] = {}
    save()
    raise SystemExit(0)

if command == "verify":
    state["events"].append("verify")
    verification_path = Path(sys.argv[-1])
    if not verification_path.is_file():
        fail("missing image", 2)
    created_path = state.get("created_path")
    if not isinstance(created_path, str):
        fail("missing named image", 2)
    image_path = Path(created_path)
    if state["mode"] == "replaced_path":
        replacement = Path(os.environ["FAKE_EXTERNAL_VICTIM"]) / "replacement.bin"
        replacement.write_bytes(b"replacement-bytes")
        image_path.unlink()
        image_path.symlink_to(replacement)
    save()
    raise SystemExit(0)

fail("unexpected hdiutil command: %s" % command, 2)
""",
    )
    return bin_dir, state_path


def _fixture_app(root: Path) -> Path:
    app = root / "Tobkiri Launcher.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources" / "app" / "python-runtime").mkdir(parents=True)
    (contents / "Info.plist").write_text("fixture", encoding="utf-8")
    (contents / "MacOS" / "launcher").write_text("fixture", encoding="utf-8")
    helper = contents / "MacOS" / "tobkiri-packvm-vz-helper"
    header = bytearray(32)
    header[:4] = b"\xcf\xfa\xed\xfe"
    struct.pack_into("<II", header, 16, 2, 88)
    linkedit = bytearray(72)
    struct.pack_into("<II16s", linkedit, 0, 0x19, 72, b"__LINKEDIT")
    struct.pack_into("<QQ", linkedit, 32, 4, 120)
    struct.pack_into("<Q", linkedit, 48, 4)
    signature = struct.pack("<IIII", 0x1D, 16, 120, 4)
    helper.write_bytes(header + linkedit + signature + b"SIGN")
    inputs = []
    bubblewrap_package = b"b" * CI_ARTIFACT.PACKVM_BUBBLEWRAP_PACKAGE_BYTES
    for name in sorted(CI_ARTIFACT.PACKVM_REQUIRED_PROVISIONING_NAMES):
        relative = f"packvm-vz-provisioning/{name}.fixture"
        if name == "bubblewrap_package":
            relative = "packvm-vz-provisioning/bubblewrap_arm64.deb"
        provisioning_input = contents / "Resources" / relative
        provisioning_input.parent.mkdir(parents=True, exist_ok=True)
        if name == "bubblewrap_package":
            payload = bubblewrap_package
        elif name == "bubblewrap_descriptor":
            payload = json.dumps(
                {
                    "schema": CI_ARTIFACT.PACKVM_BUBBLEWRAP_DESCRIPTOR_SCHEMA,
                    "package": "bubblewrap",
                    "version": "fixture",
                    "architecture": "arm64",
                    "source": {
                        "url": "https://example.test/bubblewrap.deb",
                        "size_bytes": len(bubblewrap_package),
                        "sha256": "sha256:"
                        + hashlib.sha256(bubblewrap_package).hexdigest(),
                    },
                },
                sort_keys=True,
            ).encode("utf-8")
        else:
            payload = f"fixture:{name}".encode("utf-8")
        provisioning_input.write_bytes(payload)
        inputs.append(
            {
                "name": name,
                "path": relative,
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            }
        )
    provisioning_manifest = contents / "Resources" / "packvm-vz-provisioning.v1.json"
    provisioning_manifest.write_text(
        json.dumps(
            {
                "schema": CI_ARTIFACT.PACKVM_PROVISIONING_SCHEMA,
                "target": "aarch64-apple-darwin",
                "boot_mode": "efi",
                "inputs": inputs,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    CI_ARTIFACT.write_packvm_bundle_manifest(app)
    return app


def _formal_binding() -> tuple[str, str]:
    interpreter = Path(os.path.realpath(sys.executable))
    return str(interpreter), hashlib.sha256(interpreter.read_bytes()).hexdigest()


def _prepare(root: Path, mode: str) -> tuple[Path, Path, Path, dict[str, str]]:
    root.mkdir(parents=True, exist_ok=True)
    app = _fixture_app(root)
    output_dir = root / "output"
    output_dir.mkdir()
    formal_python_snapshot = root / "formal-python-snapshot"
    formal_python_snapshot.mkdir()
    bin_dir, state_path = _create_fake_tools(root, mode)
    victim = root / "external-victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    interpreter, digest = _formal_binding()
    environment = os.environ.copy()
    if mode == "fifo_substitution":
        fifo_wrapper = root / "formal-python-fifo-substitution"
        _write_executable(
            fifo_wrapper,
            """#!/usr/bin/env python3
import os
import subprocess
import sys


source = sys.stdin.read()
needle = "    named_metadata = os.lstat(sys.argv[1])\\n"
if os.environ.get("FAKE_FIFO_SUBSTITUTION") == "1":
    source = source.replace(
        needle,
        needle + "    os.unlink(sys.argv[1])\\n    os.mkfifo(sys.argv[1])\\n",
        1,
    )
result = subprocess.run(
    [os.environ["FORMAL_PYTHON_DELEGATE"], *sys.argv[1:]],
    input=source,
    text=True,
    check=False,
)
raise SystemExit(result.returncode)
""",
        )
        interpreter = str(fifo_wrapper)
        digest = hashlib.sha256(fifo_wrapper.read_bytes()).hexdigest()
    environment.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{environment['PATH']}",
            "FAKE_HDIUTIL_STATE": str(state_path),
            "FAKE_EXTERNAL_VICTIM": str(victim),
            "TOBKIRI_PACKAGING_PYTHON": interpreter,
            "TOBKIRI_PACKAGING_PYTHON_SHA256": digest,
            "TOBKIRI_PACKAGING_PYTHON_SNAPSHOT": str(formal_python_snapshot),
        }
    )
    if mode == "fifo_substitution":
        environment.update(
            {
                "FAKE_FIFO_SUBSTITUTION": "1",
                "FORMAL_PYTHON_DELEGATE": str(Path(os.path.realpath(sys.executable))),
            }
        )
    if mode == "extra_helper_entitlement":
        environment["FAKE_PACKVM_EXTRA_ENTITLEMENT"] = "1"
    return app, output_dir, state_path, environment


def _command(app: Path, output_dir: Path) -> list[str]:
    return [
        "/bin/bash",
        str(SCRIPT),
        "--app-bundle",
        str(app),
        "--target",
        "x86_64-apple-darwin",
        "--ad-hoc",
        "--output-dir",
        str(output_dir),
    ]


def _run_packager(
    root: Path, mode: str, *, timeout: float | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    app, output_dir, state_path, environment = _prepare(root, mode)
    process = subprocess.Popen(
        _command(app, output_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as timeout_error:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        raise AssertionError(
            "packager timed out while opening a FIFO substitution\n"
            f"stdout={stdout}\n"
            f"stderr={stderr}"
        ) from timeout_error
    result = subprocess.CompletedProcess(
        process.args, process.returncode, stdout, stderr
    )
    return result, output_dir, state_path


def _state(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _temporary_workspaces(output_dir: Path) -> list[Path]:
    return list(output_dir.glob(".tobkiri-dmg.*"))


def test_resource_busy_is_one_shot_and_preserves_stderr(tmp_path: Path) -> None:
    result, output_dir, state_path = _run_packager(tmp_path, "resource_busy")
    assert isinstance(result, subprocess.CompletedProcess)
    state = _state(state_path)
    argv = state["commands"]
    assert result.returncode == 1, result.stderr
    assert state["create_count"] == 1
    assert len(argv) == 1
    assert argv[0][0] == "create", "foreign hdiutil command"
    assert "Resource busy" in result.stderr
    assert state["detach"] == ["/dev/disk42"]
    assert "/dev/disk99" not in state["detach"]
    assert not (output_dir / FINAL_NAME).exists()
    assert _temporary_workspaces(output_dir) == []


def test_permanent_hdiutil_error_is_not_retried_and_stderr_is_preserved(
    tmp_path: Path,
) -> None:
    result, output_dir, state_path = _run_packager(tmp_path, "permanent")
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 7, result.stderr
    assert _state(state_path)["create_count"] == 1
    assert "permission denied" in result.stderr
    assert "Resource busy" not in result.stderr
    assert not (output_dir / FINAL_NAME).exists()
    assert _temporary_workspaces(output_dir) == []


def test_formal_python_helpers_use_verified_interpreter_and_digest(
    tmp_path: Path,
) -> None:
    app, output_dir, state_path, environment = _prepare(tmp_path, "permanent")
    argv = _command(app, output_dir)
    missing_environment = environment.copy()
    missing_environment.pop("TOBKIRI_PACKAGING_PYTHON")
    missing = subprocess.run(
        argv, capture_output=True, check=False, env=missing_environment, text=True
    )
    assert isinstance(missing, subprocess.CompletedProcess)
    assert missing.returncode != 0
    assert "missing" in missing.stderr.lower()
    assert "path" in missing.stderr.lower()
    relative_environment = environment.copy()
    relative_environment["TOBKIRI_PACKAGING_PYTHON"] = "python3"
    relative = subprocess.run(
        argv, capture_output=True, check=False, env=relative_environment, text=True
    )
    assert isinstance(relative, subprocess.CompletedProcess)
    assert relative.returncode != 0
    assert "absolute" in relative.stderr.lower()
    assert "python3" in relative_environment["TOBKIRI_PACKAGING_PYTHON"]
    mismatch_environment = environment.copy()
    mismatch_environment["TOBKIRI_PACKAGING_PYTHON_SHA256"] = "0" * 64
    mismatch = subprocess.run(
        argv, capture_output=True, check=False, env=mismatch_environment, text=True
    )
    assert isinstance(mismatch, subprocess.CompletedProcess)
    assert mismatch.returncode != 0
    assert "sha256" in mismatch.stderr.lower()
    assert "mismatch" in mismatch.stderr.lower()
    assert "wrapper" in mismatch.stderr.lower()
    assert _state(state_path)["create_count"] == 0

    missing_snapshot_environment = environment.copy()
    missing_snapshot_environment.pop("TOBKIRI_PACKAGING_PYTHON_SNAPSHOT")
    missing_snapshot = subprocess.run(
        argv,
        capture_output=True,
        check=False,
        env=missing_snapshot_environment,
        text=True,
    )
    assert isinstance(missing_snapshot, subprocess.CompletedProcess)
    assert missing_snapshot.returncode != 0
    assert "snapshot" in missing_snapshot.stderr.lower()
    assert _state(state_path)["create_count"] == 0


def test_formal_python_runs_from_the_sealed_snapshot_root(tmp_path: Path) -> None:
    app, output_dir, state_path, environment = _prepare(tmp_path, "permanent")
    snapshot = tmp_path / "formal-python-snapshot"
    delegate = Path(os.path.realpath(sys.executable))
    wrapper = tmp_path / "formal-python"
    _write_executable(
        wrapper,
        """#!/bin/sh
set -eu
if [ "$PWD" != "$EXPECTED_FORMAL_PYTHON_CWD" ]; then
  printf 'unexpected formal Python cwd: %s\\n' "$PWD" >&2
  exit 88
fi
exec "$FORMAL_PYTHON_DELEGATE" "$@"
""",
    )
    environment.update(
        {
            "EXPECTED_FORMAL_PYTHON_CWD": str(snapshot),
            "FORMAL_PYTHON_DELEGATE": str(delegate),
            "TOBKIRI_PACKAGING_PYTHON": str(wrapper),
            "TOBKIRI_PACKAGING_PYTHON_SHA256": hashlib.sha256(
                wrapper.read_bytes()
            ).hexdigest(),
        }
    )
    caller = tmp_path / "caller"
    caller.mkdir()
    result = subprocess.run(
        _command(app, output_dir),
        capture_output=True,
        check=False,
        cwd=caller,
        env=environment,
        text=True,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 7, result.stderr
    assert "permission denied" in result.stderr
    assert _state(state_path)["create_count"] == 1


def test_failure_cleanup_only_detaches_owned_images(tmp_path: Path) -> None:
    result, output_dir, state_path = _run_packager(tmp_path, "resource_busy")
    assert isinstance(result, subprocess.CompletedProcess)
    state = _state(state_path)
    assert result.returncode == 1
    assert state["detach"] == ["/dev/disk42"]
    assert "/dev/disk99" not in state["detach"]
    assert "Resource busy" in result.stderr
    assert _temporary_workspaces(output_dir) == []


def test_output_publication_never_clobbers_existing_trusted_file(
    tmp_path: Path,
) -> None:
    app, output_dir, state_path, environment = _prepare(tmp_path, "success")
    final_path = output_dir / FINAL_NAME
    final_path.write_bytes(b"trusted-existing-output")
    result = subprocess.run(
        _command(app, output_dir),
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 1
    assert _state(state_path)["create_count"] == 0
    assert final_path.read_bytes() == b"trusted-existing-output"
    assert "Refusing to overwrite" in result.stderr
    assert _temporary_workspaces(output_dir) == []


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (
            "Contents/MacOS/tobkiri-packvm-vz-helper",
            "PackVM VZ helper is missing or unsafe",
        ),
        (
            "Contents/Resources/packvm-vz-provisioning/guest_runner.fixture",
            "PackVM provisioning identity changed",
        ),
        (
            "Contents/Resources/packvm-vz-provisioning/bubblewrap_arm64.deb",
            "PackVM provisioning identity changed",
        ),
    ],
)
def test_packager_rejects_missing_or_tampered_packvm_material_before_dmg_creation(
    tmp_path: Path, relative: str, expected: str
) -> None:
    """The installer path never accepts an app lacking its authenticated sidecar."""
    app, output_dir, state_path, environment = _prepare(tmp_path, "success")
    target = app / relative
    if target.name == "tobkiri-packvm-vz-helper":
        target.unlink()
    else:
        target.write_bytes(b"tampered")
    result = subprocess.run(
        _command(app, output_dir),
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert result.returncode == 1
    assert expected in result.stderr
    assert _state(state_path)["create_count"] == 0


def test_packager_uses_explicit_sidecar_and_outer_verification_not_deep_resigning() -> (
    None
):
    """Nested helper signing is explicit so its entitlement cannot be erased."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "verify_packvm_helper_signature" in source
    assert "codesign --verify --strict --all-architectures" in source
    assert "Signature=adhoc" in source
    assert "designated => cdhash" in source
    assert "verify_packvm_vz_entitlements.py" in source
    assert "codesign --verify --deep" not in source
    assert "codesign --force --deep" not in source


def test_packager_rejects_helper_with_extra_entitlement(tmp_path: Path) -> None:
    """Ad-hoc re-signing cannot add helper privilege before DMG publication."""
    app, output_dir, state_path, environment = _prepare(
        tmp_path, "extra_helper_entitlement"
    )
    result = subprocess.run(
        _command(app, output_dir),
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert result.returncode == 1
    assert "PackVM VZ helper entitlements are not exact" in result.stderr
    assert _state(state_path)["create_count"] == 0


def test_primary_package_error_wins_when_cleanup_rejects_external_link(
    tmp_path: Path,
) -> None:
    result, output_dir, state_path = _run_packager(tmp_path, "primary_error")
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 7, result.stderr
    assert "permission denied" in result.stderr
    assert "descriptor-bound POSIX tree contains a symlink" in result.stderr
    assert "Could not remove temporary DMG workspace" in result.stderr
    assert (tmp_path / "external-victim" / "keep.txt").read_text(
        encoding="utf-8"
    ) == "keep"
    assert _state(state_path)["create_count"] == 1
    assert len(_temporary_workspaces(output_dir)) == 1


def test_cleanup_failure_after_success_fails_without_deleting_published_dmg(
    tmp_path: Path,
) -> None:
    result, output_dir, state_path = _run_packager(tmp_path, "success_cleanup_failure")
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 1
    assert "Could not remove temporary DMG workspace" in result.stderr
    published_path = output_dir / FINAL_NAME
    with published_path.open("rb") as stream:
        assert stream.read(len(b"DMG-1")) == b"DMG-1"
    assert published_path.stat().st_size == len(b"DMG-1")
    assert (tmp_path / "external-victim" / "keep.txt").read_text(
        encoding="utf-8"
    ) == "keep"
    assert _state(state_path)["detach"] == []
    assert len(_temporary_workspaces(output_dir)) == 1


def test_rebound_device_is_rejected_without_detaching_foreign_device(
    tmp_path: Path,
) -> None:
    result, output_dir, state_path = _run_packager(tmp_path, "rebound_device")
    assert isinstance(result, subprocess.CompletedProcess)
    state = _state(state_path)
    assert result.returncode == 1
    assert state["detach"] == []
    assert "foreign detach refused" in result.stderr
    assert "/dev/disk99" not in state["detach"]
    assert not (output_dir / FINAL_NAME).exists()
    assert len(_temporary_workspaces(output_dir)) == 1


def test_mount_rebinding_is_rejected_without_detaching_owned_device(
    tmp_path: Path,
) -> None:
    result, output_dir, state_path = _run_packager(tmp_path, "mount_rebinding")
    assert isinstance(result, subprocess.CompletedProcess)
    state = _state(state_path)
    assert result.returncode == 1
    assert state["detach"] == []
    assert "mapping changed" in result.stderr
    assert "/dev/disk42" not in state["detach"]
    assert not (output_dir / FINAL_NAME).exists()
    assert len(_temporary_workspaces(output_dir)) == 1


def test_replaced_image_path_is_not_unlinked_or_detached(tmp_path: Path) -> None:
    result, output_dir, state_path = _run_packager(tmp_path, "replaced_path")
    assert isinstance(result, subprocess.CompletedProcess)
    state = _state(state_path)
    assert result.returncode == 1
    assert state["detach"] == []
    assert "identity changed" in result.stderr
    assert not (output_dir / FINAL_NAME).exists()
    workspace = _temporary_workspaces(output_dir)[0]
    replaced_path = next((workspace / "images").glob("*.dmg"))
    assert replaced_path.is_symlink()
    assert replaced_path.read_bytes() == b"replacement-bytes"
    assert (tmp_path / "external-victim" / "replacement.bin").read_bytes() == (
        b"replacement-bytes"
    )


def test_detached_create_result_is_identity_bound_regular_file_until_retention_verification(
    tmp_path: Path,
) -> None:
    result, output_dir, state_path = _run_packager(tmp_path, "success")
    state = _state(state_path)
    assert result.returncode == 0, result.stderr
    assert "-size" not in state["commands"][0]
    events = state["events"]
    assert "attach" not in events
    assert events.index("create") < events.index("verify")
    assert "detach" not in events
    assert (output_dir / FINAL_NAME).read_bytes() == b"DMG-1"


def test_detached_create_result_replacement_fails_before_attach(tmp_path: Path) -> None:
    result, output_dir, state_path = _run_packager(tmp_path, "replaced_path")
    state = _state(state_path)
    assert result.returncode == 1
    assert "identity changed during retained verification" in result.stderr
    assert "attach" not in state["events"]
    assert state["detach"] == []
    assert not (output_dir / FINAL_NAME).exists()


def test_detached_create_result_non_regular_fails_before_attach(tmp_path: Path) -> None:
    for mode in ("symlink", "directory", "fifo", "fifo_substitution"):
        result, output_dir, state_path = _run_packager(tmp_path / mode, mode, timeout=5)
        state = _state(state_path)
        assert result.returncode == 1
        assert "detached regular file" in result.stderr
        assert "attach" not in state["events"]
        assert "verify" not in state["events"]
        assert state["detach"] == []
        assert not (output_dir / FINAL_NAME).exists()


def test_srcfolder_capacity_does_not_require_a_large_compressed_file(
    tmp_path: Path,
) -> None:
    result, output_dir, state_path = _run_packager(tmp_path, "success")
    state = _state(state_path)
    assert result.returncode == 0, result.stderr
    assert "-size" not in state["commands"][0]
    assert (output_dir / FINAL_NAME).read_bytes() == b"DMG-1"
    assert "attach" not in state["events"]
    assert "detach" not in state["events"]


def test_descriptor_bound_publisher_moves_the_exact_inode(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "temporary.dmg"
    destination = destination_dir / "published.dmg"
    source.write_bytes(b"verified-image")
    expected_identity = PUBLISHER.format_identity(source.stat())

    PUBLISHER.publish(source, destination, expected_identity)

    assert not source.exists()
    assert destination.read_bytes() == b"verified-image"
    assert PUBLISHER.format_identity(destination.stat()) == expected_identity


def test_descriptor_bound_publisher_never_replaces_an_existing_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "temporary.dmg"
    destination = tmp_path / "published.dmg"
    source.write_bytes(b"verified-image")
    destination.write_bytes(b"trusted-existing-output")
    expected_identity = PUBLISHER.format_identity(source.stat())

    with pytest.raises(PUBLISHER.PublicationError, match="overwrite"):
        PUBLISHER.publish(source, destination, expected_identity)

    assert source.read_bytes() == b"verified-image"
    assert destination.read_bytes() == b"trusted-existing-output"


def test_descriptor_bound_publisher_rejects_a_source_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "temporary.dmg"
    retained = source_dir / "retained-original.dmg"
    foreign = source_dir / "foreign.dmg"
    destination = destination_dir / "published.dmg"
    source.write_bytes(b"verified-image")
    foreign.write_bytes(b"foreign-image")
    expected_identity = PUBLISHER.format_identity(source.stat())
    real_rename = PUBLISHER._rename_exclusive

    def swap_then_rename(
        source_directory: int,
        source_name: str,
        destination_directory: int,
        destination_name: str,
    ) -> None:
        source.rename(retained)
        foreign.rename(source)
        real_rename(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
        )

    monkeypatch.setattr(PUBLISHER, "_rename_exclusive", swap_then_rename)
    with pytest.raises(PUBLISHER.PublicationError, match="identity changed"):
        PUBLISHER.publish(source, destination, expected_identity)

    assert retained.read_bytes() == b"verified-image"
    assert destination.read_bytes() == b"foreign-image"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the real hdiutil")
def test_real_hdiutil_create_is_detached_and_compressed(tmp_path: Path) -> None:
    hdiutil = shutil.which("hdiutil")
    assert hdiutil is not None
    source = tmp_path / "source"
    source.mkdir()
    image = tmp_path / "probe.dmg"
    created = subprocess.run(
        [
            hdiutil,
            "create",
            "-srcfolder",
            str(source),
            "-volname",
            "TobkiriProbe",
            "-fs",
            "APFS",
            "-format",
            "UDZO",
            str(image),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert created.returncode == 0, created.stderr
    image_info = subprocess.run(
        [hdiutil, "imageinfo", "-plist", str(image)],
        capture_output=True,
        check=False,
    )
    assert image_info.returncode == 0, image_info.stderr.decode(errors="replace")
    metadata = plistlib.loads(image_info.stdout)
    assert metadata["Format"] == "UDZO"
    assert metadata["Properties"]["Checksummed"] is True
    assert metadata["Properties"]["Compressed"] is True
    assert metadata["Size Information"]["Total Bytes"] > image.stat().st_size
    verified = subprocess.run(
        [hdiutil, "verify", str(image)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    active_images = subprocess.run(
        [hdiutil, "info"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    assert str(image) not in active_images
