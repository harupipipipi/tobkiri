"""Private, bounded process evidence; never part of a guest or public error."""

from __future__ import annotations

import os
import threading
from typing import BinaryIO


class QemuProcessDiagnostics:
    """Retain a small stderr tail and the first failure before child cleanup."""

    __slots__ = ("_first_failure", "_lock", "_tail")
    limit = 16 * 1024

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tail = bytearray()
        self._first_failure: tuple[str, int | None, int | None, str] | None = None

    def __repr__(self) -> str:
        return "<private QEMU process diagnostics>"

    def drain(self, stream: BinaryIO) -> None:
        """Read at most 64 KiB from a nonblocking pipe per owner iteration."""
        for _ in range(4):
            try:
                chunk = os.read(stream.fileno(), self.limit)
            except (BlockingIOError, OSError, ValueError):
                return
            if not chunk:
                return
            with self._lock:
                self._tail.extend(chunk)
                del self._tail[:-self.limit]

    def capture(
        self, error: Exception, *, phase: str, pid: int | None,
        returncode: int | None,
    ) -> None:
        """Attach evidence without adding stderr or exception text to logs."""
        with self._lock:
            if self._first_failure is None:
                self._first_failure = (phase, pid, returncode, type(error).__name__)
        # Exception arguments and stringification remain unchanged. The object
        # survives allocation cleanup through the preserved exception chain.
        try:
            error._qemu_process_diagnostics = self  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass

    def snapshot(self) -> tuple[
        tuple[str, int | None, int | None, str] | None, bytes,
    ]:
        """Return private evidence for explicit diagnostic inspection only."""
        with self._lock:
            return self._first_failure, bytes(self._tail)
