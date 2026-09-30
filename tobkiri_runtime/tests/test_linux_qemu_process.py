"""No-download process/transport conformance tests, not real-guest boot evidence."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tobkiri_host import linux_qemu_process as qemu
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.linux_qemu_process import LinuxQemuLaunchConfig, LinuxQemuProcess


class FakeProcess:
    """Only process ownership is fake; serial tests use real local Unix I/O."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.returncode: int | None = None
        self.terminate_count = 0
        self.kill_count = 0
        self.ignore_terminate = False
        self.ignore_kill = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_count += 1
        if not self.ignore_terminate:
            self.returncode = -15

    def kill(self) -> None:
        self.kill_count += 1
        if not self.ignore_kill:
            self.returncode = -9

    def wait(self, timeout: float) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-qemu", timeout)
        return self.returncode


@pytest.fixture
def config(tmp_path: Path) -> LinuxQemuLaunchConfig:
    tmp_path.chmod(0o700)
    executable = tmp_path / "qemu"
    executable.write_bytes(b"trusted-qemu-placeholder")
    executable.chmod(0o700)
    paths = {}
    for name in ("disk", "agent", "config", "code", "vars"):
        path = tmp_path / name
        path.write_bytes((name + "-bytes").encode())
        path.chmod(0o600)
        paths[name] = path
    return LinuxQemuLaunchConfig(
        qemu_path=executable,
        qemu_digest="sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest(),
        run_root=tmp_path,
        private_disk_path=paths["disk"],
        agent_image_path=paths["agent"],
        seed_image_path=paths["config"],
        firmware_code_path=paths["code"],
        private_firmware_vars_path=paths["vars"],
        firmware_code_digest="sha256:" + hashlib.sha256(paths["code"].read_bytes()).hexdigest(),
        private_disk_digest="sha256:" + hashlib.sha256(paths["disk"].read_bytes()).hexdigest(),
        agent_image_digest="sha256:" + hashlib.sha256(paths["agent"].read_bytes()).hexdigest(),
        seed_image_digest="sha256:" + hashlib.sha256(paths["config"].read_bytes()).hexdigest(),
        private_firmware_vars_digest="sha256:"
        + hashlib.sha256(paths["vars"].read_bytes()).hexdigest(),
        accelerator="tcg",
        allow_insecure_tcg=True,
    )


@pytest.fixture
def launched(
    config: LinuxQemuLaunchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    process = FakeProcess()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def spawn(command: list[str], **kwargs: Any) -> FakeProcess:
        # The caller still holds all pinned FDs through Popen.
        for descriptor in kwargs["pass_fds"]:
            assert os.fstat(descriptor).st_size > 0
        calls.append((command, kwargs))
        return process

    monkeypatch.setattr(qemu.subprocess, "Popen", spawn)
    vm = LinuxQemuProcess(config)
    vm.start()
    yield vm, process, calls
    process.ignore_kill = False
    process.ignore_terminate = False
    if vm.socket_path.exists() and not vm.socket_path.is_socket():
        vm.socket_path.unlink()
    vm.stop()


def _listener(vm: LinuxQemuProcess) -> socket.socket:
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("Execution environment denies real Unix sockets; not boot evidence")
    listener.bind(str(vm.socket_path))
    vm.socket_path.chmod(0o600)
    listener.listen(1)
    listener.settimeout(2)
    return listener


def _request(number: int, operation: str = "invoke") -> dict[str, Any]:
    return {"guest_challenge": f"{number:064x}", "operation": operation}


def _frame(request: dict[str, Any]) -> bytes:
    return json.dumps(request, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def test_command_has_only_private_disks_serial_and_no_network(launched: Any) -> None:
    vm, _, calls = launched
    command, options = calls[0]
    arguments = command[10:]
    assert command[:4] == [qemu.sys.executable, "-I", "-S", "-c"]
    assert "libc.prctl(1, signal.SIGKILL" in command[4]
    assert "libc.prctl(38, 1" in command[4]
    assert arguments[0] == str(vm.config.qemu_path)
    assert arguments[arguments.index("-accel") + 1] == "tcg"
    assert arguments[arguments.index("-nic") + 1] == "none"
    assert arguments[arguments.index("-display") + 1] == "none"
    assert arguments[arguments.index("-monitor") + 1] == "none"
    assert "-nodefaults" in arguments and "-no-user-config" in arguments
    assert not any(
        value in arguments
        for value in (
            "-qmp",
            "-netdev",
            "-virtfs",
            "-fsdev",
            "-kernel",
            "-bios",
            "-L",
        )
    )
    drives = [arguments[i + 1] for i, arg in enumerate(arguments) if arg == "-drive"]
    assert len(drives) == 5
    assert sum("readonly=on" in drive for drive in drives) == 3
    assert all("file=/proc/self/fd/" in drive and "format=raw" in drive for drive in drives)
    assert "if=pflash,unit=0,readonly=on" in drives[0]
    assert "if=pflash,unit=1" in drives[1]
    assert "io.tobkiri.packvm.agent" in " ".join(arguments)
    assert options["umask"] == 0o077
    assert options["close_fds"] and options["start_new_session"]
    assert options["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert len(options["pass_fds"]) == 6
    assert vm.pid == os.getpid() and vm.alive()


@pytest.mark.parametrize(
    "updates",
    [
        {"accelerator": "tcg", "allow_insecure_tcg": False},
        {"accelerator": "kvm:tcg"},
        {"memory_mib": 32769},
        {"memory_mib": True},
        {"vcpus": 0},
        {"vcpus": 17},
        {"machine_type": "q35,accel=tcg"},
        {"serial_port_name": "other"},
        {"max_response_bytes": 16 * 1024 * 1024 + 1},
        {"qemu_digest": "sha256:bad"},
        {"qemu_path": Path("relative")},
        {"private_disk_path": Path("/tmp/disk,file=other")},
    ],
)
def test_rejects_unbounded_or_injectable_configuration(
    config: LinuxQemuLaunchConfig,
    updates: dict[str, Any],
) -> None:
    with pytest.raises(BackendUnavailableError):
        replace(config, **updates)


@pytest.mark.parametrize(
    "change",
    ["root", "digest", "disk_mode", "directory_mode", "symlink", "hardlink", "socket", "outside"],
)
def test_prelaunch_security_checks_fail_before_spawn(
    config: LinuxQemuLaunchConfig,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("spawn should not run")

    monkeypatch.setattr(qemu.subprocess, "Popen", forbidden)
    if change == "root":
        monkeypatch.setattr(qemu.os, "geteuid", lambda: 0)
    elif change == "digest":
        config = replace(config, qemu_digest="sha256:" + "0" * 64)
    elif change == "disk_mode":
        config.private_disk_path.chmod(0o644)
    elif change == "directory_mode":
        config.run_root.chmod(0o755)
    elif change == "symlink":
        config.private_disk_path.unlink()
        config.private_disk_path.symlink_to(config.seed_image_path)
    elif change == "hardlink":
        os.link(config.private_disk_path, config.run_root / "linked")
    elif change == "socket":
        (config.run_root / "agent.sock").write_bytes(b"unexpected")
    elif change == "outside":
        config = replace(config, private_disk_path=config.run_root.parent / "outside")
    with pytest.raises(BackendUnavailableError):
        LinuxQemuProcess(config).start()


def test_kvm_failure_does_not_fallback_to_tcg(
    config: LinuxQemuLaunchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = []

    def unavailable() -> None:
        checked.append(True)
        raise PermissionError("KVM unavailable")

    monkeypatch.setattr(qemu, "_check_kvm", unavailable)
    with pytest.raises(BackendUnavailableError, match="launch failed"):
        LinuxQemuProcess(replace(config, accelerator="kvm")).start()
    assert checked == [True]


def test_out_of_order_responses_correlate_concurrent_cancel(launched: Any) -> None:
    vm, _, _ = launched
    listener = _listener(vm)
    with listener, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(vm.exchange, _request(1), 2)
        connection, _ = listener.accept()
        with connection:
            reader = connection.makefile("rb")
            invoke = json.loads(reader.readline())
            second = pool.submit(vm.exchange, _request(2, "cancel"), 2)
            cancel = json.loads(reader.readline())
            connection.sendall(_frame(cancel) + _frame(invoke))
            assert second.result(2) == cancel
            assert first.result(2) == invoke


@pytest.mark.parametrize(
    "payload",
    [
        b'{"guest_challenge":"' + b"1" * 64 + b'"}\n',
        b'{ "guest_challenge":"' + b"0" * 63 + b'1"}\n',
        b'{"a":1,"a":2}\n',
        b"[]\n",
        b"NaN\n",
        b"\xff\n",
        b"{}\n",
    ],
)
def test_bad_response_retires_stream(launched: Any, payload: bytes) -> None:
    vm, _, _ = launched
    listener = _listener(vm)
    with listener, ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(vm.exchange, _request(1), 2)
        connection, _ = listener.accept()
        with connection:
            connection.recv(4096)
            connection.sendall(payload)
            with pytest.raises(BackendUnavailableError):
                response.result(2)
            with pytest.raises(BackendUnavailableError, match="retired"):
                vm.exchange(_request(2), 1)


def test_response_byte_limit_is_incremental(launched: Any) -> None:
    vm, _, _ = launched
    vm.config = replace(vm.config, max_response_bytes=80)
    listener = _listener(vm)
    with listener, ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(vm.exchange, _request(1), 2)
        connection, _ = listener.accept()
        with connection:
            connection.recv(4096)
            connection.sendall(b"x" * 81)
            with pytest.raises(BackendUnavailableError):
                response.result(2)


def test_timeout_retires_stream_and_stop_keeps_allocator_files(launched: Any) -> None:
    vm, process, _ = launched
    listener = _listener(vm)
    with listener, ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(vm.exchange, _request(1), 0.15)
        connection, _ = listener.accept()
        with connection:
            connection.recv(4096)
            with pytest.raises(TimeoutError):
                response.result(2)
            with pytest.raises(BackendUnavailableError):
                vm.exchange(_request(2), 1)
    vm.stop()
    assert process.terminate_count == 1
    assert not vm.alive() and not vm.socket_path.exists()
    assert vm.config.private_disk_path.exists()
    assert vm.config.seed_image_path.exists()
    with pytest.raises(BackendUnavailableError, match="restarted"):
        vm.start()


def test_reserved_cancel_slot_and_fresh_challenge_enforced(launched: Any) -> None:
    vm, _, _ = launched
    listener = _listener(vm)
    with listener, ThreadPoolExecutor(max_workers=8) as pool:
        responses = [pool.submit(vm.exchange, _request(1), 2)]
        connection, _ = listener.accept()
        with connection:
            reader = connection.makefile("rb")
            requests = [json.loads(reader.readline())]
            for number in range(2, 8):
                responses.append(pool.submit(vm.exchange, _request(number), 2))
                requests.append(json.loads(reader.readline()))
            with pytest.raises(BackendUnavailableError, match="concurrency"):
                vm.exchange(_request(9), 1)
            responses.append(pool.submit(vm.exchange, _request(8, "cancel"), 2))
            requests.append(json.loads(reader.readline()))
            connection.sendall(b"".join(_frame(request) for request in requests))
            assert [response.result(2) for response in responses] == requests
            with pytest.raises(BackendUnavailableError, match="replay"):
                vm.exchange(_request(1), 1)


def test_missing_socket_wait_is_bounded(launched: Any) -> None:
    vm, _, _ = launched
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("Execution environment denies Unix sockets")
    probe.close()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        vm.wait_for_serial(0.1)
    assert time.monotonic() - started < 1


def test_stop_escalates_to_kill_and_reaps(launched: Any) -> None:
    vm, process, _ = launched
    process.ignore_terminate = True
    vm.stop()
    assert process.terminate_count == process.kill_count == 1
    assert not vm.alive()


def test_failed_reap_preserves_allocation_and_can_retry(launched: Any) -> None:
    vm, process, _ = launched
    process.ignore_terminate = process.ignore_kill = True
    with pytest.raises(BackendUnavailableError, match="reaped"):
        vm.stop(0.1)
    assert vm.alive() and vm.config.seed_image_path.exists()
    process.ignore_kill = False
    vm.stop()
    assert not vm.alive()


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_invalid_timeout_rejected(launched: Any, timeout: float) -> None:
    vm, _, _ = launched
    with pytest.raises(ValueError):
        vm.wait_for_serial(timeout)


def test_response_demux_is_out_of_order_without_socket(config: LinuxQemuLaunchConfig) -> None:
    vm = LinuxQemuProcess(config)
    first, second = qemu._PendingExchange(), qemu._PendingExchange(is_cancel=True)
    vm._pending = {f"{1:064x}": first, f"{2:064x}": second}
    vm._deliver_frame(_frame(_request(2, "cancel"))[:-1])
    assert second.event.is_set() and not first.event.is_set()
    vm._deliver_frame(_frame(_request(1))[:-1])
    assert first.response == _request(1)
    with pytest.raises(BackendUnavailableError, match="correlation"):
        vm._deliver_frame(_frame(_request(1))[:-1])


@pytest.mark.parametrize("payload", [b"{}", b"[]", b'{"a":1,"a":2}', b"NaN", b"\\xff", b'{ "a":1}'])
def test_response_parser_rejects_untrusted_frames_without_socket(
    config: LinuxQemuLaunchConfig,
    payload: bytes,
) -> None:
    vm = LinuxQemuProcess(config)
    with pytest.raises((BackendUnavailableError, ValueError)):
        vm._deliver_frame(payload)


def test_failure_wakes_all_pending_without_socket(config: LinuxQemuLaunchConfig) -> None:
    vm = LinuxQemuProcess(config)
    requests = [qemu._PendingExchange(), qemu._PendingExchange()]
    vm._pending = {str(index): request for index, request in enumerate(requests)}
    vm._fail_channel(TimeoutError("expired"))
    assert all(request.event.is_set() and request.error is not None for request in requests)


def test_fifo_asset_is_rejected_without_blocking(config: LinuxQemuLaunchConfig) -> None:
    config.private_disk_path.unlink()
    os.mkfifo(config.private_disk_path, 0o600)
    with pytest.raises(BackendUnavailableError, match="unsafe"):
        LinuxQemuProcess(config).start()
