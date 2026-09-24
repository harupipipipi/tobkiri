"""Memory-only, audience-bound exchanges for the Defaultspack local UI."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Mapping


EXCHANGE_TTL_SECONDS = 20
SESSION_TTL_SECONDS = 8 * 60 * 60
LOCAL_AUTH_SCOPE = "defaultspack-local-ui"


@dataclass(frozen=True)
class LocalAuthAudience:
    """Identity attributes that bind an exchange and its resulting session."""

    origin: str
    window_id: str
    process_id: str
    device_id: str
    nonce: str
    scope: str = LOCAL_AUTH_SCOPE

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "LocalAuthAudience":
        """Build a bounded audience or raise ``ValueError`` on invalid input."""

        fields = {
            key: str(values.get(key) or "").strip()
            for key in ("origin", "window_id", "process_id", "device_id", "nonce", "scope")
        }
        fields["scope"] = fields["scope"] or LOCAL_AUTH_SCOPE
        if fields["scope"] != LOCAL_AUTH_SCOPE:
            raise ValueError("local auth scope is invalid")
        try:
            origin = urllib.parse.urlsplit(fields["origin"])
            valid_origin = (
                origin.scheme == "http"
                and origin.hostname in {"127.0.0.1", "localhost"}
                and origin.port is not None
                and origin.username is None
                and origin.password is None
                and not origin.path
                and not origin.query
                and not origin.fragment
            )
        except (TypeError, ValueError):
            valid_origin = False
        if not valid_origin:
            raise ValueError("local auth origin is invalid")
        for key in ("window_id", "process_id", "device_id", "nonce"):
            value = fields[key]
            if not value or len(value) > 160:
                raise ValueError(f"local auth {key} is invalid")
        return cls(**fields)


@dataclass
class _PendingExchange:
    audience: LocalAuthAudience
    subject: str
    expires_at: float


@dataclass
class _Session:
    audience: LocalAuthAudience
    subject: str
    expires_at: float


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class LocalAuthExchangeStore:
    """Issue single-use codes and memory-only bound bearer sessions."""

    def __init__(self, now=time.time) -> None:
        self._now = now
        self._lock = threading.RLock()
        self._pending: dict[str, _PendingExchange] = {}
        self._sessions: dict[str, _Session] = {}

    def issue(self, subject: str, audience: LocalAuthAudience) -> dict[str, object]:
        """Issue a short-lived code for an authenticated local subject."""

        if not subject:
            raise ValueError("local auth subject is required")
        code = secrets.token_urlsafe(32)
        expires_at = self._now() + EXCHANGE_TTL_SECONDS
        with self._lock:
            self._prune()
            self._pending[_digest(code)] = _PendingExchange(
                audience=audience,
                subject=subject,
                expires_at=expires_at,
            )
        return {"exchange_code": code, "expires_at": expires_at}

    def redeem(self, code: str, audience: LocalAuthAudience) -> dict[str, object]:
        """Consume a code once and create a bound, memory-only session."""

        code_digest = _digest(str(code or ""))
        with self._lock:
            self._prune()
            pending = self._pending.pop(code_digest, None)
            if pending is None:
                raise ValueError("invalid, expired, or already consumed local auth exchange")
            if pending.audience != audience:
                raise PermissionError("local auth exchange audience does not match")
            token = secrets.token_urlsafe(48)
            expires_at = self._now() + SESSION_TTL_SECONDS
            self._sessions[_digest(token)] = _Session(
                audience=audience,
                subject=pending.subject,
                expires_at=expires_at,
            )
        return {"session_token": token, "expires_at": expires_at}

    def authorize(self, token: str, audience: LocalAuthAudience) -> bool:
        """Return whether a session token is live and bound to ``audience``."""

        with self._lock:
            self._prune()
            session = self._sessions.get(_digest(str(token or "")))
            return bool(session and hmac.compare_digest(repr(session.audience), repr(audience)))

    def subject(self, token: str, audience: LocalAuthAudience) -> str:
        """Return the authenticated subject for a live bound session."""

        with self._lock:
            self._prune()
            session = self._sessions.get(_digest(str(token or "")))
            if not session or session.audience != audience:
                return ""
            return session.subject

    def _prune(self) -> None:
        now = self._now()
        self._pending = {
            key: value for key, value in self._pending.items() if value.expires_at > now
        }
        self._sessions = {
            key: value for key, value in self._sessions.items() if value.expires_at > now
        }


_STORE = LocalAuthExchangeStore()


def get_local_auth_exchange_store() -> LocalAuthExchangeStore:
    """Return the process-local exchange store."""

    return _STORE
