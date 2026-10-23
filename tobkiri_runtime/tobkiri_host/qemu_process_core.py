"""Platform-neutral bounded correlation for owned QEMU guest streams.

Transport and process ownership remain implemented by each platform driver.
No frame is trusted here; guest signatures are verified by the supervisor.
"""
from __future__ import annotations

import hmac
import json
import math
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from tobkiri_protocol.canonical import canonical_json, strict_loads
from tobkiri_protocol.packvm_serial import SERIAL_READY_FRAME

from .errors import BackendUnavailableError

_CHALLENGE = re.compile(r"[a-f0-9]{64}\Z")
_MAX_PENDING = 8
_MAX_CHALLENGES = 256


class GuestStreamConfig(Protocol):
    """Limits and allocation identity consumed by the shared stream."""
    @property
    def run_root(self) -> Path: ...
    @property
    def max_request_bytes(self) -> int: ...
    @property
    def max_response_bytes(self) -> int: ...


class OwnedGuestChild(Protocol):
    """Minimal owned-process liveness boundary."""
    @property
    def pid(self) -> int | None: ...
    def poll(self) -> int | None: ...


ConfigT = TypeVar("ConfigT", bound=GuestStreamConfig)
ChildT = TypeVar("ChildT", bound=OwnedGuestChild)


@dataclass
class _PendingExchange:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: Exception | None = None
    is_cancel: bool = False


class QemuProcessCore(ABC, Generic[ConfigT, ChildT]):
    """Common framing and correlation without platform inheritance."""

    def __init__(self, config: ConfigT) -> None:
        self.config = config
        self._process: ChildT | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._connect_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[str, _PendingExchange] = {}
        self._used_challenges: set[str] = set()
        self._failure: Exception | None = None
        self._started = False
        self._ready_received = False
        self._ready_event = threading.Event()
        self._stopping = False


    @property
    def pid(self) -> int | None:
        """Return the owned child PID, or None before launch."""
        return self._process.pid if self._process is not None else None


    @property
    def domain_directory(self) -> Path:
        """Return the allocation directory owned by the upstream provisioner."""
        return self.config.run_root


    def wait_for_guest_ready(self, timeout: float = 60.0) -> None:
        """Order writes after the agent opens its stream, without trusting it."""
        deadline = _deadline(timeout)
        try:
            self.wait_for_serial(_remaining(deadline))
            with self._lock:
                self._require_running()
            if not self._ready_event.wait(_remaining(deadline)):
                raise TimeoutError("QEMU guest transport readiness timed out")
            with self._lock:
                self._require_running()
                if not self._ready_received:
                    raise BackendUnavailableError("QEMU guest transport did not become ready")
        except Exception as error:
            self._fail_channel(error)
            raise

    def _frame_capacity(self) -> int:
        with self._lock:
            return self.config.max_response_bytes if self._ready_received else len(SERIAL_READY_FRAME)

    def _validate_partial_frame(self, content: bytes | bytearray) -> None:
        if not content:
            return
        with self._lock:
            if not self._ready_received:
                if not (SERIAL_READY_FRAME + b"\n").startswith(content):
                    raise BackendUnavailableError("QEMU guest startup frame is invalid")
            elif not self._pending:
                raise BackendUnavailableError("Linux QEMU unsolicited response")

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
        self.wait_for_guest_ready(_remaining(deadline))
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


    def _deliver_frame(self, frame: bytes) -> None:
        with self._lock:
            if self._failure is not None or self._stopping:
                raise BackendUnavailableError("QEMU guest transport is retired")
            if not self._ready_received:
                if not hmac.compare_digest(frame, SERIAL_READY_FRAME) or self._pending or self._used_challenges:
                    raise BackendUnavailableError("QEMU guest startup frame is invalid")
                self._ready_received = True
                self._ready_event.set()
                return
            if hmac.compare_digest(frame, SERIAL_READY_FRAME):
                raise BackendUnavailableError("QEMU guest startup frame was repeated")
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


    def _require_running(self) -> ChildT:
        if self._failure is not None:
            raise BackendUnavailableError("Linux QEMU serial channel is retired") from self._failure
        if self._stopping or self._process is None:
            raise BackendUnavailableError("Linux QEMU process is not running")
        code = self._process.poll()
        if code is not None:
            raise BackendUnavailableError(f"Linux QEMU process exited with status {code}")

        return self._process


    def alive(self) -> bool:
        """Report child process liveness, independently of stream health."""
        with self._lock:
            return self._process is not None and self._process.poll() is None


    @abstractmethod
    def wait_for_serial(self, timeout: float = 60.0) -> None:
        """Require the platform-owned transport within the deadline."""

    @abstractmethod
    def _send_frame(self, encoded: bytes, deadline: float) -> None:
        """Write through the platform-owned bounded transport."""

    @abstractmethod
    def _fail_channel(self, error: Exception) -> None:
        """Permanently retire the platform channel and wake pending calls."""


def _deadline(timeout: float) -> float:
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Linux QEMU timeout must be finite and positive")
    return time.monotonic() + timeout


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Linux QEMU deadline expired")
    return remaining

