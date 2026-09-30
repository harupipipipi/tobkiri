"""Unprivileged, one-domain Linux QEMU process and bounded serial transport.

This substrate is not an attestor. The caller verifies image, firmware and
guest provenance, signs the launch binding, and authenticates every guest
response. An open Unix socket or a live QEMU process proves none of those.
Only KVM is suitable for production isolation. Explicitly opted-in TCG is
for conformance work with trusted guests, never an isolation guarantee.

The allocator owns the private raw disk and two read-only seed images. This
module neither mounts the base image nor mutates host KVM configuration. It
does not delete allocation files, even on failure; the caller may do that
only after ``stop`` has successfully reaped the owned child. Guest RAM and
vCPU bounds are configuration bounds, not a hard cap on QEMU host RSS; host
resource enforcement is the caller's responsibility.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import math
import os
import re
import select
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tobkiri_protocol.canonical import canonical_json, strict_loads

from .errors import BackendUnavailableError

_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
_CHALLENGE = re.compile(r"[a-f0-9]{64}\Z")
_MACHINE = re.compile(r"(?:q35|pc|pc-(?:q35|i440fx)-[0-9]+\.[0-9]+)\Z")
_PORT_NAME = "io.tobkiri.packvm.agent"
_MAX_PENDING = 8
_MAX_CHALLENGES = 256
_POLL_SECONDS = 0.05
_EXEC_LAUNCHER = """
import ctypes, os, resource, signal, sys
parent, executable = int(sys.argv[1]), int(sys.argv[2])
limit_as, limit_cpu, limit_file = map(int, sys.argv[3:6])
if os.getppid() != parent:
    os._exit(78)
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
    os._exit(78)
if os.getppid() != parent:
    os._exit(78)
if libc.prctl(38, 1, 0, 0, 0) != 0:
    os._exit(78)
resource.setrlimit(resource.RLIMIT_AS, (limit_as, limit_as))
resource.setrlimit(resource.RLIMIT_CPU, (limit_cpu, limit_cpu))
resource.setrlimit(resource.RLIMIT_FSIZE, (limit_file, limit_file))
resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.execve(executable, sys.argv[6:], os.environ)
"""


@dataclass(frozen=True)
class LinuxQemuLaunchConfig:
    """Exact prevalidated launch inputs, with no arbitrary QEMU arguments.

    All paths must be absolute and symlink-free. ``run_root`` already exists,
    is owned by this UID, and is inaccessible to group/other. Its private
    disk and seed files must be singly linked and private to this UID. The
    caller must verify immutable base provenance before making the private
    raw disk, and must never use the immutable base as ``private_disk_path``.
    UEFI code and initial variable-store provenance are pinned upstream.
    """

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
    memory_mib: int = 1024
    vcpus: int = 1
    accelerator: str = "kvm"
    allow_insecure_tcg: bool = False
    machine_type: str = "q35"
    serial_port_name: str = _PORT_NAME
    max_request_bytes: int = 1344 * 1024
    max_response_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            not isinstance(self.qemu_digest, str)
            or _DIGEST.fullmatch(self.qemu_digest) is None
            or any(
                not isinstance(value, str) or _DIGEST.fullmatch(value) is None
                for value in (
                    self.firmware_code_digest,
                    self.private_disk_digest,
                    self.agent_image_digest,
                    self.seed_image_digest,
                    self.private_firmware_vars_digest,
                )
            )
            or type(self.memory_mib) is not int
            or not 128 <= self.memory_mib <= 32768
            or type(self.vcpus) is not int
            or not 1 <= self.vcpus <= 16
            or self.accelerator not in {"kvm", "tcg"}
            or type(self.allow_insecure_tcg) is not bool
            or (self.accelerator == "tcg" and not self.allow_insecure_tcg)
            or not isinstance(self.machine_type, str)
            or _MACHINE.fullmatch(self.machine_type) is None
            or self.serial_port_name != _PORT_NAME
            or type(self.max_request_bytes) is not int
            or not 1 <= self.max_request_bytes <= 1344 * 1024
            or type(self.max_response_bytes) is not int
            or not 1 <= self.max_response_bytes <= 16 * 1024 * 1024
        ):
            raise BackendUnavailableError("Linux QEMU launch configuration is invalid")
        for path in (
            self.qemu_path,
            self.run_root,
            self.private_disk_path,
            self.agent_image_path,
            self.seed_image_path,
            self.firmware_code_path,
            self.private_firmware_vars_path,
        ):
            _check_path_syntax(path)


@dataclass
class _PendingExchange:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: Exception | None = None
    is_cancel: bool = False


class LinuxQemuProcess:
    """Own one QEMU child and one multiplexed, fail-closed guest stream.

    ``exchange`` supports concurrent invoke/cancel calls. Correlation is by
    the fresh guest challenge, not the shared operation request ID. Returned
    JSON is untrusted until the supervisor verifies its guest signature.
    Any timeout or protocol failure permanently retires the stream; call
    ``stop`` before reclaiming the allocation. Instances cannot be restarted.
    """

    def __init__(self, config: LinuxQemuLaunchConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._channel: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._owner: threading.Thread | None = None
        self._lock = threading.RLock()
        self._connect_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[str, _PendingExchange] = {}
        self._used_challenges: set[str] = set()
        self._failure: Exception | None = None
        self._started = False
        self._stopping = False
        self._socket_identity: tuple[int, int] | None = None

    @property
    def pid(self) -> int | None:
        """Return the owned child PID, or None before launch."""
        return self._process.pid if self._process is not None else None

    @property
    def domain_directory(self) -> Path:
        """Return the allocation directory owned by the upstream provisioner."""
        return self.config.run_root

    @property
    def socket_path(self) -> Path:
        """Return the only host-facing guest transport endpoint."""
        return self.config.run_root / "agent.sock"

    def start(self) -> None:
        """Verify local launch files and spawn QEMU without elevated rights."""
        with self._lock:
            if self._started or self._stopping:
                raise BackendUnavailableError("Linux QEMU process cannot be restarted")
            if not sys.platform.startswith("linux") or os.geteuid() == 0:
                raise BackendUnavailableError("Linux QEMU requires an unprivileged Linux UID")
            descriptors: list[int] = []
            try:
                _validate_directory(self.config.run_root, private=True)
                if len(os.fsencode(self.socket_path)) > 107:
                    raise BackendUnavailableError("Linux QEMU socket path is too long")
                if os.path.lexists(self.socket_path):
                    raise BackendUnavailableError("Linux QEMU socket path already exists")
                executable = _open_regular(self.config.qemu_path, executable=True)
                descriptors.append(executable)
                digest = hashlib.sha256()
                while chunk := os.read(executable, 1024 * 1024):
                    digest.update(chunk)
                if not hmac.compare_digest(
                    "sha256:" + digest.hexdigest(),
                    self.config.qemu_digest,
                ):
                    raise BackendUnavailableError("Linux QEMU executable digest mismatch")
                os.lseek(executable, 0, os.SEEK_SET)
                files: list[int] = []
                for path, writable, expected in (
                    (self.config.private_disk_path, True, self.config.private_disk_digest),
                    (self.config.agent_image_path, False, self.config.agent_image_digest),
                    (self.config.seed_image_path, False, self.config.seed_image_digest),
                    (
                        self.config.private_firmware_vars_path,
                        True,
                        self.config.private_firmware_vars_digest,
                    ),
                ):
                    if path == self.config.run_root or not path.is_relative_to(
                        self.config.run_root
                    ):
                        raise BackendUnavailableError("Linux QEMU disk is outside its allocation")
                    descriptor = _open_regular(path, private=True, writable=writable)
                    descriptors.append(descriptor)
                    _verify_fd_digest(descriptor, expected)
                    files.append(descriptor)
                if len({(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in files}) != len(files):
                    raise BackendUnavailableError("Linux QEMU disks must be distinct files")
                firmware = _open_regular(self.config.firmware_code_path)
                descriptors.append(firmware)
                _verify_fd_digest(firmware, self.config.firmware_code_digest)
                if self.config.accelerator == "kvm":
                    _check_kvm()
                command = self._command(files, firmware)
                spawned = threading.Event()
                errors: list[Exception] = []

                def own_child() -> None:
                    # Linux PDEATHSIG follows the parent *thread*. Keep the
                    # spawning thread alive for the entire VM lifetime.
                    try:
                        self._process = subprocess.Popen(
                            [
                                sys.executable,
                                "-I",
                                "-S",
                                "-c",
                                _EXEC_LAUNCHER,
                                str(os.getpid()),
                                str(executable),
                                str((self.config.memory_mib * 4 + 512) * 1024 * 1024),
                                "605",
                                str(os.fstat(files[0]).st_size),
                                *command,
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            cwd=self.config.run_root,
                            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                            close_fds=True,
                            pass_fds=tuple(descriptors),
                            start_new_session=True,
                            umask=0o077,
                        )
                    except Exception as exc:  # noqa: BLE001 - forward thread failure to spawning caller
                        errors.append(exc)
                    finally:
                        spawned.set()
                    while self._process is not None and self._process.poll() is None:
                        time.sleep(_POLL_SECONDS)

                self._owner = threading.Thread(
                    target=own_child,
                    name="tobkiri-qemu-owner",
                    daemon=True,
                )
                self._owner.start()
                spawned.wait()
                if errors:
                    raise errors[0]
                self._started = True
            except (OSError, ValueError) as exc:
                raise BackendUnavailableError("Linux QEMU launch failed") from exc
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)

    def _command(self, files: list[int], firmware: int) -> list[str]:
        config = self.config
        command = [
            str(config.qemu_path),
            "-no-user-config",
            "-nodefaults",
            "-machine",
            config.machine_type,
            "-accel",
            config.accelerator,
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
            "-sandbox",
            "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
            "-drive",
            f"file=/proc/self/fd/{firmware},format=raw,if=pflash,unit=0,readonly=on",
            "-drive",
            f"file=/proc/self/fd/{files[3]},format=raw,if=pflash,unit=1",
            "-drive",
            f"file=/proc/self/fd/{files[0]},format=raw,if=none,id=packdisk",
            "-device",
            "virtio-blk-pci,drive=packdisk,bootindex=1,romfile=",
            "-drive",
            f"file=/proc/self/fd/{files[1]},format=raw,if=none,id=agentseed,readonly=on",
            "-device",
            "virtio-blk-pci,drive=agentseed,romfile=",
            "-drive",
            f"file=/proc/self/fd/{files[2]},format=raw,if=none,id=configseed,readonly=on",
            "-device",
            "virtio-blk-pci,drive=configseed,romfile=",
            "-device",
            "virtio-serial-pci,id=packserial,romfile=",
            "-chardev",
            f"socket,id=agent,path={self.socket_path},server=on,wait=off",
            "-device",
            f"virtserialport,bus=packserial.0,chardev=agent,name={_PORT_NAME}",
        ]
        return command

    def wait_for_serial(self, timeout: float = 60.0) -> None:
        """Connect to QEMU's private Unix socket within one absolute deadline.

        Socket readiness is not guest readiness or authenticated attestation.
        The caller must still perform and verify a fresh guest challenge.
        """
        deadline = _deadline(timeout)
        if not self._connect_lock.acquire(timeout=_remaining(deadline)):
            raise TimeoutError("Linux QEMU serial connection timed out")
        try:
            with self._lock:
                self._require_running()
                if self._channel is not None:
                    return
            while True:
                with self._lock:
                    self._require_running()
                _remaining(deadline)
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    connection.settimeout(min(_POLL_SECONDS, _remaining(deadline)))
                    metadata = self.socket_path.lstat()
                    if (
                        not stat.S_ISSOCK(metadata.st_mode)
                        or metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(metadata.st_mode) & 0o077
                    ):
                        raise BackendUnavailableError("Linux QEMU socket ownership is unsafe")
                    connection.connect(str(self.socket_path))
                    pid, uid, _gid = struct.unpack(
                        "3i",
                        connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
                    )
                    if uid != os.geteuid() or pid != self.pid:
                        raise BackendUnavailableError(
                            "Linux QEMU socket peer is not the owned child"
                        )
                    connection.setblocking(False)
                    with self._lock:
                        self._require_running()
                        self._socket_identity = (metadata.st_dev, metadata.st_ino)
                        self._channel = connection
                        self._reader = threading.Thread(
                            target=self._read_responses,
                            name="tobkiri-qemu-serial",
                            daemon=True,
                        )
                        self._reader.start()
                    return
                except OSError as exc:
                    connection.close()
                    if exc.errno not in {errno.ENOENT, errno.ECONNREFUSED}:
                        raise BackendUnavailableError(
                            "Linux QEMU serial connection failed"
                        ) from exc
                    time.sleep(min(_POLL_SECONDS, _remaining(deadline)))
                except Exception:
                    connection.close()
                    raise
        finally:
            self._connect_lock.release()

    def exchange(
        self,
        envelope: Mapping[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Exchange canonical NDJSON with bounded, out-of-order correlation."""
        deadline = _deadline(timeout)
        challenge = envelope.get("guest_challenge")
        if not isinstance(challenge, str) or _CHALLENGE.fullmatch(challenge) is None:
            raise BackendUnavailableError("Linux QEMU guest challenge is invalid")
        try:
            encoded = canonical_json(dict(envelope)) + b"\n"
        except (ValueError, TypeError, RecursionError) as exc:
            raise BackendUnavailableError("Linux QEMU request is not canonical JSON") from exc
        if len(encoded) > self.config.max_request_bytes:
            raise BackendUnavailableError("Linux QEMU request exceeds size limit")
        self.wait_for_serial(_remaining(deadline))
        pending = _PendingExchange(is_cancel=envelope.get("operation") == "cancel")
        with self._lock:
            self._require_running()
            if (
                challenge in self._used_challenges
                or len(self._used_challenges) >= _MAX_CHALLENGES
                or len(self._pending) >= _MAX_PENDING
                or (
                    not pending.is_cancel
                    and sum(not item.is_cancel for item in self._pending.values())
                    >= _MAX_PENDING - 1
                )
            ):
                raise BackendUnavailableError("Linux QEMU request concurrency or replay limit")
            self._used_challenges.add(challenge)
            self._pending[challenge] = pending
        try:
            if not self._write_lock.acquire(timeout=_remaining(deadline)):
                raise TimeoutError("Linux QEMU serial write timed out")
            try:
                self._send_frame(encoded, deadline)
            finally:
                self._write_lock.release()
            if not pending.event.wait(_remaining(deadline)):
                raise TimeoutError("Linux QEMU guest response timed out")
            if pending.error is not None:
                raise pending.error
            assert pending.response is not None
            return pending.response
        except Exception as exc:
            self._fail_channel(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(challenge, None)

    def _send_frame(self, encoded: bytes, deadline: float) -> None:
        sent = 0
        while sent < len(encoded):
            with self._lock:
                self._require_running()
                connection = self._channel
            if connection is None:
                raise BackendUnavailableError("Linux QEMU serial channel is closed")
            _, writable, _ = select.select([], [connection], [], _remaining(deadline))
            if not writable:
                raise TimeoutError("Linux QEMU serial write timed out")
            try:
                size = connection.send(encoded[sent : sent + 65536])
            except BlockingIOError:
                continue
            if size == 0:
                raise BackendUnavailableError("Linux QEMU serial channel ended")
            sent += size

    def _read_responses(self) -> None:
        content = bytearray()
        try:
            while True:
                with self._lock:
                    if self._stopping or self._failure is not None:
                        return
                    self._require_running()
                    connection = self._channel
                if connection is None:
                    return
                readable, _, _ = select.select([connection], [], [], _POLL_SECONDS)
                if not readable:
                    continue
                try:
                    chunk = connection.recv(
                        min(
                            65536,
                            self.config.max_response_bytes + 1 - len(content),
                        )
                    )
                except BlockingIOError:
                    continue
                if not chunk:
                    raise BackendUnavailableError("Linux QEMU guest stream ended")
                content.extend(chunk)
                while b"\n" in content:
                    frame, _, rest = content.partition(b"\n")
                    content = bytearray(rest)
                    self._deliver_frame(bytes(frame))
                if len(content) > self.config.max_response_bytes:
                    raise BackendUnavailableError("Linux QEMU response exceeds size limit")
                with self._lock:
                    if content and not self._pending:
                        raise BackendUnavailableError("Linux QEMU unsolicited response")
        except Exception as exc:  # noqa: BLE001 - fail every pending exchange, never swallow
            self._fail_channel(exc)

    def _deliver_frame(self, frame: bytes) -> None:
        if len(frame) > self.config.max_response_bytes:
            raise BackendUnavailableError("Linux QEMU response exceeds size limit")
        response = strict_loads(frame, max_bytes=self.config.max_response_bytes)
        if not isinstance(response, dict) or not hmac.compare_digest(
            frame,
            json.dumps(
                response, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
        ):
            raise BackendUnavailableError("Linux QEMU response is not canonical JSON")
        challenge = response.get("guest_challenge")
        with self._lock:
            pending = self._pending.get(challenge) if isinstance(challenge, str) else None
            if pending is None or pending.event.is_set():
                raise BackendUnavailableError("Linux QEMU response correlation failed")
            pending.response = response
            pending.event.set()

    def _require_running(self) -> None:
        if self._failure is not None:
            raise BackendUnavailableError("Linux QEMU serial channel is retired") from self._failure
        if self._stopping or self._process is None:
            raise BackendUnavailableError("Linux QEMU process is not running")
        code = self._process.poll()
        if code is not None:
            raise BackendUnavailableError(f"Linux QEMU process exited with status {code}")

    def _fail_channel(self, error: Exception) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = error
            connection, self._channel = self._channel, None
            for pending in self._pending.values():
                pending.error = BackendUnavailableError("Linux QEMU guest channel failed")
                pending.event.set()
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def alive(self) -> bool:
        """Report child process liveness, independently of stream health."""
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def stop(self, timeout: float = 5.0) -> None:
        """Close transport, terminate and reap the child within a deadline.

        On an unreaped child this raises and retains ownership for a retry;
        the caller must not remove its allocation or release its resources.
        """
        deadline = _deadline(timeout)
        with self._lock:
            self._stopping = True
            process = self._process
        self._fail_channel(BackendUnavailableError("Linux QEMU process was stopped"))
        if process is not None:
            try:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=_remaining(deadline) / 2)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                process.wait(timeout=_remaining(deadline))
            except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
                raise BackendUnavailableError("Linux QEMU child could not be reaped") from exc
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=max(0, deadline - time.monotonic()))
            if reader.is_alive():
                raise BackendUnavailableError("Linux QEMU serial reader did not stop")
        owner = self._owner
        if owner is not None:
            owner.join(timeout=max(0, deadline - time.monotonic()))
            if owner.is_alive():
                raise BackendUnavailableError("Linux QEMU owner thread did not stop")
        self._unlink_socket()

    def _unlink_socket(self) -> None:
        try:
            metadata = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            self._socket_identity is not None
            and (metadata.st_dev, metadata.st_ino) != self._socket_identity
        ):
            raise BackendUnavailableError("Linux QEMU socket changed before cleanup")
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or self._process is None
        ):
            raise BackendUnavailableError("Linux QEMU socket cleanup is unsafe")
        self.socket_path.unlink()


def _check_path_syntax(path: Path) -> None:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or ".." in path.parts
        or any(character in str(path) for character in (",", "\x00", "\n", "\r"))
    ):
        raise BackendUnavailableError("Linux QEMU path is invalid")


def _validate_directory(path: Path, *, private: bool = False) -> None:
    """Check every path component without accepting symlinked ancestors."""
    _check_path_syntax(path)
    uid = os.geteuid()
    for component in reversed((path, *path.parents)):
        metadata = component.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, uid}:
            raise BackendUnavailableError("Linux QEMU directory ownership is unsafe")
        # Root-owned sticky temporary roots protect our private child names.
        sticky_root = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
        if mode & 0o022 and not sticky_root:
            raise BackendUnavailableError("Linux QEMU ancestor is writable by another user")
        if component == path and private and (metadata.st_uid != uid or mode & 0o077):
            raise BackendUnavailableError("Linux QEMU allocation directory is not private")


def _open_regular(
    path: Path,
    *,
    private: bool = False,
    executable: bool = False,
    writable: bool = False,
) -> int:
    _validate_directory(path.parent)
    descriptor = os.open(
        path,
        (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
    )
    try:
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_nlink != 1
            or mode & 0o022
            or metadata.st_size == 0
            or (private and (metadata.st_uid != os.geteuid() or mode & 0o077))
            or (executable and (not mode & 0o111 or mode & 0o6000))
            or (writable and not mode & 0o200)
        ):
            raise BackendUnavailableError("Linux QEMU launch file is unsafe")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _verify_fd_digest(descriptor: int, expected: str) -> None:
    """Hash the exact descriptor that QEMU will inherit, not a reopened path."""
    before = os.fstat(descriptor)
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    after = os.fstat(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    if not hmac.compare_digest("sha256:" + digest.hexdigest(), expected) or (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise BackendUnavailableError("Linux QEMU pinned launch asset digest changed")


def _check_kvm() -> None:
    """Require usable KVM ABI access without changing permissions or modules."""
    import fcntl

    descriptor = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
            raise BackendUnavailableError("Linux KVM device is not a character device")
        if fcntl.ioctl(descriptor, 0xAE00) != 12:  # KVM_GET_API_VERSION
            raise BackendUnavailableError("Linux KVM ABI is unsupported")
    finally:
        os.close(descriptor)


def _deadline(timeout: float) -> float:
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Linux QEMU timeout must be finite and positive")
    return time.monotonic() + timeout


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Linux QEMU deadline expired")
    return remaining
