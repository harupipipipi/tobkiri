"""Owned WHPX QEMU process with private inherited virtio-serial pipes.

There is no TCP endpoint, AF_UNIX dependency, POSIX syscall, elevated launch,
or software-emulation fallback. Parent death closes the ownership Job and
kills QEMU. Pipe bytes remain untrusted until the supervisor verifies the
fresh per-domain guest signatures.
"""

from __future__ import annotations

import hmac
import os
import re
import subprocess
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from .errors import BackendUnavailableError
from .linux_qemu_process import LinuxQemuProcess, _deadline, _remaining
from .windows_whpx_native import WindowsJobProcess
from .windows_whpx_probe import whpx_capability
from .windows_whpx_security import _stream_hash, private_directory, stable_file

_PORT_NAME = "io.tobkiri.packvm.agent"
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class WindowsWHPXLaunchConfig:
    """Closed WHPX launch inputs; arbitrary QEMU arguments are never accepted."""

    qemu_path: Path
    qemu_digest: str
    run_root: Path
    private_disk_path: Path
    agent_image_path: Path
    seed_image_path: Path
    firmware_code_path: Path
    private_firmware_vars_path: Path
    firmware_code_digest: str
    private_disk_digest: str
    agent_image_digest: str
    seed_image_digest: str
    private_firmware_vars_digest: str
    immutable_assets: tuple[tuple[Path, str], ...] = ()
    memory_mib: int = 1024
    vcpus: int = 1
    max_request_bytes: int = 1344 * 1024
    max_response_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            not isinstance(self.qemu_digest, str)
            or not _DIGEST.fullmatch(self.qemu_digest)
            or type(self.memory_mib) is not int
            or not 128 <= self.memory_mib <= 32768
            or type(self.vcpus) is not int
            or not 1 <= self.vcpus <= 16
            or type(self.max_request_bytes) is not int
            or not 1 <= self.max_request_bytes <= 1344 * 1024
            or type(self.max_response_bytes) is not int
            or not 1 <= self.max_response_bytes <= 16 * 1024 * 1024
        ):
            raise BackendUnavailableError("Windows WHPX launch configuration is invalid")
        for path in (
            self.qemu_path,
            self.run_root,
            self.private_disk_path,
            self.agent_image_path,
            self.seed_image_path,
            self.firmware_code_path,
            self.private_firmware_vars_path,
            *(path for path, _ in self.immutable_assets),
        ):
            if (
                not isinstance(path, Path)
                or not path.is_absolute()
                or ".." in path.parts
                or any(c in str(path) for c in (",", "\0", "\r", "\n"))
            ):
                raise BackendUnavailableError("Windows WHPX launch path is invalid")
        if any(
            not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
            for digest in (
                self.firmware_code_digest,
                self.private_disk_digest,
                self.agent_image_digest,
                self.seed_image_digest,
                self.private_firmware_vars_digest,
                *(digest for _, digest in self.immutable_assets),
            )
        ):
            raise BackendUnavailableError("Windows WHPX immutable identity is invalid")


class WindowsWHPXProcess(LinuxQemuProcess):
    """Share bounded challenge correlation, replacing every platform operation.

    The inherited methods implement only canonical framing, challenge limits,
    locks and deadlines. No inherited Linux process, socket or syscall method
    is used by this implementation.
    """

    def __init__(self, config: WindowsWHPXLaunchConfig) -> None:
        super().__init__(config)  # type: ignore[arg-type]
        self._pins = ExitStack()
        self._writers: set[threading.Thread] = set()

    def start(self) -> None:
        """Pin immutable code, create private pipes, then launch into a Job."""
        with self._lock:
            if self._started or self._stopping:
                raise BackendUnavailableError("Windows WHPX process cannot be restarted")
            ready, reason = whpx_capability()
            if not ready:
                raise BackendUnavailableError(reason or "Windows WHPX is unavailable")
            try:
                storage = private_directory(self.config.run_root, create=False)
                # Pin allocation ancestors for QEMU's entire lifetime; their
                # names cannot be swapped for junctions after validation.
                self._pins.enter_context(
                    storage._windows_parent(self.config.private_disk_path.name, create=False)
                )
                immutable = dict(self.config.immutable_assets)
                immutable[self.config.qemu_path] = self.config.qemu_digest
                immutable[self.config.firmware_code_path] = self.config.firmware_code_digest
                for path, expected in immutable.items():
                    stream = self._pins.enter_context(stable_file(path))
                    if not hmac.compare_digest(
                        "sha256:" + _stream_hash(stream).hexdigest(), expected
                    ):
                        raise BackendUnavailableError("Windows WHPX immutable asset changed")
                identities = set()
                for path, expected in (
                    (self.config.private_disk_path, self.config.private_disk_digest),
                    (self.config.agent_image_path, self.config.agent_image_digest),
                    (self.config.seed_image_path, self.config.seed_image_digest),
                    (
                        self.config.private_firmware_vars_path,
                        self.config.private_firmware_vars_digest,
                    ),
                ):
                    if path.parent != self.config.run_root:
                        raise BackendUnavailableError(
                            "Windows WHPX private file escaped its allocation"
                        )
                    # Keep the name/File ID pinned without denying QEMU write
                    # access to the private raw disk and variable store.
                    from tobkiri_protocol.secure_persistence import _windows_open_file_descriptor

                    fd, identity = _windows_open_file_descriptor(path, os.O_RDONLY)
                    self._pins.callback(os.close, fd)
                    if identity in identities:
                        raise BackendUnavailableError("Windows WHPX private files are aliased")
                    identities.add(identity)
                    with os.fdopen(os.dup(fd), "rb") as source:
                        actual = "sha256:" + _stream_hash(source).hexdigest()
                    if not hmac.compare_digest(actual, expected):
                        raise BackendUnavailableError("Windows WHPX private launch bytes changed")
                system_root = _system_root()
                environment = {
                    "SystemRoot": system_root,
                    "WINDIR": system_root,
                    "PATH": str(Path(system_root) / "System32"),
                    "TEMP": str(self.config.run_root),
                    "TMP": str(self.config.run_root),
                    "LANG": "C",
                    "LC_ALL": "C",
                }
                self._process = WindowsJobProcess(
                    self.config.qemu_path,
                    self.command(),
                    cwd=self.config.run_root,
                    env=environment,
                    memory_limit_bytes=(self.config.memory_mib + 1536) * 1024 * 1024,
                )
                self._started = True
                self._reader = threading.Thread(
                    target=self._read_responses, name="tobkiri-whpx-serial", daemon=True
                )
                self._reader.start()
            except BaseException:
                if self._process is not None:
                    self._process.close()
                self._pins.close()
                raise

    def command(self) -> list[str]:
        """Return the fixed hardware-accelerated, offline QEMU argument vector."""
        config = self.config
        return [
            str(config.qemu_path),
            "-no-user-config",
            "-nodefaults",
            "-machine",
            "q35",
            "-accel",
            "whpx",
            "-m",
            str(config.memory_mib),
            "-smp",
            str(config.vcpus),
            "-display",
            "none",
            "-monitor",
            "none",
            "-serial",
            "none",
            "-parallel",
            "none",
            "-nic",
            "none",
            "-no-reboot",
            "-drive",
            f"file={config.firmware_code_path},format=raw,if=pflash,unit=0,readonly=on",
            "-drive",
            f"file={config.private_firmware_vars_path},format=raw,if=pflash,unit=1",
            "-drive",
            f"file={config.private_disk_path},format=raw,if=none,id=packdisk",
            "-device",
            "virtio-blk-pci,drive=packdisk,bootindex=1,romfile=",
            "-drive",
            f"file={config.agent_image_path},format=raw,if=none,id=agentseed,readonly=on",
            "-device",
            "virtio-blk-pci,drive=agentseed,romfile=",
            "-drive",
            f"file={config.seed_image_path},format=raw,if=none,id=configseed,readonly=on",
            "-device",
            "virtio-blk-pci,drive=configseed,romfile=",
            "-device",
            "virtio-serial-pci,id=packserial,romfile=",
            "-chardev",
            "stdio,id=agent,signal=off,mux=off",
            "-device",
            f"virtserialport,bus=packserial.0,chardev=agent,name={_PORT_NAME}",
        ]

    def wait_for_serial(self, timeout: float = 60.0) -> None:
        """Check pipe/process ownership; only signed guest replies prove readiness."""
        _deadline(timeout)
        with self._lock:
            self._require_running()
            if self._process.stdin is None or self._process.stdout is None:
                raise BackendUnavailableError("Windows WHPX private pipes are unavailable")

    def _send_frame(self, encoded: bytes, deadline: float) -> None:
        done = threading.Event()
        errors: list[BaseException] = []

        def write() -> None:
            try:
                view = memoryview(encoded)
                while view:
                    with self._lock:
                        self._require_running()
                    size = self._process.stdin.write(view[:65536])
                    if not size:
                        raise BrokenPipeError("Windows WHPX guest pipe ended")
                    view = view[size:]
            except (OSError, ValueError, BackendUnavailableError) as exc:
                errors.append(exc)
            finally:
                done.set()
                with self._lock:
                    self._writers.discard(threading.current_thread())

        writer = threading.Thread(target=write, name="tobkiri-whpx-write", daemon=True)
        with self._lock:
            self._writers.add(writer)
        writer.start()
        if not done.wait(_remaining(deadline)):
            # A blocked synchronous anonymous-pipe write cannot be polled with
            # Windows select(). Killing this exact child closes its pipe peer.
            self._fail_channel(TimeoutError("Windows WHPX pipe write timed out"))
            raise TimeoutError("Windows WHPX pipe write timed out")
        if errors:
            raise errors[0]

    def _read_responses(self) -> None:
        content = bytearray()
        try:
            while True:
                with self._lock:
                    if self._stopping or self._failure is not None:
                        return
                    self._require_running()
                chunk = self._process.stdout.read(
                    min(65536, self.config.max_response_bytes + 1 - len(content))
                )
                if not chunk:
                    raise BackendUnavailableError("Windows WHPX guest pipe ended")
                content.extend(chunk)
                while b"\n" in content:
                    frame, _, rest = content.partition(b"\n")
                    content = bytearray(rest)
                    self._deliver_frame(bytes(frame))
                if len(content) > self.config.max_response_bytes:
                    raise BackendUnavailableError("Windows WHPX guest frame exceeds size limit")
                with self._lock:
                    if content and not self._pending:
                        raise BackendUnavailableError("Windows WHPX unsolicited guest frame")
        except (OSError, ValueError, BackendUnavailableError) as exc:
            self._fail_channel(exc)

    def _fail_channel(self, error: Exception) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = error
            for pending in self._pending.values():
                pending.error = BackendUnavailableError("Windows WHPX guest channel failed")
                pending.event.set()
            process = self._process
        if process is not None:
            process.terminate()

    def stop(self, timeout: float = 5.0) -> None:
        """Reap the owned child before releasing immutable pins/private files."""
        deadline = _deadline(timeout)
        with self._lock:
            self._stopping = True
        self._fail_channel(BackendUnavailableError("Windows WHPX process was stopped"))
        if self._process is not None:
            try:
                self._process.wait(timeout=_remaining(deadline))
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise BackendUnavailableError("Windows WHPX child could not be reaped") from exc
        with self._lock:
            workers = [self._reader, *self._writers]
        for worker in workers:
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=max(0, deadline - time.monotonic()))
                if worker.is_alive():
                    raise BackendUnavailableError("Windows WHPX pipe worker did not stop")
        if self._process is not None:
            self._process.close()
        self._pins.close()


def _system_root() -> str:
    """Resolve Windows itself through the OS, never mutable PATH/SystemRoot."""
    import ctypes

    api = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x800)
    api.GetWindowsDirectoryW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
    api.GetWindowsDirectoryW.restype = ctypes.c_uint32
    result = ctypes.create_unicode_buffer(32768)
    size = api.GetWindowsDirectoryW(result, len(result))
    if not 0 < size < len(result):
        raise OSError("Windows directory is unavailable")
    return result.value
