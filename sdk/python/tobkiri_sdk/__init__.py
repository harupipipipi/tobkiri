"""Pure Python guest SDK compiled into a wasm component, never a host runtime.

An adapter supplies generated WIT imports explicitly. No SDK operation has an OS,
socket, subprocess or native sqlite fallback.
"""

from . import audit, http, ports, proc, secrets, storage, streams
from ._binding import GuestError, bind

__all__ = ["GuestError", "audit", "bind", "http", "ports", "proc", "secrets", "storage", "streams"]
