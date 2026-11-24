import os


from blocks._common import ok, error, timestamp

import json
import socket
import threading
import importlib


_ROUTE_MAP = [
    ("POST", "/v1/chat/completions", "blocks.chat.send_message"),
    ("POST", "/api/chat/conversations", "blocks.chat.create_conversation"),
    ("GET", "/api/chat/conversations", "blocks.chat.list_conversations"),
    ("GET", "/api/chat/conversations/{id}", "blocks.chat.get_conversation"),
    ("PUT", "/api/chat/conversations/{id}", "blocks.chat.update_conversation"),
    ("DELETE", "/api/chat/conversations/{id}", "blocks.chat.delete_conversation"),
    ("POST", "/api/chat/conversations/{id}/messages", "blocks.chat.send_message"),
    ("POST", "/api/chat/conversations/{id}/stream", "blocks.chat.stream_response"),
    ("POST", "/api/chat/conversations/{id}/stop", "blocks.chat.stop"),
    ("POST", "/api/chat/conversations/{id}/export", "blocks.chat.export_conversation"),
    ("POST", "/api/agent/execute", "blocks.agent.execute_flow"),
    ("POST", "/api/agent/{id}/approve", "blocks.agent.approve_step"),
    ("POST", "/api/agent/{id}/reject", "blocks.agent.reject_step"),
    ("POST", "/api/agent/{id}/cancel", "blocks.agent.cancel_execution"),
    ("GET", "/api/agent/{id}/status", "blocks.agent.get_status"),
    ("GET", "/api/health", None),
    ("GET", "/api/context", None),
]

_ID_INJECT_MAP = {
    "/api/chat/conversations/{id}": ("conversation_id", "id"),
    "/api/chat/conversations/{id}/messages": ("conversation_id", "id"),
    "/api/chat/conversations/{id}/stream": ("conversation_id", "id"),
    "/api/chat/conversations/{id}/stop": ("conversation_id", "id"),
    "/api/chat/conversations/{id}/export": ("conversation_id", "id"),
    "/api/agent/{id}/approve": ("execution_id", "id"),
    "/api/agent/{id}/reject": ("execution_id", "id"),
    "/api/agent/{id}/cancel": ("execution_id", "id"),
    "/api/agent/{id}/status": ("execution_id", "id"),
}


def _match_route(method, path):
    import re

    for route_method, pattern, module_name in _ROUTE_MAP:
        if route_method != method:
            continue
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
        regex = "^" + regex + "$"
        m = re.match(regex, path)
        if m is not None:
            return pattern, module_name, m.groupdict()
    return None, None, {}


class DefaultsUdsTransport:
    def __init__(self, socket_path=None):
        self.socket_path = socket_path or os.environ.get(
            "DEFAULTS_UDS_PATH", "/tmp/rumi_defaults.sock"
        )
        self._server = None
        self._running = False

    def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(self.socket_path)
        self._server.listen(8)
        self._server.settimeout(1.0)
        self._running = True
        print(f"[defaults] UDS transport listening on {self.socket_path}")
        while self._running:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()

    def stop(self):
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    def _recv_exact(self, conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed prematurely")
            buf += chunk
        return buf

    def _handle_client(self, conn):
        try:
            length_bytes = self._recv_exact(conn, 4)
            length = int.from_bytes(length_bytes, "big")
            if length <= 0 or length > 10 * 1024 * 1024:
                response = error("invalid message length")
            else:
                data = self._recv_exact(conn, length)
                request = json.loads(data.decode("utf-8"))
                response = self._handle_request(request)
        except (ConnectionError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            response = error("protocol error: " + str(exc))
        except Exception as exc:
            response = error("internal error: " + str(exc))
        try:
            response_bytes = json.dumps(response, ensure_ascii=False).encode("utf-8")
            conn.sendall(len(response_bytes).to_bytes(4, "big") + response_bytes)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle_request(self, request):
        method = request.get("method", "GET").upper()
        path = request.get("path", "")
        data = request.get("data", {})

        pattern, module_name, path_params = _match_route(method, path)

        if pattern is None:
            return error("not found: " + method + " " + path)

        if pattern == "/api/health" and module_name is None:
            return ok({"status": "healthy", "pack": "defaultspack", "ts": timestamp()})

        if pattern == "/api/context" and module_name is None:
            return ok({"pack": "defaultspack", "ts": timestamp()})

        if pattern in _ID_INJECT_MAP:
            field_name, param_name = _ID_INJECT_MAP[pattern]
            data[field_name] = path_params.get(param_name, "")

        try:
            mod = importlib.import_module(module_name)
            handler_run = getattr(mod, "run")
        except (ImportError, AttributeError) as exc:
            return error("handler not available: " + str(exc))

        context = self._build_context()
        try:
            return handler_run(data, context)
        except Exception as exc:
            return error("handler error: " + str(exc))

    def _build_context(self):
        return {
            "flow_id": "uds_direct",
            "step_id": "uds_request",
            "phase": "execute",
            "ts": timestamp(),
            "owner_pack": "defaultspack",
            "inputs": {},
        }
