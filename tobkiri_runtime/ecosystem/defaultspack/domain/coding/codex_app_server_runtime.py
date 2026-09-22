"""Production lifecycle for Codex App Server coding sessions.

The runtime binds persisted App Server configuration to a registered, trusted
workspace.  Browser callers only provide the workspace id; paths and approval
decisions remain server-owned.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from blocks.coding.codex_app_server import (
    CodexAppServerBackend,
    CodexAppServerClient,
    CodingSession,
    RequestTimeout,
)
from domain.codex.app_server import (
    build_codex_app_server_command,
    load_codex_app_server_config,
)
from domain.coding.workspace_policy import require_registered_trusted_workspace
from domain.coding.workspace_resolver import WorkspaceResolver
from domain.safety import approval
from domain.safety.audit import record_approval, record_denial


_STATE_ENV = "RUMI_CODEX_APP_SERVER_SESSION_STATE_PATH"
_TERMINAL_TURN_STATES = {"completed", "failed", "interrupted"}


class CodexRuntimeError(RuntimeError):
    """Raised when the configured production backend cannot serve a request."""


@dataclass
class _RuntimeBinding:
    workspace_id: str
    workspace_root: str
    config_digest: str
    client: CodexAppServerClient
    session: CodingSession
    models: list[dict[str, Any]] = field(default_factory=list)
    active_turn_id: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    poll_thread: threading.Thread | None = None
    runtime_error: str = ""


def _state_path() -> Path:
    override = os.getenv(_STATE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "codex_app_server_sessions.json"


def _safe_json_load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "workspaces": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("workspaces"), dict):
        return {"version": 1, "workspaces": {}}
    return payload


def _config_digest(config: dict[str, Any]) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _turn_id(result: dict[str, Any]) -> str:
    turn = result.get("turn")
    if isinstance(turn, dict) and turn.get("id"):
        return str(turn["id"])
    return str(result.get("turnId") or result.get("turn_id") or "")


class CodexAppServerRuntime:
    """Own App Server processes, trusted workspace bindings, and durable threads."""

    def __init__(
        self,
        *,
        backend_factory: Callable[[], CodexAppServerBackend] = CodexAppServerBackend,
        config_loader: Callable[[], dict[str, Any]] = load_codex_app_server_config,
        workspace_resolver: WorkspaceResolver | None = None,
        state_path: Path | None = None,
        background_polling: bool = True,
    ) -> None:
        self._backend_factory = backend_factory
        self._config_loader = config_loader
        self._workspace_resolver = workspace_resolver or WorkspaceResolver()
        self._state_path = state_path or _state_path()
        self._background_polling = bool(background_polling)
        self._bindings: dict[str, _RuntimeBinding] = {}
        self._lock = threading.RLock()

    def _workspace(self, workspace_id: str):
        value = str(workspace_id or "").strip()
        if not value:
            raise CodexRuntimeError("workspace_id is required")
        resolution = self._workspace_resolver.resolve(
            {"workspace_id": value}, allow_cwd_fallback=False
        )
        return require_registered_trusted_workspace(
            resolution, operation="codex.app_server.turn"
        )

    @staticmethod
    def _validate_config(config: dict[str, Any]) -> None:
        if not config.get("enabled") or config.get("transport") == "off":
            raise CodexRuntimeError(
                "Codex App Server is disabled. Enable stdio in Settings > Coding Backends."
            )
        if config.get("transport") != "stdio":
            raise CodexRuntimeError(
                "Coding sessions currently require the local stdio transport. "
                "Choose stdio in Settings > Coding Backends."
            )
        if not build_codex_app_server_command(config):
            raise CodexRuntimeError("Codex App Server configuration is incomplete")

    def _saved_thread(self, workspace_id: str, workspace_root: str) -> str:
        record = _safe_json_load(self._state_path).get("workspaces", {}).get(workspace_id)
        if not isinstance(record, dict) or record.get("workspace_root") != workspace_root:
            return ""
        return str(record.get("thread_id") or "")

    def _save_binding(self, binding: _RuntimeBinding) -> None:
        with self._lock:
            payload = _safe_json_load(self._state_path)
            payload["version"] = 1
            payload.setdefault("workspaces", {})[binding.workspace_id] = {
                "workspace_root": binding.workspace_root,
                "thread_id": binding.session.thread_id,
                "session_id": binding.session.session_id,
                "updated_at": int(time.time()),
            }
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._state_path)

    def _authority(
        self, workspace_id: str, workspace_root: str, timeout: int
    ) -> Callable[[str, dict[str, Any]], bool | dict[str, Any]]:
        def decide(operation: str, params: dict[str, Any]) -> bool | dict[str, Any]:
            if operation == "user.input":
                return {"action": "decline", "content": None}
            if operation == "auth.external_token_refresh":
                return {"error": "unsupported"}
            details = {
                "source": "codex_app_server",
                "workspace_id": workspace_id,
                "workspace_root": workspace_root,
                "arguments": params,
                "operation_owner": "defaultspack:codex-app-server",
                "conversation_id": f"codex-app-server:{workspace_id}",
            }
            request = approval.create_approval_request(
                operation,
                "high" if operation != "permission.grant" else "critical",
                params,
                expires_in=timeout,
                details=details,
            )
            request_id = str(request["request_id"])
            record_approval(operation, request_id, "requested", source="codex_app_server")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = approval.get_approval_request(request_id) or {}
                status = str(current.get("status") or "")
                if status == "approved":
                    record_approval(
                        operation, request_id, "accepted", source="codex_app_server"
                    )
                    if operation == "permission.grant":
                        requested = params.get("permissions")
                        return {
                            "permissions": requested if isinstance(requested, dict) else {},
                            "scope": "turn",
                        }
                    return {"decision": "accept"}
                if status in {"denied", "expired", "obsolete", "consumed"}:
                    record_denial(
                        operation,
                        "high",
                        f"app-server approval {status}",
                        params,
                        source="codex_app_server",
                    )
                    return False
                time.sleep(0.1)
            approval.deny(request_id, "Codex App Server approval timed out")
            record_denial(
                operation,
                "high",
                "app-server approval timed out",
                params,
                source="codex_app_server",
            )
            return False

        return decide

    def _connect(self, workspace_id: str) -> _RuntimeBinding:
        resolution = self._workspace(workspace_id)
        config = self._config_loader()
        self._validate_config(config)
        digest = _config_digest(config)
        with self._lock:
            existing = self._bindings.get(workspace_id)
            if (
                existing
                and existing.workspace_root == resolution.root_path
                and existing.config_digest == digest
                and not existing.stop_event.is_set()
            ):
                return existing
            if existing:
                existing.stop_event.set()
                existing.client.close()
                self._bindings.pop(workspace_id, None)

        timeout = int(config.get("approval_timeout_seconds") or 120)
        client = self._backend_factory().connect(
            command=build_codex_app_server_command(config),
            cwd=resolution.root_path,
            authority=self._authority(workspace_id, resolution.root_path, timeout),
            timeout=max(10.0, float(timeout) + 5.0),
            required_mcp_servers=tuple(config.get("required_mcp_servers") or ()),
        )
        try:
            models = client.list_models()
            saved_thread = self._saved_thread(workspace_id, resolution.root_path)
            if saved_thread:
                try:
                    session = client.resume_thread(saved_thread, resolution.root_path)
                except Exception:
                    session = client.start_thread(resolution.root_path)
            else:
                session = client.start_thread(resolution.root_path)
        except Exception:
            client.close()
            raise
        binding = _RuntimeBinding(
            workspace_id=workspace_id,
            workspace_root=resolution.root_path,
            config_digest=digest,
            client=client,
            session=session,
            models=models,
        )
        with self._lock:
            self._bindings[workspace_id] = binding
        self._save_binding(binding)
        if self._background_polling:
            binding.poll_thread = threading.Thread(
                target=self._poll_events,
                args=(binding,),
                name=f"codex-app-server-{workspace_id}",
                daemon=True,
            )
            binding.poll_thread.start()
        return binding

    @staticmethod
    def _poll_events(binding: _RuntimeBinding) -> None:
        """Continuously dispatch events and Authority requests off the HTTP thread."""

        while not binding.stop_event.is_set():
            try:
                with binding.lock:
                    binding.client.poll(0.25)
            except RequestTimeout:
                continue
            except Exception as exc:
                binding.runtime_error = str(exc)
                binding.stop_event.set()
                return

    def status(self, workspace_id: str, *, drain_events: bool = True) -> dict[str, Any]:
        binding = self._connect(workspace_id)
        if drain_events and not self._background_polling:
            with binding.lock:
                for _ in range(100):
                    try:
                        binding.client.poll(0.001)
                    except RequestTimeout:
                        break
        turn = binding.client.turn_status.get(binding.active_turn_id, {})
        if str(turn.get("status") or "") in _TERMINAL_TURN_STATES:
            binding.active_turn_id = ""
        return self._snapshot(binding)

    def start_turn(
        self, workspace_id: str, text: str, *, model: str = "", effort: str = ""
    ) -> dict[str, Any]:
        prompt = str(text or "").strip()
        if not prompt:
            raise CodexRuntimeError("message is required")
        if len(prompt) > 200_000:
            raise CodexRuntimeError("message is too large")
        binding = self._connect(workspace_id)
        with binding.lock:
            if binding.active_turn_id:
                known = binding.client.turn_status.get(binding.active_turn_id, {})
                if str(known.get("status") or "") not in _TERMINAL_TURN_STATES:
                    raise CodexRuntimeError("a Codex App Server turn is already active")
            allowed_models = {str(item.get("id") or "") for item in binding.models}
            if model and model not in allowed_models:
                raise CodexRuntimeError("selected model is not available")
            result = binding.client.start_turn(
                binding.session, prompt, model=model or None, effort=effort or None
            )
            binding.active_turn_id = _turn_id(result)
            if not binding.active_turn_id:
                raise CodexRuntimeError("Codex App Server did not return a turn id")
            return {**self._snapshot(binding), "start": result}

    def interrupt(self, workspace_id: str) -> dict[str, Any]:
        binding = self._connect(workspace_id)
        with binding.lock:
            if not binding.active_turn_id:
                raise CodexRuntimeError("no Codex App Server turn is active")
            result = binding.client.interrupt_turn(
                binding.session, binding.active_turn_id, wait_for_terminal=False
            )
            return {**self._snapshot(binding), "interrupt": result}

    @staticmethod
    def _snapshot(binding: _RuntimeBinding) -> dict[str, Any]:
        events = list(binding.client.events[-500:])
        turn = binding.client.turn_status.get(binding.active_turn_id, {})
        return {
            "configured": True,
            "connected": not binding.stop_event.is_set(),
            "error": binding.runtime_error,
            "workspace_id": binding.workspace_id,
            "thread_id": binding.session.thread_id,
            "session_id": binding.session.session_id,
            "active_turn_id": binding.active_turn_id,
            "turn": turn,
            "models": binding.models,
            "events": events,
            "account": binding.client.account,
            "usage": binding.client.usage,
            "mcp_servers": list(binding.client.mcp_status.values()),
        }


_RUNTIME = CodexAppServerRuntime()


def get_codex_app_server_runtime() -> CodexAppServerRuntime:
    """Return the process-wide production App Server runtime."""

    return _RUNTIME
