"""Typed desktop observe/control requests for the Viewer host broker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final


OBSERVE_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "desktop.state",
        "desktop.applications.list",
        "desktop.windows.list",
        "desktop.accessibility.snapshot",
        "desktop.capture.frame",
    }
)
CONTROL_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "desktop.application.select",
        "desktop.application.activate",
        "desktop.window.select",
        "desktop.pointer.move",
        "desktop.pointer.click",
        "desktop.pointer.drag",
        "desktop.keyboard.type",
        "desktop.keyboard.key",
        "desktop.scroll",
        "desktop.accessibility.action",
    }
)
_FORBIDDEN_ARGUMENTS: Final[frozenset[str]] = frozenset(
    {"approved", "approval_token", "authority_token", "viewer_host_approved", "yolo_mode"}
)
_HOST_FUNCTIONS: Final[dict[str, str]] = {
    "desktop.state": "computer.context",
    "desktop.applications.list": "computer.apps",
    "desktop.windows.list": "computer.windows",
    "desktop.accessibility.snapshot": "computer.ax_tree",
    "desktop.capture.frame": "computer.screenshot",
    "desktop.application.select": "computer.select_app",
    "desktop.application.activate": "computer.show_app",
    "desktop.window.select": "computer.select_window",
    "desktop.pointer.move": "computer.move",
    "desktop.pointer.click": "computer.click",
    "desktop.pointer.drag": "computer.drag",
    "desktop.keyboard.type": "computer.type",
    "desktop.keyboard.key": "computer.key",
    "desktop.scroll": "computer.scroll",
    "desktop.accessibility.action": "computer.semantic_action",
}


@dataclass(frozen=True)
class DesktopHostService:
    """Build desktop HostIntents while retaining Authority outside the pack."""

    access: str
    operations: frozenset[str]

    def invoke(
        self,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a caller-bound HostIntent or a typed denial."""

        normalized_operation = str(operation or "").strip()
        if normalized_operation not in self.operations:
            return {
                "status": "denied",
                "success": False,
                "error_type": "operation_outside_desktop_contract",
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
            "host_function_id": _HOST_FUNCTIONS[normalized_operation],
        }


def create_desktop_observer(_context: dict[str, Any] | None = None) -> DesktopHostService:
    """Create the desktop observation provider."""

    return DesktopHostService(access="observe", operations=OBSERVE_OPERATIONS)


def create_desktop_control(_context: dict[str, Any] | None = None) -> DesktopHostService:
    """Create the desktop control provider."""

    return DesktopHostService(access="control", operations=CONTROL_OPERATIONS)

