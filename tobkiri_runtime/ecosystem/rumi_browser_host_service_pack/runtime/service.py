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
    "browser.capture.page": "computer.screenshot",
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
            "conversation_id": str(
                caller_context.get("conversation_id") or ""
            ).strip(),
            "host_function_id": host_function_id,
        }


def create_browser_observer(_context: dict[str, Any] | None = None) -> BrowserHostService:
    """Create the read-only browser observation contract provider."""

    return BrowserHostService(access="observe", operations=OBSERVE_OPERATIONS)


def create_browser_control(_context: dict[str, Any] | None = None) -> BrowserHostService:
    """Create the browser mutation contract provider."""

    return BrowserHostService(access="control", operations=CONTROL_OPERATIONS)

