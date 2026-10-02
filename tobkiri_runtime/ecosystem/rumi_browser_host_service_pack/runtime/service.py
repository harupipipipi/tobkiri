"""Typed browser observe/control requests for the Viewer host broker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final


OBSERVE_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "browser.session.get",
        "browser.sessions.list",
        "browser.profiles.list",
        "browser.tabs.list",
        "browser.cookies.list",
        "browser.capture.page",
        "browser.downloads.list",
        "browser.runtime.status",
        "browser.extensions.list",
        "browser.devtools.inspect",
    }
)
CONTROL_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "browser.session.create",
        "browser.session.close",
        "browser.profile.create",
        "browser.profile.set_active",
        "browser.profile.delete",
        "browser.profile.clear_cache",
        "browser.profile.clear_cookies",
        "browser.tab.select",
        "browser.navigate",
        "browser.cookies.import",
        "browser.cookies.delete",
        "browser.download.collect",
        "browser.runtime.start",
        "browser.runtime.stop",
        "browser.extensions.install",
        "browser.extensions.remove",
        "browser.devtools.evaluate",
        "browser.network.capture",
    }
)
_FORBIDDEN_ARGUMENTS: Final[frozenset[str]] = frozenset(
    {"approved", "approval_token", "authority_token", "yolo_mode"}
)
_HOST_FUNCTIONS: Final[dict[str, str]] = {
    "browser.session.get": "browser.session",
    "browser.sessions.list": "browser.session",
    "browser.profiles.list": "browser.profiles.list",
    "browser.tabs.list": "browser.tabs",
    "browser.cookies.list": "browser.cookies.list",
    "browser.capture.page": "browser.capture.page",
    "browser.downloads.list": "browser.downloads.list",
    "browser.session.create": "browser.session",
    "browser.session.close": "browser.session",
    "browser.profile.create": "browser.profile.create",
    "browser.profile.set_active": "browser.profile.set_active",
    "browser.profile.delete": "browser.profile.delete",
    "browser.profile.clear_cache": "browser.profile.clear_cache",
    "browser.profile.clear_cookies": "browser.profile.clear_cookies",
    "browser.tab.select": "browser.select_tab",
    "browser.navigate": "browser.open_url",
    "browser.cookies.import": "browser.cookies.import",
    "browser.cookies.delete": "browser.cookies.delete",
    "browser.download.collect": "browser.download.collect",
    "browser.runtime.status": "browser.runtime.status",
    "browser.runtime.start": "browser.runtime.start",
    "browser.runtime.stop": "browser.runtime.stop",
    "browser.extensions.list": "browser.extensions.list",
    "browser.extensions.install": "browser.extensions.install",
    "browser.extensions.remove": "browser.extensions.remove",
    "browser.devtools.inspect": "browser.devtools.inspect",
    "browser.devtools.evaluate": "browser.devtools.evaluate",
    "browser.network.capture": "browser.network.capture",
}


@dataclass(frozen=True)
class BrowserHostService:
    """Build fail-closed browser requests without executing host operations."""

    access: str
    operations: frozenset[str]

    def invoke(
        self,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a HostIntent accepted by the core Authority mediation path."""

        normalized_operation = str(operation or "").strip()
        if normalized_operation not in self.operations:
            return {
                "status": "denied",
                "success": False,
                "error_type": "operation_outside_browser_contract",
                "operation": normalized_operation,
                "access": self.access,
            }
        normalized_arguments = dict(arguments or {})
        forbidden = sorted(_FORBIDDEN_ARGUMENTS.intersection(normalized_arguments))
        if forbidden:
            return {
                "status": "denied",
                "success": False,
                "error_type": "client_authority_material_forbidden",
                "forbidden_arguments": forbidden,
            }
        caller_context = dict(context or {})
        normalized_arguments.pop("_contract_consumer_pack_id", None)
        normalized_arguments.pop(
            "_contract_consumer_function_id",
            normalized_arguments.pop("_source_function_id", ""),
        )
        host_function_id = _HOST_FUNCTIONS.get(normalized_operation)
        if host_function_id is None:
            return {
                "status": "unavailable",
                "success": False,
                "error_type": "browser_host_runner_unavailable",
                "operation": normalized_operation,
            }
        normalized_arguments["_rumi_contract_operation"] = normalized_operation
        return {
            "type": "host_intent",
            "version": 1,
            "operation": "host.intent.execute",
            "args": normalized_arguments,
            "stream": {"enabled": False},
            "reason": str(caller_context.get("reason") or "").strip(),
            "caller": {
                "pack_id": "",
                "function_id": "",
            },
            "conversation_id": str(caller_context.get("conversation_id") or "").strip(),
            "host_function_id": host_function_id,
        }


def create_browser_observer(_context: dict[str, Any] | None = None) -> BrowserHostService:
    """Create the read-only browser observation contract provider."""

    return BrowserHostService(access="observe", operations=OBSERVE_OPERATIONS)


def create_browser_control(_context: dict[str, Any] | None = None) -> BrowserHostService:
    """Create the browser mutation contract provider."""

    return BrowserHostService(access="control", operations=CONTROL_OPERATIONS)


# This capability is captured by the v4 Host only after digest verification.
# The legacy service API above remains a HostIntent builder; the captured
# provider executes the allowlisted runner after exact Broker authorization.
from ecosystem.rumi_browser_host_service_pack.runtime.host_provider import (  # noqa: E402
    BROWSER_HOST_PROVIDER_FACTORIES,
)

HOST_PROVIDER_FACTORY = BROWSER_HOST_PROVIDER_FACTORIES
