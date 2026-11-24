"""Data models for the ComputerSeat architecture.

These dataclasses represent targets, capabilities, observation results,
action results, and accessibility tree elements used throughout the
driver chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ComputerTarget:
    """Identifies the target application/window for an action."""

    kind: Literal["desktop", "window", "browser_tab"] = "desktop"
    app: str | None = None
    pid: int | None = None

    # Cross-platform legacy/window fields.
    window_id: int | None = None
    window_title: str | None = None
    window_bounds: dict[str, float] = field(default_factory=dict)

    # Windows.
    hwnd: int | None = None

    # macOS.
    bundle_id: str | None = None

    # Browser targets.
    browser_client_id: str | None = None
    browser_tab_id: int | None = None
    url: str | None = None

    # Coordinates supplied to drivers are interpreted in this space.
    coordinate_space: Literal[
        "screen",
        "window",
        "client",
        "viewport",
        "normalized_1000",
    ] = "window"


@dataclass
class ComputerCapabilities:
    """Declares what a driver can do."""

    can_capture_background_window: bool = False
    can_capture_hidden_window: bool = False
    can_semantic_action: bool = False
    can_dom_action: bool = False
    can_background_click: bool = False
    can_background_type: bool = False
    can_background_set_text_control: bool = False
    can_background_key: bool = False
    can_background_scroll: bool = False
    can_pid_event: bool = False
    can_foreground_action: bool = True
    can_parallel_user_work: bool = False
    requires_foreground_for_capture: bool = False
    requires_user_permission: bool = False


@dataclass
class AXElement:
    """Represents a single element in the accessibility tree."""

    id: str = ""
    role: str = ""
    title: str = ""
    description: str = ""
    value: Any = None
    enabled: bool = True
    frame: dict[str, float] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)


@dataclass
class ObserveResult:
    """Result of an observe operation – screenshot + AX tree + metadata."""

    platform: str = ""
    target_window: dict[str, Any] = field(default_factory=dict)
    screenshot: dict[str, Any] = field(default_factory=dict)
    ax_tree: dict[str, Any] = field(default_factory=dict)
    dom_tree: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, bool] = field(default_factory=dict)
    recommended_next_actions: list[dict[str, Any]] = field(default_factory=list)
    fallback_available: bool = True


@dataclass
class ActionResult:
    """Result of executing an action through a driver."""

    action: str = ""
    driver: str = ""
    executed: bool = False
    confidence: str = "best_effort"
    target_kind: str = "desktop"
    is_fallback: bool = False
    can_parallel_user_work: bool = False
    requires_foreground: bool = False
    uses_physical_input: bool = False
    visibility_state: str | None = None
    render_state: str | None = None
    notes: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
