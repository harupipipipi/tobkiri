"""Finite text clipboard adapter; no shell, PATH lookup, or backend fallback.

The Host process and Apple's fixed clipboard utilities are authority-bearing
TCB. This adapter does not claim that a workspace confines the global clipboard.
Linux selection ownership and Windows native clipboard support are deliberately
not emulated with an unbounded process or an arbitrary script.
"""

from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Callable, Mapping, Any

from .effects import EffectDisposition, ProviderOutcome

MAX_TEXT_BYTES = 1_048_576
MAX_SECONDS = 5.0


def unavailable(code: str, recovery: str) -> ProviderOutcome:
    """Return a failure which does not assert consent or native execution."""
    return ProviderOutcome(
        {
            "status": "error",
            "success": False,
            "error_type": code,
            "executed": False,
            "recovery": recovery,
        },
        EffectDisposition.NOT_ACCEPTED,
    )


class MacOSTextClipboard:
    """Execute only the OS-owned pbcopy/pbpaste text interface."""

    def availability(self, access: str) -> Mapping[str, Any]:
        """Inspect platform and executable metadata without touching clipboard."""
        if access not in {"read", "write"}:
            raise ValueError("unknown clipboard access")
        if sys.platform != "darwin":
            return {
                "available": False,
                "error_type": "unsupported_os",
                "recovery": "Use a supported native clipboard provider on macOS.",
            }
        try:
            self._trusted_command(access)
        except (OSError, ValueError):
            return {
                "available": False,
                "error_type": "provider_unavailable",
                "recovery": "Restore the OS-owned clipboard utility.",
            }
        return {
            "available": True,
            "resource_scope": "global_clipboard",
            "authorization": "checked_at_execution",
            "os_permission": "checked_at_execution",
        }

    @staticmethod
    def _trusted_command(access: str) -> str:
        # On supported macOS these are SIP-protected OS files. Never accept a
        # caller-selected helper, writable ancestor, symlink, or PATH override.
        command = "/usr/bin/pbpaste" if access == "read" else "/usr/bin/pbcopy"
        for path in (Path("/"), Path("/usr"), Path("/usr/bin"), Path(command)):
            info = path.lstat()
            expected = stat.S_ISREG if path == Path(command) else stat.S_ISDIR
            if not expected(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError("clipboard executable trust is unavailable")
        return command

    def execute(
        self,
        access: str,
        payload: Mapping[str, Any],
        *,
        deadline: float,
        check_authority: Callable[[], None],
    ) -> ProviderOutcome:
        """Perform one bounded effect after a Host-owned final authority check."""
        if access == "read":
            if payload:
                raise ValueError("clipboard read takes no arguments")
            data = None
        elif access == "write":
            if set(payload) != {"text"} or not isinstance(payload["text"], str):
                raise ValueError("clipboard write requires only text")
            data = payload["text"].encode("utf-8", errors="strict")
            if len(data) > MAX_TEXT_BYTES:
                raise ValueError("clipboard text exceeds one MiB")
        else:
            raise ValueError("unknown clipboard access")
        availability = self.availability(access)
        if not availability["available"]:
            return unavailable(availability["error_type"], availability["recovery"])
        command = self._trusted_command(access)
        end = min(deadline, time.monotonic() + MAX_SECONDS)
        check_authority()
        if time.monotonic() >= end:
            return unavailable("timed_out", "Retry with fresh authorization.")
        try:
            process = subprocess.Popen(
                [command],
                stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env={"LANG": "en_US.UTF-8", "LC_CTYPE": "en_US.UTF-8"},
                close_fds=True,
                start_new_session=True,
            )
        except PermissionError:
            return unavailable("os_permission_denied", "Check native OS access.")
        except OSError:
            return unavailable("provider_unavailable", "Check the OS clipboard service.")
        try:
            output = self._exchange(process, data, end, check_authority)
            if process.returncode != 0:
                return self._failed_after_start(access, "execution_failed")
            if access == "read":
                return ProviderOutcome({
                    "status": "ok", "success": True,
                    "text": output.decode("utf-8", errors="strict"),
                    "format": "text/plain", "resource_scope": "global_clipboard",
                })
            return ProviderOutcome({
                "status": "ok", "success": True, "written": True,
                "format": "text/plain", "resource_scope": "global_clipboard",
            })
        except (TimeoutError, OSError, ValueError, PermissionError):
            # Once pbcopy started, a failure/cancel/timeout cannot prove that
            # clipboard was untouched. Never retry or fall back automatically.
            return self._failed_after_start(access, "execution_interrupted")
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    @staticmethod
    def _failed_after_start(access: str, code: str) -> ProviderOutcome:
        if access == "write":
            return ProviderOutcome(None, EffectDisposition.UNKNOWN)
        return unavailable(code, "Retry only after checking native clipboard access.")

    @staticmethod
    def _exchange(
        process: subprocess.Popen[bytes],
        data: bytes | None,
        deadline: float,
        check_authority: Callable[[], None],
    ) -> bytes:
        output = bytearray()
        errors = 0
        offset = 0
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            if process.stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
                if data:
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                else:
                    process.stdin.close()
            while selector.get_map() or process.poll() is None:
                check_authority()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("clipboard deadline exceeded")
                for key, event in selector.select(min(remaining, 0.05)):
                    stream = key.fileobj
                    if event & selectors.EVENT_WRITE:
                        assert data is not None
                        offset += os.write(key.fd, data[offset:offset + 65536])
                        if offset == len(data):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(stream)
                        elif stream is process.stdout:
                            if len(output) + len(chunk) > MAX_TEXT_BYTES:
                                raise ValueError("clipboard response exceeds one MiB")
                            output.extend(chunk)
                        else:
                            errors += len(chunk)
                            if errors > 65536:
                                raise ValueError("clipboard error response is oversized")
        return bytes(output)
