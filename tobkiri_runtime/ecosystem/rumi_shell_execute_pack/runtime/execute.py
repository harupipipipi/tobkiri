"""Receipt-gated workspace-jailed bounded shell execution."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
POLICY = "rumi.service.shell.inspect.v1"
WORKSPACE = "rumi.resource.workspace.v1"
SERVICE_PACK_ID = "rumi_shell_execute_pack"
_MAX_OUTPUT_BYTES = 128 * 1024
_MAX_TIMEOUT = 900
_ENV_ALLOWLIST = {
    "PATH", "HOME", "USER", "USERNAME", "USERPROFILE", "TEMP", "TMP",
    "SYSTEMROOT", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
}


class ShellExecuteService:
    """Execute one bounded command after exact authority redemption."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Execute one command; no client approval boolean is trusted."""
        if name != "execute":
            raise ValueError(f"unknown shell execute operation: {name}")
        arguments = _arguments(payload)
        policy = self.client.invoke(POLICY, "classify", arguments)
        if policy.get("shell_syntax") and not arguments["shell"]:
            raise PermissionError("shell syntax requires explicit shell mode")
        self._redeem(payload, arguments)
        root = self._workspace(payload)
        cwd = _cwd(root, arguments["cwd"])
        command = arguments["command"]
        argv: Any = command if arguments["shell"] else _argv(command)
        env = _environment(arguments["env"])
        started = time.monotonic()
        options: dict[str, Any] = {
            "cwd": cwd,
            "env": env,
            "shell": arguments["shell"],
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": False,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(argv, **options)
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=arguments["timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_group(process)
            stdout, stderr = process.communicate()
        return {
            "command": command,
            "workspace_id": str(payload.get("workspace_id") or ""),
            "cwd": cwd.relative_to(root).as_posix() if cwd != root else ".",
            "classification": policy.get("classification"),
            "risk_reasons": list(policy.get("risk_reasons") or []),
            "approval_required": bool(policy.get("approval_required")),
            "exit_code": process.returncode,
            "stdout": _output(stdout),
            "stderr": _output(stderr),
            "timed_out": timed_out,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "executed": True,
            "authority_receipt_redeemed": True,
        }

    def _workspace(self, payload: Mapping[str, Any]) -> Path:
        mount = self.client.invoke(
            WORKSPACE,
            "get",
            {
                "profile_id": _profile(payload),
                "workspace_id": str(payload.get("workspace_id") or ""),
            },
        )
        if not isinstance(mount, Mapping):
            raise KeyError("workspace mount is unknown")
        root = Path(str(mount.get("root_path") or "")).resolve(strict=True)
        if not root.is_dir():
            raise PermissionError("workspace root is unavailable")
        return root

    def _redeem(
        self, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": "shell.execute",
                "authority": "shell.execute",
                "caller_id": str(payload.get("caller_id") or ""),
                "caller_pack_id": str(payload.get("caller_pack_id") or ""),
                "caller_function_id": str(payload.get("caller_function_id") or ""),
                "profile_id": _profile(payload),
                "workspace_id": str(payload.get("workspace_id") or ""),
                "session_id": str(payload.get("session_id") or ""),
                "arguments": dict(arguments),
            },
        )
        if not result.get("authorized"):
            raise PermissionError(str(result.get("reason") or "shell authority denied"))


def create_shell_execute_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated shell execution."""
    return ShellExecuteService(client).invoke


def _arguments(payload: Mapping[str, Any]) -> dict[str, Any]:
    command = payload.get("command")
    if not isinstance(command, (str, list, tuple)) or not command:
        raise ValueError("command is required")
    timeout = max(1, min(_MAX_TIMEOUT, int(payload.get("timeout") or 30)))
    env = payload.get("env")
    if env is not None and not isinstance(env, Mapping):
        raise ValueError("shell environment must be an object")
    return {
        "command": list(command) if isinstance(command, (list, tuple)) else command,
        "cwd": str(payload.get("cwd") or "."),
        "timeout": timeout,
        "shell": bool(payload.get("shell", False)),
        "env": {str(key): str(value) for key, value in (env or {}).items()},
    }


def _cwd(root: Path, value: str) -> Path:
    raw = Path(str(value or "."))
    if raw.is_absolute():
        raise PermissionError("absolute shell cwd is denied")
    resolved = (root / raw).resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError("shell cwd escapes workspace") from exc
    if not resolved.is_dir():
        raise NotADirectoryError("shell cwd is not a directory")
    return resolved


def _argv(command: Any) -> list[str]:
    if isinstance(command, list):
        return [str(item) for item in command]
    return shlex.split(str(command), posix=sys.platform != "win32")


def _environment(overrides: Mapping[str, str]) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _ENV_ALLOWLIST
    }
    for key, value in overrides.items():
        upper = key.upper()
        if upper not in _ENV_ALLOWLIST and not (
            key.startswith("RUMI_") and not _secret_key(key)
        ):
            raise PermissionError(f"shell environment key is denied: {key}")
        env[key] = value
    return env


def _secret_key(key: str) -> bool:
    lower = key.casefold()
    return any(word in lower for word in ("token", "secret", "password", "api_key", "authorization"))


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
    else:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)


def _output(value: bytes) -> str:
    clipped = value[:_MAX_OUTPUT_BYTES]
    text = clipped.decode("utf-8", errors="replace")
    return text + ("\n[output truncated]\n" if len(value) > len(clipped) else "")


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")

