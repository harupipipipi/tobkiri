"""Launcher-private IPC for the original Host's keyless Provider preparation."""

from __future__ import annotations

import ctypes
import json
import os
import re
import socket
import struct
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache, partial
from typing import Any

from core_runtime.process_identity import process_start_identity
from core_runtime.provider_prepare_native_v4 import (
    MINIMUM_PREPARE_MEMORY,
    NativeProviderPrepareBorrow,
    NativeProviderPreparePlanner,
    NativeProviderRegistryRequiresLegacyRead,
)
from tobkiri_protocol.canonical import strict_loads


_SOCKET_ENV = "TOBKIRI_NATIVE_PROVIDER_PREPARE_SOCKET"
_OWNER_ENV = "TOBKIRI_NATIVE_PROVIDER_PREPARE_OWNER"
_OWNER = re.compile(r"([0-9]{1,10}):([0-9]{1,20}):([0-9]{1,6})")
_INSTANCE = re.compile(r"[0-9a-f]{32}")
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2
_SOCKET_PATH_MAX = 103  # Darwin sockaddr_un.sun_path includes the terminating NUL.
_FRAME = struct.Struct("!BI")
_START, _CHECK, _LIVE = 1, 2, 3
_PLAN, _FAILURE, _CANCEL = 7, 8, 9
_INAPPLICABLE = 10
_JSON_LIMIT = 16 * 1024
_FAILURES = frozenset({
    b"denied", b"approval-required", b"cancelled", b"not-found", b"invalid",
    b"limit-exceeded", b"conflict", b"unavailable", b"internal", b"legacy-read-required",
})
_INBOUND_LIMITS = {
    _CHECK: 0,
    _PLAN: _JSON_LIMIT,
    _FAILURE: max(map(len, _FAILURES)),
    _INAPPLICABLE: 0,
}
_POLL_SECONDS = 0.05
_CLEANUP_SECONDS = 10.0
_NANOS_PER_SECOND = 1_000_000_000
_U64_MAX = (1 << 64) - 1


class NativeProviderPrepareUnavailable(RuntimeError):
    """The authenticated Launcher declined this request before any native work."""


class _ProtocolError(RuntimeError):
    """A fixed transport or protocol failure without peer-supplied details."""


@dataclass(frozen=True)
class _Launcher:
    address: bytes
    pid: int
    start_identity: str

    def assert_parent(self) -> None:
        if os.getppid() != self.pid:
            raise PermissionError("native Provider preparation Launcher changed")

    def assert_start(self) -> None:
        self.assert_parent()
        identity = process_start_identity(self.pid)
        if identity.state != "live" or identity.identity != self.start_identity:
            raise PermissionError("native Provider preparation Launcher changed")


@lru_cache(maxsize=1)
def _getpeereid() -> Any:
    getpeereid = ctypes.CDLL("/usr/lib/libSystem.B.dylib").getpeereid
    getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    getpeereid.restype = ctypes.c_int
    return getpeereid


def _authenticate(connection: socket.socket, launcher: _Launcher) -> None:
    uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
    peer_error = "native Provider preparation peer could not be verified"
    try:
        getpeereid = _getpeereid()
        result = getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid))
        pid_bytes = connection.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4)
        pid = struct.unpack("=i", pid_bytes)[0]
    except (AttributeError, OSError, struct.error):
        raise PermissionError(peer_error) from None
    if result != 0 or uid.value != os.geteuid() or pid != launcher.pid:
        raise PermissionError(peer_error)
    launcher.assert_start()


def capture_launcher_provider_prepare_planner() -> NativeProviderPreparePlanner | None:
    """Capture the managed Launcher's fixed endpoint when the Host is composed."""
    if sys.platform != "darwin":
        return None
    address_env = os.environ.get(_SOCKET_ENV)
    owner_env = os.environ.get(_OWNER_ENV)
    if address_env is None and owner_env is None:
        return None
    address = os.fsencode(address_env or "")
    owner = _OWNER.fullmatch(owner_env or "")
    if (
        owner is None
        or not address.startswith(b"/")
        or len(address) > _SOCKET_PATH_MAX
        or b"\0" in address
    ):
        raise _ProtocolError("native Provider preparation launch binding is invalid")
    pid, seconds, microseconds = map(int, owner.groups())
    if not (1 < pid <= (1 << 31) - 1 and 0 < seconds <= _U64_MAX):
        raise _ProtocolError("native Provider preparation launch binding is invalid")
    launcher = _Launcher(address, pid, f"darwin:{pid}:{seconds}:{microseconds:06d}")
    plan = partial(_plan, launcher)
    return NativeProviderPreparePlanner(MINIMUM_PREPARE_MEMORY, plan)


def _start_payload(borrow: NativeProviderPrepareBorrow, deadline_ns: int) -> bytes:
    context = borrow.context
    runtime_instance = borrow.runtime_instance
    request = borrow.request
    if (
        not isinstance(runtime_instance, str)
        or _INSTANCE.fullmatch(runtime_instance) is None
        or type(context.security_epoch) is not int
        or not 0 < context.security_epoch <= _U64_MAX
    ):
        raise _ProtocolError("native Provider preparation binding is invalid")
    frame = {
        "identity": {
            "profile_id": context.profile_id,
            "profile_revision": context.profile_revision,
            "activation_id": context.activation_id,
            "plan_digest": context.plan_digest,
        },
        "security_epoch": context.security_epoch,
        "runtime_instance": runtime_instance,
        "deadline_ns": deadline_ns,
        "request": {
            "connection-name": request["connection_name"],
            "endpoint": request["endpoint"],
            "catalog-provider-id": request.get("catalog_provider_id"),
            "display-name": request.get("display_name"),
            "capabilities": request.get("capabilities"),
        },
    }
    frozen_plan = borrow.frozen_plan
    if frozen_plan is not None:
        frame["apply"] = dict(frozen_plan)
    payload = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError:
        raise _ProtocolError("native Provider request contains invalid UTF-8") from None
    if len(encoded) > _JSON_LIMIT:
        raise _ProtocolError("native Provider preparation request exceeds its bound")
    return encoded


def _decode_plan(payload: bytes) -> dict[str, Any]:
    try:
        plan = strict_loads(payload, max_bytes=_JSON_LIMIT)
    except (ValueError, RecursionError):
        raise _ProtocolError("native Provider preparation plan is invalid") from None
    if not isinstance(plan, dict):
        raise _ProtocolError("native Provider preparation plan is invalid")
    return plan


class _Exchange:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.received = bytearray()
        self.partial_write = False
        self.started = False
        self.terminal = False

    def send(
        self, tag: int, payload: bytes, wait_seconds: Callable[[], float],
    ) -> None:
        frame = memoryview(_FRAME.pack(tag, len(payload)) + payload)
        while frame:
            self.connection.settimeout(wait_seconds())
            try:
                sent = self.connection.send(frame)
            except TimeoutError:
                continue
            if not sent:
                raise _ProtocolError("native Provider preparation connection closed")
            frame = frame[sent:]
            self.partial_write = bool(frame)

    def _receive_until(self, size: int, wait_seconds: Callable[[], float]) -> None:
        while len(self.received) < size:
            self.connection.settimeout(wait_seconds())
            try:
                part = self.connection.recv(size - len(self.received))
            except TimeoutError:
                continue
            if not part:
                raise _ProtocolError("native Provider preparation connection closed")
            self.received.extend(part)

    def receive(self, wait_seconds: Callable[[], float]) -> tuple[int, bytes]:
        self._receive_until(_FRAME.size, wait_seconds)
        tag, size = _FRAME.unpack_from(self.received)
        limit = _INBOUND_LIMITS.get(tag)
        if limit is None or size > limit:
            raise _ProtocolError("native Provider preparation frame is invalid")
        self._receive_until(_FRAME.size + size, wait_seconds)
        payload = bytes(self.received[_FRAME.size:])
        self.received.clear()
        return tag, payload

    def cancel_and_confirm(self) -> None:
        deadline = time.monotonic() + _CLEANUP_SECONDS

        def wait_seconds() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            return min(_POLL_SECONDS, remaining)

        try:
            if self.partial_write:
                # A CANCEL header inside an incomplete payload is just more data.
                # Stop writing so the Launcher sees EOF and cancels its worker.
                self.connection.shutdown(socket.SHUT_WR)
            else:
                self.send(_CANCEL, b"", wait_seconds)
        except (OSError, _ProtocolError):
            # The Launcher may have closed its receive side after sending proof.
            pass
        try:
            while True:
                tag, payload = self.receive(wait_seconds)
                if tag == _FAILURE and payload in _FAILURES:
                    return
                if tag != _CHECK:
                    break
        except (OSError, _ProtocolError):
            pass
        raise _ProtocolError("native Provider preparation cleanup was unconfirmed")

    def run(
        self,
        borrow: NativeProviderPrepareBorrow,
        deadline_ns: int,
        wait_seconds: Callable[[], float],
    ) -> dict[str, Any]:
        self.send(_START, _start_payload(borrow, deadline_ns), wait_seconds)
        self.started = True
        entered_live_phase = False
        while True:
            tag, payload = self.receive(wait_seconds)
            if tag == _FAILURE and payload in _FAILURES:
                self.terminal = True
                if payload == b"legacy-read-required" and entered_live_phase:
                    wait_seconds()
                    raise NativeProviderRegistryRequiresLegacyRead(
                        "Provider Registry exceeds the native envelope before writes",
                    )
                raise _ProtocolError(
                    f"native Provider preparation failed ({payload.decode('ascii')})"
                )
            wait_seconds()
            if tag == _CHECK:
                self.send(_LIVE, b"", wait_seconds)
                entered_live_phase = True
            elif tag == _PLAN and entered_live_phase:
                plan = _decode_plan(payload)
                wait_seconds()
                self.terminal = True
                return plan
            elif tag == _INAPPLICABLE and not entered_live_phase:
                self.terminal = True
                raise NativeProviderPrepareUnavailable("native Provider is unavailable")
            else:
                raise _ProtocolError("native Provider frame is out of order")


def _plan(
    launcher: _Launcher, borrow: NativeProviderPrepareBorrow,
) -> Mapping[str, Any]:
    numerator, denominator = borrow.deadline_monotonic.as_integer_ratio()
    deadline_ns = numerator * _NANOS_PER_SECOND // denominator
    if not 0 < deadline_ns <= _U64_MAX:
        raise _ProtocolError("native Provider preparation deadline is invalid")

    def wait_seconds() -> float:
        borrow.assert_current()
        launcher.assert_parent()
        remaining = deadline_ns - time.monotonic_ns()
        if remaining <= 0:
            raise TimeoutError("native Provider preparation deadline elapsed")
        return min(_POLL_SECONDS, remaining / _NANOS_PER_SECOND)

    launcher.assert_start()
    connection_error = "native Provider preparation connection failed"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(wait_seconds())
        try:
            connection.connect(launcher.address)
        except OSError:
            raise _ProtocolError(connection_error) from None
        _authenticate(connection, launcher)
        exchange = _Exchange(connection)
        try:
            return exchange.run(borrow, deadline_ns, wait_seconds)
        except Exception as error:
            if exchange.started and not exchange.terminal:
                try:
                    exchange.cancel_and_confirm()
                except _ProtocolError as cleanup_error:
                    raise cleanup_error from None
            if isinstance(error, OSError) and not isinstance(
                error, (PermissionError, TimeoutError),
            ):
                raise _ProtocolError(connection_error) from None
            raise
