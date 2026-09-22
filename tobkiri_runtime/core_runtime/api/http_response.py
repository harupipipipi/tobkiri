from __future__ import annotations

import json
import logging
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler as _HTTPHandlerBase
else:
    _HTTPHandlerBase = object

from .api_response import APIResponse


class ResponseWriterMixin(_HTTPHandlerBase):
    _panel_session_cookie: str | None
    _CLIENT_DISCONNECT_EXCEPTIONS: tuple[type[OSError], ...]
    _completed_access_logs: list[tuple[int, int]]
    _completed_diagnostic_logs: list[
        tuple[logging.Logger, int, str, tuple[object, ...], BaseException | None]
    ]

    if TYPE_CHECKING:
        def _get_cors_origin(self, origin: str) -> str | None: ...

    def _send_response(
        self,
        response: APIResponse,
        status: int = 200,
        extra_headers: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        data = response.to_json().encode("utf-8")
        response_headers = list(extra_headers or [])
        if self._panel_session_cookie:
            response_headers.append(("Set-Cookie", self._panel_session_cookie))
        try:
            # ``BaseHTTPRequestHandler.send_response`` calls ``log_request``
            # before it emits the status line.  Keep access logging outside the
            # response critical path so a slow diagnostic sink cannot prevent
            # an otherwise complete bounded response from reaching the client.
            self.send_response_only(status)
            self.send_header("Server", self.version_string())
            self.send_header("Date", self.date_time_string())
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            origin = self._get_cors_origin(self.headers.get("Origin", ""))
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            for header_name, header_value in response_headers:
                self.send_header(header_name, header_value)
            self.end_headers()
            self.wfile.write(data)
            # Complete delivery before callers perform diagnostics or other
            # post-response cleanup that may contend under suite-wide load.
            self.wfile.flush()
            completed = getattr(self, "_completed_access_logs", None)
            if completed is None:
                completed = []
                self._completed_access_logs = completed
            completed.append((status, len(data)))
        except self._CLIENT_DISCONNECT_EXCEPTIONS:
            self.close_connection = True

    def finish(self) -> None:
        """Close the response before synchronous logging can contend."""

        try:
            super().finish()
        finally:
            diagnostics = getattr(self, "_completed_diagnostic_logs", ())
            self._completed_diagnostic_logs = []
            completed = getattr(self, "_completed_access_logs", ())
            self._completed_access_logs = []
            try:
                for log, level, message, args, error in diagnostics:
                    log.log(level, message, *args, exc_info=error)
            finally:
                for status, length in completed:
                    self.log_request(status, length)

    def _defer_response_log(
        self,
        log: logging.Logger,
        level: int,
        message: str,
        *args: object,
        exc_info: BaseException | None = None,
    ) -> None:
        """Guarantee one diagnostic synchronously after response close."""

        completed = getattr(self, "_completed_diagnostic_logs", None)
        if completed is None:
            completed = []
            self._completed_diagnostic_logs = completed
        completed.append((log, level, message, args, exc_info))

    def _send_raw_json(
        self,
        payload: Any,
        status: int = 200,
        extra_headers: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response_headers = list(extra_headers or [])
        if self._panel_session_cookie:
            response_headers.append(("Set-Cookie", self._panel_session_cookie))
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            origin = self._get_cors_origin(self.headers.get("Origin", ""))
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            for header_name, header_value in response_headers:
                self.send_header(header_name, header_value)
            self.end_headers()
            self.wfile.write(data)
        except self._CLIENT_DISCONNECT_EXCEPTIONS:
            self.close_connection = True

    def _send_sse(self, events) -> None:
        response_headers: list[tuple[str, str]] = []
        if self._panel_session_cookie:
            response_headers.append(("Set-Cookie", self._panel_session_cookie))
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            origin = self._get_cors_origin(self.headers.get("Origin", ""))
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            for header_name, header_value in response_headers:
                self.send_header(header_name, header_value)
            self.end_headers()
            for event in events:
                if isinstance(event, bytes):
                    payload = event
                else:
                    payload = (
                        "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                    ).encode("utf-8")
                self.wfile.write(payload)
                self.wfile.flush()
        except self._CLIENT_DISCONNECT_EXCEPTIONS:
            self.close_connection = True
        finally:
            self.close_connection = True

    @staticmethod
    def _sse_events_from_result(result):
        if isinstance(result, dict) and result.get("_sse"):
            return result.get("events", [])
        if (
            isinstance(result, dict)
            and result.get("status") == "ok"
            and isinstance(result.get("data"), dict)
            and result["data"].get("_sse")
        ):
            return result["data"].get("events", [])
        return None

    def _send_defaultspack_http_result(self, result: Any) -> None:
        if isinstance(result, dict) and result.get("_static"):
            body = str(result.get("body", "")).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    str(result.get("content_type", "text/html")),
                )
                self.send_header("Content-Length", str(len(body)))
                origin = self._get_cors_origin(self.headers.get("Origin", ""))
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.end_headers()
                self.wfile.write(body)
            except self._CLIENT_DISCONNECT_EXCEPTIONS:
                self.close_connection = True
            return
        if isinstance(result, dict) and result.get("_redirect"):
            try:
                self.send_response(int(result.get("status_code", 302)))
                self.send_header("Location", str(result.get("location") or "/chat"))
                self.end_headers()
            except self._CLIENT_DISCONNECT_EXCEPTIONS:
                self.close_connection = True
            return
        sse_events = self._sse_events_from_result(result)
        if sse_events is not None:
            self._send_sse(sse_events)
            return
        status_code = 200
        payload = result
        if isinstance(result, dict) and result.get("status") == "error":
            payload = dict(result)
            status_code = int(payload.pop("_http_status", 400))
        self._send_raw_json(payload, status=status_code)

    def _send_result(self, result, error_status: int = 500) -> None:
        sse_events = self._sse_events_from_result(result)
        if sse_events is not None:
            self._send_sse(sse_events)
            return
        if isinstance(result, dict) and "error" in result:
            status = result.get("status_code", error_status)
            self._send_response(APIResponse(False, error=result["error"]), status)
        else:
            self._send_response(APIResponse(True, data=result))
