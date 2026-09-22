from __future__ import annotations

import json
import hashlib
import hmac
import multiprocessing
import os
import platform
import shutil
import stat
import subprocess
import sys
import threading
import time
import tempfile
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import pytest
import yaml

from ecosystem.defaultspack.backend.sandbox.isolation.lima_runtime import (
    PACKVM_ARTIFACT_STORAGE_BUDGET_BYTES,
    PACKVM_BACKEND_ID,
    PACKVM_DISK_SIZE_BYTES,
    PACKVM_GUEST_FREE_RESERVE_BYTES,
    PACKVM_HOST_STORAGE_RESERVE_BYTES,
    PACKVM_LIMA_INSTANCE,
    PACKVM_PINNED_IMAGE_VIRTUAL_SIZE_BYTES,
    PackVMLimaProvisioner,
    PackVMForeignInstanceError,
    PackVMMutationConflict,
    PackVMProcessError,
    PackVMProvisioningRequest,
    PackVMResponseReconciliationRequired,
    _default_packvm_lima_home,
    _FileLockUnavailable,
    _acquire_exclusive_file_lock,
    _darwin_stat_flags,
    lima_state_path,
    _load_file_lock_module,
    _process_is_alive,
    _release_exclusive_file_lock,
    _safe_process_diagnostic,
)
from ecosystem.defaultspack.backend.sandbox.isolation.packvm_image_cache import (
    PackVMImageCache,
    PackVMImageCancelled,
    PackVMImageProgress,
    PackVMPinnedImage,
    PackVMVerifiedImage,
)
from ecosystem.defaultspack.backend.sandbox.isolation.packvm_image_handoff import (
    PackVMLoopbackImageHandoff,
)
from ecosystem.defaultspack.backend.sandbox.isolation.resources import (
    packvm_guest_runner,
)
from core_runtime.packvm_lifecycle_v4 import PackVMLifecycleV4


MACHINE_ID = "0123456789abcdef0123456789abcdef"


class FakeLima:
    def __init__(self, command_path: Path, instance_dir: Path) -> None:
        self.command_path = command_path
        self.instance_dir = instance_dir
        self.exists = False
        self.running = False
        self.runner_digest = ""
        self.machine_id = MACHINE_ID
        self.config_marker = "original"
        self.commands: list[tuple[str, ...]] = []
        self.fail_install = False
        self.fail_start_after_create = False
        self.fail_delete = False
        self.timeout_start = False
        self.block_delete = False
        self.delete_started = threading.Event()
        self.delete_release = threading.Event()
        self.response_identity_missing = False
        self.challenge_digest_mismatch = False
        self.challenge_calls = 0
        self.persist_start_config = False
        self.include_handoff_in_stderr = False
        self.last_start_location = ""
        self.start_log_payload: bytes | None = None
        self.remove_files_on_delete = False
        self.before_start: Callable[[bytes, tuple[int, ...]], None] | None = None

    def __call__(self, command, input_text, _timeout, inherited_fds=()):
        argv = tuple(str(item) for item in command)
        self.commands.append(argv)
        args = argv[1:]
        if args == ("list", "--format", "{{.Name}}"):
            stdout = f"{PACKVM_LIMA_INSTANCE}\n" if self.exists else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if len(args) >= 4 and args[:2] == ("start", "--name"):
            if self.before_start is not None:
                self.before_start(str(input_text).encode("utf-8"), tuple(inherited_fds))
            config = yaml.safe_load(str(input_text))
            location = config["images"][0]["location"]
            self.last_start_location = str(location)
            if self.persist_start_config:
                self.instance_dir.chmod(0o700)
                config_path = self.instance_dir / "lima.yaml"
                config_path.write_text(str(input_text), encoding="utf-8")
                config_path.chmod(0o600)
                log_directory = self.instance_dir / "logs"
                log_directory.mkdir(mode=0o700, exist_ok=True)
                log_path = log_directory / "download.log"
                log_path.write_bytes(
                    self.start_log_payload
                    if self.start_log_payload is not None
                    else f"source={location}\n".encode()
                )
                log_path.chmod(0o600)
            with urllib.request.urlopen(location, timeout=5) as response:
                consumed_image = response.read()
            assert consumed_image == b"fixture image boundary"
            self.exists = True
            self.running = not self.fail_start_after_create
            if self.timeout_start:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="download stalled at /private/secret/image.img",
                    timed_out=True,
                )
            if self.fail_start_after_create:
                stderr = "start failed at /private/secret/instance"
                if self.include_handoff_in_stderr:
                    stderr += f" {location} {str(location).rsplit('/', 1)[-1]}"
                return SimpleNamespace(
                    returncode=23,
                    stdout="",
                    stderr=stderr,
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ("stop", "--force", PACKVM_LIMA_INSTANCE):
            self.running = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ("delete", "--force", PACKVM_LIMA_INSTANCE):
            if self.block_delete:
                self.delete_started.set()
                self.delete_release.wait(timeout=5)
            if self.fail_delete:
                return SimpleNamespace(returncode=31, stdout="", stderr="delete blocked")
            self.exists = False
            self.running = False
            if self.remove_files_on_delete and self.instance_dir.exists():
                shutil.rmtree(self.instance_dir)
                self.instance_dir.mkdir(mode=0o700)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ("list", PACKVM_LIMA_INSTANCE, "--format"):
            payload = {
                "name": PACKVM_LIMA_INSTANCE,
                "status": "Running" if self.running else "Stopped",
                "arch": "aarch64",
                "vmType": "vz",
                "dir": str(self.instance_dir),
                "config": {
                    "identityMarker": self.config_marker,
                    "vmType": "vz",
                    "mounts": [],
                    "networks": [],
                    "containerd": {"system": False, "user": False},
                    "ssh": {
                        "forwardAgent": False,
                        "forwardX11": False,
                        "forwardX11Trusted": False,
                    },
                    "propagateProxyEnv": False,
                    "hostResolver": {"enabled": False},
                    "portForwards": [
                        {
                            "guestIP": "0.0.0.0",
                            "guestPortRange": [1, 65535],
                            "ignore": True,
                        }
                    ],
                },
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if "install" in args:
            if self.fail_install:
                return SimpleNamespace(returncode=1, stdout="", stderr="install failed")
            from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

            self.runner_digest = lima_runtime._file_digest(lima_runtime._PACKVM_RUNNER)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[-2:] == ("cat", "/etc/machine-id"):
            return SimpleNamespace(returncode=0, stdout=self.machine_id + "\n", stderr="")
        if "sha256sum" in args:
            return SimpleNamespace(
                returncode=0,
                stdout=self.runner_digest.removeprefix("sha256:") + "  runner\n",
                stderr="",
            )
        if args[-1] == "/usr/local/libexec/tobkiri-packvm-supervisor":
            request = json.loads(input_text)
            if request["operation"] == "invoke" and request.get("operation_id") == "challenge":
                challenge = request["payload"]["challenge"]
                self.challenge_calls += 1
                payload = {
                    "challenge_digest": "sha256:" + hashlib.sha256(challenge.encode()).hexdigest()
                }
                if self.challenge_digest_mismatch and self.challenge_calls == 3:
                    payload["challenge_digest"] = "sha256:" + "f" * 64
                identities = {}
            elif request["operation"] == "invoke":
                payload = {"result": "ok"}
                identities = {"guest_artifact_identity": request["guest_artifact_identity"]}
            elif request["operation"] == "materialize":
                payload = None
                identities = {
                    "artifact_digest": request["artifact_digest"],
                    "materialization_digest": request["materialization_digest"],
                    "guest_artifact_identity": "sha256:" + "a" * 64,
                }
            else:
                payload = None
                identities = {}
            if self.response_identity_missing:
                identities = {}
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "protocol": "io.tobkiri.packvm-supervisor.v1",
                        "build_id": "tobkiri-packvm-runner-1",
                        **identities,
                        **({"payload": payload} if payload is not None else {}),
                    }
                ),
                stderr="",
            )
        raise AssertionError(argv)


@pytest.fixture(autouse=True)
def _isolate_packvm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host Lima image-cache discovery inside each test's temp home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def provisioner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_home = tmp_path / "home"
    state_dir = tmp_path / "state"
    short_root = Path("/private/tmp") if platform.system() == "Darwin" else Path("/tmp")
    test_lima_home = short_root / f"tobkiri-lima-test-{uuid.uuid4().hex[:12]}"
    test_home.mkdir(exist_ok=True)
    test_lima_home.mkdir()
    monkeypatch.setenv("HOME", str(test_home))
    monkeypatch.setenv("LIMA_HOME", str(test_lima_home))
    command = tmp_path / "limactl"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o755)
    instance_dir = test_lima_home / PACKVM_LIMA_INSTANCE
    instance_dir.mkdir(mode=0o700)
    fake = FakeLima(command, instance_dir)
    image_cache = _FixtureImageCache(state_dir / "packvm-image-cache")
    manager = PackVMLimaProvisioner(
        command_path=str(command),
        runner=fake,
        state_dir=state_dir,
        machine="arm64",
        disk_usage=lambda _path: SimpleNamespace(free=64 * 1024**3),
        lima_home=test_lima_home,
        image_cache=image_cache,
    )
    yield manager, fake, command
    shutil.rmtree(test_lima_home, ignore_errors=True)


class _FixtureImageCache:
    """Fast verified-local-image boundary used by Lima lifecycle unit tests."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, mode=0o700)
        self._layout = PackVMImageCache(root)
        self._verified: PackVMVerifiedImage | None = None
        self.verified_calls = 0

    def image_path(self, _authority) -> Path:
        return self._layout.image_path(_authority)

    def status(self, _authority) -> tuple[str, None]:
        return ("verified_source" if self._verified else "absent", None)

    def remaining_bytes(self, authority) -> int:
        return 0 if self._verified else authority.size_bytes

    def garbage_collect(self, _authority) -> int:
        return 0

    def prefetch(self, authority, **_kwargs) -> PackVMVerifiedImage:
        path = self.image_path(authority)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        content = b"fixture image boundary"
        path.write_bytes(content)
        metadata = path.stat()
        self._verified = PackVMVerifiedImage(
            path=path,
            digest="sha256:" + hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            device=metadata.st_dev,
            inode=metadata.st_ino,
            source_url=authority.source_url,
        )
        return self._verified

    @contextmanager
    def provisioning_image(self, authority, **_kwargs):
        verified = self.prefetch(authority)
        descriptor = os.open(verified.path, os.O_RDONLY)
        try:
            yield PackVMPinnedImage(verified, descriptor)
        finally:
            os.close(descriptor)

    def verified(self, _authority) -> PackVMVerifiedImage:
        self.verified_calls += 1
        if self._verified is None:
            raise FileNotFoundError
        return self._verified


def _request(plan, *, approve: bool = True) -> PackVMProvisioningRequest:
    return PackVMProvisioningRequest(
        plan_digest=plan.plan_digest,
        ceremony_nonce=plan.ceremony_nonce,
        confirmation=plan.confirmation,
        approve_image_download=approve,
    )


def _hold_packvm_process_claim(
    command_path: str,
    state_dir: str,
    lima_home: str,
    entered: object,
    release: object,
) -> None:
    """Hold the fixed-instance claim from an independent process."""

    manager = PackVMLimaProvisioner(
        command_path=command_path,
        runner=lambda *_args: None,
        state_dir=Path(state_dir),
        machine="arm64",
        lima_home=Path(lima_home),
    )
    binding = {
        "session_digest": "sha256:" + "1" * 64,
        "plan_digest": "sha256:" + "2" * 64,
        "ceremony_nonce_digest": "sha256:" + "3" * 64,
    }
    with manager.operation_gate("provision", binding):
        entered.set()  # type: ignore[attr-defined]
        release.wait(5)  # type: ignore[attr-defined]


def _wait_operation(
    lifecycle: PackVMLifecycleV4,
    operation_id: str,
    *,
    session_id: str = "panel-session-a",
) -> dict[str, object]:
    for _ in range(200):
        result = dict(lifecycle.progress(operation_id, session_id=session_id))
        if result["state"] not in {"queued", "running"}:
            return result
        time.sleep(0.01)
    raise AssertionError("PackVM operation did not settle")


def _write_environment_probe(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "printf 'HOME=%s\\n' \"${HOME-}\"\n"
        "printf 'LIMA_HOME=%s\\n' \"${LIMA_HOME-}\"\n"
        "printf 'XDG_CACHE_HOME=%s\\n' \"${XDG_CACHE_HOME-}\"\n"
        "printf 'TMPDIR=%s\\n' \"${TMPDIR-}\"\n"
        "printf 'PATH=%s\\n' \"${PATH-}\"\n"
        "printf 'UNTRUSTED=%s\\n' \"${PACKVM_UNTRUSTED-}\"\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def test_fixed_instance_claim_is_single_flight_across_processes(provisioner) -> None:
    manager, _fake, command = provisioner
    context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
    entered = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_packvm_process_claim,
        args=(
            str(command),
            str(manager.state_path.parent),
            str(manager.lima_home),
            entered,
            release,
        ),
    )
    process.start()
    assert entered.wait(5)
    try:
        with pytest.raises(PackVMMutationConflict, match="in progress"):
            with manager.operation_gate("prepare", {"session_digest": "sha256:" + "4" * 64}):
                raise AssertionError("conflicting operation unexpectedly acquired the claim")
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0


def test_lock_backend_selection_never_imports_fcntl_for_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    requested: list[str] = []
    windows_backend = object()

    def import_module(name: str) -> object:
        requested.append(name)
        if name == "msvcrt":
            return windows_backend
        raise AssertionError(f"unexpected lock backend import: {name}")

    monkeypatch.setattr(lima_runtime.importlib, "import_module", import_module)
    assert _load_file_lock_module("nt") is windows_backend
    assert requested == ["msvcrt"]


def test_windows_pid_probe_uses_open_process_not_os_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    class Function:
        def __init__(self, result) -> None:
            self.result = result
            self.argtypes = None
            self.restype = None

        def __call__(self, *_args):
            return self.result

    exit_code = SimpleNamespace(value=259)
    ctypes = SimpleNamespace(
        windll=SimpleNamespace(
            kernel32=SimpleNamespace(
                OpenProcess=Function(123),
                GetExitCodeProcess=Function(1),
                CloseHandle=Function(1),
            )
        ),
        c_ulong=lambda: exit_code,
        c_int=object(),
        c_void_p=object(),
        POINTER=lambda _value: object(),
        byref=lambda value: value,
        get_last_error=lambda: 0,
    )
    monkeypatch.setattr(lima_runtime.os, "name", "nt")
    monkeypatch.setattr(
        lima_runtime.importlib,
        "import_module",
        lambda name: ctypes if name == "ctypes" else None,
    )
    monkeypatch.setattr(
        lima_runtime.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(AssertionError("os.kill used on Windows")),
    )
    assert _process_is_alive(1234) is True


def test_file_lock_is_non_reentrant_and_owns_one_byte(tmp_path: Path) -> None:
    lock_path = tmp_path / "portable.lock"
    first = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    second = os.open(lock_path, os.O_RDWR, 0o600)
    try:
        _acquire_exclusive_file_lock(first, timeout_seconds=0.0)
        assert os.fstat(first).st_size == 1
        with pytest.raises(_FileLockUnavailable):
            _acquire_exclusive_file_lock(second, timeout_seconds=0.0)
        _release_exclusive_file_lock(first)
        _acquire_exclusive_file_lock(second, timeout_seconds=0.0)
        _release_exclusive_file_lock(second)
    finally:
        os.close(second)
        os.close(first)


def test_file_lock_waits_only_for_the_explicit_timeout(tmp_path: Path) -> None:
    lock_path = tmp_path / "bounded-wait.lock"
    first = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    second = os.open(lock_path, os.O_RDWR, 0o600)
    released = threading.Event()

    def release_owner() -> None:
        time.sleep(0.05)
        _release_exclusive_file_lock(first)
        released.set()

    try:
        _acquire_exclusive_file_lock(first, timeout_seconds=0.0)
        worker = threading.Thread(target=release_owner)
        worker.start()
        _acquire_exclusive_file_lock(second, timeout_seconds=1.0)
        assert released.wait(1)
        _release_exclusive_file_lock(second)
        worker.join(1)
        assert not worker.is_alive()
    finally:
        os.close(second)
        os.close(first)


def test_process_crash_releases_os_lock_for_exact_recovery(provisioner) -> None:
    manager, _fake, command = provisioner
    context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
    entered = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_packvm_process_claim,
        args=(
            str(command),
            str(manager.state_path.parent),
            str(manager.lima_home),
            entered,
            release,
        ),
    )
    process.start()
    assert entered.wait(5)
    process.terminate()
    process.join(5)
    assert process.exitcode not in {None, 0}
    binding = {
        "session_digest": "sha256:" + "1" * 64,
        "plan_digest": "sha256:" + "2" * 64,
        "ceremony_nonce_digest": "sha256:" + "3" * 64,
    }
    with manager.operation_gate("provision", binding, recover_claim=True):
        assert manager.mutation_claim_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows msvcrt backend")
def test_windows_byte_range_lock_excludes_independent_handles(tmp_path: Path) -> None:
    lock_path = tmp_path / "windows-byte-range.lock"
    first = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    second = os.open(lock_path, os.O_RDWR, 0o600)
    try:
        _acquire_exclusive_file_lock(first, timeout_seconds=0.0)
        with pytest.raises(_FileLockUnavailable):
            _acquire_exclusive_file_lock(second, timeout_seconds=0.0)
        _release_exclusive_file_lock(first)
        _acquire_exclusive_file_lock(second, timeout_seconds=0.0)
        _release_exclusive_file_lock(second)
    finally:
        os.close(second)
        os.close(first)


def test_failed_competitor_never_reconciles_the_owner_instance(provisioner) -> None:
    manager, fake, _command = provisioner
    fake.exists = True
    fake.running = True
    owner = {
        "session_digest": "sha256:" + "1" * 64,
        "plan_digest": "sha256:" + "2" * 64,
        "ceremony_nonce_digest": "sha256:" + "3" * 64,
    }
    competitor = {**owner, "ceremony_nonce_digest": "sha256:" + "4" * 64}
    with manager.operation_gate("provision", owner):
        before = tuple(fake.commands)
        with pytest.raises(PackVMMutationConflict):
            manager.cleanup_failed_provision(
                f"DELETE {PACKVM_LIMA_INSTANCE}",
                competitor,
            )
        assert tuple(fake.commands) == before
        assert fake.exists is True
        assert fake.running is True


def test_restart_recovery_adopts_only_the_exact_dead_owner_claim(provisioner) -> None:
    manager, _fake, _command = provisioner
    binding = {
        "session_digest": "sha256:" + "1" * 64,
        "plan_digest": "sha256:" + "2" * 64,
        "ceremony_nonce_digest": "sha256:" + "3" * 64,
    }
    manager.state_path.parent.mkdir(parents=True, exist_ok=True)
    manager.mutation_claim_path.write_text(
        json.dumps(
            {
                "version": 1,
                "operation": "provision",
                "instance": PACKVM_LIMA_INSTANCE,
                "owner_pid": 99_999_999,
                "binding": binding,
            }
        ),
        encoding="utf-8",
    )
    manager.mutation_claim_path.chmod(0o600)

    with manager.operation_gate("provision", binding, recover_claim=True):
        claim = json.loads(manager.mutation_claim_path.read_text(encoding="utf-8"))
        assert claim["owner_pid"] == os.getpid()
    assert not manager.mutation_claim_path.exists()


def test_sensitive_shell_rebinds_identity_after_response(
    provisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, fake, _command = provisioner
    assert manager.provision(_request(manager.prepare())).ready
    original = manager._verify_exact_current_instance
    calls = 0

    def swap_after_sensitive_shell(state, *, require_guest):
        nonlocal calls
        calls += 1
        if calls == 2:
            fake.machine_id = "f" * 32
        return original(state, require_guest=require_guest)

    monkeypatch.setattr(manager, "_verify_exact_current_instance", swap_after_sensitive_shell)
    request = {
        "operation": "invoke",
        "guest_artifact_identity": "sha256:" + "b" * 64,
    }
    with pytest.raises(PackVMForeignInstanceError, match="reconciliation"):
        manager.invoke_guest(request)


def test_sensitive_response_requires_exact_artifact_identity(provisioner) -> None:
    manager, fake, _command = provisioner
    assert manager.provision(_request(manager.prepare())).ready
    fake.response_identity_missing = True

    with pytest.raises(PackVMResponseReconciliationRequired, match="identity is missing"):
        manager.materialize_artifact(
            {
                "operation": "materialize",
                "artifact_digest": "sha256:" + "a" * 64,
                "materialization_digest": "sha256:" + "b" * 64,
            }
        )


def test_sensitive_response_transcript_rejects_digest_or_nonce_replay(provisioner) -> None:
    manager, fake, _command = provisioner
    assert manager.provision(_request(manager.prepare())).ready
    fake.challenge_digest_mismatch = True
    fake.challenge_calls = 0

    with pytest.raises(PackVMResponseReconciliationRequired, match="transcript mismatch"):
        manager.invoke_guest(
            {
                "operation": "invoke",
                "guest_artifact_identity": "sha256:" + "c" * 64,
            }
        )


def test_transcript_binding_is_forward_compatible_with_new_guest_operations(
    provisioner,
) -> None:
    manager, _fake, _command = provisioner
    assert manager.provision(_request(manager.prepare())).ready

    response = manager.invoke_guest({"operation": "cancel", "request_id": "request-1"})
    assert response["ok"] is True
    assert response["protocol"] == "io.tobkiri.packvm-supervisor.v1"


def _resign_operations(manager: PackVMLimaProvisioner, payload: dict[str, object]) -> None:
    """Write an intentionally modified but authentically signed operation state."""

    operations_path = manager.state_path.parent / "packvm-operations.json"
    unsigned = {key: value for key, value in payload.items() if key != "authentication"}
    key = (manager.state_path.parent / "packvm-operations.key").read_bytes()
    authentication = hmac.new(
        key,
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    operations_path.write_text(
        json.dumps(
            {**unsigned, "authentication": authentication},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    operations_path.chmod(0o600)


def test_call_passes_only_validated_lima_environment_to_real_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    probe = tmp_path / "environment-probe"
    _write_environment_probe(probe)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LIMA_HOME", raising=False)
    monkeypatch.setenv("PACKVM_UNTRUSTED", "must-not-cross-process-boundary")

    manager = PackVMLimaProvisioner(
        command_path=str(probe),
        state_dir=tmp_path / "state",
        machine="arm64",
    )
    result = manager._call((str(probe),), timeout=10)

    assert result.returncode == 0
    private_home = manager.state_path.parent / "packvm-lima-process-home"
    assert result.stdout.splitlines() == [
        f"HOME={private_home / 'home'}",
        f"LIMA_HOME={manager.lima_home}",
        f"XDG_CACHE_HOME={private_home / 'cache'}",
        f"TMPDIR={private_home / 'tmp'}",
        "PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        "UNTRUSTED=",
    ]


@pytest.mark.parametrize("variable", ["HOME", "LIMA_HOME"])
@pytest.mark.parametrize(
    "invalid_kind",
    ["empty", "relative", "parent", "symlink", "file", "unsafe_permissions"],
)
def test_lima_environment_ignores_unsafe_host_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    invalid_kind: str,
) -> None:
    safe_home = tmp_path / "safe-home"
    safe_lima_home = tmp_path / "safe-lima-home"
    safe_home.mkdir()
    safe_lima_home.mkdir()
    invalid_root = tmp_path / "invalid"
    invalid_root.mkdir()
    if invalid_kind == "empty":
        invalid = ""
    elif invalid_kind == "relative":
        invalid = "relative-lima-home"
    elif invalid_kind == "parent":
        parent_target = tmp_path / "parent-target"
        parent_target.mkdir()
        invalid = str(tmp_path / "invalid" / ".." / "parent-target")
    elif invalid_kind == "symlink":
        target = invalid_root / "target"
        target.mkdir()
        link = invalid_root / "link"
        link.symlink_to(target)
        invalid = str(link)
    elif invalid_kind == "file":
        file_path = invalid_root / "not-a-directory"
        file_path.write_text("not a directory", encoding="utf-8")
        invalid = str(file_path)
    else:
        unsafe = invalid_root / "world-writable"
        unsafe.mkdir()
        os.chmod(unsafe, 0o777)
        invalid = str(unsafe)

    monkeypatch.setenv("HOME", str(safe_home))
    monkeypatch.setenv("LIMA_HOME", str(safe_lima_home))
    monkeypatch.setenv(variable, invalid)
    probe = tmp_path / "environment-probe"
    _write_environment_probe(probe)
    manager = PackVMLimaProvisioner(
        command_path=str(probe),
        state_dir=tmp_path / "state",
        machine="arm64",
    )

    result = manager._call((str(probe),), timeout=10)
    environment = manager._lima_process_environment()
    assert result.returncode == 0
    assert environment["HOME"].startswith(str(manager.state_path.parent))
    assert environment["LIMA_HOME"] == str(manager.lima_home)
    assert invalid not in environment.values()


def test_packvm_rejects_user_default_lima_home_and_foreign_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    default_lima_home = home / ".lima"
    default_lima_home.mkdir()
    command = tmp_path / "limactl"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LIMA_HOME", str(default_lima_home))
    manager = PackVMLimaProvisioner(
        command_path=str(command),
        state_dir=tmp_path / "state",
        machine="arm64",
        lima_home=default_lima_home,
    )
    with pytest.raises(ValueError, match=r"~/.lima"):
        manager._call((str(command), "list"), timeout=10)

    monkeypatch.setenv("LIMA_HOME", str(tmp_path / "dedicated"))
    foreign = PackVMLimaProvisioner(
        command_path=str(command),
        state_dir=tmp_path / "foreign-state",
        machine="arm64",
        instance="default",
    )
    with pytest.raises(ValueError, match="fixed managed identity"):
        foreign.prepare()


def test_default_runtime_root_is_persistent_short_and_restart_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ("界" * 20) / "state"
    first = _default_packvm_lima_home(state_dir, PACKVM_LIMA_INSTANCE)
    monkeypatch.setenv("HOME", str(tmp_path / "attacker-home"))
    monkeypatch.setenv("LIMA_HOME", str(tmp_path / "attacker-lima"))
    second = _default_packvm_lima_home(state_dir, PACKVM_LIMA_INSTANCE)
    restarted = PackVMLimaProvisioner(state_dir=state_dir, machine="arm64")

    assert first == second == restarted.lima_home
    assert "/.tobkiri/packvm/runtime-" in str(first)
    assert not str(first).startswith(("/tmp/", "/private/tmp/"))
    assert len(os.fsencode(first)) < 80

    different_state = _default_packvm_lima_home(tmp_path / "other-state", PACKVM_LIMA_INSTANCE)
    assert different_state != first


def test_lima_state_path_prefers_canonical_user_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PackVM state follows the same canonical root as the v4 runtime."""

    canonical = tmp_path / "canonical-user-data"
    legacy = tmp_path / "legacy-user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(canonical))
    monkeypatch.setenv("RUMI_USER_DATA", str(legacy))
    monkeypatch.delenv("RUMI_SANDBOX_LIMA_STATE", raising=False)

    assert lima_state_path() == canonical / "sandbox" / "lima-runtime.json"


def test_lima_state_path_uses_legacy_fallback_without_ambient_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy state remains readable while ambient homes cannot select its root."""

    canonical = tmp_path / "canonical-user-data"
    legacy = tmp_path / "legacy-user-data"
    ambient_home = tmp_path / "ambient-home"
    ambient_lima = tmp_path / "ambient-lima"
    monkeypatch.setenv("HOME", str(ambient_home))
    monkeypatch.setenv("LIMA_HOME", str(ambient_lima))
    monkeypatch.delenv("TOBKIRI_USER_DATA", raising=False)
    monkeypatch.delenv("RUMI_USER_DATA", raising=False)
    monkeypatch.delenv("RUMI_SANDBOX_LIMA_STATE", raising=False)

    default_path = lima_state_path()
    assert default_path.name == "lima-runtime.json"
    assert default_path.parent.name == "sandbox"
    assert ambient_home not in default_path.parents
    assert ambient_lima not in default_path.parents

    monkeypatch.setenv("RUMI_USER_DATA", str(legacy))
    assert lima_state_path() == legacy / "sandbox" / "lima-runtime.json"

    monkeypatch.setenv("TOBKIRI_USER_DATA", str(canonical))
    assert lima_state_path() == canonical / "sandbox" / "lima-runtime.json"

    explicit = tmp_path / "explicit-state" / "lima-runtime.json"
    monkeypatch.setenv("RUMI_SANDBOX_LIMA_STATE", str(explicit))
    assert lima_state_path() == explicit


def test_packvm_lifecycle_constructor_is_filesystem_immutable(tmp_path: Path) -> None:
    """PackVM journal recovery waits for an operation instead of creating state."""

    state_dir = tmp_path / "packvm-state"
    provisioner = PackVMLimaProvisioner(state_dir=state_dir, machine="arm64")

    PackVMLifecycleV4(provisioner)

    assert not state_dir.exists()


def test_lifecycle_rejects_environment_injection_payload(provisioner) -> None:
    manager, _fake, _command = provisioner
    lifecycle = PackVMLifecycleV4(manager)
    plan = lifecycle.prepare()
    consent_payload = {
        "plan_digest": plan["plan_digest"],
        "ceremony_nonce": plan["ceremony_nonce"],
        "confirmation": plan["confirmation"],
        "approve_image_download": True,
        "env": {"LIMA_HOME": "/tmp/attacker-controlled-lima-home"},
    }

    with pytest.raises(ValueError, match="typed contract"):
        lifecycle.consent(consent_payload)

    consent_payload.pop("env")
    consent = lifecycle.consent(consent_payload)
    with pytest.raises(ValueError, match="typed contract"):
        lifecycle.provision(
            {
                "consent_id": consent["consent_id"],
                "operation_id": str(uuid.uuid4()),
                "env": {"LIMA_HOME": "/tmp/attacker-controlled-lima-home"},
            }
        )


@pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("limactl") is None,
    reason="requires the installed macOS limactl for a non-mutating isolation check",
)
def test_real_limactl_list_isolated_from_user_lima_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.delenv("LIMA_HOME", raising=False)
    monkeypatch.delenv("PACKVM_UNTRUSTED", raising=False)

    command = shutil.which("limactl")
    assert command is not None
    manager = PackVMLimaProvisioner(
        command_path=command,
        state_dir=tmp_path / "state",
        machine="arm64",
    )
    result = manager._call(
        (manager._require_command(), "list", "--format", "{{.Name}}"),
        timeout=10,
    )

    assert result.returncode == 0
    assert PACKVM_LIMA_INSTANCE not in result.stdout
    assert not (manager.lima_home / PACKVM_LIMA_INSTANCE).exists()


def test_fresh_provision_requires_download_approval_and_consumes_ceremony(
    provisioner,
) -> None:
    manager, fake, _command = provisioner
    plan = manager.prepare()
    assert plan.backend_id == PACKVM_BACKEND_ID
    assert plan.image_download_required is True
    assert plan.image_source.startswith("https://cloud-images.ubuntu.com/jammy/20260807/")
    assert plan.image_size_bytes > 600_000_000
    assert plan.disk_size_bytes == 4 * 1024**3
    assert plan.host_free_space_required_bytes == (
        PACKVM_DISK_SIZE_BYTES
        + PACKVM_HOST_STORAGE_RESERVE_BYTES
        + PACKVM_PINNED_IMAGE_VIRTUAL_SIZE_BYTES
        + 4 * plan.image_size_bytes
        + plan.image_size_bytes
    )
    assert plan.host_free_space_available_bytes == 64 * 1024**3
    assert plan.host_free_space_reason is None
    assert plan.image_cache_status == "absent"
    assert plan.image_cache_reason is None
    assert plan.architecture == "arm64"
    assert plan.config_digest.startswith("sha256:")
    assert plan.guest_runner_digest.startswith("sha256:")
    assert plan.host_build_digest.startswith("sha256:")

    with pytest.raises(ValueError, match="explicit approval"):
        manager.provision(_request(plan, approve=False))
    assert all(command[1] == "list" for command in fake.commands)
    with pytest.raises(ValueError, match="already consumed"):
        manager.provision(_request(plan))


def test_checked_in_policy_has_no_network_mount_or_guest_download(provisioner) -> None:
    manager, _fake, _command = provisioner
    config = yaml.safe_load(manager._rendered_config())

    assert config["cpus"] == 2
    assert config["memory"] == "4GiB"
    assert config["disk"] == "4GiB"
    assert config["mounts"] == []
    assert config["networks"] == []
    assert config["propagateProxyEnv"] is False
    assert config["images"][0]["digest"].startswith("sha256:")
    assert "provision" not in config
    assert PACKVM_ARTIFACT_STORAGE_BUDGET_BYTES == packvm_guest_runner.MAX_ARTIFACT_STORAGE_BYTES
    assert PACKVM_GUEST_FREE_RESERVE_BYTES == packvm_guest_runner.MIN_GUEST_FREE_RESERVE_BYTES


def test_provision_fails_before_lima_mutation_when_host_space_is_insufficient(
    tmp_path: Path,
) -> None:
    command = tmp_path / "limactl"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o755)
    instance_dir = tmp_path / PACKVM_LIMA_INSTANCE
    instance_dir.mkdir()
    fake = FakeLima(command, instance_dir)
    available = 2 * 1024**3
    manager = PackVMLimaProvisioner(
        command_path=str(command),
        runner=fake,
        state_dir=tmp_path / "state",
        machine="arm64",
        disk_usage=lambda _path: SimpleNamespace(free=available),
    )

    plan = manager.prepare()
    assert plan.host_free_space_available_bytes == available
    assert "requires at least" in str(plan.host_free_space_reason)
    assert "only 2.00 GiB" in str(plan.host_free_space_reason)
    with pytest.raises(ValueError, match="only 2.00 GiB"):
        manager.provision(_request(plan))
    assert fake.exists is False
    assert all(command[1] == "list" for command in fake.commands)


def test_user_lima_cache_hit_is_never_packvm_authority(
    provisioner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    manager, _fake, _command = provisioner
    content = b"pinned-test-image"
    source = "https://example.invalid/pinned-packvm.img"
    image = dict(lima_runtime._PACKVM_IMAGES["arm64"])
    image.update(
        {
            "url": source,
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    )
    monkeypatch.setitem(lima_runtime._PACKVM_IMAGES, "arm64", image)
    monkeypatch.setenv("HOME", str(tmp_path))
    entry = (
        tmp_path
        / "Library"
        / "Caches"
        / "lima"
        / "download"
        / "by-url-sha256"
        / hashlib.sha256(source.encode()).hexdigest()
    )
    entry.mkdir(parents=True)
    (entry / "url").write_text(source, encoding="utf-8")
    (entry / "data").write_bytes(content)

    plan = manager.prepare()
    assert plan.image_download_required is True
    assert plan.image_cache_status == "absent"
    assert plan.host_free_space_required_bytes == (
        PACKVM_DISK_SIZE_BYTES
        + PACKVM_HOST_STORAGE_RESERVE_BYTES
        + PACKVM_PINNED_IMAGE_VIRTUAL_SIZE_BYTES
        + 4 * len(content)
        + len(content)
    )
    (entry / "data").write_bytes(b"tampered-test-image")
    with pytest.raises(ValueError, match="explicit approval"):
        manager.provision(_request(plan, approve=False))


def test_user_lima_cache_mismatch_cannot_influence_packvm_status(
    provisioner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    manager, _fake, _command = provisioner
    content = b"expected"
    source = "https://example.invalid/pinned-packvm.img"
    image = dict(lima_runtime._PACKVM_IMAGES["arm64"])
    image.update(
        {
            "url": source,
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    )
    monkeypatch.setitem(lima_runtime._PACKVM_IMAGES, "arm64", image)
    monkeypatch.setenv("HOME", str(tmp_path))
    entry = (
        tmp_path
        / "Library"
        / "Caches"
        / "lima"
        / "download"
        / "by-url-sha256"
        / hashlib.sha256(source.encode()).hexdigest()
    )
    entry.mkdir(parents=True)
    (entry / "url").write_text(source, encoding="utf-8")
    (entry / "data").write_bytes(b"tampered")

    plan = manager.prepare()
    assert plan.image_download_required is True
    assert plan.image_cache_status == "absent"
    assert plan.image_cache_reason is None
    assert plan.host_free_space_required_bytes == (
        PACKVM_DISK_SIZE_BYTES
        + PACKVM_HOST_STORAGE_RESERVE_BYTES
        + PACKVM_PINNED_IMAGE_VIRTUAL_SIZE_BYTES
        + 4 * len(content)
        + len(content)
    )
    with pytest.raises(ValueError, match="explicit approval"):
        manager.provision(_request(plan, approve=False))


def test_lima_converted_raw_cache_is_ignored_entirely(
    provisioner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    manager, fake, _command = provisioner
    content = b"expected"
    source = "https://example.invalid/pinned-packvm.img"
    image = dict(lima_runtime._PACKVM_IMAGES["arm64"])
    image.update(
        {
            "url": source,
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    )
    monkeypatch.setitem(lima_runtime._PACKVM_IMAGES, "arm64", image)
    monkeypatch.setenv("HOME", str(tmp_path))
    entry = (
        tmp_path
        / "Library"
        / "Caches"
        / "lima"
        / "download"
        / "by-url-sha256"
        / hashlib.sha256(source.encode()).hexdigest()
    )
    (entry / "imgconv").mkdir(parents=True)
    (entry / "url").write_text(source, encoding="utf-8")
    (entry / "data").write_bytes(content)
    (entry / "imgconv" / "raw").write_bytes(content)
    (entry / "imgconv" / "raw.digest").write_text(
        "sha256:" + hashlib.sha256(content).hexdigest(),
        encoding="utf-8",
    )

    plan = manager.prepare()
    assert plan.image_cache_status == "absent"
    assert plan.image_cache_reason is None
    with pytest.raises(ValueError, match="explicit approval"):
        manager.provision(_request(plan, approve=False))
    assert fake.exists is False


def test_provision_doctor_stop_and_cleanup_are_authenticated(provisioner) -> None:
    manager, fake, _command = provisioner
    plan = manager.prepare()
    doctor = manager.provision(_request(plan))

    assert doctor.ready is True
    assert doctor.backend_id == PACKVM_BACKEND_ID
    assert doctor.attestation_digest
    assert manager.doctor().ready is True
    with pytest.raises(ValueError, match="exact confirmation"):
        manager.stop("STOP something-else")
    manager.stop(f"STOP {PACKVM_LIMA_INSTANCE}")
    assert manager.doctor().ready is False
    fake.running = True
    with pytest.raises(ValueError, match="exact confirmation"):
        manager.cleanup("DELETE something-else")
    manager.cleanup(f"DELETE {PACKVM_LIMA_INSTANCE}")
    assert not manager.state_path.exists()
    assert fake.exists is False


def test_steady_state_remains_cache_independent_after_verified_source_eviction(
    provisioner,
) -> None:
    manager, fake, _command = provisioner
    assert manager.provision(_request(manager.prepare())).ready
    cache = manager.image_cache
    verified_calls = cache.verified_calls
    verified = cache._verified
    assert verified is not None
    original_inode = verified.inode
    verified.path.unlink()
    verified.path.write_bytes(b"evicted and replaced cache entry")
    assert verified.path.stat().st_ino != original_inode
    cache._verified = None

    assert manager.doctor().ready is True
    response = manager.invoke_guest(
        {
            "operation": "invoke",
            "guest_artifact_identity": "sha256:" + "b" * 64,
        }
    )
    assert response["ok"] is True
    manager.stop(f"STOP {PACKVM_LIMA_INSTANCE}")
    fake.running = True
    manager.cleanup(f"DELETE {PACKVM_LIMA_INSTANCE}")

    assert cache.verified_calls == verified_calls
    assert verified.path.read_bytes() == b"evicted and replaced cache entry"
    assert fake.exists is False
    assert not manager.state_path.exists()


def test_cache_hit_provisioning_reads_source_exactly_once_while_descriptor_is_pinned(
    provisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    manager, _fake, _command = provisioner
    authority = manager._image_authority(
        plan_digest="sha256:" + "0" * 64,
        session_digest="sha256:" + "0" * 64,
        operation_id="counting-proof",
    )
    verified = manager.image_cache.prefetch(authority)
    source_inode = verified.inode
    source_bytes = 0
    source_reads = 0
    original_pread = os.pread

    def counted_read(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal source_bytes, source_reads
        data = original_pread(descriptor, size, offset)
        if os.fstat(descriptor).st_ino == source_inode:
            source_reads += 1
            source_bytes += len(data)
        return data

    monkeypatch.setattr(lima_runtime.os, "pread", counted_read)
    plan = manager.prepare()
    assert plan.image_cache_status == "verified_source"
    assert manager.provision(_request(plan)).ready

    assert source_bytes == verified.size_bytes
    assert source_reads == 2  # one bounded copy plus one exact EOF check
    assert manager.image_cache.verified_calls == 0


@pytest.mark.parametrize(
    "action, mutation",
    [
        ("stop", "machine"),
        ("stop", "runner"),
        ("stop", "config"),
        ("stop", "image"),
        ("stop", "directory"),
        ("cleanup", "machine"),
        ("cleanup", "runner"),
        ("cleanup", "config"),
        ("cleanup", "image"),
        ("cleanup", "directory"),
        ("cleanup", "symlink"),
    ],
)
def test_destructive_actions_refuse_same_name_replacement_and_identity_swaps(
    provisioner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    mutation: str,
) -> None:
    manager, fake, command = provisioner
    assert manager.provision(_request(manager.prepare())).ready
    user_lima = tmp_path / "home" / ".lima"
    user_lima.mkdir()
    marker = user_lima / "do-not-touch"
    marker.write_text("user instance", encoding="utf-8")
    before = len(fake.commands)

    if mutation == "machine":
        fake.machine_id = "f" * 32
    elif mutation == "runner":
        fake.runner_digest = "sha256:" + "f" * 64
    elif mutation == "config":
        fake.config_marker = "foreign"
    elif mutation == "image":
        from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

        replacement = dict(lima_runtime._PACKVM_IMAGES["arm64"])
        replacement["digest"] = "sha256:" + "a" * 64
        monkeypatch.setitem(lima_runtime._PACKVM_IMAGES, "arm64", replacement)
    else:
        fake.instance_dir.rmdir()
        if mutation == "directory":
            fake.instance_dir.mkdir()
        else:
            target = tmp_path / "foreign-instance"
            target.mkdir()
            fake.instance_dir.symlink_to(target, target_is_directory=True)

    confirmation = (
        f"STOP {PACKVM_LIMA_INSTANCE}" if action == "stop" else f"DELETE {PACKVM_LIMA_INSTANCE}"
    )
    with pytest.raises(PackVMForeignInstanceError, match="reconciliation"):
        getattr(manager, action)(confirmation)

    destructive = {
        command_tuple[1]
        for command_tuple in fake.commands[before:]
        if len(command_tuple) > 1 and command_tuple[1] in {"stop", "delete"}
    }
    assert destructive == set()
    assert fake.exists is True
    assert marker.read_text(encoding="utf-8") == "user instance"
    assert command.parent != user_lima


def test_restarted_provisioner_refuses_replaced_fixed_name_instance(
    provisioner,
) -> None:
    manager, fake, command = provisioner
    assert manager.provision(_request(manager.prepare())).ready
    fake.instance_dir.rmdir()
    fake.instance_dir.mkdir()
    restarted = PackVMLimaProvisioner(
        command_path=str(command),
        runner=fake,
        state_dir=manager.state_path.parent,
        machine="arm64",
        image_cache=manager.image_cache,
        lima_home=manager.lima_home,
    )

    assert restarted.doctor().ready is False
    before = len(fake.commands)
    with pytest.raises(PackVMForeignInstanceError, match="reconciliation"):
        restarted.cleanup(f"DELETE {PACKVM_LIMA_INSTANCE}")
    assert not any(
        len(item) > 1 and item[1] in {"stop", "delete"} for item in fake.commands[before:]
    )


def test_failed_provision_cleanup_refuses_replaced_same_name_orphan(
    provisioner,
) -> None:
    manager, fake, _command = provisioner
    fake.fail_start_after_create = True
    fake.fail_delete = True
    with pytest.raises(PackVMProcessError):
        manager.provision(_request(manager.prepare()))
    recovery = manager._load_authenticated_recovery()
    fake.instance_dir.rmdir()
    fake.instance_dir.mkdir()
    fake.fail_delete = False
    before = len(fake.commands)

    with pytest.raises(PackVMForeignInstanceError, match="reconciliation"):
        manager.cleanup_failed_provision(f"DELETE {PACKVM_LIMA_INSTANCE}", recovery)
    assert not any(len(item) > 1 and item[1] == "delete" for item in fake.commands[before:])


def test_runtime_surface_recomputes_exact_packvm_attestation_digest(
    provisioner,
) -> None:
    from core_runtime.runtime_surface_v4 import _packvm_attested

    manager, _fake, _command = provisioner
    plan = manager.prepare()
    manager.provision(_request(plan))
    snapshot = manager.readiness_snapshot()

    assert _packvm_attested(snapshot) is True
    assert snapshot["config_digest"] == plan.config_digest
    assert snapshot["image_digest"] == plan.image_digest
    assert snapshot["guest_runner_digest"] == plan.guest_runner_digest
    assert snapshot["host_build_digest"] == plan.host_build_digest

    tampered = {**snapshot, "guest_runner_digest": "sha256:" + "0" * 64}
    assert _packvm_attested(tampered) is False
    expired = {**snapshot, "observed_unix": int(time.time()) - 31}
    assert _packvm_attested(expired) is False


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("state", "authentication failed"),
        ("binary", "limactl binary changed"),
        ("instance", "instance identity changed"),
        ("runner", "guest supervisor changed"),
    ],
)
def test_doctor_rejects_tampered_identity(provisioner, mutation: str, expected: str) -> None:
    manager, fake, command = provisioner
    plan = manager.prepare()
    assert manager.provision(_request(plan)).ready
    if mutation == "state":
        payload = json.loads(manager.state_path.read_text(encoding="utf-8"))
        payload["config_digest"] = "sha256:" + "0" * 64
        manager.state_path.write_text(json.dumps(payload), encoding="utf-8")
        manager.state_path.chmod(0o600)
    elif mutation == "binary":
        command.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        command.chmod(0o755)
    elif mutation == "instance":
        fake.machine_id = "f" * 32
    else:
        fake.runner_digest = "sha256:" + "f" * 64
    result = manager.doctor()
    assert result.ready is False
    assert expected in str(result.reason)


def test_symlinked_cli_and_state_fail_closed(provisioner, tmp_path: Path) -> None:
    manager, _fake, command = provisioner
    symlink = tmp_path / "limactl-link"
    symlink.symlink_to(command)
    linked = PackVMLimaProvisioner(
        command_path=str(symlink), state_dir=tmp_path / "linked", machine="arm64"
    )
    linked_plan = linked.prepare()
    assert linked_plan.limactl is None
    assert "regular executable" in str(linked_plan.launcher_reason)

    plan = manager.prepare()
    assert manager.provision(_request(plan)).ready
    state_copy = manager.state_path.read_bytes()
    manager.state_path.unlink()
    target = tmp_path / "state-copy"
    target.write_bytes(state_copy)
    target.chmod(0o600)
    manager.state_path.symlink_to(target)
    assert manager.doctor().ready is False
    assert "unsafe PackVM state" in str(manager.doctor().reason)


def test_partial_provision_stops_guest_without_attesting(provisioner) -> None:
    manager, fake, _command = provisioner
    fake.fail_install = True
    plan = manager.prepare()
    with pytest.raises(PackVMProcessError, match="install failed"):
        manager.provision(_request(plan))
    assert fake.exists is False
    assert fake.running is False
    assert not manager.state_path.exists()
    assert "provision_failed" in manager.audit_path.read_text(encoding="utf-8")


def test_failed_start_reconciles_created_stopped_instance(provisioner) -> None:
    manager, fake, _command = provisioner
    fake.fail_start_after_create = True
    plan = manager.prepare()

    with pytest.raises(PackVMProcessError) as captured:
        manager.provision(_request(plan))

    assert captured.value.stage == "start"
    assert captured.value.kind == "exit"
    assert captured.value.exit_code == 23
    assert captured.value.stderr == "start failed at <host-path>"
    assert fake.exists is False
    assert manager.recovery_path.exists() is False
    assert "failed_provision_reconciled" in manager.audit_path.read_text(encoding="utf-8")


def test_orphan_cleanup_uses_authenticated_instance_not_evicted_source_cache(
    provisioner,
) -> None:
    manager, fake, _command = provisioner
    fake.fail_start_after_create = True
    fake.fail_delete = True
    with pytest.raises(PackVMProcessError):
        manager.provision(_request(manager.prepare()))
    recovery = manager._load_authenticated_recovery()
    cached = manager.image_cache._verified
    assert cached is not None
    cached.path.write_bytes(b"tampered after failed start")
    manager.image_cache._verified = None
    fake.fail_delete = False

    result = manager.cleanup_failed_provision(f"DELETE {PACKVM_LIMA_INSTANCE}", recovery)

    assert result == {"missing": False}
    assert fake.exists is False
    assert not manager.recovery_path.exists()


def test_evicted_cache_does_not_weaken_unrelated_orphan_protection(provisioner) -> None:
    manager, fake, _command = provisioner
    fake.fail_start_after_create = True
    fake.fail_delete = True
    with pytest.raises(PackVMProcessError):
        manager.provision(_request(manager.prepare()))
    recovery = manager._load_authenticated_recovery()
    cached = manager.image_cache._verified
    assert cached is not None
    cached.path.unlink()
    manager.image_cache._verified = None
    fake.instance_dir.rmdir()
    fake.instance_dir.mkdir()
    fake.fail_delete = False

    with pytest.raises(PackVMForeignInstanceError, match="reconciliation"):
        manager.cleanup_failed_provision(f"DELETE {PACKVM_LIMA_INSTANCE}", recovery)
    assert fake.exists is True


@pytest.mark.parametrize("mutation", ["pathname", "parent"])
def test_lima_handoff_rejects_staging_replacement_and_never_writes_outside_jail(
    provisioner,
    tmp_path: Path,
    mutation: str,
) -> None:
    manager, fake, _command = provisioner
    outside = tmp_path / "outside-staging"
    outside.mkdir()

    def swap(_config: bytes, _inherited_fds: tuple[int, ...]) -> None:
        authority = manager._image_authority(
            plan_digest="sha256:" + "0" * 64,
            session_digest="sha256:" + "0" * 64,
            operation_id="staging",
        )
        staged = manager._staging_image_path(authority)
        if mutation == "pathname":
            assert not staged.exists()
            staged.write_bytes(b"unverified replacement")
        else:
            displaced_parent = staged.parent.with_name("displaced-staging")
            staged.parent.rename(displaced_parent)
            staged.parent.symlink_to(outside, target_is_directory=True)

    fake.before_start = swap
    assert manager.provision(_request(manager.prepare())).ready is True

    assert list(outside.iterdir()) == []
    assert fake.exists is True
    assert manager.state_path.exists()


def test_lima_handoff_rejects_same_inode_parent_alias_then_retarget(
    provisioner,
    tmp_path: Path,
) -> None:
    manager, fake, _command = provisioner
    outside = tmp_path / "outside-retarget"
    outside.mkdir()

    def alias_then_retarget(_config: bytes, _inherited_fds: tuple[int, ...]) -> None:
        authority = manager._image_authority(
            plan_digest="sha256:" + "0" * 64,
            session_digest="sha256:" + "0" * 64,
            operation_id="staging",
        )
        staged = manager._staging_image_path(authority)
        assert not staged.exists()
        original_inode = staged.parent.stat().st_ino
        displaced = staged.parent.with_name("displaced-staging-chain")
        staged.parent.rename(displaced)
        staged.parent.symlink_to(displaced, target_is_directory=True)
        assert staged.parent.stat().st_ino == original_inode
        staged.parent.unlink()
        staged.parent.symlink_to(outside, target_is_directory=True)

    fake.before_start = alias_then_retarget
    assert manager.provision(_request(manager.prepare())).ready is True

    assert fake.exists is True
    assert manager.state_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="PackVM Lima is a POSIX runtime")
def test_lima_child_consumes_one_shot_stream_before_uninherited_hostagent_boundary(
    provisioner,
    tmp_path: Path,
) -> None:
    manager, fake, _command = provisioner
    ready = tmp_path / "shim-ready"
    release = tmp_path / "shim-release"
    consumed = tmp_path / "shim-consumed.json"
    outside = tmp_path / "outside-child-handoff"
    outside.mkdir()

    class DescriptorConsumer:
        expected_config_digest = ""

        def __call__(self, command, input_text, timeout, inherited_fds=()):
            argv = tuple(str(item) for item in command)
            if argv[1:3] != ("start", "--name"):
                return fake(command, input_text, timeout, inherited_fds)
            assert argv[-1] == "-"
            assert inherited_fds == ()
            config = str(input_text).encode("utf-8")
            self.expected_config_digest = hashlib.sha256(config).hexdigest()
            location = yaml.safe_load(config)["images"][0]["location"]
            code = (
                "import hashlib,json,pathlib,subprocess,sys,time,urllib.request;"
                "ready=pathlib.Path(sys.argv[1]);release=pathlib.Path(sys.argv[2]);"
                "output=pathlib.Path(sys.argv[3]);location=sys.argv[4];"
                "ready.write_text('ready');"
                "deadline=time.monotonic()+5;"
                "\nwhile not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                "config=sys.stdin.buffer.read();image=urllib.request.urlopen(location).read();"
                "digest=hashlib.sha256(image).hexdigest();"
                "helper=subprocess.run([sys.executable,'-c',"
                "'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',"
                "str(output),digest]);"
                "assert helper.returncode == 0;"
                "output.write_text(json.dumps({'config':hashlib.sha256(config).hexdigest(),"
                "'image':hashlib.sha256(image).hexdigest()}))"
            )
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(ready),
                    str(release),
                    str(consumed),
                    location,
                ],
                input=config,
                capture_output=True,
                timeout=10,
            )
            fake.exists = child.returncode == 0
            fake.running = fake.exists
            return SimpleNamespace(
                returncode=child.returncode,
                stdout=child.stdout,
                stderr=child.stderr,
            )

    consumer = DescriptorConsumer()
    manager._runner = consumer
    result: list[BaseException] = []

    def provision() -> None:
        try:
            manager.provision(_request(manager.prepare()))
        except BaseException as exc:
            result.append(exc)

    worker = threading.Thread(target=provision)
    worker.start()
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    authority = manager._image_authority(
        plan_digest="sha256:" + "0" * 64,
        session_digest="sha256:" + "0" * 64,
        operation_id="staging",
    )
    staged = manager._staging_image_path(authority)
    staging = staged.parent
    assert not staged.exists()
    displaced = staging.with_name("displaced-child-handoff")
    staging.rename(displaced)
    (outside / staged.name).write_bytes(b"unverified replacement")
    staging.symlink_to(outside, target_is_directory=True)
    (manager.state_path.parent / "attacker-config.yaml").write_text(
        "images: [{location: file:///unverified}]\n", encoding="utf-8"
    )
    release.write_text("release", encoding="utf-8")
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert result == []
    child_result = json.loads(consumed.read_text(encoding="utf-8"))
    assert child_result["config"] == consumer.expected_config_digest
    assert child_result["image"] == hashlib.sha256(b"fixture image boundary").hexdigest()
    assert fake.exists is True


def test_failed_start_timeout_preserves_typed_bounded_diagnostic(provisioner) -> None:
    manager, fake, _command = provisioner
    fake.timeout_start = True
    plan = manager.prepare()

    with pytest.raises(PackVMProcessError) as captured:
        manager.provision(_request(plan))

    assert captured.value.kind == "timeout"
    assert captured.value.exit_code is None
    assert captured.value.stderr == "download stalled at <host-path>"
    assert "/private/secret" not in str(captured.value)


def test_reconciled_failed_provision_cleanup_is_safely_idempotent(provisioner) -> None:
    manager, fake, _command = provisioner
    fake.fail_start_after_create = True
    lifecycle = PackVMLifecycleV4(manager)
    plan = lifecycle.prepare(session_id="panel-session-a")
    consent = lifecycle.consent(
        {
            "plan_digest": plan["plan_digest"],
            "ceremony_nonce": plan["ceremony_nonce"],
            "confirmation": plan["confirmation"],
            "approve_image_download": True,
        },
        session_id="panel-session-a",
    )
    provision_operation_id = str(uuid.uuid4())
    lifecycle.provision(
        {"consent_id": consent["consent_id"], "operation_id": provision_operation_id},
        session_id="panel-session-a",
    )
    assert _wait_operation(lifecycle, provision_operation_id)["state"] == "failed"
    assert fake.exists is False
    cleanup_operation_id = str(uuid.uuid4())
    lifecycle.cleanup(
        {
            "confirmation": f"DELETE {PACKVM_LIMA_INSTANCE}",
            "operation_id": cleanup_operation_id,
            "source_operation_id": provision_operation_id,
        },
        session_id="panel-session-a",
    )
    result = _wait_operation(lifecycle, cleanup_operation_id)
    assert result["state"] == "succeeded"
    assert result["result"]["missing"] is True


def test_failed_provision_cleanup_is_durable_session_bound_and_replay_safe(
    provisioner,
) -> None:
    manager, fake, _command = provisioner
    fake.fail_start_after_create = True
    fake.fail_delete = True
    lifecycle = PackVMLifecycleV4(manager)
    session_id = "panel-session-a"
    plan = lifecycle.prepare(session_id=session_id)
    consent = lifecycle.consent(
        {
            "plan_digest": plan["plan_digest"],
            "ceremony_nonce": plan["ceremony_nonce"],
            "confirmation": plan["confirmation"],
            "approve_image_download": True,
        },
        session_id=session_id,
    )
    provision_operation_id = str(uuid.uuid4())
    lifecycle.provision(
        {"consent_id": consent["consent_id"], "operation_id": provision_operation_id},
        session_id=session_id,
    )
    failed = _wait_operation(lifecycle, provision_operation_id)
    assert failed["state"] == "failed"
    assert failed["operation_kind"] == "provision"
    assert failed["diagnostic"] == {
        "code": "packvm_lima_process_failed",
        "stage": "start",
        "kind": "exit",
        "exit_code": 23,
        "stderr": "start failed at <host-path>",
    }
    assert "recovery_proof" not in failed
    assert manager.recovery_path.exists()
    assert fake.exists

    recovery = manager._load_authenticated_recovery()
    with pytest.raises(ValueError, match="proof does not match"):
        manager.cleanup_failed_provision(
            f"DELETE {PACKVM_LIMA_INSTANCE}",
            {**recovery, "image_digest": "sha256:" + "0" * 64},
        )

    cleanup_operation_id = str(uuid.uuid4())
    cleanup_payload = {
        "confirmation": f"DELETE {PACKVM_LIMA_INSTANCE}",
        "operation_id": cleanup_operation_id,
        "source_operation_id": provision_operation_id,
    }
    with pytest.raises(ValueError, match="another authenticated session"):
        lifecycle.progress(provision_operation_id, session_id="wrong-session")
    with pytest.raises(ValueError, match="source is invalid"):
        lifecycle.cleanup(cleanup_payload, session_id="wrong-session")
    with pytest.raises(ValueError, match="exact confirmation"):
        lifecycle.cleanup(
            {**cleanup_payload, "confirmation": "DELETE default"},
            session_id=session_id,
        )

    fake.fail_delete = False
    queued = lifecycle.cleanup(cleanup_payload, session_id=session_id)
    assert queued["operation_kind"] == "cleanup"
    assert queued["state"] in {"queued", "running"}
    cleaned = _wait_operation(lifecycle, cleanup_operation_id)
    assert cleaned["state"] == "succeeded"
    assert cleaned["result"] == {
        "ready": False,
        "instance": PACKVM_LIMA_INSTANCE,
        "cleanup_confirmation": f"DELETE {PACKVM_LIMA_INSTANCE}",
        "missing": False,
    }
    assert fake.exists is False
    assert manager.recovery_path.exists() is False
    assert lifecycle.cleanup(cleanup_payload, session_id=session_id) == cleaned
    with pytest.raises(ValueError, match="already bound"):
        lifecycle.cleanup(
            {**cleanup_payload, "operation_id": str(uuid.uuid4())},
            session_id=session_id,
        )

    restarted = PackVMLifecycleV4(manager)
    assert restarted.progress(cleanup_operation_id, session_id=session_id) == cleaned


def test_running_cleanup_recovers_as_interrupted_after_host_restart(provisioner) -> None:
    manager, fake, _command = provisioner
    assert manager.provision(_request(manager.prepare())).ready
    lifecycle = PackVMLifecycleV4(manager)
    fake.block_delete = True
    operation_id = str(uuid.uuid4())
    lifecycle.cleanup(
        {
            "confirmation": f"DELETE {PACKVM_LIMA_INSTANCE}",
            "operation_id": operation_id,
            "source_operation_id": None,
        },
        session_id="panel-session-a",
    )
    assert fake.delete_started.wait(timeout=2)

    restarted = PackVMLifecycleV4(manager)
    interrupted = restarted.progress(operation_id, session_id="panel-session-a")
    assert interrupted["operation_kind"] == "cleanup"
    assert interrupted["state"] == "interrupted"
    assert interrupted["error_type"] == "PackVMOperationInterrupted"
    fake.delete_release.set()


def test_orphan_cleanup_rejects_symlinked_dedicated_lima_home(
    provisioner,
    tmp_path: Path,
) -> None:
    manager, fake, _command = provisioner
    fake.fail_start_after_create = True
    fake.fail_delete = True
    with pytest.raises(PackVMProcessError):
        manager.provision(_request(manager.prepare()))
    recovery = manager._load_authenticated_recovery()
    original = manager.lima_home
    moved = tmp_path / "moved-lima-home"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinks|unsafe"):
        manager.cleanup_failed_provision(f"DELETE {PACKVM_LIMA_INSTANCE}", recovery)
    assert fake.exists is True


def test_reviewed_image_digest_cannot_change_before_provision(
    provisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    manager, fake, _command = provisioner
    plan = manager.prepare()
    replacement = dict(lima_runtime._PACKVM_IMAGES["arm64"])
    replacement["digest"] = "sha256:" + "f" * 64
    monkeypatch.setitem(lima_runtime._PACKVM_IMAGES, "arm64", replacement)

    with pytest.raises(ValueError, match="plan changed"):
        manager.provision(_request(plan))
    assert fake.exists is False


def test_reviewed_config_cannot_change_before_provision(
    provisioner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    manager, fake, _command = provisioner
    plan = manager.prepare()
    changed = tmp_path / "changed.yaml"
    changed.write_text(
        lima_runtime._PACKVM_CONFIG.read_text(encoding="utf-8") + "cpus: 8\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lima_runtime, "_PACKVM_CONFIG", changed)

    with pytest.raises(ValueError, match="plan changed"):
        manager.provision(_request(plan))
    assert fake.exists is False


@pytest.mark.parametrize("mutation", ["cpus", "image_location"])
def test_executed_stdin_config_must_match_reviewed_semantics(
    provisioner, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    manager, fake, _command = provisioner
    plan = manager.prepare()
    original = manager._rendered_config

    def mutate_only_executed(*, image_location=None):
        rendered = original(image_location=image_location)
        if image_location is None:
            return rendered
        if mutation == "cpus":
            return rendered.replace(b"cpus: 2", b"cpus: 3")
        loaded = yaml.safe_load(rendered)
        loaded["images"][0]["location"] = "http://127.0.0.1:1/packvm-image/" + "f" * 64
        return yaml.safe_dump(loaded).encode("utf-8")

    monkeypatch.setattr(manager, "_rendered_config", mutate_only_executed)
    with pytest.raises(ValueError, match="config|locator"):
        manager.provision(_request(plan))
    assert fake.exists is False
    assert not any(command[1:3] == ("start", "--name") for command in fake.commands)


def test_progress_retained_writable_inode_cannot_change_lima_handoff(
    provisioner,
) -> None:
    manager, fake, _command = provisioner
    retained: list[int] = []
    callback_inode = 0

    def retain_writer(progress: PackVMImageProgress) -> None:
        nonlocal callback_inode
        if progress.stage != "verified" or retained:
            return
        authority = manager._image_authority(
            plan_digest="sha256:" + "0" * 64,
            session_digest="sha256:" + "0" * 64,
            operation_id="staging",
        )
        staged = manager._staging_image_path(authority)
        staged.write_bytes(b"x" * progress.total_bytes)
        descriptor = os.open(staged, os.O_RDWR)
        callback_inode = os.fstat(descriptor).st_ino
        retained.append(descriptor)

    def consume(config: bytes, inherited_fds: tuple[int, ...]) -> None:
        assert inherited_fds == ()
        os.pwrite(retained[0], b"y" * len(b"fixture image boundary"), 0)
        assert os.fstat(retained[0]).st_ino == callback_inode
        location = yaml.safe_load(config)["images"][0]["location"]
        parsed = urllib.parse.urlparse(location)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.path.startswith("/packvm-image/")

    fake.before_start = consume
    try:
        assert manager.provision(_request(manager.prepare()), progress=retain_writer).ready is True
    finally:
        for descriptor in retained:
            os.close(descriptor)


def test_final_progress_cancellation_prevents_staging_and_start(provisioner) -> None:
    manager, fake, _command = provisioner
    cancel_requested = False

    def request_cancel(progress: PackVMImageProgress) -> None:
        nonlocal cancel_requested
        if progress.stage == "verified":
            cancel_requested = True

    with pytest.raises(PackVMImageCancelled):
        manager.provision(
            _request(manager.prepare()),
            progress=request_cancel,
            cancelled=lambda: cancel_requested,
        )

    assert fake.exists is False
    assert not manager.state_path.exists()
    assert not any(command[1:3] == ("start", "--name") for command in fake.commands)
    staging = manager.state_path.parent / "packvm-image-staging"
    assert list(staging.iterdir()) == []


def test_progress_exception_closes_all_staging_descriptors(provisioner) -> None:
    manager, fake, _command = provisioner
    before = len(os.listdir("/dev/fd"))

    def fail(_progress: PackVMImageProgress) -> None:
        raise RuntimeError("progress callback failed")

    with pytest.raises(RuntimeError, match="progress callback failed"):
        manager.provision(_request(manager.prepare()), progress=fail)

    assert len(os.listdir("/dev/fd")) == before
    assert list((manager.state_path.parent / "packvm-image-staging").iterdir()) == []
    assert fake.exists is False


@pytest.mark.skipif(os.name != "posix", reason="crash cleanup uses fork semantics")
def test_crash_reclaims_complete_unlinked_staging_inode(provisioner) -> None:
    manager, _fake, _command = provisioner
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    authority = manager._image_authority(
        plan_digest="sha256:" + "0" * 64,
        session_digest="sha256:" + "0" * 64,
        operation_id="crash-staging",
    )

    def stage_until_crash() -> None:
        with manager.image_cache.provisioning_image(authority) as pinned:
            with manager._staged_image(pinned, progress=None, cancelled=None):
                ready.set()
                release.wait(timeout=30)

    process = context.Process(target=stage_until_crash)
    process.start()
    assert ready.wait(timeout=5)
    staging = manager.state_path.parent / "packvm-image-staging"
    assert list(staging.iterdir()) == []
    process.kill()
    process.join(timeout=5)
    assert process.exitcode is not None
    assert list(staging.iterdir()) == []


def test_darwin_stat_flags_is_safe_for_non_darwin_stat_results() -> None:
    metadata = os.stat(__file__)

    assert _darwin_stat_flags(metadata) == int(getattr(metadata, "st_flags", 0))


@pytest.mark.skipif(platform.system() != "Darwin", reason="legacy flags are Darwin-only")
def test_restart_reconciles_only_valid_legacy_immutable_staging(provisioner) -> None:
    manager, fake, _command = provisioner
    plan = manager.prepare()
    authority = manager._image_authority(
        plan_digest="sha256:" + "0" * 64,
        session_digest="sha256:" + "0" * 64,
        operation_id="staging",
    )
    staged = manager._staging_image_path(authority)
    staged.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged.write_bytes(b"fixture image boundary")
    staged.chmod(0o400)
    os.chflags(staged, stat.UF_IMMUTABLE)
    os.chflags(staged.parent, stat.UF_IMMUTABLE)
    try:
        assert manager.provision(_request(plan)).ready is True
    finally:
        if staged.parent.exists():
            os.chflags(staged.parent, 0)
        if staged.exists():
            os.chflags(staged, 0)

    assert list(staged.parent.iterdir()) == []
    assert fake.exists is True


@pytest.mark.skipif(platform.system() != "Darwin", reason="legacy flags are Darwin-only")
def test_legacy_cleanup_never_clears_unproven_immutable_residue(provisioner) -> None:
    manager, _fake, _command = provisioner
    authority = manager._image_authority(
        plan_digest="sha256:" + "0" * 64,
        session_digest="sha256:" + "0" * 64,
        operation_id="legacy-invalid",
    )
    verified = manager.image_cache.prefetch(authority)
    staged = manager._staging_image_path(authority)
    staged.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged.write_bytes(b"changed image boundary")
    staged.chmod(0o400)
    os.chflags(staged, stat.UF_IMMUTABLE)
    os.chflags(staged.parent, stat.UF_IMMUTABLE)
    descriptor = os.open(staged.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        before_file_flags = staged.stat().st_flags
        before_directory_flags = staged.parent.stat().st_flags
        with pytest.raises(ValueError, match="digest changed"):
            manager._reconcile_legacy_staging(staged, descriptor, verified)
        assert staged.read_bytes() == b"changed image boundary"
        assert staged.stat().st_flags == before_file_flags
        assert staged.parent.stat().st_flags == before_directory_flags
        os.chflags(staged.parent, 0)
        os.chflags(staged, 0)
        staged.unlink()
        os.chflags(staged.parent, stat.UF_IMMUTABLE)
        missing_directory_flags = staged.parent.stat().st_flags
        manager._reconcile_legacy_staging(staged, descriptor, verified)
        assert staged.parent.stat().st_flags == missing_directory_flags
    finally:
        os.close(descriptor)
        os.chflags(staged.parent, 0)
        if staged.exists():
            os.chflags(staged, 0)


def test_handoff_token_is_scrubbed_after_success_and_failure(provisioner) -> None:
    manager, fake, _command = provisioner
    fake.persist_start_config = True
    assert manager.provision(_request(manager.prepare())).ready is True
    endpoint = fake.last_start_location
    token = endpoint.rsplit("/", 1)[-1]
    persisted = (fake.instance_dir / "lima.yaml").read_text(encoding="utf-8")
    assert endpoint not in persisted
    assert token not in persisted
    assert "https://cloud-images.ubuntu.com/" in persisted
    for path in (manager.state_path.parent, manager.lima_home):
        for candidate in path.rglob("*"):
            if candidate.is_file() and candidate.stat().st_size < 1024 * 1024:
                assert token.encode() not in candidate.read_bytes()

    manager.cleanup(f"DELETE {PACKVM_LIMA_INSTANCE}")
    fake.persist_start_config = True
    fake.fail_start_after_create = True
    fake.include_handoff_in_stderr = True
    with pytest.raises(PackVMProcessError) as captured:
        manager.provision(_request(manager.prepare()))
    endpoint = fake.last_start_location
    token = endpoint.rsplit("/", 1)[-1]
    assert endpoint not in str(captured.value)
    assert token not in str(captured.value)
    assert captured.value.stderr is not None
    assert "<packvm-handoff-redacted>" in captured.value.stderr
    for path in (manager.state_path.parent, manager.lima_home):
        for candidate in path.rglob("*"):
            if candidate.is_file() and candidate.stat().st_size < 1024 * 1024:
                assert token.encode() not in candidate.read_bytes()


def test_handoff_scrub_streams_large_and_boundary_spanning_tokens(provisioner) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    manager, fake, _command = provisioner
    endpoint = "http://127.0.0.1:32123/packvm-image/" + "a" * 64
    token = "a" * 64
    logs = fake.instance_dir / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    below = logs / "below.log"
    above = logs / "above.log"
    boundary = logs / "boundary.log"
    binary = logs / "unrelated.bin"
    below.write_bytes(b"x" * 1024 + endpoint.encode())
    above.write_bytes(b"x" * 65_637 + endpoint.encode())
    boundary.write_bytes(
        b"x" * (lima_runtime.PACKVM_LIMA_SCRUB_CHUNK_BYTES - 7) + endpoint.encode()
    )
    binary_payload = b"\x00" + os.urandom(70_000)
    binary.write_bytes(binary_payload)
    above.chmod(0o640)
    binary_identity = (binary.stat().st_dev, binary.stat().st_ino)

    manager._scrub_lima_handoff_artifacts(endpoint, (endpoint, token))

    for path in (below, above, boundary):
        payload = path.read_bytes()
        assert endpoint.encode() not in payload
        assert token.encode() not in payload
        assert b"<packvm-handoff-redacted>" in payload
    assert stat.S_IMODE(above.stat().st_mode) == 0o640
    assert binary.read_bytes() == binary_payload
    assert (binary.stat().st_dev, binary.stat().st_ino) == binary_identity


@pytest.mark.parametrize("unsafe_kind", ["hardlink", "symlink"])
def test_handoff_scrub_rejects_unprovable_links(
    provisioner, tmp_path: Path, unsafe_kind: str
) -> None:
    manager, fake, _command = provisioner
    endpoint = "http://127.0.0.1:32123/packvm-image/" + "b" * 64
    target = tmp_path / "outside.log"
    target.write_text(endpoint, encoding="utf-8")
    candidate = fake.instance_dir / "unsafe.log"
    if unsafe_kind == "hardlink":
        os.link(target, candidate)
    else:
        candidate.symlink_to(target)

    with pytest.raises(ValueError, match="unsafe"):
        manager._scrub_lima_handoff_artifacts(endpoint, (endpoint, "b" * 64))
    assert target.read_text(encoding="utf-8") == endpoint


def test_handoff_scrub_budget_failure_is_tree_atomic(
    provisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    manager, fake, _command = provisioner
    endpoint = "http://127.0.0.1:32123/packvm-image/" + "c" * 64
    first = fake.instance_dir / "first.log"
    second = fake.instance_dir / "second.log"
    first_payload = b"token=" + endpoint.encode()
    second_payload = b"unrelated-log-data" * 128
    first.write_bytes(first_payload)
    second.write_bytes(second_payload)
    identity = (first.stat().st_dev, first.stat().st_ino)
    monkeypatch.setattr(
        lima_runtime,
        "PACKVM_LIMA_SCRUB_MAX_TOTAL_BYTES",
        len(first_payload) * 2 + 128,
    )

    with pytest.raises(ValueError, match="byte bound"):
        manager._scrub_lima_handoff_artifacts(endpoint, (endpoint, "c" * 64))

    assert first.read_bytes() == first_payload
    assert second.read_bytes() == second_payload
    assert (first.stat().st_dev, first.stat().st_ino) == identity
    assert not list(fake.instance_dir.glob(".packvm-scrub-*"))


def test_handoff_scrub_rejects_hardlink_added_during_replace(
    provisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    manager, fake, _command = provisioner
    endpoint = "http://127.0.0.1:32123/packvm-image/" + "d" * 64
    source = fake.instance_dir / "late.log"
    alias = fake.instance_dir / "late-alias.log"
    source.write_text(endpoint, encoding="utf-8")
    original_replace = lima_runtime.os.replace

    def add_alias_then_replace(
        source_name: str,
        destination_name: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        os.link(
            destination_name,
            alias.name,
            src_dir_fd=dst_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        original_replace(
            source_name,
            destination_name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(lima_runtime.os, "replace", add_alias_then_replace)
    with pytest.raises(ValueError, match="alias survived"):
        manager._scrub_lima_handoff_artifacts(endpoint, (endpoint, "d" * 64))

    assert alias.read_text(encoding="utf-8") == endpoint


def test_unredactable_lima_log_fails_and_cleans_created_instance(provisioner) -> None:
    manager, fake, _command = provisioner
    fake.persist_start_config = True
    fake.remove_files_on_delete = True
    fake.start_log_payload = b"\x00token="

    def add_endpoint(config: bytes, _fds: tuple[int, ...]) -> None:
        location = str(yaml.safe_load(config)["images"][0]["location"])
        fake.start_log_payload = b"\x00token=" + location.encode()

    fake.before_start = add_endpoint
    with pytest.raises(ValueError, match="binary metadata"):
        manager.provision(_request(manager.prepare()))

    assert fake.exists is False
    assert not manager.state_path.exists()
    assert not manager.recovery_path.exists()
    assert list(fake.instance_dir.iterdir()) == []


def test_cancellation_after_final_seal_prevents_start(
    provisioner, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, fake, _command = provisioner
    cancel_requested = False
    original_seal = manager._seal_staged_image

    def seal_then_cancel(staged) -> None:
        nonlocal cancel_requested
        original_seal(staged)
        cancel_requested = True

    monkeypatch.setattr(manager, "_seal_staged_image", seal_then_cancel)
    with pytest.raises(PackVMImageCancelled):
        manager.provision(_request(manager.prepare()), cancelled=lambda: cancel_requested)

    assert fake.exists is False
    assert not manager.recovery_path.exists()
    assert not any(command[1:3] == ("start", "--name") for command in fake.commands)


@pytest.mark.skipif(platform.system() != "Darwin", reason="Lima VZ is macOS-only")
def test_installed_lima_220_never_persists_handoff_in_dedicated_home_cache() -> None:
    """Exercise production HOME/cache isolation with installed Lima and no VM."""

    limactl = shutil.which("limactl")
    if limactl is None:
        pytest.skip("installed Lima compatibility probe requires limactl")
    version = subprocess.run((limactl, "--version"), capture_output=True, text=True, timeout=5)
    if version.returncode != 0 or "version 2.2.0" not in version.stdout:
        pytest.skip("compatibility probe is pinned to installed Lima 2.2.0")

    root = Path(tempfile.mkdtemp(prefix="pv", dir="/tmp")).resolve()
    try:
        lima_home = root / "l"
        lima_home.mkdir(mode=0o700)
        state = root / "s"
        probe = PackVMLimaProvisioner(
            command_path=limactl,
            state_dir=state,
            lima_home=lima_home,
            machine="arm64",
        )
        image = root / "i"
        content = b"packvm-local-image-probe\n"
        image.write_bytes(content)
        descriptor = os.open(image, os.O_RDONLY)
        image.unlink()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        operation_root: Path | None = None
        try:
            with probe._lima_handoff_operation_environment() as operation_root:
                environment = probe._lima_process_environment()
                assert Path(environment["HOME"]).is_relative_to(operation_root)
                assert Path(environment["XDG_CACHE_HOME"]).is_relative_to(operation_root)
                with PackVMLoopbackImageHandoff(
                    descriptor, size_bytes=len(content), digest=digest
                ) as handoff:
                    endpoint = handoff.url
                    sensitive_values = handoff.sensitive_values
                    config = {
                        "vmType": "vz",
                        "arch": "aarch64",
                        "cpus": 1,
                        "memory": "1GiB",
                        "disk": "1GiB",
                        "plain": True,
                        "containerd": {"system": False, "user": False},
                        "images": [{"location": handoff.url, "digest": digest}],
                        "mounts": [],
                        "networks": [],
                    }
                    result = subprocess.run(
                        (limactl, "create", "--name", PACKVM_LIMA_INSTANCE, "-"),
                        input=yaml.safe_dump(config),
                        capture_output=True,
                        text=True,
                        timeout=20,
                        env=environment,
                    )
                    handoff.require_consumed()
                    probe._scrub_lima_handoff_artifacts(endpoint, sensitive_values)
        finally:
            os.close(descriptor)
        assert result.returncode == 0, result.stderr
        assert "Downloaded the image from `http://127.0.0.1:" in result.stderr
        assert "https://" not in result.stderr
        diagnostic = _safe_process_diagnostic(result.stderr, sensitive_values)
        assert diagnostic is not None
        assert endpoint not in diagnostic
        assert sensitive_values[1] not in diagnostic
        for private_root in (lima_home, state):
            for candidate in private_root.rglob("*"):
                if candidate.is_file() and candidate.stat().st_size < 1024 * 1024:
                    payload = candidate.read_bytes()
                    assert endpoint.encode() not in payload
                    assert sensitive_values[1].encode() not in payload
        assert operation_root is not None
        assert list(operation_root.iterdir()) == []
        assert (lima_home / PACKVM_LIMA_INSTANCE / "disk").exists()
        assert not (lima_home / PACKVM_LIMA_INSTANCE / "disk").stat().st_flags & stat.UF_IMMUTABLE
        assert not (lima_home / PACKVM_LIMA_INSTANCE / "ha.sock").exists()
    finally:
        shutil.rmtree(root)


def test_doctor_rejects_a_changed_host_build(
    provisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    manager, _fake, _command = provisioner
    plan = manager.prepare()
    assert manager.provision(_request(plan)).ready
    original_digest = lima_runtime._file_digest

    def changed_host_digest(path: Path) -> str:
        if path == Path(lima_runtime.__file__):
            return "sha256:" + "f" * 64
        return original_digest(path)

    monkeypatch.setattr(lima_runtime, "_file_digest", changed_host_digest)
    health = manager.doctor()
    assert health.ready is False
    assert "Host build changed" in str(health.reason)


def test_typed_consent_is_one_shot_and_attestation_survives_restart(provisioner) -> None:
    manager, fake, command = provisioner
    lifecycle = PackVMLifecycleV4(manager)
    plan = lifecycle.prepare()
    consent_payload = {
        "plan_digest": plan["plan_digest"],
        "ceremony_nonce": plan["ceremony_nonce"],
        "confirmation": plan["confirmation"],
        "approve_image_download": True,
    }
    consent = lifecycle.consent(consent_payload)
    with pytest.raises(ValueError, match="pending plan"):
        lifecycle.consent(consent_payload)
    operation_id = str(uuid.uuid4())
    started = lifecycle.provision(
        {"consent_id": consent["consent_id"], "operation_id": operation_id}
    )
    assert started["state"] in {"queued", "running"}
    for _ in range(100):
        progress = lifecycle.progress(operation_id)
        if progress["state"] == "succeeded":
            break
        time.sleep(0.01)
    assert progress["doctor"]["ready"] is True
    assert (
        lifecycle.provision({"consent_id": consent["consent_id"], "operation_id": operation_id})[
            "state"
        ]
        == "succeeded"
    )

    restarted = PackVMLimaProvisioner(
        command_path=str(command),
        runner=fake,
        state_dir=manager.state_path.parent,
        machine="arm64",
        image_cache=manager.image_cache,
        lima_home=manager.lima_home,
    )
    assert restarted.doctor().ready is True
    assert restarted.doctor().instance == PACKVM_LIMA_INSTANCE
    assert all("rumi-managed-runtime" not in command for command in fake.commands)
    restarted_lifecycle = PackVMLifecycleV4(restarted)
    assert restarted_lifecycle.progress(operation_id)["state"] == "succeeded"
    operations_path = manager.state_path.parent / "packvm-operations.json"
    operation_state = json.loads(operations_path.read_text(encoding="utf-8"))
    operation_state["operations"][operation_id]["state"] = "failed"
    operations_path.write_text(json.dumps(operation_state), encoding="utf-8")
    operations_path.chmod(0o600)
    with pytest.raises(ValueError, match="authentication failed"):
        PackVMLifecycleV4(restarted)


def test_active_image_download_cancellation_is_terminal_cancelled(
    provisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _fake, _command = provisioner
    lifecycle = PackVMLifecycleV4(manager)
    plan = lifecycle.prepare(session_id="panel-session-a")
    consent = lifecycle.consent(
        {
            "plan_digest": plan["plan_digest"],
            "ceremony_nonce": plan["ceremony_nonce"],
            "confirmation": plan["confirmation"],
            "approve_image_download": True,
        },
        session_id="panel-session-a",
    )
    entered = threading.Event()

    def downloading(_request, *, progress, cancelled):
        entered.set()
        while not cancelled():
            time.sleep(0.001)
        raise PackVMImageCancelled("packvm_image_cancelled", "PackVM image download was cancelled")

    monkeypatch.setattr(manager, "provision", downloading)
    operation_id = str(uuid.uuid4())
    lifecycle.provision(
        {"consent_id": consent["consent_id"], "operation_id": operation_id},
        session_id="panel-session-a",
    )
    assert entered.wait(2)
    lifecycle.cancel({"operation_id": operation_id}, session_id="panel-session-a")
    result = _wait_operation(lifecycle, operation_id, session_id="panel-session-a")
    assert result["state"] == "cancelled"
    assert result["stage"] == "image_prefetch"
    assert "error" not in result


def test_cache_hit_progress_fences_cancellation_before_lima_mutation(
    provisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _fake, _command = provisioner
    lifecycle = PackVMLifecycleV4(manager)
    plan = lifecycle.prepare(session_id="panel-session-a")
    consent = lifecycle.consent(
        {
            "plan_digest": plan["plan_digest"],
            "ceremony_nonce": plan["ceremony_nonce"],
            "confirmation": plan["confirmation"],
            "approve_image_download": True,
        },
        session_id="panel-session-a",
    )
    entered = threading.Event()
    release = threading.Event()

    def cache_hit(_request, *, progress, cancelled):
        assert cancelled() is False
        progress(PackVMImageProgress("verified", 17, 17, 17))
        entered.set()
        assert release.wait(2)
        return manager.doctor()

    monkeypatch.setattr(manager, "provision", cache_hit)
    operation_id = str(uuid.uuid4())
    lifecycle.provision(
        {"consent_id": consent["consent_id"], "operation_id": operation_id},
        session_id="panel-session-a",
    )
    assert entered.wait(2)
    try:
        with pytest.raises(ValueError, match="cannot be cancelled after it starts"):
            lifecycle.cancel({"operation_id": operation_id}, session_id="panel-session-a")
    finally:
        release.set()
    result = _wait_operation(lifecycle, operation_id, session_id="panel-session-a")
    assert result["state"] == "succeeded"
    assert result["stage"] == "provisioning"


def test_cancel_accepted_before_verified_barrier_is_terminal_cancelled(
    provisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, fake, _command = provisioner
    lifecycle = PackVMLifecycleV4(manager)
    plan = lifecycle.prepare(session_id="panel-session-a")
    consent = lifecycle.consent(
        {
            "plan_digest": plan["plan_digest"],
            "ceremony_nonce": plan["ceremony_nonce"],
            "confirmation": plan["confirmation"],
            "approve_image_download": True,
        },
        session_id="panel-session-a",
    )
    verification_blocked = threading.Event()
    release_verification = threading.Event()

    def blocked_verification(_request, *, progress, cancelled):
        verification_blocked.set()
        assert release_verification.wait(2)
        progress(PackVMImageProgress("verified", 17, 17, 17))
        raise AssertionError("verified cancellation barrier unexpectedly returned")

    monkeypatch.setattr(manager, "provision", blocked_verification)
    operation_id = str(uuid.uuid4())
    lifecycle.provision(
        {"consent_id": consent["consent_id"], "operation_id": operation_id},
        session_id="panel-session-a",
    )
    assert verification_blocked.wait(2)
    lifecycle.cancel({"operation_id": operation_id}, session_id="panel-session-a")
    release_verification.set()

    result = _wait_operation(lifecycle, operation_id, session_id="panel-session-a")
    assert result["state"] == "cancelled"
    assert result["stage"] == "image_prefetch"
    assert fake.exists is False


def test_restart_recovers_only_exact_session_plan_and_recovery_proof(
    provisioner,
) -> None:
    manager, _fake, _command = provisioner
    lifecycle = PackVMLifecycleV4(manager)
    session_id = "panel-session-a"
    plan = lifecycle.prepare(session_id=session_id)
    consent = lifecycle.consent(
        {
            "plan_digest": plan["plan_digest"],
            "ceremony_nonce": plan["ceremony_nonce"],
            "confirmation": plan["confirmation"],
            "approve_image_download": True,
        },
        session_id=session_id,
    )
    operation_id = str(uuid.uuid4())
    lifecycle.provision(
        {"consent_id": consent["consent_id"], "operation_id": operation_id},
        session_id=session_id,
    )
    assert _wait_operation(lifecycle, operation_id, session_id=session_id)["state"] == ("succeeded")

    operations_path = manager.state_path.parent / "packvm-operations.json"
    payload = json.loads(operations_path.read_text(encoding="utf-8"))
    exact = payload["operations"][operation_id]
    exact["state"] = "running"
    different_plan_id = str(uuid.uuid4())
    different_plan = json.loads(json.dumps(exact))
    different_plan.update(
        {
            "operation_id": different_plan_id,
            "state": "queued",
            "consent_digest": "sha256:" + hashlib.sha256(b"different").hexdigest(),
            "plan_digest": "sha256:" + "d" * 64,
        }
    )
    different_plan["recovery_proof"]["plan_digest"] = "sha256:" + "d" * 64
    tampered_proof_id = str(uuid.uuid4())
    tampered_proof = json.loads(json.dumps(exact))
    tampered_proof.update(
        {
            "operation_id": tampered_proof_id,
            "state": "running",
            "consent_digest": "sha256:" + hashlib.sha256(b"tampered").hexdigest(),
        }
    )
    tampered_proof["recovery_proof"]["guest_runner_digest"] = "sha256:" + "e" * 64
    payload["operations"][different_plan_id] = different_plan
    payload["operations"][tampered_proof_id] = tampered_proof
    _resign_operations(manager, payload)

    restarted = PackVMLifecycleV4(manager)
    recovered = restarted.progress(operation_id, session_id=session_id)
    assert recovered["state"] == "succeeded"
    for mismatched_id in (different_plan_id, tampered_proof_id):
        interrupted = restarted.progress(mismatched_id, session_id=session_id)
        assert interrupted["state"] == "interrupted"
        assert interrupted["error_type"] == "PackVMReconciliationRequired"
        assert "reconciliation is required" in str(interrupted["error"])
    assert (
        restarted.provision(
            {"consent_id": consent["consent_id"], "operation_id": operation_id},
            session_id=session_id,
        )["state"]
        == "succeeded"
    )
    with pytest.raises(ValueError, match="another consent"):
        restarted.provision(
            {"consent_id": "foreign-consent", "operation_id": operation_id},
            session_id=session_id,
        )


def test_operation_journal_compacts_with_authenticated_replay_and_dependencies(
    provisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_runtime import packvm_lifecycle_v4

    class InertThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(
        packvm_lifecycle_v4,
        "threading",
        SimpleNamespace(Thread=InertThread, RLock=threading.RLock),
    )
    manager, _fake, _command = provisioner
    lifecycle = PackVMLifecycleV4(manager)
    session_id = "panel-session-a"
    session_digest = "sha256:" + hashlib.sha256(session_id.encode()).hexdigest()
    operation_ids: list[str] = []
    consent_ids: dict[str, str] = {}
    for _index in range(140):
        plan = lifecycle.prepare(session_id=session_id)
        consent = lifecycle.consent(
            {
                "plan_digest": plan["plan_digest"],
                "ceremony_nonce": plan["ceremony_nonce"],
                "confirmation": plan["confirmation"],
                "approve_image_download": True,
            },
            session_id=session_id,
        )
        operation_id = str(uuid.uuid4())
        operation_ids.append(operation_id)
        consent_ids[operation_id] = str(consent["consent_id"])
        lifecycle.provision(
            {"consent_id": consent["consent_id"], "operation_id": operation_id},
            session_id=session_id,
        )
        cancelled = lifecycle.cancel({"operation_id": operation_id}, session_id=session_id)
        assert cancelled["state"] == "cancelled"
    source_id = str(uuid.uuid4())
    cleanup_id = str(uuid.uuid4())
    lifecycle._operations[source_id] = {
        "operation_id": source_id,
        "operation_kind": "provision",
        "session_digest": session_digest,
        "state": "failed",
        "plan_digest": "sha256:" + "a" * 64,
        "recovery_proof": {"retained": True},
        "cleanup_operation_id": cleanup_id,
        "updated_unix": 200,
    }
    lifecycle._operations[cleanup_id] = {
        "operation_id": cleanup_id,
        "operation_kind": "cleanup",
        "session_digest": session_digest,
        "source_operation_id": source_id,
        "state": "running",
        "plan_digest": "sha256:" + "a" * 64,
        "updated_unix": 201,
    }
    lifecycle._persist_operations()

    archive_path = manager.state_path.parent / "packvm-operations-archive.jsonl"
    assert archive_path.exists()
    assert len(lifecycle._operations) < 128
    assert source_id in lifecycle._operations
    assert cleanup_id in lifecycle._operations
    assert len(operation_ids) == 140
    archived_id = next(iter(lifecycle._archived_operations))

    restarted = PackVMLifecycleV4(manager)
    assert restarted.progress(archived_id, session_id=session_id)["state"] == "cancelled"
    assert restarted.progress(source_id, session_id=session_id)["state"] == "failed"
    assert restarted.progress(cleanup_id, session_id=session_id)["state"] == "interrupted"
    with pytest.raises(ValueError, match="another authenticated session"):
        restarted.progress(archived_id, session_id="foreign-session")
    replay = restarted.provision(
        {"consent_id": consent_ids[archived_id], "operation_id": archived_id},
        session_id=session_id,
    )
    assert replay["state"] == "cancelled"

    encoded = archive_path.read_bytes()
    archive_path.write_bytes(encoded.replace(b'"state":"cancelled"', b'"state":"tampered"', 1))
    archive_path.chmod(0o600)
    with pytest.raises(ValueError, match="authentication failed|digest failed"):
        PackVMLifecycleV4(manager)


def test_operation_journal_serializes_two_instances_at_publication(
    provisioner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_runtime import packvm_lifecycle_v4

    real_thread = threading.Thread

    class InertThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(
        packvm_lifecycle_v4,
        "threading",
        SimpleNamespace(Thread=InertThread, RLock=threading.RLock),
    )
    manager, _fake, _command = provisioner
    first = PackVMLifecycleV4(manager)
    second = PackVMLifecycleV4(manager)
    session_id = "panel-session-a"

    def consent_for(lifecycle: PackVMLifecycleV4) -> Mapping[str, Any]:
        plan = lifecycle.prepare(session_id=session_id)
        return lifecycle.consent(
            {
                "plan_digest": plan["plan_digest"],
                "ceremony_nonce": plan["ceremony_nonce"],
                "confirmation": plan["confirmation"],
                "approve_image_download": True,
            },
            session_id=session_id,
        )

    first_consent = consent_for(first)
    second_consent = consent_for(second)
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    first_at_replace = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    original_replace = os.replace
    replace_count = 0
    count_lock = threading.Lock()
    operations_path = manager.state_path.parent / "packvm-operations.json"

    def synchronized_replace(source, destination) -> None:
        nonlocal replace_count
        if Path(destination) == operations_path:
            with count_lock:
                replace_count += 1
                current = replace_count
            if current == 1:
                first_at_replace.set()
                assert release_first.wait(2)
        original_replace(source, destination)

    monkeypatch.setattr(packvm_lifecycle_v4.os, "replace", synchronized_replace)
    failures: list[BaseException] = []

    def provision_first() -> None:
        try:
            first.provision(
                {"consent_id": first_consent["consent_id"], "operation_id": first_id},
                session_id=session_id,
            )
        except BaseException as error:
            failures.append(error)

    def provision_second() -> None:
        try:
            second.provision(
                {
                    "consent_id": second_consent["consent_id"],
                    "operation_id": second_id,
                },
                session_id=session_id,
            )
        except BaseException as error:
            failures.append(error)
        finally:
            second_finished.set()

    first_worker = real_thread(target=provision_first)
    second_worker = real_thread(target=provision_second)
    first_worker.start()
    assert first_at_replace.wait(2)
    second_worker.start()
    assert second_finished.wait(0.05) is False
    release_first.set()
    first_worker.join(2)
    second_worker.join(2)
    assert failures == []
    assert second_finished.is_set()

    first.cancel({"operation_id": first_id}, session_id=session_id)
    second.cancel({"operation_id": second_id}, session_id=session_id)
    restarted = PackVMLifecycleV4(manager)
    assert restarted.progress(first_id, session_id=session_id)["state"] == "cancelled"
    assert restarted.progress(second_id, session_id=session_id)["state"] == "cancelled"


def test_operation_archive_enforces_exact_byte_and_record_bounds(provisioner) -> None:
    manager, _fake, _command = provisioner
    lifecycle = PackVMLifecycleV4(
        manager,
        archive_max_bytes=1024 * 1024,
        archive_max_records=1,
    )
    operation_id = str(uuid.uuid4())
    record = {
        "operation_id": operation_id,
        "operation_kind": "provision",
        "session_digest": "sha256:" + hashlib.sha256(b"panel-session-a").hexdigest(),
        "state": "cancelled",
        "plan_digest": "sha256:" + "a" * 64,
        "updated_unix": 1,
    }
    with lifecycle._journal_transaction():
        lifecycle._operations[operation_id] = record
        lifecycle._append_operations_archive([(operation_id, record)])
        lifecycle._persist_operations()

    archive_path = manager.state_path.parent / "packvm-operations-archive.jsonl"
    exact_size = archive_path.stat().st_size
    at_limit = PackVMLifecycleV4(
        manager,
        archive_max_bytes=exact_size,
        archive_max_records=1,
    )
    assert at_limit.progress(operation_id, session_id="panel-session-a")["state"] == ("cancelled")
    with pytest.raises(ValueError, match="byte limit exceeded"):
        PackVMLifecycleV4(
            manager,
            archive_max_bytes=exact_size - 1,
            archive_max_records=1,
        )

    encoded = archive_path.read_bytes()
    second_id = str(uuid.uuid4())
    second_record = {**record, "operation_id": second_id, "updated_unix": 2}
    with at_limit._journal_transaction():
        with pytest.raises(ValueError, match="record limit exceeded"):
            at_limit._append_operations_archive([(second_id, second_record)])
    assert archive_path.read_bytes() == encoded


def test_operation_state_publication_detects_authenticated_cas_conflict(
    provisioner,
) -> None:
    manager, _fake, _command = provisioner
    lifecycle = PackVMLifecycleV4(manager)
    with lifecycle._journal_transaction():
        lifecycle._persist_operations()
    with lifecycle._journal_transaction():
        payload = json.loads(lifecycle._operations_path.read_text(encoding="utf-8"))
        payload["generation"] += 1
        _resign_operations(manager, payload)
        with pytest.raises(ValueError, match="changed during transaction"):
            lifecycle._persist_operations()


def test_operation_journal_lock_blocks_an_overlapping_process(tmp_path: Path) -> None:
    from core_runtime.packvm_lifecycle_v4 import (
        _acquire_journal_lock,
        _open_journal_lock,
        _release_journal_lock,
    )

    lock_path = tmp_path / "packvm-operations.lock"
    child_script = """
import os
import sys
from pathlib import Path
from core_runtime.packvm_lifecycle_v4 import (
    _acquire_journal_lock,
    _open_journal_lock,
    _release_journal_lock,
)
descriptor = _open_journal_lock(Path(sys.argv[1]))
_acquire_journal_lock(descriptor)
print("locked", flush=True)
sys.stdin.readline()
_release_journal_lock(descriptor)
os.close(descriptor)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_script, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdin is not None
    assert child.stdout.readline().strip() == "locked"
    acquired = threading.Event()

    def acquire_in_parent() -> None:
        descriptor = _open_journal_lock(lock_path)
        try:
            _acquire_journal_lock(descriptor)
            acquired.set()
            _release_journal_lock(descriptor)
        finally:
            os.close(descriptor)

    waiter = threading.Thread(target=acquire_in_parent)
    waiter.start()
    assert acquired.wait(0.05) is False
    child.stdin.write("release\n")
    child.stdin.flush()
    assert acquired.wait(2)
    waiter.join(2)
    assert child.wait(timeout=2) == 0


def test_windows_operation_lock_uses_pinned_identity_and_owner_acl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core_runtime.packvm_lifecycle_v4 as lifecycle_module

    lock_path = tmp_path / "packvm-operations.lock"
    secured: list[Path] = []
    validated: list[tuple[str, int]] = []

    class FakeSecureDirectory:
        def __init__(self, path: Path, *, create: bool) -> None:
            assert path == tmp_path
            assert create is True

        def open_lock(self, name: str) -> int:
            assert name == lock_path.name
            return os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)

        def validate_open_file(self, name: str, descriptor: int) -> None:
            validated.append((name, descriptor))

    monkeypatch.setattr(lifecycle_module, "SecureDirectory", FakeSecureDirectory)
    monkeypatch.setattr(
        lifecycle_module,
        "_secure_windows_journal_lock_acl",
        secured.append,
    )
    lifecycle_module._WINDOWS_VERIFIED_LOCKS.clear()

    first = lifecycle_module._open_windows_journal_lock(lock_path)
    os.close(first)
    second = lifecycle_module._open_windows_journal_lock(lock_path)
    os.close(second)

    assert secured == [lock_path]
    assert len(validated) == 1
    assert lock_path.read_bytes() == b"\0"


def test_windows_operation_lock_rejects_hardlink_before_acl_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core_runtime.packvm_lifecycle_v4 as lifecycle_module

    victim = tmp_path / "victim"
    victim.write_bytes(b"victim")
    lock_path = tmp_path / "packvm-operations.lock"
    os.link(victim, lock_path)

    class HardlinkedSecureDirectory:
        def __init__(self, _path: Path, *, create: bool) -> None:
            assert create is True

        def open_lock(self, _name: str) -> int:
            return os.open(lock_path, os.O_RDWR)

    monkeypatch.setattr(
        lifecycle_module,
        "SecureDirectory",
        HardlinkedSecureDirectory,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "_secure_windows_journal_lock_acl",
        lambda _path: pytest.fail("unsafe lock ACL was mutated"),
    )

    with pytest.raises(ValueError, match="operation lock is unsafe"):
        lifecycle_module._open_windows_journal_lock(lock_path)

    assert victim.read_bytes() == b"victim"


def test_guest_runner_executes_only_the_explicit_staged_python_abi(tmp_path: Path) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import lima_runtime

    implementation = tmp_path / "operation.py"
    implementation.write_text(
        "def tobkiri_packvm_invoke(operation_id, payload):\n"
        "    return {'operation_id': operation_id, 'value': payload['value']}\n",
        encoding="utf-8",
    )
    request = json.dumps(
        {
            "contract_id": "example.contract.v1",
            "operation_id": "example-pack.inspect",
            "payload": {"value": 7},
        }
    )
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-S",
            str(lima_runtime._PACKVM_RUNNER),
            "--execute",
            str(implementation),
        ),
        input=request,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "operation_id": "example-pack.inspect",
        "value": 7,
    }

    implementation.write_text("RESULT = {}\n", encoding="utf-8")
    denied = subprocess.run(
        (
            sys.executable,
            "-I",
            "-S",
            str(lima_runtime._PACKVM_RUNNER),
            "--execute",
            str(implementation),
        ),
        input=request,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert denied.returncode == 1
    assert "does not export tobkiri_packvm_invoke" in denied.stderr
