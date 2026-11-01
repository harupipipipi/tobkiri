"""Narrow replay-safe native browser-admission request authentication."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from typing import Callable

_ALLOWED_PATHS = frozenset(
    {
        "/api/panel/browser-access/context",
        "/api/panel/browser-access/decision",
    }
)
_HEX = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"[0-9]{1,12}")


class BrowserAccessNativeAuth:
    """Authenticate only admission transport without exposing its shared secret."""

    def __init__(self, secret: str, *, clock: Callable[[], float] = time.time) -> None:
        self._secret = secret.encode("utf-8")
        self._clock = clock
        self._lock = threading.Lock()
        self._nonces: dict[str, float] = {}

    def _sign(self, message: str) -> str:
        return hmac.new(
            self._secret, message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def verify_request(
        self,
        timestamp: str,
        nonce: str,
        proof: str,
        path: str,
        body: bytes,
    ) -> bool:
        """Consume a fresh valid request nonce atomically; reject all other input."""
        if (
            not self._secret
            or path not in _ALLOWED_PATHS
            or not isinstance(timestamp, str)
            or _TIMESTAMP.fullmatch(timestamp) is None
            or not isinstance(nonce, str)
            or _HEX.fullmatch(nonce) is None
            or not isinstance(proof, str)
            or _HEX.fullmatch(proof) is None
            or not isinstance(body, bytes)
            or len(body) > 4096
        ):
            return False
        now = self._clock()
        if abs(now - int(timestamp)) > 30:
            return False
        message = (
            f"tobkiri.browser-access.request.v1\n{timestamp}\n{nonce}\nPOST\n"
            f"{path}\n{hashlib.sha256(body).hexdigest()}"
        )
        if not hmac.compare_digest(self._sign(message), proof):
            return False
        with self._lock:
            self._nonces = {
                key: expiry for key, expiry in self._nonces.items() if expiry > now
            }
            if nonce in self._nonces or len(self._nonces) >= 128:
                return False
            self._nonces[nonce] = now + 60
            return True

    def response_proof(
        self,
        timestamp: str,
        nonce: str,
        path: str,
        status: int,
        body: bytes,
    ) -> str:
        """Bind a response's exact bytes and status to its verified request."""
        if (
            not self._secret
            or path not in _ALLOWED_PATHS
            or not isinstance(timestamp, str)
            or _TIMESTAMP.fullmatch(timestamp) is None
            or not isinstance(nonce, str)
            or _HEX.fullmatch(nonce) is None
            or not isinstance(status, int)
            or not 100 <= status <= 599
            or not isinstance(body, bytes)
        ):
            return ""
        return self._sign(
            f"tobkiri.browser-access.response.v1\n{timestamp}\n{nonce}\nPOST\n"
            f"{path}\n{status}\n{hashlib.sha256(body).hexdigest()}"
        )
