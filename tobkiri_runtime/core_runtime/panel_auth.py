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
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .host_contract import HostContractError, capture_launcher_bootstrap_secret


@dataclass(frozen=True, slots=True)
class PanelAuthBinding:
    """Host-captured authority identity for one panel credential generation."""

    profile_id: str
    profile_revision: str
    activation_id: str
    plan_digest: str
    security_epoch: int


# The approval-presenter grant key is the interactive request id the Launcher
# approval window presents; it must stay within the Bridge's request-id shape.
_PRESENTER_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,160}")


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
        self._journal_scope = (
            "sha256:"
            + hashlib.sha256(
                b"tobkiri.panel.mutation-journal.v1\0"
                + self._bootstrap_secret.encode("utf-8")
            ).hexdigest()
            if self._bootstrap_secret
            else ""
        )
        self._code_ttl_seconds = max(15, int(code_ttl_seconds))
        self._session_ttl_seconds = max(300, int(session_ttl_seconds))
        self._lock = threading.Lock()
        self._active_codes: Dict[str, Dict[str, Any]] = {}
        self._active_sessions: Dict[str, Dict[str, Any]] = {}
        # request_id -> owner journal + binding recorded while the verified
        # presentation owner opens the dedicated Launcher approval window.
        # Each grant is bound to exactly one issued one-time code and is
        # consumed by that code's exchange.
        self._presenter_grants: Dict[str, Dict[str, Any]] = {}

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

        expired_grants = [
            request_id
            for request_id, info in self._presenter_grants.items()
            if info.get("expires_at", 0.0) <= now
        ]
        for request_id in expired_grants:
            del self._presenter_grants[request_id]

    def validate_bootstrap_secret(self, candidate: str) -> bool:
        if not self._bootstrap_secret or not candidate:
            return False
        return hmac.compare_digest(candidate, self._bootstrap_secret)

    def desktop_challenge_response(self, challenge: str) -> str:
        """Sign one bounded Launcher health challenge from the sealed snapshot."""

        if (
            not self._bootstrap_secret
            or not isinstance(challenge, str)
            or not challenge
            or len(challenge) > 256
            or challenge != challenge.strip()
        ):
            return ""
        return hmac.new(
            self._bootstrap_secret.encode("utf-8"),
            challenge.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def issue_login_code(
        self,
        binding: PanelAuthBinding,
        *,
        presenter_request_id: str = "",
    ) -> Dict[str, Any]:
        """Issue one one-time code, dedicated to a pending grant when asked.

        ``presenter_request_id`` is only honoured when a live, unbound
        presenter grant for that exact request exists under the same authn
        binding: the grant then binds to this code's hash so no other code
        can claim it.  In every other case the issued code is an ordinary
        login code — a client-supplied request id never by itself marks a
        code or lifts a grant.
        """

        now = time.time()
        code = self._generate_secret_token()
        code_hash = self._hash_value(code)
        expires_at = now + self._code_ttl_seconds
        with self._lock:
            self._cleanup_locked(now)
            code_info: Dict[str, Any] = {
                "issued_at": now,
                "expires_at": expires_at,
                "binding": binding,
            }
            if (
                isinstance(presenter_request_id, str)
                and _PRESENTER_REQUEST_ID.fullmatch(presenter_request_id)
                is not None
            ):
                grant = self._presenter_grants.get(presenter_request_id)
                if (
                    grant is not None
                    and grant.get("expires_at", 0.0) > now
                    and grant.get("code_hash") is None
                    and grant.get("binding") == binding
                ):
                    grant["code_hash"] = code_hash
                    code_info["presenter_request_id"] = presenter_request_id
            self._active_codes[code_hash] = code_info
        return {
            "code": code,
            "expires_in": self._code_ttl_seconds,
        }

    def record_approval_presenter_grant(
        self,
        request_id: str,
        journal_session: str,
        binding: PanelAuthBinding,
    ) -> None:
        """Bind one approval-window exchange to the verified owner session.

        The Host records this grant only after the interactive-approval port
        proves that the caller invoking ``authority_approval.open`` already is
        the request's presentation owner.  The grant therefore can only alias
        the caller's own journal; it never derives authority from a
        client-supplied owner identity.  The dedicated approval window holds a
        separate cookie store and mints a fresh session through the one-time
        ``?code=`` exchange, so the grant lets that exact exchange resolve to
        the same presentation-owner session the request is bound to.

        The grant is bound to the owner journal, the exact ``request_id``,
        and the authn binding current when the owner opened the window.  It
        is claimed by the first ``issue_login_code`` that names the request —
        the Launcher's approval-window bootstrap — and is consumed by that
        one code's exchange.
        """

        if (
            not isinstance(request_id, str)
            or _PRESENTER_REQUEST_ID.fullmatch(request_id) is None
            or not isinstance(journal_session, str)
            or not journal_session
            or len(journal_session) > 512
            or "." in journal_session
            or not isinstance(binding, PanelAuthBinding)
        ):
            raise ValueError("approval presenter grant is invalid")
        now = time.time()
        with self._lock:
            self._cleanup_locked(now)
            self._presenter_grants[request_id] = {
                "journal_session": journal_session,
                "binding": binding,
                "code_hash": None,
                "expires_at": now + self._code_ttl_seconds,
            }

    def exchange_code(
        self,
        code: str,
        binding: PanelAuthBinding,
        *,
        previous_session: str = "",
        presenter_request_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        if not code:
            return None
        now = time.time()
        code_hash = self._hash_value(code)
        with self._lock:
            self._cleanup_locked(now)
            code_info = self._active_codes.get(code_hash)
            if code_info is None or code_info.get("expires_at", 0.0) <= now:
                return None
            if code_info.get("binding") != binding:
                return None
            raw_presenter_mark = code_info.get("presenter_request_id")
            presenter_mark = (
                raw_presenter_mark
                if isinstance(raw_presenter_mark, str)
                else ""
            )
            presenter_grant = None
            if presenter_mark:
                # A code dedicated to one presenter grant fails closed: the
                # exchange must name that exact request, the grant must still
                # be live, bound to this code, and issued under the current
                # authn binding.  Any mismatch denies without consuming the
                # code so a bounded retry can still succeed.
                presenter_grant = self._presenter_grants.get(presenter_mark)
                if (
                    presenter_request_id != presenter_mark
                    or presenter_grant is None
                    or presenter_grant.get("expires_at", 0.0) <= now
                    or presenter_grant.get("code_hash") != code_hash
                    or presenter_grant.get("binding") != binding
                ):
                    return None
            del self._active_codes[code_hash]

            session_id = self._generate_secret_token()
            csrf_token = self._generate_secret_token()
            session_hash = self._hash_value(session_id)
            expires_at = now + self._session_ttl_seconds
            previous_hash = self._hash_value(previous_session)
            previous = self._active_sessions.get(previous_hash)
            previous_binding = previous.get("binding") if previous else None
            # A fresh desktop code authorizes the new capture. The still-live
            # HttpOnly cookie only carries journal ownership across a Profile
            # revision; it cannot authorize the new capture by itself.  A
            # presenter-bound code additionally lets the dedicated approval
            # window's exchange resolve to the verified owner journal; the
            # grant is consumed here so it can never mint a second session.
            # An unmarked code never grafts a presenter grant, so a foreign
            # ``request_id`` claim simply mints an ordinary session.
            journal_session = session_hash
            request_scope = ""
            if presenter_grant is not None:
                del self._presenter_grants[presenter_mark]
                journal_session = str(presenter_grant["journal_session"])
                request_scope = presenter_mark
            elif (
                previous is not None
                and isinstance(previous_binding, PanelAuthBinding)
                and previous_binding.profile_id == binding.profile_id
                and previous_binding.security_epoch == binding.security_epoch
            ):
                journal_session = previous.get("journal_session", previous_hash)
                # A request-scoped session keeps its confinement across a
                # reauthorization instead of silently widening to the owner.
                request_scope = str(previous.get("request_scope") or "")
                del self._active_sessions[previous_hash]
            self._active_sessions[session_hash] = {
                "journal_session": journal_session,
                "csrf_token": csrf_token,
                "issued_at": now,
                "expires_at": expires_at,
                "binding": binding,
                "request_scope": request_scope,
            }
        return {
            "session_id": session_id,
            "csrf_token": csrf_token,
            "expires_in": self._session_ttl_seconds,
            "journal_scope": self._journal_scope,
            # Internal to the HTTP boundary: the exchange handler reads the
            # minted session's confinement to name its Set-Cookie per surface.
            # It is never forwarded to the client response body.
            "request_scope": request_scope,
        }

    def verify_session(
        self,
        session_id: str,
        binding: PanelAuthBinding,
    ) -> Optional[Dict[str, Any]]:
        if not session_id:
            return None
        now = time.time()
        session_hash = self._hash_value(session_id)
        with self._lock:
            self._cleanup_locked(now)
            session_info = self._active_sessions.get(session_hash)
            if session_info is None:
                return None
            if session_info.get("binding") != binding:
                return None
            session_info["expires_at"] = now + self._session_ttl_seconds
            return {
                "session_id": session_info.get("journal_session", session_hash),
                "csrf_token": session_info["csrf_token"],
                "expires_in": self._session_ttl_seconds,
                "request_scope": str(session_info.get("request_scope") or ""),
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
        try:
            # This is intentionally a one-way credential capture.  In
            # particular, a Launcher bootstrap contract never becomes a
            # normal Host execution contract or route-identity source.
            bootstrap_secret = capture_launcher_bootstrap_secret()
        except HostContractError:
            bootstrap_secret = ""
        _panel_auth_manager = PanelAuthManager(bootstrap_secret=bootstrap_secret)
    return _panel_auth_manager


def reset_panel_auth_manager_for_tests(
    manager: Optional[PanelAuthManager] = None,
    *,
    capture_launcher_credential: bool = False,
) -> PanelAuthManager:
    """Replace the process singleton, optionally as a fresh Host process would."""

    global _panel_auth_manager
    if manager is not None:
        _panel_auth_manager = manager
    elif capture_launcher_credential:
        _panel_auth_manager = None
        return get_panel_auth_manager()
    else:
        _panel_auth_manager = PanelAuthManager(bootstrap_secret="test-bootstrap")
    return _panel_auth_manager
