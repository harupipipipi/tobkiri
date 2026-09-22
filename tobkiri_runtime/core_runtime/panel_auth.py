"""Panel browser authentication helpers.

Desktop bootstrap flow:
1. Native app proves itself with a desktop-only bootstrap secret.
2. Server issues a short-lived one-time code.
3. Browser exchanges the code for an HttpOnly session cookie.
4. Mutating panel requests must also present an in-memory CSRF token.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Any, Dict, Optional

from .host_contract import host_contract_value


class PanelAuthManager:
    """Issue one-time bootstrap codes and validate panel sessions."""

    DEFAULT_CODE_TTL_SECONDS = 90
    DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60

    def __init__(
        self,
        *,
        bootstrap_secret: Optional[str] = None,
        code_ttl_seconds: int = DEFAULT_CODE_TTL_SECONDS,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    ) -> None:
        self._bootstrap_secret = bootstrap_secret or ""
        self._code_ttl_seconds = max(15, int(code_ttl_seconds))
        self._session_ttl_seconds = max(300, int(session_ttl_seconds))
        self._lock = threading.Lock()
        self._active_codes: Dict[str, Dict[str, Any]] = {}
        self._active_sessions: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _generate_secret_token() -> str:
        return secrets.token_urlsafe(48)

    @staticmethod
    def _hash_value(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _cleanup_locked(self, now: float) -> None:
        expired_codes = [
            key_hash
            for key_hash, info in self._active_codes.items()
            if info.get("expires_at", 0.0) <= now
        ]
        for key_hash in expired_codes:
            del self._active_codes[key_hash]

        expired_sessions = [
            session_hash
            for session_hash, info in self._active_sessions.items()
            if info.get("expires_at", 0.0) <= now
        ]
        for session_hash in expired_sessions:
            del self._active_sessions[session_hash]

    def validate_bootstrap_secret(self, candidate: str) -> bool:
        if not self._bootstrap_secret or not candidate:
            return False
        return hmac.compare_digest(candidate, self._bootstrap_secret)

    def issue_login_code(self) -> Dict[str, Any]:
        now = time.time()
        code = self._generate_secret_token()
        code_hash = self._hash_value(code)
        expires_at = now + self._code_ttl_seconds
        with self._lock:
            self._cleanup_locked(now)
            self._active_codes[code_hash] = {
                "issued_at": now,
                "expires_at": expires_at,
            }
        return {
            "code": code,
            "expires_in": self._code_ttl_seconds,
        }

    def exchange_code(self, code: str) -> Optional[Dict[str, Any]]:
        if not code:
            return None
        now = time.time()
        code_hash = self._hash_value(code)
        with self._lock:
            self._cleanup_locked(now)
            code_info = self._active_codes.pop(code_hash, None)
            if code_info is None or code_info.get("expires_at", 0.0) <= now:
                return None

            session_id = self._generate_secret_token()
            csrf_token = self._generate_secret_token()
            session_hash = self._hash_value(session_id)
            expires_at = now + self._session_ttl_seconds
            self._active_sessions[session_hash] = {
                "csrf_token": csrf_token,
                "issued_at": now,
                "expires_at": expires_at,
            }
        return {
            "session_id": session_id,
            "csrf_token": csrf_token,
            "expires_in": self._session_ttl_seconds,
        }

    def verify_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        if not session_id:
            return None
        now = time.time()
        session_hash = self._hash_value(session_id)
        with self._lock:
            self._cleanup_locked(now)
            session_info = self._active_sessions.get(session_hash)
            if session_info is None:
                return None
            session_info["expires_at"] = now + self._session_ttl_seconds
            return {
                "session_id": session_hash,
                "csrf_token": session_info["csrf_token"],
                "expires_in": self._session_ttl_seconds,
            }

    def revoke_session(self, session_id: str) -> None:
        if not session_id:
            return
        session_hash = self._hash_value(session_id)
        with self._lock:
            self._active_sessions.pop(session_hash, None)


_panel_auth_manager: Optional[PanelAuthManager] = None


def get_panel_auth_manager() -> PanelAuthManager:
    global _panel_auth_manager
    if _panel_auth_manager is None:
        bootstrap_secret = host_contract_value("panel_bootstrap_secret")
        _panel_auth_manager = PanelAuthManager(bootstrap_secret=bootstrap_secret)
    return _panel_auth_manager


def reset_panel_auth_manager_for_tests(
    manager: Optional[PanelAuthManager] = None,
) -> PanelAuthManager:
    global _panel_auth_manager
    _panel_auth_manager = manager or PanelAuthManager(bootstrap_secret="test-bootstrap")
    return _panel_auth_manager
