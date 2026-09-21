from __future__ import annotations

import hashlib
import hmac
import hashlib
import logging
import os
from http import cookies
from typing import Any

from .api_response import APIResponse
from .auth_principal import AuthenticatedPrincipal
from .request_authorizer import authorize_route
from ..access_tokens import TOKEN_PREFIX, get_scoped_access_token_manager
from ..panel_auth import PanelAuthManager


logger = logging.getLogger(__name__)


class AuthGateMixin:
    @staticmethod
    def _authority_device_scope(method: str | None, path: str | None) -> str:
        method_upper = str(method or "").upper()
        path_value = str(path or "")
        if not path_value.startswith("/api/authority/"):
            return ""
        if method_upper == "GET" and path_value == "/api/authority/requests":
            return "authority.request.list"
        if method_upper == "GET" and path_value.startswith("/api/authority/requests/"):
            return "authority.request.read"
        if method_upper == "POST" and path_value.startswith("/api/authority/requests/"):
            if path_value.endswith("/approve"):
                return "authority.request.approve"
            if path_value.endswith("/deny"):
                return "authority.request.deny"
            if path_value.endswith("/challenge"):
                return "authority.request.approve"
        return ""

    @staticmethod
    def _mobile_device_principal(token: str, device: Any, scopes: set[str]) -> AuthenticatedPrincipal:
        role = (
            "mobile_approver"
            if any(scope.startswith("authority.request.") for scope in scopes)
            else "mobile_client"
        )
        return AuthenticatedPrincipal(
            token_id=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            profile_id=str(getattr(device, "profile_id", "") or "default"),
            surface_id="mobile-approver" if role == "mobile_approver" else "mobile",
            device_id=str(getattr(device, "device_id", "") or ""),
            role=role,
            audiences=("kernel_api",),
            issued_at="",
            expires_at=None,
            auth_mode="device_bearer",
            core_role=False,
            scopes=tuple(sorted(scopes)),
        )

    def _check_bearer_auth(
        self,
        method: str | None = None,
        path: str | None = None,
        *,
        allow_device: bool = True,
    ) -> bool:
        self._authenticated_principal = None
        self._authenticated_device_scope_authorized = False
        auth_header = self.headers.get("Authorization", "")
        if not auth_header or not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:]

        if token.startswith(TOKEN_PREFIX):
            principal = get_scoped_access_token_manager().verify_token(token, audience="kernel_api")
            if principal is None:
                return False
            self._authenticated_principal = principal
            return True

        if token.startswith("dtk_"):
            self._authenticated_device_id = None
            self._authenticated_scopes = []
            if not allow_device or not method or not path:
                return False
            try:
                from ecosystem.defaultspack.domain.mobile.contract import required_device_scope
                from ecosystem.defaultspack.domain.p2p.device_store import DeviceStore

                store = DeviceStore()
                device = store.verify_token(token)
                if device is None:
                    return False
                scopes = set(device.scopes)
                path_value = str(path)
                if path_value.startswith("/api/mobile/v1/"):
                    required_scope = required_device_scope(method, path_value)
                elif path_value.startswith("/api/authority/"):
                    required_scope = self._authority_device_scope(method, path_value)
                    if path_value.endswith("/challenge") and not (
                        {"authority.request.approve", "authority.request.deny"} & scopes
                    ):
                        return False
                else:
                    return False
                if not required_scope or required_scope not in scopes:
                    return False
                self._authenticated_device_id = device.device_id
                self._authenticated_scopes = list(device.scopes)
                self._authenticated_principal = self._mobile_device_principal(token, device, scopes)
                self._authenticated_device_scope_authorized = True
                store.touch(device.device_id)
                return True
            except Exception:
                logger.debug("device token auth failed", exc_info=True)
                return False

        if not self._legacy_bearer_allowed_from_client():
            logger.warning("Rejecting legacy bearer token from non-loopback client")
            return False

        verified = False
        if self._hmac_key_manager is not None:
            verified = bool(self._hmac_key_manager.verify_token(token))
        elif not self.internal_token:
            logger.error("API token not configured - rejecting request")
            return False
        else:
            verified = hmac.compare_digest(token, self.internal_token)
        if verified:
            self._authenticated_principal = AuthenticatedPrincipal.legacy_root()
        return verified

    def _legacy_bearer_allowed_from_client(self) -> bool:
        if os.environ.get("RUMI_ALLOW_LEGACY_REMOTE_BEARER", "").strip() == "1":
            return True
        client_address = getattr(self, "client_address", ("127.0.0.1", 0))
        ip = client_address[0] if isinstance(client_address, tuple) and client_address else "127.0.0.1"
        return str(ip) in {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

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
        origin = self.headers.get("Origin", "")
        return bool(self._get_cors_origin(origin))

    def _check_panel_session(self, method: str) -> bool:
        if self._panel_auth_manager is None:
            return False
        cookies_map = self._parse_cookie_header()
        session_id = cookies_map.get("rumi_panel_session", "")
        session = self._panel_auth_manager.verify_session(session_id)
        if session is None:
            return False
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            if not self._check_panel_origin():
                return False
            csrf_header = self.headers.get("X-Rumi-CSRF", "")
            session_csrf = session.get("csrf_token", "")
            if not csrf_header or not hmac.compare_digest(csrf_header, session_csrf):
                return False

        self._panel_session = session
        self._authenticated_principal = AuthenticatedPrincipal.panel_session(session)
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
        return True

    def _check_auth(self, method: str, path: str) -> bool:
        self._authenticated_principal = None
        self._authenticated_device_scope_authorized = False
        if self._check_bearer_auth(method, path):
            self._request_auth_mode = "bearer"
            return True
        if path.startswith("/api/") and self._check_panel_session(method):
            self._request_auth_mode = "panel_session"
            return True
        self._request_auth_mode = None
        return False

    def _check_web_mount_auth(self, method: str, web_mount: dict[str, Any]) -> bool:
        self._authenticated_principal = None
        self._authenticated_device_scope_authorized = False
        if self._check_bearer_auth(method, web_mount.get("path_prefix", ""), allow_device=False):
            self._request_auth_mode = "bearer"
            return True
        if self._check_panel_session(method):
            self._request_auth_mode = "panel_session"
            return True
        self._request_auth_mode = None
        return False

    def _authorize_authenticated_route(
        self,
        method: str,
        path: str,
        route_entry: dict[str, Any] | None = None,
    ) -> bool:
        if getattr(self, "_authenticated_device_scope_authorized", False):
            return True
        principal = getattr(self, "_authenticated_principal", None)
        if principal is None or principal.core_role:
            return True
        authorization = authorize_route(
            principal=principal,
            method=method,
            path=path,
            route_entry=route_entry,
        )
        if authorization.allowed:
            return True
        self._send_response(
            APIResponse(False, error=authorization.reason or "Forbidden"),
            authorization.status_code,
        )
        return False
