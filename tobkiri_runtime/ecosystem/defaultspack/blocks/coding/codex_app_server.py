"""First-class Codex App Server coding backend.

The backend speaks the official newline-delimited JSON-RPC protocol and is
deliberately separate from the OpenAI LLM provider.  It defaults to a read-only
workspace and fails closed for approvals, permission grants, and required MCP
startup failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Protocol


class CodexAppServerError(RuntimeError):
    """Base error for App Server transport and protocol failures."""

    pass


class ProtocolError(CodexAppServerError):
    """Raised when App Server violates the negotiated protocol."""

    pass


class RequestTimeout(CodexAppServerError):
    """Raised when a correlated App Server response does not arrive in time."""

    pass


class WorkspaceBoundaryError(ValueError):
    """Raised when an operation escapes its trusted workspace root."""

    pass


class ServerApprovalRequiredError(PermissionError):
    """Raised when a high-risk action lacks host-side authorization."""

    pass


class RequiredMcpServerError(CodexAppServerError):
    """Raised when a required MCP server is absent or not ready."""

    pass


HIGH_RISK_ACTIONS = {
    "file.write",
    "file.patch",
    "file.delete",
    "terminal.exec",
    "terminal.stream",
    "git.commit",
    "git.push",
    "network.access",
    "mcp.call",
}

_SECRET_KEYS = {
    "account_id",
    "access_token",
    "api_key",
    "authorization",
    "chatgpt_account_id",
    "headers",
    "id_token",
    "private_key",
    "refresh_token",
    "secret",
    "session_secret",
    "token",
}

_ALLOWED_COMMAND_DECISIONS = {
    "accept",
    "acceptForSession",
    "decline",
    "cancel",
}
_ALLOWED_FILE_DECISIONS = _ALLOWED_COMMAND_DECISIONS
_ACCOUNT_LOGIN_TYPES = {
    "apiKey",
    "chatgpt",
    "chatgptDeviceCode",
    "chatgptAuthTokens",
    "amazonBedrock",
}
_MAX_CACHED_RESPONSES = 1024


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def ensure_within_workspace(
    workspace_root: str | Path,
    target_path: str | Path | None = None,
) -> Path:
    root = _resolved(workspace_root)
    if not root.is_dir():
        raise WorkspaceBoundaryError(f"workspace root is not a directory: {root}")
    target = root if target_path in (None, "") else _resolved(target_path)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise WorkspaceBoundaryError(f"path is outside workspace root: {target}") from exc
    return target


def server_approval_granted(context: dict[str, Any] | None, action_id: str) -> bool:
    context = context if isinstance(context, dict) else {}
    approvals = context.get("server_approvals")
    if isinstance(approvals, dict) and bool(approvals.get(action_id)):
        return True
    return bool(
        context.get("_server_side_approved") and context.get("_approved_action_id") == action_id
    )


def require_server_approval(
    action_id: str,
    *,
    context: dict[str, Any] | None = None,
    client_supplied_approved: bool | None = None,
) -> None:
    del client_supplied_approved
    if action_id in HIGH_RISK_ACTIONS and not server_approval_granted(context, action_id):
        raise ServerApprovalRequiredError(f"server-side approval required for {action_id}")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if _normalized_key(key) in _SECRET_KEYS else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _normalized_key(value: Any) -> str:
    """Normalize snake, kebab, and camel case keys for secret redaction."""
    text = str(value)
    pieces: list[str] = []
    for character in text:
        if character.isupper() and pieces and pieces[-1] != "_":
            pieces.append("_")
        pieces.append(character.lower() if character.isalnum() else "_")
    return "_".join(part for part in "".join(pieces).split("_") if part)


def _safe_protocol_error(method: str, error: Any) -> str:
    """Return an actionable JSON-RPC failure without reflecting server secrets."""
    code = error.get("code") if isinstance(error, dict) else None
    suffix = f" (code {code})" if isinstance(code, int) else ""
    return f"{method} failed{suffix}"


def _permission_subset(requested: Any, granted: Any) -> bool:
    """Return whether a permission grant is structurally within the request."""
    if isinstance(requested, dict) and isinstance(granted, dict):
        return all(
            key in requested and _permission_subset(requested[key], value)
            for key, value in granted.items()
        )
    if isinstance(requested, list) and isinstance(granted, list):
        return all(item in requested for item in granted)
    return granted == requested


class AppServerTransport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...

    def receive(self, timeout: float) -> dict[str, Any]: ...

    def close(self) -> None: ...


class StdioJsonRpcTransport:
    """Cross-platform, shell-free Codex App Server process transport."""

    def __init__(
        self,
        command: list[str] | None = None,
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        max_frame_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        command = list(command or ["codex", "app-server"])
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("a non-empty argv list is required")
        creationflags = 0
        startupinfo: Any = None
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo_class = getattr(subprocess, "STARTUPINFO")
            startupinfo = startupinfo_class()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW")
        self._process = subprocess.Popen(
            command,
            cwd=str(_resolved(cwd)) if cwd else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
            shell=False,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        self._max_frame_bytes = int(max_frame_bytes)
        self._messages: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_drain = threading.Thread(target=self._drain_stderr, daemon=True)
        self._reader.start()
        self._stderr_drain.start()

    @property
    def pid(self) -> int:
        return int(self._process.pid)

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                if len(line.encode("utf-8")) > self._max_frame_bytes:
                    raise ProtocolError("app-server frame exceeds maximum size")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ProtocolError("malformed app-server JSON frame") from exc
                if not isinstance(message, dict):
                    raise ProtocolError("app-server frame must be a JSON object")
                self._messages.put(message)
        except BaseException as exc:
            self._messages.put(exc)
        finally:
            if self._process.poll() is not None:
                self._messages.put(
                    CodexAppServerError(
                        f"app-server process exited with code {self._process.returncode}"
                    )
                )

    def _drain_stderr(self) -> None:
        # Drain to prevent child-process backpressure. Never persist stderr: it
        # may contain paths, prompts, tool output, or credentials.
        if self._process.stderr is not None:
            for _line in self._process.stderr:
                pass

    def send(self, message: dict[str, Any]) -> None:
        if self._process.poll() is not None:
            raise CodexAppServerError("app-server process is not running")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            assert self._process.stdin is not None
            self._process.stdin.write(payload + "\n")
            self._process.stdin.flush()

    def receive(self, timeout: float) -> dict[str, Any]:
        try:
            value = self._messages.get(timeout=max(float(timeout), 0.001))
        except queue.Empty as exc:
            raise RequestTimeout("timed out waiting for app-server message") from exc
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self) -> None:
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=3)


AuthorityBridge = Callable[[str, dict[str, Any]], bool | dict[str, Any]]


@dataclass
class CodingSession:
    """Local binding between one trusted workspace and an App Server thread."""

    session_id: str
    workspace_root: str
    thread_id: str = ""
    profile: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)


class CodexAppServerClient:
    """Stateful v2 JSON-RPC client with an injectable transport."""

    def __init__(
        self,
        transport: AppServerTransport,
        *,
        authority: AuthorityBridge | None = None,
        timeout: float = 10.0,
        experimental_api: bool = False,
        required_mcp_servers: tuple[str, ...] = (),
    ) -> None:
        self.transport = transport
        self.authority = authority
        self.timeout = float(timeout)
        self.experimental_api = bool(experimental_api)
        self.required_mcp_servers = frozenset(
            str(name) for name in required_mcp_servers if str(name)
        )
        self.initialized = False
        self._next_id = 1
        self._responses: dict[int | str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.account: dict[str, Any] = {}
        self.rate_limits: dict[str, Any] = {}
        self.usage: dict[str, Any] = {}
        self.mcp_status: dict[str, dict[str, Any]] = {}
        self.thread_status: dict[str, dict[str, Any]] = {}
        self.turn_status: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, CodingSession] = {}
        self._event_sequence = 0

    def initialize(self, *, version: str = "1.0.0") -> dict[str, Any]:
        if self.initialized:
            raise ProtocolError("client is already initialized")
        params: dict[str, Any] = {
            "clientInfo": {
                "name": "tobkiri",
                "title": "Tobkiri",
                "version": str(version),
            }
        }
        params["capabilities"] = {
            "experimentalApi": self.experimental_api,
            "requestAttestation": False,
        }
        result = self._request("initialize", params, allow_uninitialized=True)
        self.transport.send({"method": "initialized", "params": {}})
        self.initialized = True
        return result

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        allow_uninitialized: bool = False,
    ) -> dict[str, Any]:
        if not self.initialized and not allow_uninitialized:
            raise ProtocolError("initialize must complete before requests")
        request_id = self._next_id
        self._next_id += 1
        self.transport.send({"method": method, "id": request_id, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            cached = self._responses.pop(request_id, None)
            if cached is not None:
                message = cached
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RequestTimeout(f"timed out waiting for {method}")
                message = self.transport.receive(remaining)
            if "id" in message and ("result" in message or "error" in message):
                if message["id"] != request_id:
                    if len(self._responses) >= _MAX_CACHED_RESPONSES:
                        raise ProtocolError("too many unclaimed app-server responses")
                    self._responses[message["id"]] = message
                    continue
                if "error" in message:
                    raise CodexAppServerError(_safe_protocol_error(method, message.get("error")))
                result = message.get("result", {})
                if not isinstance(result, dict):
                    raise ProtocolError(f"{method} returned a non-object result")
                return result
            self._dispatch_incoming(message)

    def _dispatch_incoming(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        if not method:
            raise ProtocolError("message has no method, result, or error")
        if "id" in message:
            self._handle_server_request(message)
            return
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        self._event_sequence += 1
        safe_event = {
            "sequence": self._event_sequence,
            "method": method,
            "params": _redact(params),
        }
        if isinstance(message.get("emittedAtMs"), int):
            safe_event["emitted_at_ms"] = message["emittedAtMs"]
        self.events.append(safe_event)
        if method == "account/updated":
            self.account = _redact(params)
        elif method == "account/rateLimits/updated":
            self.rate_limits = _redact(params)
        elif method in {"account/usage/updated", "thread/tokenUsage/updated"}:
            self.usage = _redact(params)
        elif method == "mcpServer/startupStatus/updated":
            name = str(params.get("name") or "")
            if name:
                self.mcp_status[name] = _redact(params)
        elif method == "thread/status/changed":
            thread_id = str(params.get("threadId") or "")
            if thread_id:
                self.thread_status[thread_id] = _redact(params.get("status") or {})
        elif method in {"turn/started", "turn/completed"}:
            turn = params.get("turn")
            if isinstance(turn, dict) and turn.get("id"):
                self.turn_status[str(turn["id"])] = _redact(turn)

    def _authority_decision(self, operation: str, params: dict[str, Any]) -> bool | dict[str, Any]:
        if self.authority is None:
            return False
        return self.authority(operation, _redact(params))

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        method = str(message.get("method") or "")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        result: dict[str, Any]
        if method == "item/commandExecution/requestApproval":
            operation = (
                "network.access"
                if isinstance(params.get("networkApprovalContext"), dict)
                else "terminal.exec"
            )
            decision = self._authority_decision(operation, params)
            result = {
                "decision": self._approval_decision(
                    decision,
                    params,
                    allowed=_ALLOWED_COMMAND_DECISIONS,
                )
            }
        elif method == "item/fileChange/requestApproval":
            decision = self._authority_decision("file.patch", params)
            result = {
                "decision": self._approval_decision(
                    decision,
                    params,
                    allowed=_ALLOWED_FILE_DECISIONS,
                )
            }
        elif method == "item/permissions/requestApproval":
            decision = self._authority_decision("permission.grant", params)
            requested = params.get("permissions")
            requested = requested if isinstance(requested, dict) else {}
            granted = decision.get("permissions", {}) if isinstance(decision, dict) else {}
            if not isinstance(granted, dict) or not _permission_subset(requested, granted):
                granted = {}
            scope = decision.get("scope") if isinstance(decision, dict) else None
            result = {
                "permissions": granted,
                "scope": scope if scope in {"turn", "session"} else "turn",
            }
        elif method in {
            "tool/requestUserInput",
            "item/tool/requestUserInput",
            "mcpServer/elicitation/request",
        }:
            decision = self._authority_decision("user.input", params)
            result = (
                decision if isinstance(decision, dict) else {"action": "decline", "content": None}
            )
        elif method == "account/chatgptAuthTokens/refresh":
            # External tokens are intentionally unsupported unless a host-owned
            # Authority bridge explicitly provides the whole response.
            decision = (
                self._authority_decision("auth.external_token_refresh", {})
                if self.experimental_api
                else False
            )
            result = decision if isinstance(decision, dict) else {"error": "unsupported"}
        else:
            self.transport.send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "unsupported server request",
                    },
                }
            )
            return
        self.transport.send({"id": request_id, "result": result})

    @staticmethod
    def _approval_decision(
        authority_result: bool | dict[str, Any],
        params: dict[str, Any],
        *,
        allowed: set[str],
    ) -> str | dict[str, Any]:
        available = params.get("availableDecisions")
        available_set = (
            {str(item) for item in available if isinstance(item, str)}
            if isinstance(available, list)
            else set(allowed)
        )
        if authority_result is True:
            candidate: str | dict[str, Any] = "accept"
        elif isinstance(authority_result, dict):
            candidate = authority_result.get("decision", "decline")
        else:
            candidate = "decline"
        if isinstance(candidate, str):
            if candidate in allowed and candidate in available_set:
                return candidate
            return "decline"
        amendment = candidate.get("acceptWithExecpolicyAmendment")
        if isinstance(amendment, dict) and "acceptWithExecpolicyAmendment" in available_set:
            values = amendment.get("execpolicy_amendment")
            if isinstance(values, list) and all(isinstance(item, str) for item in values):
                return {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": values}}
        return "decline"

    def poll(self, timeout: float = 0.01) -> dict[str, Any]:
        message = self.transport.receive(timeout)
        self._dispatch_incoming(message)
        return _redact(message)

    def _paged(self, method: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        request = dict(params or {})
        data: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            result = self._request(method, request)
            page = result.get("data", [])
            if not isinstance(page, list):
                raise ProtocolError(f"{method} data must be an array")
            data.extend(item for item in page if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if cursor in (None, ""):
                return data
            cursor = str(cursor)
            if cursor in seen:
                raise ProtocolError(f"{method} returned a repeated cursor")
            seen.add(cursor)
            request["cursor"] = cursor

    def list_models(self, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        models = self._paged("model/list", {"includeHidden": bool(include_hidden), "limit": 100})
        normalized = []
        for model in models:
            model_id = str(model.get("id") or model.get("model") or "")
            if not model_id:
                continue
            normalized.append(
                {
                    "id": model_id,
                    "model": str(model.get("model") or model_id),
                    "display_name": str(model.get("displayName") or model_id),
                    "hidden": bool(model.get("hidden", False)),
                    "default_reasoning_effort": model.get("defaultReasoningEffort"),
                    "supported_reasoning_efforts": list(
                        model.get("supportedReasoningEfforts") or []
                    ),
                    "input_modalities": list(model.get("inputModalities") or ["text", "image"]),
                    "supports_personality": bool(model.get("supportsPersonality", False)),
                    "is_default": bool(model.get("isDefault", False)),
                    "upgrade": model.get("upgrade"),
                    "upgrade_info": model.get("upgradeInfo"),
                }
            )
        return normalized

    def read_account(self, *, refresh_token: bool = False) -> dict[str, Any]:
        result = self._request("account/read", {"refreshToken": bool(refresh_token)})
        self.account = _redact(result)
        return self.account

    def start_account_login(self, login: dict[str, Any]) -> dict[str, Any]:
        """Start a supported auth flow without retaining supplied credentials."""
        login_type = str(login.get("type") or "")
        if login_type not in _ACCOUNT_LOGIN_TYPES:
            raise ValueError("unsupported App Server account login type")
        required_fields = {
            "apiKey": ("apiKey",),
            "chatgptAuthTokens": ("accessToken", "chatgptAccountId"),
            "amazonBedrock": ("apiKey", "region"),
        }.get(login_type, ())
        if any(not isinstance(login.get(key), str) or not login[key] for key in required_fields):
            raise ValueError(f"{login_type} login is missing a required field")
        if login_type == "chatgptAuthTokens" and not self.experimental_api:
            raise ProtocolError("externally managed ChatGPT tokens require experimentalApi")
        return _redact(self._request("account/login/start", dict(login)))

    def cancel_account_login(self, login_id: str) -> dict[str, Any]:
        """Cancel a pending App Server account login flow."""
        return self._request("account/login/cancel", {"loginId": str(login_id)})

    def logout_account(self) -> dict[str, Any]:
        """Log out the App Server account and clear cached account metadata."""
        result = self._request("account/logout", {})
        self.account = {}
        return result

    def read_rate_limits(self) -> dict[str, Any]:
        """Read the authenticated account's current rate-limit state."""
        self.rate_limits = _redact(self._request("account/rateLimits/read", {}))
        return self.rate_limits

    def read_usage(self) -> dict[str, Any]:
        """Read current account usage without retaining secret-bearing fields."""
        self.usage = _redact(self._request("account/usage/read", {}))
        return self.usage

    def read_model_provider_capabilities(self, **params: Any) -> dict[str, Any]:
        return self._request("modelProvider/capabilities/read", dict(params))

    def list_permission_profiles(self, *, cwd: str | Path) -> list[dict[str, Any]]:
        if not self.experimental_api:
            raise ProtocolError("permission profiles require experimentalApi")
        return self._paged("permissionProfile/list", {"cwd": str(_resolved(cwd)), "limit": 100})

    def list_mcp_status(self, *, detail: str = "toolsAndAuthOnly") -> list[dict[str, Any]]:
        items = self._paged("mcpServerStatus/list", {"detail": detail, "limit": 100})
        for item in items:
            name = str(item.get("name") or "")
            if name:
                self.mcp_status[name] = _redact(item)
        return [_redact(item) for item in items]

    def require_mcp_ready(self) -> None:
        statuses = self.list_mcp_status()
        seen: set[str] = set()
        for item in statuses:
            name = str(item.get("name") or "")
            if name:
                seen.add(name)
            status = str(item.get("status") or item.get("startupStatus") or "").lower()
            required = bool(item.get("required")) or name in self.required_mcp_servers
            if required and status not in {"ready", "connected", "initialized"}:
                raise RequiredMcpServerError(
                    f"required MCP server failed to initialize: {item.get('name', 'unknown')}"
                )
        missing = sorted(self.required_mcp_servers - seen)
        if missing:
            raise RequiredMcpServerError(f"required MCP server status is unavailable: {missing[0]}")

    def _thread_config(
        self,
        workspace_root: str | Path,
        *,
        permissions: str | None = None,
        sandbox: str | None = None,
        **overrides: Any,
    ) -> dict[str, Any]:
        cwd = str(ensure_within_workspace(workspace_root))
        if permissions and sandbox:
            raise ValueError("permissions and sandbox are mutually exclusive")
        if permissions and not self.experimental_api:
            raise ProtocolError("permission profiles require experimentalApi")
        params: dict[str, Any] = {
            "cwd": cwd,
            "approvalPolicy": "untrusted",
        }
        if permissions:
            params["permissions"] = permissions
        else:
            params["sandbox"] = sandbox or "read-only"
        allowed_overrides = {
            "model",
            "modelProvider",
            "effort",
            "personality",
            "config",
            "ephemeral",
        }
        unexpected = sorted(set(overrides) - allowed_overrides)
        if unexpected:
            raise ValueError(f"unsupported thread option: {unexpected[0]}")
        params.update({key: value for key, value in overrides.items() if value is not None})
        return params

    def start_thread(self, workspace_root: str | Path, **options: Any) -> CodingSession:
        self.require_mcp_ready()
        params = self._thread_config(workspace_root, **options)
        result = self._request("thread/start", params)
        return self._session_from_result(result, workspace_root, params)

    def resume_thread(
        self, thread_id: str, workspace_root: str | Path, **options: Any
    ) -> CodingSession:
        self.require_mcp_ready()
        params = self._thread_config(workspace_root, **options)
        params["threadId"] = str(thread_id)
        result = self._request("thread/resume", params)
        return self._session_from_result(result, workspace_root, params)

    def fork_thread(
        self,
        thread_id: str,
        workspace_root: str | Path,
        *,
        last_turn_id: str | None = None,
        **options: Any,
    ) -> CodingSession:
        self.require_mcp_ready()
        ensure_within_workspace(workspace_root)
        unexpected = sorted(set(options) - {"ephemeral"})
        if unexpected:
            raise ValueError(f"unsupported fork option: {unexpected[0]}")
        params: dict[str, Any] = {"threadId": str(thread_id)}
        if last_turn_id:
            params["lastTurnId"] = str(last_turn_id)
        if options.get("ephemeral") is not None:
            params["ephemeral"] = bool(options["ephemeral"])
        result = self._request("thread/fork", params)
        return self._session_from_result(
            result,
            workspace_root,
            {"cwd": str(_resolved(workspace_root)), **params},
        )

    def _session_from_result(
        self,
        result: dict[str, Any],
        workspace_root: str | Path,
        profile: dict[str, Any],
    ) -> CodingSession:
        thread = result.get("thread")
        if not isinstance(thread, dict) or not thread.get("id"):
            raise ProtocolError("thread response is missing thread.id")
        thread_id = str(thread["id"])
        session_id = str(thread.get("sessionId") or "")
        if not session_id:
            raise ProtocolError("thread response is missing thread.sessionId")
        session = CodingSession(
            session_id=session_id,
            thread_id=thread_id,
            workspace_root=str(_resolved(workspace_root)),
            profile=dict(profile),
        )
        self.sessions[thread_id] = session
        return session

    def list_threads(self, **filters: Any) -> list[dict[str, Any]]:
        return self._paged("thread/list", {"limit": 100, **filters})

    def read_thread(self, thread_id: str, *, include_turns: bool = True) -> dict[str, Any]:
        return self._request(
            "thread/read",
            {"threadId": str(thread_id), "includeTurns": bool(include_turns)},
        )

    def start_turn(
        self,
        session: CodingSession,
        text: str,
        *,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        ensure_within_workspace(session.workspace_root)
        params: dict[str, Any] = {
            "threadId": session.thread_id,
            "input": [{"type": "text", "text": str(text)}],
            "cwd": session.workspace_root,
            "approvalPolicy": "untrusted",
        }
        permission_profile = session.profile.get("permissions")
        if isinstance(permission_profile, str) and permission_profile:
            params["permissions"] = permission_profile
        else:
            params["sandboxPolicy"] = {
                "type": "readOnly",
                "networkAccess": False,
            }
        if model:
            params["model"] = str(model)
        if effort:
            params["effort"] = str(effort)
        return self._request("turn/start", params)

    def steer_turn(
        self,
        session: CodingSession,
        turn_id: str,
        text: str,
    ) -> dict[str, Any]:
        """Append input only to the expected active turn."""
        return self._request(
            "turn/steer",
            {
                "threadId": session.thread_id,
                "expectedTurnId": str(turn_id),
                "input": [{"type": "text", "text": str(text)}],
            },
        )

    def interrupt_turn(
        self,
        session: CodingSession,
        turn_id: str,
        *,
        wait_for_terminal: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Cancel a turn and, by default, wait for its terminal notification."""
        acknowledgement = self._request(
            "turn/interrupt",
            {"threadId": session.thread_id, "turnId": str(turn_id)},
        )
        if not wait_for_terminal:
            return acknowledgement
        return {
            "acknowledgement": acknowledgement,
            "turn": self.wait_for_turn(turn_id, timeout=timeout),
        }

    def wait_for_turn(
        self,
        turn_id: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Read authoritative events until the named turn becomes terminal."""
        deadline = time.monotonic() + (self.timeout if timeout is None else float(timeout))
        while True:
            known = self.turn_status.get(str(turn_id))
            if known and known.get("status") in {"completed", "interrupted", "failed"}:
                return dict(known)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RequestTimeout(f"timed out waiting for turn {turn_id}")
            self.poll(remaining)

    def reconnect(self, transport: AppServerTransport) -> dict[str, Any]:
        """Replace a failed transport and rehydrate known threads authoritatively."""
        try:
            self.transport.close()
        except Exception:
            pass
        self.transport = transport
        self.initialized = False
        self._responses.clear()
        self._next_id = 1
        initialize_result = self.initialize()
        refreshed: dict[str, Any] = {}
        for thread_id in tuple(self.sessions):
            refreshed[thread_id] = _redact(self.read_thread(thread_id, include_turns=True))
        return {
            "initialize": _redact(initialize_result),
            "threads": refreshed,
        }

    def close(self) -> None:
        self.transport.close()


class CodexAppServerBackend:
    """Compatibility facade used by the domain component entrypoint."""

    backend_id = "codex-app-server"

    def connect(
        self,
        *,
        command: list[str] | None = None,
        cwd: str | Path | None = None,
        authority: AuthorityBridge | None = None,
        timeout: float = 10.0,
        experimental_api: bool = False,
        required_mcp_servers: tuple[str, ...] = (),
    ) -> CodexAppServerClient:
        """Start a shell-free local App Server transport and initialize it."""
        transport = StdioJsonRpcTransport(command, cwd=cwd)
        client = CodexAppServerClient(
            transport,
            authority=authority,
            timeout=timeout,
            experimental_api=experimental_api,
            required_mcp_servers=required_mcp_servers,
        )
        try:
            client.initialize()
        except Exception:
            client.close()
            raise
        return client

    def create_session(
        self,
        workspace_root: str,
        profile: dict[str, Any] | None = None,
    ) -> CodingSession:
        root = ensure_within_workspace(workspace_root)
        return CodingSession(
            session_id="",
            workspace_root=str(root),
            profile=dict(profile or {}),
        )

    def send_user_input(
        self,
        session: CodingSession,
        message: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        event = {
            "type": "user_input",
            "session_id": session.session_id,
            "message": str(message or ""),
            "attachments": list(attachments or []),
        }
        session.events.append(event)
        return event

    def approve_action(
        self,
        session: CodingSession,
        action_id: str,
        approval: dict[str, Any],
    ) -> dict[str, Any]:
        event = {
            "type": "approval",
            "session_id": session.session_id,
            "action_id": str(action_id),
            "approved": False,
            "status": "pending_server_authority",
        }
        session.events.append(event)
        return event

    def validate_action(
        self,
        session: CodingSession,
        action_id: str,
        *,
        target_path: str | Path | None = None,
        context: dict[str, Any] | None = None,
        client_supplied_approved: bool | None = None,
    ) -> None:
        ensure_within_workspace(session.workspace_root, target_path)
        require_server_approval(
            action_id,
            context=context,
            client_supplied_approved=client_supplied_approved,
        )
