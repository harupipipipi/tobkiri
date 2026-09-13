"""Bounded observable and receipt-controlled terminal sessions."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
POLICY = "rumi.service.shell.inspect.v1"
WORKSPACE = "rumi.resource.workspace.v1"
SERVICE_PACK_ID = "rumi_terminal_session_pack"
_MAX_SESSIONS = 32
_MAX_BUFFER = 128 * 1024
_TERMINAL = {"exited", "cancelled", "failed"}


class TerminalSessions:
    """Own process-lifetime terminal sessions and bounded output buffers."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.lock = threading.RLock()
        self.sessions: dict[str, dict[str, Any]] = {}

    def read(self, name: str, payload: Mapping[str, Any]) -> Any:
        """Observe session state without control authority."""
        if name == "get":
            return self._snapshot(str(payload.get("terminal_session_id") or ""))
        if name == "list":
            with self.lock:
                return {"sessions": [self._public(item) for item in self.sessions.values()]}
        raise ValueError(f"unknown terminal session resource operation: {name}")

    def control(self, name: str, payload: Mapping[str, Any]) -> Any:
        """Apply receipt-gated session control."""
        if name == "start":
            arguments = _start_arguments(payload)
        elif name == "input":
            arguments = {
                "terminal_session_id": str(payload.get("terminal_session_id") or ""),
                "data": str(payload.get("data") or ""),
            }
        elif name == "cancel":
            arguments = {
                "terminal_session_id": str(payload.get("terminal_session_id") or "")
            }
        elif name == "shutdown":
            arguments = {"all": True}
        else:
            raise ValueError(f"unknown terminal session control operation: {name}")
        self._redeem(name, payload, arguments)
        if name == "start":
            return self._start(payload, arguments)
        if name == "input":
            return self._input(arguments)
        if name == "cancel":
            return self._cancel(arguments["terminal_session_id"])
        return self._shutdown()

    def _start(
        self, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        policy = self.client.invoke(POLICY, "classify", arguments)
        if policy.get("shell_syntax") and not arguments["shell"]:
            raise PermissionError("shell syntax requires explicit shell mode")
        root = self._workspace(payload)
        cwd = _cwd(root, str(arguments["cwd"]))
        argv: Any = (
            arguments["command"]
            if arguments["shell"]
            else _argv(arguments["command"])
        )
        options: dict[str, Any] = {
            "cwd": cwd,
            "shell": bool(arguments["shell"]),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": False,
            "bufsize": 0,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(argv, **options)
        session_id = str(uuid.uuid4())
        session = {
            "id": session_id,
            "profile_id": _profile(payload),
            "workspace_id": str(payload.get("workspace_id") or ""),
            "command": arguments["command"],
            "cwd": cwd.relative_to(root).as_posix() if cwd != root else ".",
            "classification": policy.get("classification"),
            "status": "running",
            "created_at": time.time(),
            "updated_at": time.time(),
            "stdout": bytearray(),
            "stderr": bytearray(),
            "process": process,
        }
        with self.lock:
            self._prune()
            if len(self.sessions) >= _MAX_SESSIONS:
                _terminate(process)
                raise RuntimeError("terminal session limit reached")
            self.sessions[session_id] = session
        self._reader(session_id, "stdout", process.stdout)
        self._reader(session_id, "stderr", process.stderr)
        return self._public(session)

    def _reader(self, session_id: str, field: str, pipe: Any) -> None:
        def read() -> None:
            try:
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    with self.lock:
                        session = self.sessions.get(session_id)
                        if session is None:
                            break
                        session[field].extend(chunk)
                        if len(session[field]) > _MAX_BUFFER:
                            del session[field][:-_MAX_BUFFER]
                        session["updated_at"] = time.time()
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass

        threading.Thread(target=read, daemon=True).start()

    def _input(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        session = self._required(str(arguments["terminal_session_id"]))
        process = session["process"]
        if process.poll() is not None or process.stdin is None:
            raise RuntimeError("terminal session is not accepting input")
        data = str(arguments["data"]).encode("utf-8")
        process.stdin.write(data)
        process.stdin.flush()
        session["updated_at"] = time.time()
        return {"terminal_session_id": session["id"], "written_bytes": len(data)}

    def _cancel(self, session_id: str) -> dict[str, Any]:
        session = self._required(session_id)
        _terminate(session["process"])
        session["status"] = "cancelled"
        session["updated_at"] = time.time()
        return self._public(session)

    def _shutdown(self) -> dict[str, Any]:
        cancelled = []
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            if session["process"].poll() is None:
                _terminate(session["process"])
                session["status"] = "cancelled"
                cancelled.append(session["id"])
        return {"shutdown": True, "cancelled_session_ids": cancelled}

    def _snapshot(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            session = self.sessions.get(session_id)
            return self._public(session) if session is not None else None

    def _required(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError("terminal session is unknown")
            return session

    def _public(self, session: Mapping[str, Any]) -> dict[str, Any]:
        process = session["process"]
        exit_code = process.poll()
        status = str(session["status"])
        if status not in _TERMINAL and exit_code is not None:
            status = "exited"
            session["status"] = status
            session["updated_at"] = time.time()
        return {
            "id": session["id"],
            "profile_id": session["profile_id"],
            "workspace_id": session["workspace_id"],
            "command": session["command"],
            "cwd": session["cwd"],
            "classification": session["classification"],
            "status": status,
            "exit_code": exit_code,
            "stdout": bytes(session["stdout"]).decode("utf-8", errors="replace"),
            "stderr": bytes(session["stderr"]).decode("utf-8", errors="replace"),
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "bounded": True,
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
        return Path(str(mount.get("root_path") or "")).resolve(strict=True)

    def _redeem(
        self, name: str, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": f"terminal.session.{name}",
                "authority": "terminal.session.control",
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
            raise PermissionError(str(result.get("reason") or "terminal authority denied"))

    def _prune(self) -> None:
        for item in self.sessions.values():
            if item["status"] not in _TERMINAL and item["process"].poll() is not None:
                item["status"] = "exited"
                item["updated_at"] = time.time()
        terminal = sorted(
            (item for item in self.sessions.values() if item["status"] in _TERMINAL),
            key=lambda item: float(item["updated_at"]),
        )
        for item in terminal[: max(0, len(self.sessions) - _MAX_SESSIONS + 1)]:
            del self.sessions[item["id"]]


_RUNTIMES: dict[str, TerminalSessions] = {}
_RUNTIME_LOCK = threading.Lock()


def create_terminal_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create terminal session observe operations."""
    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return _runtime(client, payload).read(name, payload)
    return operation


def create_terminal_control(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated terminal session control operations."""
    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return _runtime(client, payload).control(name, payload)
    return operation


def _runtime(client: Any, payload: Mapping[str, Any]) -> TerminalSessions:
    profile_id = _profile(payload)
    with _RUNTIME_LOCK:
        return _RUNTIMES.setdefault(profile_id, TerminalSessions(client))


def _start_arguments(payload: Mapping[str, Any]) -> dict[str, Any]:
    command = payload.get("command")
    if not isinstance(command, (str, list, tuple)) or not command:
        raise ValueError("terminal command is required")
    return {
        "command": list(command) if isinstance(command, (list, tuple)) else command,
        "cwd": str(payload.get("cwd") or "."),
        "shell": bool(payload.get("shell", False)),
    }


def _cwd(root: Path, value: str) -> Path:
    raw = Path(value)
    if raw.is_absolute():
        raise PermissionError("absolute terminal cwd is denied")
    resolved = (root / raw).resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError("terminal cwd escapes workspace") from exc
    if not resolved.is_dir():
        raise NotADirectoryError("terminal cwd is not a directory")
    return resolved


def _argv(command: Any) -> list[str]:
    if isinstance(command, list):
        return [str(item) for item in command]
    return shlex.split(str(command), posix=sys.platform != "win32")


def _terminate(process: subprocess.Popen[bytes]) -> None:
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


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")

