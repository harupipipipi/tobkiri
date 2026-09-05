"""Process-local restart signal owned by the finite Host control boundary."""

from __future__ import annotations

from threading import Lock


_LOCK = Lock()
_restart_requested = False


def request_kernel_restart() -> None:
    global _restart_requested
    with _LOCK:
        _restart_requested = True


def is_kernel_restart_requested() -> bool:
    with _LOCK:
        return _restart_requested


def clear_kernel_restart_request() -> None:
    global _restart_requested
    with _LOCK:
        _restart_requested = False


__all__ = [
    "clear_kernel_restart_request",
    "is_kernel_restart_requested",
    "request_kernel_restart",
]
