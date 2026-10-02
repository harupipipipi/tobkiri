"""Default tools action projection onto captured global Host contracts."""

from __future__ import annotations

from typing import Any, Final, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract


BROWSER_OBSERVE: Final[str] = "rumi.resource.browser.host.v1"
BROWSER_CONTROL: Final[str] = "rumi.action.browser.host.v1"
DESKTOP_OBSERVE: Final[str] = "rumi.resource.desktop.host.v1"
DESKTOP_CONTROL: Final[str] = "rumi.action.desktop.host.v1"
CLIPBOARD_READ: Final[str] = "rumi.resource.clipboard.v1"
CLIPBOARD_WRITE: Final[str] = "rumi.action.clipboard.v1"

_ACTION_MAP: Final[dict[str, tuple[str, str]]] = {
    "browser.session": (BROWSER_OBSERVE, "browser.session.get"),
    "browser.profiles.list": (BROWSER_OBSERVE, "browser.profiles.list"),
    "browser.cookies.list": (BROWSER_OBSERVE, "browser.cookies.list"),
    "browser.open_url": (BROWSER_CONTROL, "browser.navigate"),
    "browser.profile.create": (BROWSER_CONTROL, "browser.profile.create"),
    "browser.profile.set_active": (BROWSER_CONTROL, "browser.profile.set_active"),
    "browser.profile.delete": (BROWSER_CONTROL, "browser.profile.delete"),
    "browser.profile.clear_cache": (
        BROWSER_CONTROL,
        "browser.profile.clear_cache",
    ),
    "browser.profile.clear_cookies": (
        BROWSER_CONTROL,
        "browser.profile.clear_cookies",
    ),
    "browser.cookies.import": (BROWSER_CONTROL, "browser.cookies.import"),
    "browser.cookies.delete": (BROWSER_CONTROL, "browser.cookies.delete"),
    "computer.context": (DESKTOP_OBSERVE, "desktop.state"),
    "computer.state": (DESKTOP_OBSERVE, "desktop.state"),
    "computer.app_context": (DESKTOP_OBSERVE, "desktop.state"),
    "computer.apps": (DESKTOP_OBSERVE, "desktop.applications.list"),
    "computer.list_apps": (DESKTOP_OBSERVE, "desktop.applications.list"),
    "computer.windows": (DESKTOP_OBSERVE, "desktop.windows.list"),
    "computer.list_windows": (DESKTOP_OBSERVE, "desktop.windows.list"),
    "computer.screenshot": (DESKTOP_OBSERVE, "desktop.capture.frame"),
    "computer.observe": (DESKTOP_OBSERVE, "desktop.capture.frame"),
    "computer.ax_tree": (
        DESKTOP_OBSERVE,
        "desktop.accessibility.snapshot",
    ),
    "computer.ocr": (DESKTOP_OBSERVE, "desktop.accessibility.snapshot"),
    "computer.doctor": (DESKTOP_OBSERVE, "desktop.state"),
    "computer.select_app": (DESKTOP_CONTROL, "desktop.application.select"),
    "computer.show_app": (DESKTOP_CONTROL, "desktop.application.activate"),
    "computer.focus_app": (DESKTOP_CONTROL, "desktop.application.activate"),
    "computer.activate_app": (DESKTOP_CONTROL, "desktop.application.activate"),
    "computer.select_window": (DESKTOP_CONTROL, "desktop.window.select"),
    "computer.move": (DESKTOP_CONTROL, "desktop.pointer.move"),
    "computer.click": (DESKTOP_CONTROL, "desktop.pointer.click"),
    "computer.drag": (DESKTOP_CONTROL, "desktop.pointer.drag"),
    "computer.type": (DESKTOP_CONTROL, "desktop.keyboard.type"),
    "computer.key": (DESKTOP_CONTROL, "desktop.keyboard.key"),
    "computer.backspace": (DESKTOP_CONTROL, "desktop.keyboard.key"),
    "computer.scroll": (DESKTOP_CONTROL, "desktop.scroll"),
    "computer.semantic_action": (
        DESKTOP_CONTROL,
        "desktop.accessibility.action",
    ),
    "computer.click_text": (
        DESKTOP_CONTROL,
        "desktop.accessibility.action",
    ),
    "computer.pid_event": (
        DESKTOP_CONTROL,
        "desktop.accessibility.action",
    ),
    "computer.clipboard.read": (CLIPBOARD_READ, "read"),
    "computer.clipboard.get": (CLIPBOARD_READ, "read"),
    "computer.clipboard.write": (CLIPBOARD_WRITE, "write"),
    "computer.clipboard.set": (CLIPBOARD_WRITE, "write"),
    "computer.clipboard.clear": (CLIPBOARD_WRITE, "write"),
}
_FORBIDDEN_ARGUMENTS: Final[frozenset[str]] = frozenset(
    {
        "approved",
        "approval_token",
        "authority_token",
        "viewer_" + "host_approved",
        "yolo_" + "mode",
        "_contract_consumer_pack_id",
        "_contract_consumer_function_id",
        "_host_context",
    }
)


def run_host_contract_action(
    action: str,
    payload: Mapping[str, Any] | None,
    *,
    source_function_id: str,
) -> dict[str, Any]:
    """Project one legacy action onto an active global host contract."""

    normalized_action = str(action or "").strip()
    target = _ACTION_MAP.get(normalized_action)
    if target is None:
        return {
            "status": "unavailable",
            "success": False,
            "error_type": "legacy_host_action_not_migrated",
            "action": normalized_action,
        }
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        return {
            "status": "unavailable",
            "success": False,
            "error_type": "global_host_contract_unavailable",
            "action": normalized_action,
        }
    request = {
        key: value
        for key, value in dict(payload or {}).items()
        if key not in _FORBIDDEN_ARGUMENTS
    }
    if normalized_action == "computer.clipboard.clear":
        request = {"text": ""}
    elif normalized_action == "computer.backspace":
        request = {**request, "key": "BACKSPACE"}
    elif normalized_action == "computer.ocr":
        request = {**request, "include_ocr": True}
    elif target[0] == CLIPBOARD_WRITE:
        request = {"text": str(request.get("text", request.get("content", "")))}
    elif target[0] == CLIPBOARD_READ:
        request = {}
    # ``source_function_id`` is a local routing label.  It is deliberately
    # not serialized as authority metadata; the captured Host session owns
    # caller, Profile, activation, and Plan identity.
    del source_function_id
    result = invoke_global_contract(registry, target[0], target[1], request)
    if not isinstance(result, dict):
        raise RuntimeError("host contract returned an invalid result")
    return result
