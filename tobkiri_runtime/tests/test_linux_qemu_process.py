"""No-download process/transport conformance tests, not real-guest boot evidence."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import stat
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tobkiri_host import linux_qemu_process as qemu
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.linux_qemu_process import LinuxQemuLaunchConfig, LinuxQemuProcess
from tobkiri_protocol.packvm_serial import SERIAL_READY_FRAME


class FakeProcess:
    """Only process ownership is fake; serial tests use real local Unix I/O."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.returncode: int | None = None
        self.terminate_count = 0
        self.kill_count = 0
        self.ignore_terminate = False
        self.ignore_kill = False
        self.stderr = None

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
        metadata = [os.fstat(descriptor) for descriptor in kwargs["pass_fds"]]
        assert sum(stat.S_ISDIR(item.st_mode) for item in metadata) == 1
        assert all(
            stat.S_ISDIR(item.st_mode) or (stat.S_ISREG(item.st_mode) and item.st_size > 0)
            for item in metadata
        )
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
    try:
        listener.bind(qemu._socket_address(vm._require_run_root_fd()))
    except PermissionError:
        listener.close()
        pytest.skip("Execution environment denies real Unix socket binding; not boot evidence")
    vm.socket_path.chmod(0o600)
    listener.listen(1)
    listener.settimeout(2)
    return listener


@pytest.mark.parametrize("broken_drain", [False, True])
def test_owner_survives_stderr_eof_and_closes_pipe_on_stop(
    config: LinuxQemuLaunchConfig, monkeypatch: pytest.MonkeyPatch,
    broken_drain: bool,
) -> None:
    process = FakeProcess()
    reader, writer = os.pipe()
    process.stderr = os.fdopen(reader, "rb", buffering=0)
    os.write(writer, b"private diagnostic tail")
    os.close(writer)
    monkeypatch.setattr(qemu.subprocess, "Popen", lambda *args, **kwargs: process)
    vm = LinuxQemuProcess(config)
    drained = threading.Event()
    original = qemu.QemuProcessDiagnostics.drain

    def drain(evidence: Any, stream: Any) -> None:
        try:
            if broken_drain:
                raise RuntimeError("diagnostic drain failed")
            original(evidence, stream)
        finally:
            drained.set()

    monkeypatch.setattr(qemu.QemuProcessDiagnostics, "drain", drain)
    try:
        vm.start()
        assert drained.wait(2)
        assert vm._owner is not None and vm._owner.is_alive()
        assert process.poll() is None
        error = BackendUnavailableError("stream ended")
        vm._capture_failure_diagnostics(error)
        assert vm._diagnostics.snapshot()[1] == (
            b"" if broken_drain else b"private diagnostic tail"
        )
        assert "private diagnostic tail" not in str(error)
    finally:
        vm.stop()
    assert process.stderr.closed
    assert vm._owner is not None and not vm._owner.is_alive()
    assert vm._diagnostics.snapshot()[0] == (
        "launch", process.pid, None, "BackendUnavailableError",
    )


def test_broken_diagnostics_cannot_prevent_channel_retirement(launched, monkeypatch):
    vm, _, _ = launched

    def broken(*args, **kwargs):
        raise OSError("diagnostic failed")

    monkeypatch.setattr(qemu.QemuProcessDiagnostics, "capture", broken)
    failure = BackendUnavailableError("stream ended")
    vm._fail_channel(failure)
    assert vm._failure is failure
    assert vm._ready_event.is_set()


def test_stderr_setup_failure_keeps_child_owned_until_stop(config, monkeypatch):
    process = FakeProcess()
    reader, writer = os.pipe()
    process.stderr = os.fdopen(reader, "rb", buffering=0)
    os.close(writer)
    monkeypatch.setattr(qemu.subprocess, "Popen", lambda *args, **kwargs: process)

    def broken(*args, **kwargs):
        raise OSError("nonblocking diagnostic setup failed")

    monkeypatch.setattr(qemu.os, "set_blocking", broken)
    vm = LinuxQemuProcess(config)
    try:
        with pytest.raises(BackendUnavailableError, match="Linux QEMU launch failed"):
            vm.start()
        directory = vm._require_run_root_fd()
        assert os.fstat(directory).st_ino == config.run_root.stat().st_ino
        assert vm._owner is not None and vm._owner.is_alive()
        assert process.poll() is None
        assert process.stderr.closed
        with pytest.raises(BackendUnavailableError, match="cannot be restarted"):
            vm.start()
        assert vm._process is process
        assert vm._require_run_root_fd() == directory
    finally:
        vm.stop()
    _assert_fd_closed(directory)
    assert process.terminate_count == 1


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
    assert options["stdin"] == options["stdout"] == subprocess.DEVNULL
    assert options["stderr"] == subprocess.PIPE
    assert len(options["pass_fds"]) == 7
    assert vm._require_run_root_fd() in options["pass_fds"]
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
        connection.sendall(SERIAL_READY_FRAME + b"\n")
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
        connection.sendall(SERIAL_READY_FRAME + b"\n")
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
        connection.sendall(SERIAL_READY_FRAME + b"\n")
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
        connection.sendall(SERIAL_READY_FRAME + b"\n")
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
        connection.sendall(SERIAL_READY_FRAME + b"\n")
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
    vm._deliver_frame(SERIAL_READY_FRAME)
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


class FakeSerialSocket:
    """Record endpoint and credentials without creating a real socket."""

    def __init__(self, pid: int) -> None:
        self.peer_pid = pid
        self.peer_uid = os.geteuid()
        self.address: str | None = None
        self.directory: int | None = None
        self.closed = False
        self.pause_connect = False
        self.connect_entered = threading.Event()
        self.connect_release = threading.Event()

    def settimeout(self, timeout: float) -> None:
        assert timeout > 0

    def connect(self, address: str) -> None:
        self.address = address
        assert address.startswith("/proc/self/fd/") and address.endswith("/agent.sock")
        assert len(os.fsencode(address)) <= 107
        self.directory = int(address.split("/")[-2])
        self.connect_entered.set()
        if self.pause_connect:
            assert self.connect_release.wait(2), "test did not release the fake connect"
        assert stat.S_ISDIR(os.fstat(self.directory).st_mode)

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        assert (level, option, size) == (socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        return struct.pack("3i", self.peer_pid, self.peer_uid, os.getegid())

    def setblocking(self, blocking: bool) -> None:
        assert not blocking

    def shutdown(self, how: int) -> None:
        assert how == socket.SHUT_RDWR

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def serial_without_socket(launched: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    vm, process, _ = launched
    connection = FakeSerialSocket(process.pid)
    metadata = SimpleNamespace(
        st_mode=stat.S_IFSOCK | 0o600,
        st_uid=os.geteuid(),
        st_dev=11,
        st_ino=29,
    )
    calls: list[int] = []
    real_stat = os.stat

    def socket_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if path == "agent.sock":
            assert kwargs["follow_symlinks"] is False
            descriptor = kwargs["dir_fd"]
            assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
            calls.append(descriptor)
            return metadata
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(qemu.os, "stat", socket_stat)
    monkeypatch.setattr(qemu.socket, "socket", lambda *args, **kwargs: connection)
    monkeypatch.setattr(vm, "_read_responses", lambda: None)
    return vm, process, connection, metadata, calls


def _assert_fd_closed(descriptor: int) -> None:
    with pytest.raises(OSError) as error:
        os.fstat(descriptor)
    assert error.value.errno == errno.EBADF


def _long_allocation(config: LinuxQemuLaunchConfig) -> LinuxQemuLaunchConfig:
    directory = config.run_root / ("long-" + "a" * 80) / ("deep-" + "b" * 80)
    directory.mkdir(mode=0o700, parents=True)
    updates: dict[str, Any] = {"run_root": directory}
    for field in (
        "private_disk_path",
        "agent_image_path",
        "seed_image_path",
        "private_firmware_vars_path",
    ):
        original = getattr(config, field)
        copied = directory / original.name
        copied.write_bytes(original.read_bytes())
        copied.chmod(0o600)
        updates[field] = copied
    assert len(os.fsencode(directory / "agent.sock")) > 107
    return replace(config, **updates)


def test_long_allocation_launch_uses_pinned_short_endpoint_without_socket(
    config: LinuxQemuLaunchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _long_allocation(config)
    process = FakeProcess()
    calls: list[tuple[list[str], dict[str, Any]]] = []
    checked_directories: list[int] = []
    real_stat = os.stat

    def socket_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if path == "agent.sock":
            assert kwargs["follow_symlinks"] is False
            directory = kwargs["dir_fd"]
            assert os.fstat(directory).st_ino == config.run_root.lstat().st_ino
            checked_directories.append(directory)
        return real_stat(path, *args, **kwargs)

    def spawn(command: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((command, kwargs))
        return process

    monkeypatch.setattr(qemu.os, "stat", socket_stat)
    monkeypatch.setattr(qemu.subprocess, "Popen", spawn)
    vm = LinuxQemuProcess(config)
    try:
        vm.start()
        command, options = calls[0]
        directory = vm._require_run_root_fd()
        endpoint = qemu._socket_address(directory)
        assert len(os.fsencode(endpoint)) <= 107
        assert command[command.index("-chardev") + 1] == (
            f"socket,id=agent,path={endpoint},server=on,wait=off"
        )
        assert directory in options["pass_fds"] and len(options["pass_fds"]) == 7
        assert sum(stat.S_ISDIR(os.fstat(fd).st_mode) for fd in (directory,)) == 1
        assert checked_directories == [directory]
        assert vm.socket_path == config.run_root / "agent.sock"
        assert options["cwd"] == config.run_root
    finally:
        vm.stop()
    _assert_fd_closed(directory)


@pytest.mark.parametrize("stage", ["prelaunch", "spawn"])
def test_launch_failure_closes_pinned_directory_without_socket(
    config: LinuxQemuLaunchConfig,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    pinned: list[int] = []
    real_open = qemu._open_private_directory

    def pin(path: Path) -> int:
        descriptor = real_open(path)
        pinned.append(descriptor)
        return descriptor

    def spawn(*args: Any, **kwargs: Any) -> Any:
        assert stage == "spawn", "digest failure must precede spawn"
        raise OSError("injected spawn failure")

    monkeypatch.setattr(qemu, "_open_private_directory", pin)
    monkeypatch.setattr(qemu.subprocess, "Popen", spawn)
    if stage == "prelaunch":
        config = replace(config, qemu_digest="sha256:" + "0" * 64)
    vm = LinuxQemuProcess(config)
    with pytest.raises(BackendUnavailableError):
        vm.start()
    assert len(pinned) == 1 and vm._run_root_fd is None
    _assert_fd_closed(pinned[0])
    vm.stop()


def test_directory_pin_survives_failed_reap_and_closes_once_without_socket(launched: Any) -> None:
    vm, process, _ = launched
    directory = vm._require_run_root_fd()
    identity = os.fstat(directory).st_ino
    assert not os.get_inheritable(directory)
    process.ignore_terminate = process.ignore_kill = True
    with pytest.raises(BackendUnavailableError, match="reaped"):
        vm.stop(0.1)
    assert vm._run_root_fd == directory and os.fstat(directory).st_ino == identity
    process.ignore_kill = False
    vm.stop()
    assert vm._run_root_fd is None
    _assert_fd_closed(directory)
    replacement = os.open(vm.config.run_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        if replacement != directory:
            os.dup2(replacement, directory, inheritable=False)
            os.close(replacement)
            replacement = directory
        vm.stop()
        assert os.fstat(replacement).st_ino == identity
    finally:
        os.close(replacement)


@pytest.mark.parametrize("change", ["mode", "owner", "inode", "kind"])
def test_directory_pin_rechecks_opened_metadata_without_socket(
    config: LinuxQemuLaunchConfig,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    opened: list[int] = []
    real_open, real_fstat = os.open, os.fstat

    def open_directory(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW and flags & os.O_CLOEXEC
        descriptor = real_open(path, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def changed_metadata(descriptor: int) -> os.stat_result:
        values = list(real_fstat(descriptor))
        if change == "mode":
            values[0] = stat.S_IFDIR | 0o755
        elif change == "owner":
            values[4] = os.geteuid() + 1
        elif change == "inode":
            values[1] += 1
        else:
            values[0] = stat.S_IFREG | 0o600
        return os.stat_result(values)

    with monkeypatch.context() as patch:
        patch.setattr(qemu.os, "open", open_directory)
        patch.setattr(qemu.os, "fstat", changed_metadata)
        with pytest.raises(BackendUnavailableError, match="changed or is unsafe"):
            qemu._open_private_directory(config.run_root)
    assert len(opened) == 1
    _assert_fd_closed(opened[0])


def test_connect_uses_short_local_pin_and_closes_it_without_socket(serial_without_socket: Any) -> None:
    vm, _, connection, metadata, calls = serial_without_socket
    global_directory = vm._require_run_root_fd()
    vm.wait_for_serial(1)
    assert connection.directory is not None and connection.directory != global_directory
    assert connection.address == qemu._socket_address(connection.directory)
    assert calls == [connection.directory]
    assert vm._socket_identity == (metadata.st_dev, metadata.st_ino)
    assert vm._channel is connection and not connection.closed
    _assert_fd_closed(connection.directory)
    assert stat.S_ISDIR(os.fstat(global_directory).st_mode)


def test_missing_peer_credentials_fails_closed_without_socket(
    serial_without_socket: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm, _, connection, _, calls = serial_without_socket
    monkeypatch.delattr(qemu.socket, "SO_PEERCRED")
    with pytest.raises(BackendUnavailableError, match="peer credentials are unavailable"):
        vm.wait_for_serial(1)
    assert connection.closed and vm._channel is None
    assert len(calls) == 1
    _assert_fd_closed(calls[0])
    assert vm._run_root_fd is not None


@pytest.mark.parametrize("change", ["mode", "owner", "kind", "peer_uid", "peer_pid"])
def test_short_endpoint_preserves_socket_security_without_socket(
    serial_without_socket: Any,
    change: str,
) -> None:
    vm, _, connection, metadata, calls = serial_without_socket
    if change == "mode":
        metadata.st_mode |= 0o040
    elif change == "owner":
        metadata.st_uid += 1
    elif change == "kind":
        metadata.st_mode = stat.S_IFREG | 0o600
    elif change == "peer_uid":
        connection.peer_uid += 1
    else:
        connection.peer_pid += 1
    with pytest.raises(BackendUnavailableError, match="ownership|peer"):
        vm.wait_for_serial(1)
    assert connection.closed and vm._channel is None
    assert len(calls) == 1
    _assert_fd_closed(calls[0])
    assert vm._run_root_fd is not None
    # Restore metadata so the fixture can complete normal owned cleanup.
    metadata.st_mode = stat.S_IFSOCK | 0o600
    metadata.st_uid = os.geteuid()


def test_socket_creation_failure_closes_local_pin_without_socket(
    launched: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm, _, _ = launched
    duplicates: list[int] = []
    real_dup = os.dup

    def duplicate(descriptor: int) -> int:
        copied = real_dup(descriptor)
        duplicates.append(copied)
        return copied

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(errno.EACCES, "injected socket denial")

    with monkeypatch.context() as patch:
        patch.setattr(qemu.os, "dup", duplicate)
        patch.setattr(qemu.socket, "socket", denied)
        with pytest.raises(BackendUnavailableError, match="serial connection failed"):
            vm.wait_for_serial(1)
    assert len(duplicates) == 1
    _assert_fd_closed(duplicates[0])
    assert vm._run_root_fd is not None


def test_expired_connect_does_not_duplicate_directory_without_socket(
    launched: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm, _, _ = launched
    remaining_calls = 0
    real_remaining = qemu._remaining

    def remaining(deadline: float) -> float:
        nonlocal remaining_calls
        remaining_calls += 1
        if remaining_calls == 2:
            raise TimeoutError("injected expired connect deadline")
        return real_remaining(deadline)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("expired attempt must not allocate a directory duplicate")

    with monkeypatch.context() as patch:
        patch.setattr(qemu, "_remaining", remaining)
        patch.setattr(qemu.os, "dup", forbidden)
        with pytest.raises(TimeoutError, match="expired connect deadline"):
            vm.wait_for_serial(1)
    assert remaining_calls == 2 and vm._run_root_fd is not None


def test_socket_cleanup_is_descriptor_relative_without_socket(
    serial_without_socket: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm, process, _, metadata, calls = serial_without_socket
    directory = vm._require_run_root_fd()
    identity = os.fstat(directory).st_ino
    unlinked: list[int] = []
    real_unlink = os.unlink

    def unlink_socket(path: Any, *args: Any, **kwargs: Any) -> None:
        if path == "agent.sock":
            assert process.poll() is not None
            descriptor = kwargs["dir_fd"]
            assert os.fstat(descriptor).st_ino == identity
            unlinked.append(descriptor)
            return
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(qemu.os, "unlink", unlink_socket)
    vm._socket_identity = (metadata.st_dev, metadata.st_ino)
    vm.stop()
    assert len(unlinked) == 1 and calls == unlinked
    _assert_fd_closed(unlinked[0])
    _assert_fd_closed(directory)
    assert vm._run_root_fd is None


def test_unsafe_socket_cleanup_retains_pin_for_retry_without_socket(serial_without_socket: Any) -> None:
    vm, _, _, metadata, _ = serial_without_socket
    directory = vm._require_run_root_fd()
    vm._socket_identity = (metadata.st_dev, metadata.st_ino + 1)
    with pytest.raises(BackendUnavailableError, match="changed before cleanup"):
        vm.stop()
    assert vm._run_root_fd == directory and stat.S_ISDIR(os.fstat(directory).st_mode)
    vm._socket_identity = (metadata.st_dev, metadata.st_ino)
    vm.stop()
    _assert_fd_closed(directory)


def test_concurrent_stop_keeps_connector_local_pin_valid_without_socket(serial_without_socket: Any) -> None:
    vm, _, connection, _, _ = serial_without_socket
    global_directory = vm._require_run_root_fd()
    identity = os.fstat(global_directory).st_ino
    connection.pause_connect = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(vm.wait_for_serial, 2)
        try:
            assert connection.connect_entered.wait(1)
            local_directory = connection.directory
            assert local_directory is not None and local_directory != global_directory
            vm.stop()
            _assert_fd_closed(global_directory)
            assert os.fstat(local_directory).st_ino == identity
        finally:
            connection.connect_release.set()
        with pytest.raises(BackendUnavailableError):
            pending.result(2)
    assert connection.closed and vm._channel is None
    _assert_fd_closed(local_directory)


def test_long_allocation_procfd_socket_alias_round_trip(
    config: LinuxQemuLaunchConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional real Unix-I/O evidence, still not a real QEMU guest boot."""
    process = FakeProcess()
    monkeypatch.setattr(qemu.subprocess, "Popen", lambda *args, **kwargs: process)
    vm = LinuxQemuProcess(_long_allocation(config))
    vm.start()
    try:
        listener = _listener(vm)
        with listener, ThreadPoolExecutor(max_workers=1) as pool:
            ready = pool.submit(vm.wait_for_guest_ready, 2)
            connection, _ = listener.accept()
            with connection:
                connection.sendall(SERIAL_READY_FRAME + b"\n")
                ready.result(2)
                assert vm.socket_path.is_socket()
                assert vm._socket_identity == (
                    vm.socket_path.lstat().st_dev,
                    vm.socket_path.lstat().st_ino,
                )
    finally:
        vm.stop()
