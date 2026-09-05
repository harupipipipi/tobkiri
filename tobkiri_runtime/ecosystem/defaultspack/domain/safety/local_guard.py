from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Any


LOCAL_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "::1"}

SENSITIVE_CODING_PATHS = {
    "/api/coding/approvals",
    # File routes expose workspace metadata or file contents. They must not be
    # readable from arbitrary browser origins, even when they are read-only.
    "/api/coding/files",
    "/api/coding/files/read",
    "/api/coding/files/search",
    "/api/coding/files/diff",
    "/api/coding/files/write",
    "/api/coding/files/create",
    "/api/coding/files/delete",
    "/api/coding/files/patch",
    "/api/coding/files/snapshot",
    "/api/coding/files/restore",
    "/api/coding/checkpoints",
    "/api/coding/terminal/exec",
    "/api/coding/terminal/stream",
    "/api/coding/git/commit",
    "/api/coding/git/push",
    "/api/coding/approvals/approve",
    "/api/coding/approvals/deny",
    "/api/coding/approvals/resume",
    "/api/coding/packs/approval/request",
    "/api/coding/packs/status",
    "/api/coding/workspaces/update",
    "/api/coding/workspaces/select",
    "/api/coding/workspaces/trust",
    "/api/coding/agent/sessions",
    "/api/coding/agent/sessions/status",
    "/api/coding/agent/sessions/merge-report",
}

METHOD_SENSITIVE_CODING_PATHS = {
    "/api/coding/git/branch": {"POST"},
    "/api/coding/rumi-log": {"POST"},
    "/api/coding/workspaces": {"POST"},
}

SENSITIVE_LOCAL_PATHS = {
    "/api/authority/requests",
    "/api/authority/test/request",
    "/api/authority/browser-ui-operator",
    "/api/authority/browser-exchange",
    "/api/authority/browser-exchange/revoke",
    "/api/browser/artifacts",
    "/api/tools/browser-computer",
    "/api/tools/browser-companion/session",
    "/api/tools/create",
    "/api/tools/mcp/connect",
    "/api/runtime/ensure",
    "/api/runtime/update",
    "/api/runtime/uninstall",
    "/api/container",
    "/api/container/settings",
}

METHOD_SENSITIVE_LOCAL_PATHS = {
    "/api/tools/permissions": {"PUT"},
    "/api/consent/{id}/confirm": {"POST"},
}

SENSITIVE_LOCAL_PREFIXES = (
    "/api/authority/",
    "/api/runtime/operations/",
    "/api/sandboxes/",
    "/api/desktops/",
    "/api/container/",
    "/api/agent/self-improvement/",
    "/api/memory/memo/",
)

METHOD_SENSITIVE_LOCAL_PREFIXES = (
    ("/api/authority/requests/", {"POST", "DELETE"}, ()),
    ("/api/sandboxes", {"POST"}, ()),
    ("/api/desktops", {"POST"}, ()),
    ("/api/tools/", {"PUT", "DELETE"}, ("/api/tools/browser-companion/bridge/",)),
    ("/api/tools/", {"POST"}, ("/api/tools/browser-companion/bridge/", "/api/tools/invoke")),
)


def is_loopback_request(headers: dict[str, Any] | None = None, client_address: Any = None) -> bool:
    del headers
    host = ""
    if isinstance(client_address, (list, tuple)) and client_address:
        host = str(client_address[0])
    elif client_address:
        host = str(client_address)
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in LOCAL_ORIGIN_HOSTS


def origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True
    try:
        parsed = urllib.parse.urlsplit(origin)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = parsed.hostname or ""
    if hostname in LOCAL_ORIGIN_HOSTS:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def csrf_required(method: str, origin: str | None) -> bool:
    return str(method or "").upper() in {"POST", "PUT", "PATCH", "DELETE"} and bool(origin)


def is_sensitive_coding_path(path: str, method: str | None = None) -> bool:
    normalized_path = str(path)
    normalized_method = str(method or "").upper() if method is not None else None
    if normalized_path in SENSITIVE_CODING_PATHS:
        return True
    methods = METHOD_SENSITIVE_CODING_PATHS.get(normalized_path)
    if methods:
        if normalized_method is None:
            return True
        return normalized_method in methods
    if _is_workspace_member_mutation_path(normalized_path, normalized_method):
        return True
    if _is_change_request_sensitive_path(normalized_path, normalized_method):
        return True
    return is_sensitive_local_path(normalized_path, method)


def is_sensitive_local_path(path: str, method: str | None = None) -> bool:
    normalized_path = str(path)
    normalized_method = str(method or "").upper() if method is not None else None
    if normalized_path in SENSITIVE_LOCAL_PATHS:
        return True
    methods = METHOD_SENSITIVE_LOCAL_PATHS.get(normalized_path)
    if methods:
        if normalized_method is None:
            return True
        return normalized_method in methods
    if _matches_confirm_consent_path(normalized_path):
        if normalized_method is None:
            return True
        return normalized_method == "POST"
    if any(normalized_path == prefix.rstrip("/") or normalized_path.startswith(prefix) for prefix in SENSITIVE_LOCAL_PREFIXES):
        return True
    for prefix, methods, exclusions in METHOD_SENSITIVE_LOCAL_PREFIXES:
        if normalized_path.startswith(prefix) and not any(
            normalized_path == excluded.rstrip("/") or normalized_path.startswith(excluded)
            for excluded in exclusions
        ):
            if normalized_method is None:
                return True
            if normalized_method in methods:
                return True
    return False


def _matches_confirm_consent_path(path: str) -> bool:
    return path.startswith("/api/consent/") and path.endswith("/confirm")


def _is_workspace_member_mutation_path(path: str, method: str | None) -> bool:
    prefix = "/api/coding/workspaces/"
    if not path.startswith(prefix):
        return False
    suffix = path[len(prefix):].strip("/")
    if not suffix:
        return False
    parts = suffix.split("/")
    if len(parts) == 1:
        return method is None or method == "PUT"
    if len(parts) == 2 and parts[1] in {"select", "trust"}:
        return method is None or method == "POST"
    return False


def _is_change_request_sensitive_path(path: str, method: str | None) -> bool:
    prefix = "/api/change-requests/"
    if path == "/api/change-requests":
        return method is None or method in {"GET", "POST"}
    if not path.startswith(prefix):
        return False
    suffix = path[len(prefix):].strip("/")
    if not suffix:
        return False
    parts = suffix.split("/")
    if len(parts) == 1:
        return method is None or method in {"GET", "PATCH"}
    if len(parts) == 2 and parts[1] in {
        "refresh",
        "export-patch",
        "comments",
        "decision",
        "viewed-files",
        "checks",
        "run-check",
        "seal",
        "commit",
    }:
        allowed_methods = {
            "refresh": {"POST"},
            "export-patch": {"POST"},
            "comments": {"GET", "POST"},
            "decision": {"POST"},
            "viewed-files": {"GET", "PATCH", "POST"},
            "checks": {"GET", "POST"},
            "run-check": {"POST"},
            "seal": {"GET"},
            "commit": {"POST"},
        }
        return method is None or method in allowed_methods[parts[1]]
    if len(parts) == 3 and parts[1] == "comments":
        return method is None or method in {"GET", "PATCH"}
    if len(parts) == 3 and parts[1] == "checks":
        if parts[2] in {"run", "run-check"}:
            return method is None or method == "POST"
        return method is None or method == "GET"
    return False


def _header_value(headers: Any, name: str) -> str:
    if not headers:
        return ""
    try:
        value = headers.get(name, "")
    except AttributeError:
        value = ""
    if value:
        return str(value)
    lowered = name.lower()
    try:
        items = headers.items()
    except AttributeError:
        return ""
    for key, value in items:
        if str(key).lower() == lowered:
            return str(value)
    return ""


def require_local_guard(
    path: str,
    method: str,
    headers: dict[str, Any] | None,
    client_address: Any = None,
) -> tuple[int, str, str] | None:
    if not is_sensitive_coding_path(path, method):
        return None
    headers = headers or {}
    if not is_loopback_request(headers, client_address):
        return (403, "sensitive local route requires a loopback client", "LOCAL_ONLY_REQUIRED")
    origin = _header_value(headers, "Origin")
    if not origin_allowed(origin):
        return (403, "origin not allowed for sensitive local route", "ORIGIN_DENIED")
    if csrf_required(method, origin) and not _header_value(headers, "X-Rumi-CSRF").strip():
        return (403, "CSRF header required for sensitive local mutation", "CSRF_REQUIRED")
    return None
