from __future__ import annotations

import json
import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler as _HTTPHandlerBase
else:
    _HTTPHandlerBase = object
from urllib.parse import parse_qs, urlparse

from .api_response import APIResponse
from ..validation import MAX_REQUEST_BODY_BYTES


logger = logging.getLogger(__name__)


class RequestBodyMixin(_HTTPHandlerBase):
    _raw_body_bytes: bytes

    if TYPE_CHECKING:
        def _send_response(
            self,
            response: APIResponse,
            status: int = 200,
            extra_headers: list[tuple[str, str]] | None = None,
        ) -> None: ...

    def _read_raw_body(self) -> Optional[bytes]:
        raw_cl = self.headers.get("Content-Length", "0")
        try:
            content_length = int(raw_cl)
        except (ValueError, TypeError):
            self._send_response(
                APIResponse(False, error="Invalid Content-Length header"),
                400,
            )
            return None
        if content_length < 0:
            self._send_response(
                APIResponse(False, error="Invalid Content-Length header"),
                400,
            )
            return None
        if content_length == 0:
            self._raw_body_bytes = b""
            return b""
        if content_length > MAX_REQUEST_BODY_BYTES:
            self._send_response(APIResponse(False, error="Request body too large"), 413)
            # The rejected body stays undrained; keep-alive would let its
            # bytes poison the next pipelined request.
            self.close_connection = True
            return None
        raw = self.rfile.read(content_length)
        self._raw_body_bytes = raw
        return raw

    def _parse_body(self) -> Optional[dict]:
        raw = self._read_raw_body()
        if raw is None:
            return None
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_response(
                APIResponse(False, error="Invalid JSON in request body"),
                400,
            )
            return None

    def _discard_request_body(self) -> None:
        try:
            raw_cl = self.headers.get("Content-Length", "0")
            content_length = int(raw_cl)
        except (TypeError, ValueError):
            content_length = 0
        if content_length <= 0:
            return
        # The request was already rejected; draining more than the accepted
        # body bound only lets a rejected client pin the worker longer.
        # Anything still undrained after the cap cannot be trusted as the
        # next request boundary, so the connection must close.
        if content_length > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
        content_length = min(content_length, MAX_REQUEST_BODY_BYTES)
        try:
            self.rfile.read(content_length)
        except Exception:
            logger.debug("Failed to discard request body", exc_info=True)

    def _parse_query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        return {
            key: values[-1]
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
            if values
        }
