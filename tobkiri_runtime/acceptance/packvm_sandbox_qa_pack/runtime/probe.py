"""Finite untrusted Pack used only for live PackVM sandbox acceptance."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any, Callable, Mapping

CONTRACT_ID = "tobkiri.acceptance.packvm.sandbox.v1"
OPERATIONS = frozenset(
    {
        "probe_isolation",
        "stdout_overflow",
        "stderr_overflow",
        "deadline_hold",
        "cancel_hold",
        "abnormal_exit",
    }
)
_DENIED_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})
_HOST_PATHS = (
    "/Users",
    "/Volumes",
    "/host",
    "/mnt/host",
    "/var/lib/tobkiri-packvm",
)
_AGENT_KEY = Path("/run/tobkiri-packvm/agent-ed25519.pem")
_OVERFLOW_BYTES = 17 * 1024 * 1024


def tobkiri_packvm_invoke(
    operation_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Run one exact acceptance scenario inside the isolated Pack child."""

    if operation_id not in OPERATIONS:
        raise ValueError("unsupported PackVM acceptance operation")
    if not isinstance(payload, Mapping) or set(payload) - {"nonce"}:
        raise ValueError("PackVM acceptance payload fields are invalid")
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or len(nonce) != 64:
        raise ValueError("PackVM acceptance nonce is invalid")
    if not sys.platform.startswith("linux"):
        raise RuntimeError("PackVM acceptance requires a Linux guest child")
    if os.geteuid() == 0:
        raise RuntimeError("PackVM acceptance refuses a root process")

    if operation_id == "probe_isolation":
        return _probe_isolation(nonce)
    if operation_id == "stdout_overflow":
        os.write(sys.stdout.fileno(), b"x" * _OVERFLOW_BYTES)
        raise RuntimeError("stdout limit was not enforced")
    if operation_id == "stderr_overflow":
        os.write(sys.stderr.fileno(), b"x" * _OVERFLOW_BYTES)
        raise RuntimeError("stderr limit was not enforced")
    if operation_id in {"deadline_hold", "cancel_hold"}:
        while True:
            time.sleep(0.05)
    if operation_id == "abnormal_exit":
        os._exit(73)
    raise AssertionError("unreachable PackVM acceptance operation")


def _probe_isolation(nonce: str) -> dict[str, Any]:
    checks = {
        "host_filesystem": _probe_host_filesystem(),
        "network": _expect_errno("network", _open_network_socket),
        "process": _expect_errno("process", _fork_process),
        "vsock": _expect_errno("vsock", _open_vsock_socket),
        "agent_key": _probe_agent_key(),
        "pack_read_only": _expect_errno("pack_read_only", _write_pack_root),
    }
    if not all(item["denied"] is True for item in checks.values()):
        raise RuntimeError("PackVM sandbox acceptance boundary is open")
    return {
        "kind": "tobkiri.packvm.sandbox-observation.v1",
        "scenario": "probe_isolation",
        "nonce": nonce,
        "platform": "linux",
        "uid": os.geteuid(),
        "checks": checks,
    }


def _expect_errno(name: str, operation: Callable[[], None]) -> dict[str, Any]:
    try:
        operation()
    except OSError as error:
        return {
            "denied": error.errno in _DENIED_ERRNOS,
            "reason": "permission_denied"
            if error.errno in _DENIED_ERRNOS
            else "unexpected_os_error",
            "errno": int(error.errno or 0),
        }
    return {"denied": False, "reason": f"{name}_unexpectedly_allowed", "errno": 0}


def _probe_host_filesystem() -> dict[str, Any]:
    visible = [path for path in _HOST_PATHS if Path(path).exists()]
    return {
        "denied": not visible,
        "reason": "host_paths_absent" if not visible else "host_path_visible",
        "visible_paths": visible,
    }


def _probe_agent_key() -> dict[str, Any]:
    try:
        descriptor = os.open(_AGENT_KEY, os.O_RDONLY)
    except OSError as error:
        denied = error.errno in _DENIED_ERRNOS or error.errno == errno.ENOENT
        return {
            "denied": denied,
            "reason": "key_unavailable" if denied else "unexpected_os_error",
            "errno": int(error.errno or 0),
        }
    else:
        os.close(descriptor)
        return {"denied": False, "reason": "key_visible", "errno": 0}


def _open_network_socket() -> None:
    candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    candidate.close()


def _open_vsock_socket() -> None:
    family = getattr(socket, "AF_VSOCK", 40)
    candidate = socket.socket(family, socket.SOCK_STREAM)
    candidate.close()


def _fork_process() -> None:
    child = os.fork()
    if child == 0:
        os._exit(0)
    os.waitpid(child, 0)


def _write_pack_root() -> None:
    marker = Path("/pack/.tobkiri-packvm-acceptance-write")
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    marker.unlink(missing_ok=True)


def _main() -> int:
    """Refuse to turn a direct Host execution into guest evidence."""

    print(
        json.dumps(
            {
                "ok": False,
                "code": "DIRECT_EXECUTION_REJECTED",
                "message": "Invoke this fixture through the authenticated PackVM Broker.",
            },
            sort_keys=True,
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
