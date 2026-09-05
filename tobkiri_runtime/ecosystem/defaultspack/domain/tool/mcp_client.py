"""
MCP (Model Context Protocol) クライアント実装。
JSON-RPC 2.0 準拠。stdio / SSE トランスポート対応。
外部ライブラリ不使用（標準ライブラリのみ）。
"""

import json
import os
import re
import shlex
import subprocess
import threading
import time
import queue
import urllib.request
import urllib.error
from typing import Any


_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "rumiai-defaults", "version": "0.1.0"}
_DEFAULT_TIMEOUT = 30
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_placeholders(value):
    if isinstance(value, str):
        return _PLACEHOLDER_RE.sub(
            lambda match: os.environ.get(match.group(1), ""),
            value,
        )
    if isinstance(value, list):
        return [_expand_placeholders(item) for item in value]
    if isinstance(value, tuple):
        return [_expand_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _expand_placeholders(item) for key, item in value.items()}
    return value


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

    def send(self, message_bytes):
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
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stop_event = threading.Event()

    def start(self):
        self._stop_event.clear()
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env,
            cwd=self._cwd,
        )
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def send(self, message_bytes):
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("stdio transport not started")
        self._proc.stdin.write(message_bytes + b"\n")
        self._proc.stdin.flush()

    def recv(self, timeout=_DEFAULT_TIMEOUT):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def is_alive(self):
        return self._proc is not None and self._proc.poll() is None

    # -- internal --

    def _read_loop(self):
        try:
            while not self._stop_event.is_set():
                if self._proc is None or self._proc.stdout is None:
                    break
                line = self._proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    self._queue.put(msg)
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SSE transport
# ---------------------------------------------------------------------------
class _SseTransport(_TransportBase):
    """HTTP SSE トランスポート"""

    def __init__(self, url, headers=None):
        self._sse_url = url
        self._extra_headers = headers or {}
        self._post_url = None
        self._reader_thread = None
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stop_event = threading.Event()
        self._response = None
        self._ready_event = threading.Event()

    def start(self):
        self._stop_event.clear()
        self._ready_event.clear()
        self._reader_thread = threading.Thread(target=self._sse_loop, daemon=True)
        self._reader_thread.start()
        if not self._ready_event.wait(timeout=_DEFAULT_TIMEOUT):
            raise RuntimeError("SSE transport: failed to receive endpoint event within timeout")

    def stop(self):
        self._stop_event.set()
        if self._response is not None:
            try:
                self._response.close()
            except Exception:
                pass
            self._response = None

    def send(self, message_bytes):
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
            with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError("SSE POST failed: {} {}".format(exc.code, exc.reason))

    def recv(self, timeout=_DEFAULT_TIMEOUT):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

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
            self._response = urllib.request.urlopen(req, timeout=None)
            event_type = ""
            data_buf = []
            for raw_line in self._response:
                if self._stop_event.is_set():
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                if line.startswith("event:"):
                    event_type = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_buf.append(line[len("data:") :].strip())
                elif line == "":
                    if data_buf:
                        data_str = "\n".join(data_buf)
                        self._handle_event(event_type, data_str)
                    event_type = ""
                    data_buf = []
        except Exception:
            pass
        finally:
            if self._response is not None:
                try:
                    self._response.close()
                except Exception:
                    pass

    def _handle_event(self, event_type, data_str):
        if event_type == "endpoint":
            self._post_url = data_str.strip()
            self._ready_event.set()
        elif event_type == "message" or event_type == "":
            try:
                msg = json.loads(data_str)
                self._queue.put(msg)
            except json.JSONDecodeError:
                pass


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
        self._transport = None
        self._id_counter = 0
        self._lock = threading.Lock()

    # -- public API --

    def connect(self):
        transport_type = self.config.get("transport", "stdio")
        if transport_type == "stdio":
            command = _expand_placeholders(self.config.get("command"))
            command_args = _expand_placeholders(self.config.get("args"))
            command_parts = _normalize_stdio_command(command, command_args)
            if not command_parts:
                raise ValueError("stdio transport requires 'command' in config")
            env = os.environ.copy()
            config_env = _expand_placeholders(self.config.get("env"))
            if isinstance(config_env, dict):
                for key, value in config_env.items():
                    env[str(key)] = str(value)
            cwd = _expand_placeholders(self.config.get("cwd"))
            self._transport = _StdioTransport(command_parts, env=env, cwd=cwd)
        elif transport_type == "sse":
            url = _expand_placeholders(self.config.get("url"))
            if not url:
                raise ValueError("sse transport requires 'url' in config")
            headers = _expand_placeholders(self.config.get("headers"))
            self._transport = _SseTransport(url, headers=headers)
        else:
            raise ValueError("Unknown transport type: {}".format(transport_type))

        self._transport.start()
        self._initialize()
        self.tools = self._list_tools()
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
        if self._transport is None:
            raise RuntimeError("Not connected to server '{}'".format(self.server_name))
        msg_id = self._next_id()
        request = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params
        raw = json.dumps(request).encode("utf-8")
        self._transport.send(raw)
        deadline = time.monotonic() + _DEFAULT_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "Timeout waiting for response to '{}' (id={})".format(method, msg_id)
                )
            msg = self._transport.recv(timeout=remaining)
            if msg is None:
                raise TimeoutError(
                    "Timeout waiting for response to '{}' (id={})".format(method, msg_id)
                )
            if msg.get("id") == msg_id:
                return msg
            # else: notification or response to different id — ignore for now

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
# McpClient — シングルトン
# ---------------------------------------------------------------------------
class McpClient:
    """
    MCP クライアント（シングルトン）。
    複数の MCP サーバー接続を管理する。
    """

    _instance = None
    _initialized: bool

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._servers = {}
        self._lock = threading.Lock()

    def connect(self, server_name, config):
        """
        MCP サーバーに接続する。
        config:
            transport: "stdio" | "sse"
            command: str or list  (stdio 用)
            env: dict|None        (stdio 用、省略可)
            url: str              (sse 用)
            headers: dict|None    (sse 用、省略可)
        戻り値: 追加されたツール数 (int)
        """
        with self._lock:
            if server_name in self._servers:
                self._servers[server_name].disconnect()
            conn = _ServerConnection(server_name, config)
        try:
            tools_added = conn.connect()
        except Exception as exc:
            conn.status = "error"
            conn.tools = []
            with self._lock:
                self._servers[server_name] = conn
            raise RuntimeError("Failed to connect to MCP server '{}': {}".format(server_name, exc))
        with self._lock:
            self._servers[server_name] = conn
        return tools_added

    def disconnect(self, server_name):
        """MCP サーバーから切断する"""
        with self._lock:
            conn = self._servers.pop(server_name, None)
        if conn is not None:
            conn.disconnect()

    def reconnect(self, server_name):
        """MCP サーバーに再接続する"""
        with self._lock:
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
