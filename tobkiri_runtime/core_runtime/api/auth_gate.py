"""Panel-session authentication for the finite Pack v4 HTTP surface."""

from __future__ import annotations

import hmac
from http import cookies
from typing import Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler as _HTTPHandlerBase
else:
    _HTTPHandlerBase = object

from .api_response import APIResponse
from ..panel_auth import PanelAuthManager


class AuthGateMixin(_HTTPHandlerBase):
    """Authenticate only launcher-issued panel sessions.

    Pack v4 deliberately has no bearer compatibility path.  This boundary
    cannot initialize approval, device, access-token, or HMAC-root managers.
    """

    _request_auth_mode: str | None
    _panel_session_cookie: str | None
    _panel_session: Mapping[str, object] | None
    _panel_auth_manager: PanelAuthManager | None

    if TYPE_CHECKING:
        def _get_cors_origin(self, origin: str) -> str | None: ...

        def _send_response(
            self,
            response: APIResponse,
            status: int = 200,
            extra_headers: list[tuple[str, str]] | None = None,
        ) -> None: ...

    def _parse_cookie_header(self) -> dict[str, str]:
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return {}
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw_cookie)
        except cookies.CookieError:
            return {}
        return {key: morsel.value for key, morsel in jar.items()}

    @staticmethod
    def _build_set_cookie(
        name: str,
        value: str,
        *,
        path: str,
        max_age: int,
        http_only: bool,
        same_site: str = "Strict",
    ) -> str:
        jar = cookies.SimpleCookie()
        jar[name] = value
        morsel = jar[name]
        morsel["path"] = path
        morsel["max-age"] = str(max_age)
        morsel["samesite"] = same_site
        if http_only:
            morsel["httponly"] = True
        return morsel.OutputString()

    def _check_panel_origin(self) -> bool:
        return bool(self._get_cors_origin(self.headers.get("Origin", "")))

    def _check_panel_session(self, method: str) -> bool:
        manager = self._panel_auth_manager
        if manager is None:
            return False
        session_id = self._parse_cookie_header().get("rumi_panel_session", "")
        session = manager.verify_session(session_id)
        if session is None:
            return False
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            csrf_header = self.headers.get("X-Rumi-CSRF", "")
            session_csrf = session.get("csrf_token")
            if (
                not self._check_panel_origin()
                or not csrf_header
                or not isinstance(session_csrf, str)
                or not hmac.compare_digest(csrf_header, session_csrf)
            ):
                return False
        self._panel_session = session
        self._panel_session_cookie = self._build_set_cookie(
            "rumi_panel_session",
            session_id,
            path="/",
            max_age=int(
                session.get(
                    "expires_in",
                    PanelAuthManager.DEFAULT_SESSION_TTL_SECONDS,
                )
            ),
            http_only=True,
        )
        self._request_auth_mode = "panel_session"
        return True

    def _check_auth(self, method: str, _path: str) -> bool:
        """Accept a verified panel session and no other credential form."""

        self._request_auth_mode = None
        return self._check_panel_session(method)
