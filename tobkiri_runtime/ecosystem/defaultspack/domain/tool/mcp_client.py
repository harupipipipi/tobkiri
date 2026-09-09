"""
MCP (Model Context Protocol) クライアント実装。
JSON-RPC 2.0 準拠。stdio / SSE トランスポート対応。
外部ライブラリ不使用（標準ライブラリのみ）。
"""

import json
import os
import shlex
import subprocess
import threading
import time
import queue
import urllib.request
import urllib.error
from typing import Any, BinaryIO

from .mcp_sse_policy import SseEndpointPolicy, interrupt_sse_response
from .mcp_sse_events import SSE_EVENT_LIMIT, SSE_QUEUE_LIMIT, read_sse_events


_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "rumiai-defaults", "version": "0.1.0"}
_DEFAULT_TIMEOUT = 30
_STDIO_FRAME_LIMIT = 2 * 1024 * 1024
_STDIO_QUEUE_LIMIT = 16


def _normalize_stdio_command(command, args=None):
    if isinstance(command, (list, tuple)):
        parts = [str(part) for part in command if str(part)]
    else:
        command_text = str(command or "").strip()
        if not command_text:
            return []
        if args is not None or os.path.exists(command_text):
            parts = [command_text]
        else:
            parts = shlex.split(command_text, posix=os.name != "nt")
    if isinstance(args, (list, tuple)):
        parts.extend(str(arg) for arg in args)
    elif args is not None:
        parts.append(str(args))
    return parts


# ---------------------------------------------------------------------------
# Transport base
# ---------------------------------------------------------------------------
class _TransportBase:
    """トランスポート共通インターフェース"""

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def send(self, message_bytes: bytes, *, deadline: float | None = None) -> None:
        """JSON-RPC メッセージ（bytes）を送信する"""
        raise NotImplementedError

    def recv(self, timeout=_DEFAULT_TIMEOUT):
        """JSON-RPC メッセージ（dict）を受信する。タイムアウトで None を返す"""
        raise NotImplementedError

    @property
    def is_alive(self):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------
class _StdioTransport(_TransportBase):
    """サブプロセスの stdin/stdout で通信する stdio トランスポート"""

    def __init__(self, command, env=None, cwd=None):
        if isinstance(command, str):
            self._command = command.split()
        else:
            self._command = list(command)
        self._env = env
        self._cwd = cwd
        self._proc = None
        self._reader_thread = None
        self._stderr_thread = None
        self._writer_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._writes: queue.Queue[tuple[bytes, float, threading.Event]] = queue.Queue(1)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(_STDIO_QUEUE_LIMIT)
        self._stop_event = threading.Event()
        self._stdout_closed = threading.Event()
        self._failure: str | None = None
        self._started = False

    def start(self):
        if self._started:
            raise RuntimeError("stdio transport has already been started")
        self._started = True
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env,
            cwd=self._cwd,
        )
        self._reader_thread = threading.Thread(
            target=self._read_loop, args=(self._proc.stdout,), daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self._proc.stderr,), daemon=True,
        )
        self._writer_thread = threading.Thread(
            target=self._write_loop, args=(self._proc.stdin,), daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()
        self._writer_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._proc is not None:
            # Stop the child before closing buffered stdin: a concurrent blocked
            # writer can otherwise hold its buffer lock indefinitely.
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                # Do not discard ownership until the child has been reaped.
                # A failed kill/wait leaves the handle available for cleanup retry.
                self._proc.kill()
                self._proc.wait(timeout=5)
            deadline = time.monotonic() + 5
            for reader in (self._reader_thread, self._stderr_thread):
                if reader is not None and reader.ident is not None:
                    reader.join(timeout=max(0, deadline - time.monotonic()))
                    if reader.is_alive():
                        raise RuntimeError("stdio transport reader cleanup is incomplete")
            writer = self._writer_thread
            if writer is not None and writer.ident is not None:
                writer.join(timeout=max(0, deadline - time.monotonic()))
                if writer.is_alive():
                    raise RuntimeError("stdio transport writer cleanup is incomplete")
            for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
                if pipe is not None:
                    pipe.close()
            self._proc = None

    def send(self, message_bytes: bytes, *, deadline: float | None = None) -> None:
        """Write within the original request budget, retaining unfinished IO."""
        if deadline is None:
            deadline = time.monotonic() + _DEFAULT_TIMEOUT
        if len(message_bytes) + 1 > _STDIO_FRAME_LIMIT:
            raise ValueError("stdio transport request exceeds limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._write_lock.acquire(timeout=remaining):
            raise TimeoutError("Timeout waiting for the MCP writer")
        try:
            if self._stop_event.is_set():
                raise RuntimeError(self._failure or "stdio transport is closed")
            if self._proc is None or self._proc.stdin is None:
                raise RuntimeError("stdio transport not started")
            if time.monotonic() >= deadline:
                raise TimeoutError("Timeout waiting for the MCP writer")
            done = threading.Event()
            self._writes.put_nowait((message_bytes, deadline, done))
            while not done.wait(max(0, min(deadline - time.monotonic(), 0.05))):
                if time.monotonic() >= deadline:
                    self._fail("stdio transport write deadline exceeded")
                    raise TimeoutError("stdio transport write deadline exceeded")
                if self._stop_event.is_set():
                    raise RuntimeError(self._failure or "stdio transport is closed")
            if time.monotonic() >= deadline:
                self._fail("stdio transport write deadline exceeded")
                raise TimeoutError("stdio transport write deadline exceeded")
            if self._stop_event.is_set():
                raise RuntimeError(self._failure or "stdio transport is closed")
        finally:
            self._write_lock.release()

    def _write_loop(self, stdin: BinaryIO) -> None:
        while not self._stop_event.is_set():
            try:
                message_bytes, deadline, done = self._writes.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if time.monotonic() >= deadline:
                    self._fail("stdio transport write deadline exceeded")
                elif not self._stop_event.is_set():
                    stdin.write(message_bytes + b"\n")
                    stdin.flush()
            except (OSError, ValueError):
                self._fail("stdio transport write failed")
            finally:
                done.set()

    def recv(self, timeout=_DEFAULT_TIMEOUT):
        deadline = time.monotonic() + timeout
        while True:
            if self._failure is not None:
                raise RuntimeError(self._failure)
            remaining = deadline - time.monotonic()
            try:
                message = self._queue.get(timeout=max(0, min(remaining, 0.05)))
            except queue.Empty:
                if self._stdout_closed.is_set() or self._stop_event.is_set():
                    if self._failure is not None:
                        raise RuntimeError(self._failure)
                    raise RuntimeError("stdio transport is closed")
                if remaining <= 0:
                    return None
            else:
                if self._failure is not None:
                    raise RuntimeError(self._failure)
                return message

    @property
    def is_alive(self):
        return self._proc is not None and self._proc.poll() is None

    # -- internal --

    def _read_loop(self, stdout: BinaryIO) -> None:
        try:
            while not self._stop_event.is_set():
                # Include the newline in the frame budget. Never allocate an
                # unbounded line before deciding whether it is acceptable.
                line = stdout.readline(_STDIO_FRAME_LIMIT + 1)
                if not line:
                    break
                if len(line) > _STDIO_FRAME_LIMIT:
                    self._fail("stdio transport frame exceeds limit")
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise ValueError("invalid message")
                    self._queue.put_nowait(msg)
                except (ValueError, RecursionError):
                    self._fail("stdio transport message is invalid")
                    break
                except queue.Full:
                    self._fail("stdio transport receive queue exceeds limit")
                    break
        except (OSError, ValueError):
            if not self._stop_event.is_set():
                self._fail("stdio transport read failed")
        finally:
            self._stdout_closed.set()

    def _drain_stderr(self, stderr: BinaryIO) -> None:
        try:
            # Diagnostics may contain secrets. Drain bounded chunks without
            # retaining them or allowing a full pipe to block protocol replies.
            while not self._stop_event.is_set() and stderr.read(64 * 1024):
                pass
        except (OSError, ValueError):
            if not self._stop_event.is_set():
                self._fail("stdio transport diagnostic read failed")

    def _fail(self, reason: str) -> None:
        self._failure = reason
        self._stop_event.set()
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
        # Keep the process and reader handles: only stop() confirms their cleanup.


# ---------------------------------------------------------------------------
# SSE transport
# ---------------------------------------------------------------------------
class _SseTransport(_TransportBase):
    """HTTP SSE トランスポート"""

    def __init__(self, url, headers=None):
        self._sse_url = url
        self._endpoint_policy = SseEndpointPolicy(url)
        self._extra_headers = headers or {}
        self._post_url = None
        self._reader_thread = None
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(SSE_QUEUE_LIMIT)
        self._stop_event = threading.Event()
        self._response = None
        self._ready_event = threading.Event()
        self._failure: str | None = None
        self._started = False

    def start(self):
        if self._started:
            raise RuntimeError("SSE transport has already been started")
        self._started = True
        self._stop_event.clear()
        self._ready_event.clear()
        self._reader_thread = threading.Thread(target=self._sse_loop, daemon=True)
        self._reader_thread.start()
        if not self._ready_event.wait(timeout=_DEFAULT_TIMEOUT):
            raise RuntimeError("SSE transport: failed to receive endpoint event within timeout")
        if self._failure is not None or self._post_url is None:
            raise RuntimeError(self._failure or "MCP SSE endpoint is unavailable")

    def stop(self):
        self._stop_event.set()
        response = self._response
        if response is not None:
            interrupt_sse_response(response)
        reader = self._reader_thread
        if reader is not None and reader.ident is not None:
            reader.join(timeout=5)
            if reader.is_alive():
                raise RuntimeError("MCP SSE reader did not stop")
        # A late opener may publish its response while stop waits for the reader.
        # Only release the current response after that reader actually exits.
        if self._response is not None:
            self._response.close()
            self._response = None

    def send(self, message_bytes: bytes, *, deadline: float | None = None) -> None:
        deadline = time.monotonic() + _DEFAULT_TIMEOUT if deadline is None else deadline
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timeout waiting for the MCP writer")
        if len(message_bytes) > SSE_EVENT_LIMIT:
            raise ValueError("MCP SSE request exceeds the byte limit")
        if self._failure is not None or self._stop_event.is_set():
            raise RuntimeError(self._failure or "MCP SSE connection closed")
        if self._post_url is None:
            raise RuntimeError("SSE transport: post URL not yet received")
        req = urllib.request.Request(
            self._post_url,
            data=message_bytes,
            headers={
                "Content-Type": "application/json",
                **self._extra_headers,
            },
            method="POST",
        )
        try:
            with self._endpoint_policy.open(req, timeout=remaining) as resp:
                if len(resp.read(SSE_EVENT_LIMIT + 1)) > SSE_EVENT_LIMIT:
                    self._failure = "MCP SSE POST response exceeds the byte limit"
                    raise RuntimeError(self._failure)
            if time.monotonic() >= deadline:
                self._failure = "MCP SSE POST deadline exceeded"
                raise TimeoutError(self._failure)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise RuntimeError("MCP SSE POST failed") from None

    def recv(self, timeout=_DEFAULT_TIMEOUT):
        deadline = time.monotonic() + timeout
        while True:
            if self._failure is not None:
                raise RuntimeError(self._failure)
            remaining = deadline - time.monotonic()
            try:
                message = self._queue.get(timeout=max(0, min(remaining, 0.05)))
            except queue.Empty:
                if not self.is_alive or self._stop_event.is_set():
                    raise RuntimeError(self._failure or "MCP SSE connection closed")
                if remaining <= 0:
                    return None
            else:
                if self._failure is not None:
                    raise RuntimeError(self._failure)
                return message

    @property
    def is_alive(self):
        return self._reader_thread is not None and self._reader_thread.is_alive()

    # -- internal --

    def _sse_loop(self):
        try:
            req = urllib.request.Request(
                self._sse_url,
                headers={
                    "Accept": "text/event-stream",
                    **self._extra_headers,
                },
            )
            self._response = self._endpoint_policy.open(req, timeout=None)
            if self._stop_event.is_set():
                return
            for event_type, data_str in read_sse_events(self._response):
                if self._stop_event.is_set():
                    break
                self._handle_event(event_type, data_str)
        except Exception:
            self._failure = "MCP SSE connection failed"
        finally:
            self._ready_event.set()
            if self._response is not None:
                try:
                    self._response.close()
                except Exception:
                    pass

    def _handle_event(self, event_type, data_str):
        if event_type == "endpoint":
            self._post_url = self._endpoint_policy.resolve_endpoint(data_str)
            self._ready_event.set()
        elif event_type == "message" or event_type == "":
            try:
                msg = json.loads(data_str)
            except (ValueError, RecursionError):
                raise ValueError("MCP SSE response is not valid JSON") from None
            if not isinstance(msg, dict):
                raise ValueError("MCP SSE response must be an object")
            try:
                self._queue.put_nowait(msg)
            except queue.Full:
                raise ValueError("MCP SSE response queue is full") from None


# ---------------------------------------------------------------------------
# _ServerConnection — 1 つの MCP サーバーとの接続を管理
# ---------------------------------------------------------------------------
class _ServerConnection:
    """個別の MCP サーバー接続"""

    def __init__(self, server_name, config):
        self.server_name = server_name
        self.config = config
        self.status = "disconnected"
        self.tools = []
        self.server_capabilities = {}
        self._transport: _TransportBase | None = None
        self._id_counter = 0
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()

    # -- public API --

    def connect(self):
        # The owner already resolved and approved this snapshot. Expanding it
        # again could replace literal ${...} values after approval verification.
        transport_type = self.config.get("transport", "stdio")
        if transport_type == "stdio":
            command = self.config.get("command")
            command_args = self.config.get("args")
            command_parts = _normalize_stdio_command(command, command_args)
            if not command_parts:
                raise ValueError("stdio transport requires 'command' in config")
            env = os.environ.copy()
            config_env = self.config.get("env")
            if isinstance(config_env, dict):
                for key, value in config_env.items():
                    env[str(key)] = str(value)
            cwd = self.config.get("cwd")
            self._transport = _StdioTransport(command_parts, env=env, cwd=cwd)
        elif transport_type == "sse":
            url = self.config.get("url")
            if not url:
                raise ValueError("sse transport requires 'url' in config")
            headers = self.config.get("headers")
            self._transport = _SseTransport(url, headers=headers)
        else:
            raise ValueError("Unknown transport type: {}".format(transport_type))

        try:
            self._transport.start()
            self._initialize()
            self.tools = self._list_tools()
        except Exception:
            self.disconnect()
            self.status = "error"
            raise
        self.status = "connected"
        return len(self.tools)

    def disconnect(self):
        if self._transport is not None:
            self._transport.stop()
            self._transport = None
        self.status = "disconnected"
        self.tools = []
        self.server_capabilities = {}

    def reconnect(self):
        self.disconnect()
        return self.connect()

    def call_tool(self, tool_name, arguments):
        resp = self._send_request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments or {},
            },
        )
        if "error" in resp:
            return {
                "result": resp["error"].get("message", "MCP error"),
                "is_error": True,
                "widget": None,
            }
        result_obj = resp.get("result", {})
        content_list = result_obj.get("content", [])
        text_parts = []
        for c in content_list:
            if c.get("type") == "text":
                text_parts.append(c.get("text", ""))
            else:
                text_parts.append(json.dumps(c))
        is_error = result_obj.get("isError", False)
        return {
            "result": "\n".join(text_parts) if text_parts else "",
            "is_error": is_error,
            "widget": None,
        }

    # -- internal --

    def _next_id(self):
        with self._lock:
            self._id_counter += 1
            return self._id_counter

    def _send_request(self, method, params=None):
        # Each transport has one response queue. Keep a request and its response
        # together so another caller cannot consume and discard that response.
        transport = self._transport
        if transport is None:
            raise RuntimeError("Not connected to server '{}'".format(self.server_name))
        deadline = time.monotonic() + _DEFAULT_TIMEOUT
        if not self._request_lock.acquire(timeout=_DEFAULT_TIMEOUT):
            raise TimeoutError("Timeout waiting for the MCP connection")
        try:
            if self._transport is not transport:
                raise RuntimeError("MCP connection changed while waiting")
            if time.monotonic() >= deadline:
                raise TimeoutError("Timeout waiting for the MCP connection")
            msg_id = self._next_id()
            request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
            if params is not None:
                request["params"] = params
            transport.send(json.dumps(request).encode("utf-8"), deadline=deadline)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timeout waiting for the MCP response")
                msg = transport.recv(timeout=remaining)
                if msg is None or time.monotonic() >= deadline:
                    raise TimeoutError("Timeout waiting for the MCP response")
                if self._transport is not transport:
                    raise RuntimeError("MCP connection changed while waiting")
                if msg.get("id") == msg_id:
                    return msg
                # Notifications and late replies to timed-out requests do not
                # belong to this call. Never replay a request after timeout.
        finally:
            self._request_lock.release()

    def _send_notification(self, method, params=None):
        if self._transport is None:
            return
        notification = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            notification["params"] = params
        raw = json.dumps(notification).encode("utf-8")
        self._transport.send(raw)

    def _initialize(self):
        resp = self._send_request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        if "error" in resp:
            raise RuntimeError(
                "MCP initialize failed: {}".format(resp["error"].get("message", "unknown error"))
            )
        result = resp.get("result", {})
        self.server_capabilities = result.get("capabilities", {})
        self._send_notification("initialized")

    def _list_tools(self):
        if "tools" not in self.server_capabilities:
            return []
        resp = self._send_request("tools/list")
        if "error" in resp:
            return []
        result = resp.get("result", {})
        return result.get("tools", [])


# ---------------------------------------------------------------------------
# Explicitly owned connection collection and legacy singleton compatibility
# ---------------------------------------------------------------------------
class McpConnections:
    """One owner's connections; construction never accesses the legacy pool.

    This class does not authorize connection configuration or tool invocation.
    Its owner must obtain those permissions through the captured Host boundary.
    """

    def __init__(self) -> None:
        self._servers: dict[str, _ServerConnection] = {}
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        """Fence new work and reap owned connections, retaining failed cleanup."""
        with self._lock:
            self._closed = True
            failed = False
            for name, connection in list(self._servers.items()):
                try:
                    connection.disconnect()
                except Exception:
                    failed = True
                else:
                    del self._servers[name]
            if failed:
                raise RuntimeError("MCP connection cleanup is incomplete")

    def connect(self, server_name, config):
        """
        所有元が解決・承認した設定で MCP サーバーに接続する。
        接続時・再接続時に設定の環境変数展開は行わない。
        config:
            transport: "stdio" | "sse"
            command: str or list  (stdio 用)
            env: dict|None        (stdio 用、省略可)
            url: str              (sse 用)
            headers: dict|None    (sse 用、省略可)
        戻り値: 追加されたツール数 (int)
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP connection owner is closed")
            if server_name in self._servers:
                self._servers[server_name].disconnect()
            conn = _ServerConnection(server_name, config)
            # Publish ownership before startup, and serialize replacement with
            # disconnect/reconnect. A failed startup may already own a child.
            self._servers[server_name] = conn
            try:
                return conn.connect()
            except Exception as exc:
                conn.status = "error"
                conn.tools = []
                raise RuntimeError("Failed to connect to MCP server '{}': {}".format(server_name, exc)) from exc

    def disconnect(self, server_name):
        """MCP サーバーから切断する"""
        with self._lock:
            conn = self._servers.get(server_name)
            if conn is not None:
                conn.disconnect()
                del self._servers[server_name]

    def reconnect(self, server_name):
        """MCP サーバーに再接続する"""
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP connection owner is closed")
            conn = self._servers.get(server_name)
            if conn is None:
                raise RuntimeError("MCP server '{}' not found".format(server_name))
            return conn.reconnect()

    def list_servers(self):
        """
        接続中サーバー一覧を返す。
        戻り値: [{"name": str, "status": str, "tools": [str]}, ...]
        """
        with self._lock:
            servers = list(self._servers.values())
        result: list[dict[str, Any]] = []
        for conn in servers:
            tool_names = []
            for t in conn.tools:
                if isinstance(t, dict):
                    tool_names.append(t.get("name", ""))
                else:
                    tool_names.append(str(t))
            result.append(
                {
                    "name": conn.server_name,
                    "status": conn.status,
                    "tools": tool_names,
                }
            )
        return result

    def get_server_tools(self, server_name):
        """
        指定サーバーのツール定義一覧を返す。
        戻り値: [{"name": str, "description": str, "inputSchema": dict}, ...]
        """
        with self._lock:
            conn = self._servers.get(server_name)
        if conn is None:
            return []
        return list(conn.tools)

    def invoke(self, server_name, tool_name, arguments):
        """
        MCP ツール実行。
        戻り値: {"result": str, "is_error": bool, "widget": dict|None}
        """
        with self._lock:
            if self._closed:
                return {"result": "MCP connection owner is closed", "is_error": True, "widget": None}
            conn = self._servers.get(server_name)
        if conn is None:
            return {
                "result": "MCP server '{}' not connected".format(server_name),
                "is_error": True,
                "widget": None,
            }
        if conn.status != "connected":
            return {
                "result": "MCP server '{}' status: {}".format(server_name, conn.status),
                "is_error": True,
                "widget": None,
            }
        try:
            return conn.call_tool(tool_name, arguments)
        except Exception as exc:
            return {
                "result": "MCP call failed: {}".format(exc),
                "is_error": True,
                "widget": None,
            }


class McpClient(McpConnections):
    """Legacy process-wide pool retained until callers migrate to an owner."""

    _instance = None
    _initialized: bool

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if not self._initialized:
            super().__init__()
            self._initialized = True
