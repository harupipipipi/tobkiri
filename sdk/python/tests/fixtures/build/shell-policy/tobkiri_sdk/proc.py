"""Subprocess-like guest shim that exposes only core process resources."""

from typing import Any, Sequence
from ._binding import GuestError, invoke


class Process:
    """A core-owned, cancellable process handle; no native Python subprocess."""

    def __init__(self, resource: Any) -> None:
        self._resource = resource

    def read_stdout(self, max_bytes: int = 8192) -> bytes | None:
        """Pull a bounded stdout chunk."""
        return self._resource.read_stdout(max_bytes)

    def read_stderr(self, max_bytes: int = 8192) -> bytes | None:
        """Pull a bounded stderr chunk."""
        return self._resource.read_stderr(max_bytes)

    def kill(self) -> None:
        """Request cancellation of the core-owned process."""
        self._resource.kill()

    def wait(self) -> int:
        """Wait within the original call's cancellation and deadline."""
        return self._resource.wait()


def popen(arguments: Sequence[str], *, cwd: Any = None,
          env: dict[str, str] | None = None, shell: bool = False) -> Process:
    """Spawn an explicitly granted program; shell execution has no fallback."""
    if shell:
        raise GuestError("shell=True is not supported by tobkiri_sdk")
    if not arguments:
        raise ValueError("program is required")
    return Process(invoke("proc", "spawn", program=arguments[0], args=list(arguments[1:]),
                          cwd=cwd, env=list((env or {}).items())))
