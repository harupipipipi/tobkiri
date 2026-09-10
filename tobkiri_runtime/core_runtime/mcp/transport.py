"""
MCP (Model Context Protocol) クライアント実装。
JSON-RPC 2.0 準拠。stdio / SSE トランスポート対応。
外部ライブラリ不使用（標準ライブラリのみ）。
"""

import json
import os
import signal
import shlex
import subprocess
import threading
import time
import queue
import urllib.request
import urllib.error
from typing import Any, BinaryIO

from .sse_policy import (
    SseEndpointPolicy, allow_idle_sse_response, interrupt_sse_response,
)
from .sse_events import SSE_EVENT_LIMIT, SSE_QUEUE_LIMIT, read_sse_events


_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "tobkiri", "version": "0.1.0"}
_DEFAULT_TIMEOUT = 30
_STDIO_FRAME_LIMIT = 2 * 1024 * 1024
_STDIO_QUEUE_LIMIT = 16


def _normalize_stdio_command(command, args=None):
    if isinstance(command, (list, tuple)):
        parts = [str(part) for part in command]
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

    def send(
        self, message_bytes: bytes, *, deadline: float | None = None,
        cancellation: threading.Event | None = None,
    ) -> None:
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
        self._process_group_id: int | None = None
        self._reader_thread = None
        self._stderr_thread = None
        self._writer_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._writes: queue.Queue[
            tuple[bytes, float, threading.Event | None, threading.Event]
        ] = queue.Queue(1)
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
            start_new_session=os.name == "posix",
        )
        if os.name == "posix":
            self._process_group_id = self._proc.pid
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
                if self._process_group_id is None:
                    self._proc.terminate()
                else:
                    self._signal_process_group(signal.SIGTERM)
                self._proc.wait(timeout=2)
            except Exception:
                # Do not discard ownership until the child has been reaped.
                # A failed kill/wait leaves the handle available for cleanup retry.
                if self._process_group_id is None:
                    self._proc.kill()
                else:
                    self._signal_process_group(signal.SIGKILL)
                self._proc.wait(timeout=5)
            if self._process_group_id is not None and self._process_group_alive():
                self._signal_process_group(signal.SIGKILL)
                deadline = time.monotonic() + 5
                while self._process_group_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if self._process_group_alive():
                    raise RuntimeError("stdio transport process-group cleanup is incomplete")
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
            self._process_group_id = None

    def _signal_process_group(self, requested_signal: int) -> None:
        process_group_id = self._process_group_id
        if process_group_id is None:
            raise RuntimeError("stdio transport process group is unavailable")
        try:
            os.killpg(process_group_id, requested_signal)
        except ProcessLookupError:
            return
        except PermissionError:
            # macOS may report EPERM for an empty process group after its
            # already-reaped session leader exits. Never suppress EPERM while
            # the owned leader is still live.
            if self._proc is not None and self._proc.poll() is not None:
                return
            raise

    def _process_group_alive(self) -> bool:
        process_group_id = self._process_group_id
        if process_group_id is None:
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            if self._proc is not None and self._proc.poll() is not None:
                return False
            raise
        return True

    def send(
        self, message_bytes: bytes, *, deadline: float | None = None,
        cancellation: threading.Event | None = None,
    ) -> None:
        """Write within the original request budget, retaining unfinished IO."""
        if deadline is None:
            deadline = time.monotonic() + _DEFAULT_TIMEOUT
        if len(message_bytes) + 1 > _STDIO_FRAME_LIMIT:
            raise ValueError("stdio transport request exceeds limit")
        while True:
            _check_request_lifetime(
                deadline, cancellation, timeout_message="Timeout waiting for the MCP writer",
            )
            if self._stop_event.is_set():
                raise RuntimeError(self._failure or "stdio transport is closed")
            if self._write_lock.acquire(timeout=max(0, min(0.05, deadline - time.monotonic()))):
                break
        queued = False
        try:
            _check_request_lifetime(
                deadline, cancellation, timeout_message="Timeout waiting for the MCP writer",
            )
            if self._stop_event.is_set():
                raise RuntimeError(self._failure or "stdio transport is closed")
            if self._proc is None or self._proc.stdin is None:
                raise RuntimeError("stdio transport not started")
            done = threading.Event()
            self._writes.put_nowait((message_bytes, deadline, cancellation, done))
            queued = True
            while not done.wait(max(0, min(deadline - time.monotonic(), 0.05))):
                _check_request_lifetime(
                    deadline, cancellation, timeout_message="stdio transport write deadline exceeded",
                )
                if self._stop_event.is_set():
                    raise RuntimeError(self._failure or "stdio transport is closed")
            _check_request_lifetime(
                deadline, cancellation, timeout_message="stdio transport write deadline exceeded",
            )
            if self._stop_event.is_set():
                raise RuntimeError(self._failure or "stdio transport is closed")
        except (TimeoutError, InterruptedError) as error:
            # Once enqueued, a partial frame may already have reached the child.
            # Fence and retain it for stop(); never replay an uncertain write.
            if queued:
                self._fail(
                    "stdio transport write cancelled" if isinstance(error, InterruptedError)
                    else "stdio transport write deadline exceeded"
                )
            raise
        finally:
            self._write_lock.release()

    def _write_loop(self, stdin: BinaryIO) -> None:
        while not self._stop_event.is_set():
            try:
                message_bytes, deadline, cancellation, done = self._writes.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                _check_request_lifetime(deadline, cancellation)
                if not self._stop_event.is_set():
                    stdin.write(message_bytes + b"\n")
                    stdin.flush()
            except (TimeoutError, InterruptedError) as error:
                self._fail(
                    "stdio transport write cancelled" if isinstance(error, InterruptedError)
                    else "stdio transport write deadline exceeded"
                )
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
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._post_thread: threading.Thread | None = None
        self._post_response: Any = None
        self._post_error: Exception | None = None
        self._post_done = threading.Event()
        self._startup_deadline: float | None = None

    def start(self, *, deadline=None, cancellation=None):
        deadline = min(
            deadline if deadline is not None else float("inf"),
            time.monotonic() + _DEFAULT_TIMEOUT,
        )
        _check_request_lifetime(deadline, cancellation)
        with self._state_lock:
            if self._started:
                raise RuntimeError("SSE transport has already been started")
            if self._stop_event.is_set():
                raise RuntimeError("MCP SSE connection closed")
            self._started = True
            self._startup_deadline = deadline
            self._reader_thread = threading.Thread(target=self._sse_loop, daemon=True)
            self._reader_thread.start()
        try:
            while not self._ready_event.wait(max(0, min(0.05, deadline - time.monotonic()))):
                _check_request_lifetime(deadline, cancellation)
                if self._stop_event.is_set():
                    raise RuntimeError(self._failure or "MCP SSE connection closed")
            _check_request_lifetime(deadline, cancellation)
        except (TimeoutError, InterruptedError):
            self._fail("MCP SSE startup interrupted")
            raise
        if self._failure is not None or self._stop_event.is_set() or self._post_url is None:
            raise RuntimeError(
                self._failure or (
                    "MCP SSE connection closed" if self._stop_event.is_set()
                    else "MCP SSE endpoint is unavailable"
                )
            )

    def stop(self):
        self._interrupt()
        deadline = time.monotonic() + 5
        for label, worker in (("reader", self._reader_thread), ("POST worker", self._post_thread)):
            if worker is not None and worker.ident is not None:
                worker.join(timeout=max(0, deadline - time.monotonic()))
                if worker.is_alive():
                    raise RuntimeError(f"MCP SSE {label} did not stop")
        # A late opener may publish its response while stop waits for the reader.
        # Only release IO after every worker actually exits.
        if self._response is not None:
            self._response.close()
            self._response = None
        if self._post_response is not None:
            self._post_response.close()
            self._post_response = None
        self._post_thread = None
        self._post_error = None

    def send(
        self, message_bytes: bytes, *, deadline: float | None = None,
        cancellation: threading.Event | None = None,
    ) -> None:
        deadline = time.monotonic() + _DEFAULT_TIMEOUT if deadline is None else deadline
        if len(message_bytes) > SSE_EVENT_LIMIT:
            raise ValueError("MCP SSE request exceeds the byte limit")
        while True:
            _check_request_lifetime(deadline, cancellation)
            if self._stop_event.is_set():
                raise RuntimeError(self._failure or "MCP SSE connection closed")
            if self._write_lock.acquire(timeout=max(0, min(0.05, deadline - time.monotonic()))):
                break
        queued = False
        try:
            _check_request_lifetime(deadline, cancellation)
            with self._state_lock:
                if self._failure is not None or self._stop_event.is_set():
                    raise RuntimeError(self._failure or "MCP SSE connection closed")
                if self._post_url is None:
                    raise RuntimeError("SSE transport: post URL not yet received")
                self._post_done.clear()
                self._post_error = None
                worker = threading.Thread(
                    target=self._post,
                    args=(self._post_url, message_bytes, deadline, cancellation),
                    daemon=True,
                )
                self._post_thread = worker
                queued = True
                worker.start()
            while not self._post_done.wait(max(0, min(0.05, deadline - time.monotonic()))):
                _check_request_lifetime(
                    deadline, cancellation, timeout_message="MCP SSE POST deadline exceeded",
                )
                if self._stop_event.is_set():
                    raise RuntimeError(self._failure or "MCP SSE connection closed")
            _check_request_lifetime(
                deadline, cancellation, timeout_message="MCP SSE POST deadline exceeded",
            )
            if self._post_error is not None:
                raise self._post_error
            if self._stop_event.is_set():
                raise RuntimeError(self._failure or "MCP SSE connection closed")
            # done is the worker's last action. Keep its handle until exit is
            # confirmed, so a later send cannot overwrite uncollected IO.
            worker.join(timeout=0.05)
            if worker.is_alive():
                raise RuntimeError("MCP SSE POST cleanup is incomplete")
        except Exception as error:
            if queued:
                self._fail(
                    "MCP SSE POST cancelled" if isinstance(error, InterruptedError)
                    else "MCP SSE POST deadline exceeded" if isinstance(error, TimeoutError)
                    else str(error) if isinstance(error, (RuntimeError, PermissionError))
                    else "MCP SSE POST failed"
                )
            raise
        finally:
            self._write_lock.release()

    def _post(
        self, url: str, message_bytes: bytes, deadline: float,
        cancellation: threading.Event | None,
    ) -> None:
        try:
            _check_request_lifetime(deadline, cancellation)
            if self._stop_event.is_set():
                raise InterruptedError("MCP SSE connection closed")
            req = urllib.request.Request(
                url, data=message_bytes,
                headers={"Content-Type": "application/json", **self._extra_headers},
                method="POST",
            )
            with self._endpoint_policy.open(req, timeout=max(0, deadline - time.monotonic())) as resp:
                self._post_response = resp
                _check_request_lifetime(deadline, cancellation)
                if self._stop_event.is_set():
                    raise InterruptedError("MCP SSE connection closed")
                if len(resp.read(SSE_EVENT_LIMIT + 1)) > SSE_EVENT_LIMIT:
                    raise RuntimeError("MCP SSE POST response exceeds the byte limit")
            _check_request_lifetime(deadline, cancellation)
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            self._post_error = (
                error if isinstance(error, (TimeoutError, InterruptedError, PermissionError, RuntimeError))
                else RuntimeError("MCP SSE POST failed")
            )
        finally:
            self._post_done.set()

    def _interrupt(self) -> None:
        with self._state_lock:
            self._stop_event.set()
        self._endpoint_policy.interrupt()
        for response in (self._response, self._post_response):
            if response is not None:
                interrupt_sse_response(response)

    def _fail(self, reason: str) -> None:
        with self._state_lock:
            if self._failure is None:
                self._failure = reason
        self._interrupt()

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
            _check_request_lifetime(self._startup_deadline, None)
            timeout = (
                max(0, self._startup_deadline - time.monotonic())
                if self._startup_deadline is not None else _DEFAULT_TIMEOUT
            )
            self._response = self._endpoint_policy.open(req, timeout=timeout)
            if self._stop_event.is_set():
                return
            allow_idle_sse_response(self._response)
            for event_type, data_str in read_sse_events(self._response):
                if self._stop_event.is_set():
                    break
                self._handle_event(event_type, data_str)
        except Exception:
            if not self._stop_event.is_set():
                self._fail("MCP SSE connection failed")
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

    def __init__(self, server_name, config, *, inherit_environment=True):
        self.server_name = server_name
        self.config = config
        self._inherit_environment = inherit_environment
        self.status = "disconnected"
        self.tools = []
        self.server_capabilities = {}
        self._transport: _TransportBase | None = None
        self._id_counter = 0
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()

    # -- public API --

    def connect(self, *, deadline=None, cancellation=None):
        # The owner already resolved and approved this snapshot. Expanding it
        # again could replace literal ${...} values after approval verification.
        transport_type = self.config.get("transport", "stdio")
        sse_transport: _SseTransport | None = None
        if transport_type == "stdio":
            command = self.config.get("command")
            command_args = self.config.get("args")
            command_parts = _normalize_stdio_command(command, command_args)
            if not command_parts:
                raise ValueError("stdio transport requires 'command' in config")
            env = os.environ.copy() if self._inherit_environment else {}
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
            sse_transport = _SseTransport(url, headers=headers)
            self._transport = sse_transport
        else:
            raise ValueError("Unknown transport type: {}".format(transport_type))

        try:
            _check_request_lifetime(deadline, cancellation)
            if sse_transport is not None:
                sse_transport.start(deadline=deadline, cancellation=cancellation)
            else:
                self._transport.start()
            self._initialize(deadline=deadline, cancellation=cancellation)
            self.tools = self._list_tools(deadline=deadline, cancellation=cancellation)
            _check_request_lifetime(deadline, cancellation)
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

    def call_tool(self, tool_name, arguments, *, deadline=None, cancellation=None):
        resp = self._send_request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments or {},
            },
            deadline=deadline,
            cancellation=cancellation,
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

    def _send_request(self, method, params=None, *, deadline=None, cancellation=None):
        # Each transport has one response queue. Keep a request and its response
        # together so another caller cannot consume and discard that response.
        transport = self._transport
        if transport is None:
            raise RuntimeError("Not connected to server '{}'".format(self.server_name))
        deadline = min(
            deadline if deadline is not None else float("inf"),
            time.monotonic() + _DEFAULT_TIMEOUT,
        )
        while True:
            _check_request_lifetime(deadline, cancellation, timeout_message="Timeout waiting for the MCP connection")
            if self._request_lock.acquire(timeout=max(0, min(0.05, deadline - time.monotonic()))):
                break
        try:
            if self._transport is not transport:
                raise RuntimeError("MCP connection changed while waiting")
            if time.monotonic() >= deadline:
                raise TimeoutError("Timeout waiting for the MCP connection")
            _check_request_lifetime(deadline, cancellation)
            msg_id = self._next_id()
            request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
            if params is not None:
                request["params"] = params
            transport.send(
                json.dumps(request).encode("utf-8"), deadline=deadline,
                cancellation=cancellation,
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timeout waiting for the MCP response")
                _check_request_lifetime(deadline, cancellation)
                msg = transport.recv(timeout=min(0.05, remaining) if cancellation is not None else remaining)
                _check_request_lifetime(deadline, cancellation, timeout_message="Timeout waiting for the MCP response")
                if msg is None:
                    if cancellation is None:
                        raise TimeoutError("Timeout waiting for the MCP response")
                    continue
                if self._transport is not transport:
                    raise RuntimeError("MCP connection changed while waiting")
                if msg.get("id") == msg_id:
                    return msg
                # Notifications and late replies to timed-out requests do not
                # belong to this call. Never replay a request after timeout.
        finally:
            self._request_lock.release()

    def _send_notification(self, method, params=None, *, deadline=None, cancellation=None):
        if self._transport is None:
            return
        notification = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            notification["params"] = params
        raw = json.dumps(notification).encode("utf-8")
        _check_request_lifetime(deadline, cancellation)
        self._transport.send(raw, deadline=deadline, cancellation=cancellation)
        _check_request_lifetime(deadline, cancellation)

    def _initialize(self, *, deadline=None, cancellation=None):
        resp = self._send_request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
            deadline=deadline,
            cancellation=cancellation,
        )
        if "error" in resp:
            raise RuntimeError(
                "MCP initialize failed: {}".format(resp["error"].get("message", "unknown error"))
            )
        result = resp.get("result", {})
        self.server_capabilities = result.get("capabilities", {})
        self._send_notification("initialized", deadline=deadline, cancellation=cancellation)

    def _list_tools(self, *, deadline=None, cancellation=None):
        if "tools" not in self.server_capabilities:
            return []
        resp = self._send_request("tools/list", deadline=deadline, cancellation=cancellation)
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

    def connect(self, server_name, config, *, deadline=None, cancellation=None, inherit_environment=True):
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
            _check_request_lifetime(deadline, cancellation)
            conn = _ServerConnection(server_name, config, inherit_environment=inherit_environment)
            # Publish ownership before startup, and serialize replacement with
            # disconnect/reconnect. A failed startup may already own a child.
            self._servers[server_name] = conn
            try:
                return conn.connect(deadline=deadline, cancellation=cancellation)
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

    def call(
        self, server_name: str, tool_name: str, arguments: dict[str, Any], *,
        deadline: float | None = None,
        cancellation: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Call once, preserving local IO failures for the resource owner.

        A normal server result with isError=true is still a valid reply. Local
        transport failures raise so the Host can fence and collect the resource.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP connection owner is closed")
            conn = self._servers.get(server_name)
        if conn is None:
            raise RuntimeError("MCP server is not connected")
        if conn.status != "connected":
            raise RuntimeError("MCP server is not ready")
        return conn.call_tool(tool_name, arguments, deadline=deadline,
                              cancellation=cancellation)

    def invoke(self, server_name, tool_name, arguments, *, deadline=None, cancellation=None):
        """Preserve the legacy result-envelope API during owner migration."""
        try:
            return self.call(server_name, tool_name, arguments, deadline=deadline,
                             cancellation=cancellation)
        except Exception as exc:
            return {"result": "MCP call failed: {}".format(exc),
                    "is_error": True, "widget": None}



def _check_request_lifetime(deadline, cancellation, *, timeout_message="MCP request deadline elapsed") -> None:
    """Reject a cancelled request or an elapsed original Host deadline."""
    if cancellation is not None and cancellation.is_set():
        raise InterruptedError("MCP request was cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(timeout_message)
