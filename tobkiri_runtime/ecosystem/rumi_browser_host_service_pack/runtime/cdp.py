"""Bounded CDP transport for a Tobkiri-owned loopback browser."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections import deque
from typing import Any

MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class CDPError(RuntimeError):
    """A CDP command failed without exposing the browser's error payload."""

    def __init__(self, method: str, code: int | None = None, *, unsupported: bool = False) -> None:
        super().__init__(f"Managed browser command failed: {method}")
        self.method = method
        self.code = code
        self.unsupported = unsupported or code == -32601


def validate_websocket_url(url: str, port: int, path: str | None = None) -> str:
    """Accept only a socket at the managed browser's exact loopback port."""

    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/devtools/")
        or (path is not None and parsed.path != path)
    ):
        raise PermissionError("Untrusted managed browser socket")
    return url


def get_json(port: int, path: str, *, timeout: float = 1.0) -> Any:
    """Read bounded discovery data without proxy or redirect support."""

    if not 1 <= port <= 65535 or path not in {"/json/version", "/json/list"}:
        raise ValueError("Invalid managed browser discovery request")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        body = response.read(MAX_MESSAGE_BYTES + 1)
    if len(body) > MAX_MESSAGE_BYTES:
        raise ValueError("Managed browser discovery response is too large")
    return json.loads(body.decode("utf-8"))


class CDPConnection:
    """Keep one websocket open for commands and bounded event observation."""

    def __init__(self, url: str, *, port: int, timeout: float = 5.0) -> None:
        self.url = validate_websocket_url(url, port)
        self.timeout = timeout
        self._socket: Any = None
        self._next_id = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=2_000)
        self.events_truncated = False

    def __enter__(self) -> CDPConnection:
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError(
                "Install Tobkiri's browser-cdp optional dependency to use the managed browser."
            ) from exc
        try:
            self._socket = websocket.create_connection(
                self.url,
                timeout=self.timeout,
                suppress_origin=True,
                http_proxy_host=None,
                http_no_proxy=["127.0.0.1"],
                enable_multithread=False,
            )
        except websocket.WebSocketException as exc:
            raise ConnectionError("Could not connect to the managed browser") from exc
        return self

    def __exit__(self, *_args: Any) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Issue a command with a wall-clock deadline while buffering events."""

        if self._socket is None:
            raise RuntimeError("Managed browser socket is not open")
        self._next_id += 1
        request_id = self._next_id
        self._socket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            message = self._receive(max(0.01, deadline - time.monotonic()))
            if message is None:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    error = message["error"]
                    code = error.get("code") if isinstance(error, dict) else None
                    error_message = (
                        str(error.get("message") or "") if isinstance(error, dict) else ""
                    )
                    unavailable = "enable-unsafe-extension-debugging" in error_message.lower()
                    raise CDPError(
                        method,
                        code if isinstance(code, int) else None,
                        unsupported=unavailable,
                    )
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            if "method" in message:
                if len(self._events) == self._events.maxlen:
                    self.events_truncated = True
                self._events.append(message)
        raise TimeoutError("Managed browser command timed out")

    def event(self, timeout: float) -> dict[str, Any] | None:
        """Return one event without extending the caller's capture deadline."""

        if self._events:
            return self._events.popleft()
        message = self._receive(timeout)
        return message if message is not None and "method" in message else None

    def _receive(self, timeout: float) -> dict[str, Any] | None:
        import websocket

        self._socket.settimeout(max(0.01, timeout))
        try:
            raw = self._socket.recv()
        except websocket.WebSocketTimeoutException:
            return None
        except websocket.WebSocketException as exc:
            raise ConnectionError("Managed browser closed the debugging socket") from exc
        if not raw:
            raise ConnectionError("Managed browser closed the debugging socket")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("Managed browser event is too large")
        message = json.loads(raw)
        return message if isinstance(message, dict) else None


def public_url(value: Any) -> str:
    """Remove URL credentials, query values and fragments from observations."""

    text = str(value or "")[:16_384]
    try:
        parsed = urllib.parse.urlsplit(text)
        if parsed.scheme not in {"http", "https", "chrome-extension"}:
            return parsed.scheme + ":" if parsed.scheme else ""
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host += f":{parsed.port}"
        names = [
            urllib.parse.quote(name, safe="") + "=<redacted>"
            for name, _value in urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True, max_num_fields=100
            )
        ]
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "&".join(names), ""))[
            :4_096
        ]
    except (ValueError, UnicodeError):
        return ""
