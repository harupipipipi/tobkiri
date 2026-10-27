"""Abstract base class for ComputerSeat drivers.

All drivers must implement this interface. The service layer uses these
methods to interact with desktop applications through different strategies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import ActionResult, ComputerCapabilities, ComputerTarget, ObserveResult


class ComputerDriver(ABC):
    """Abstract base class for all ComputerSeat drivers.

    Each driver provides a specific strategy for observing and interacting
    with desktop applications (e.g. accessibility APIs, CGEvent injection,
    foreground activation, etc.).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this driver."""
        ...

    @property
    @abstractmethod
    def platform(self) -> str:
        """Platform this driver supports (e.g. 'darwin', 'win32')."""
        ...

    @abstractmethod
    def capabilities(self) -> ComputerCapabilities:
        """Return the capabilities of this driver.

        Returns:
            ComputerCapabilities declaring what this driver can do.
        """
        ...

    @abstractmethod
    def observe(self, target: ComputerTarget) -> ObserveResult:
        """Observe the target application/window.

        Args:
            target: The target to observe.

        Returns:
            ObserveResult with screenshot, AX tree, and metadata.
        """
        ...

    @abstractmethod
    def click(
        self,
        target: ComputerTarget,
        x: int = 0,
        y: int = 0,
        button: str = "left",
    ) -> ActionResult:
        """Click at coordinates on the target.

        Args:
            target: The target application/window.
            x: X coordinate.
            y: Y coordinate.
            button: Mouse button ("left", "right", "middle").

        Returns:
            ActionResult describing what happened.
        """
        ...

    @abstractmethod
    def type_text(self, target: ComputerTarget, text: str = "") -> ActionResult:
        """Type text into the target.

        Args:
            target: The target application/window.
            text: The text to type.

        Returns:
            ActionResult describing what happened.
        """
        ...

    @abstractmethod
    def key(self, target: ComputerTarget, key_combo: str = "") -> ActionResult:
        """Send a key combination to the target.

        Args:
            target: The target application/window.
            key_combo: Key combination string (e.g. "cmd+s").

        Returns:
            ActionResult describing what happened.
        """
        ...

    @abstractmethod
    def scroll(
        self,
        target: ComputerTarget,
        x: int = 0,
        y: int = 0,
        direction: str = "down",
        clicks: int = 3,
    ) -> ActionResult:
        """Scroll at a position on the target.

        Args:
            target: The target application/window.
            x: X coordinate.
            y: Y coordinate.
            direction: Scroll direction.
            clicks: Number of scroll clicks.

        Returns:
            ActionResult describing what happened.
        """
        ...

    @abstractmethod
    def semantic_action(
        self,
        target: ComputerTarget,
        intent: str = "",
        element_or_point: Any = None,
    ) -> ActionResult:
        """Execute a semantic action on the target.

        Args:
            target: The target application/window.
            intent: Natural language intent (e.g. "press Save button").
            element_or_point: The AX element or coordinate to act on.

        Returns:
            ActionResult describing what happened.
        """
        ...

    def move(
        self,
        target: ComputerTarget,
        x: int = 0,
        y: int = 0,
    ) -> ActionResult:
        """Move cursor to coordinates. Optional – not all drivers support this.

        Args:
            target: The target application/window.
            x: X coordinate.
            y: Y coordinate.

        Returns:
            ActionResult describing what happened.
        """
        return ActionResult(
            action="move", driver=self.name, executed=False,
            confidence="not_supported",
            notes=[f"{self.name} does not support move"],
        )

    def set_text_control(
        self,
        target: ComputerTarget,
        text: str = "",
        selector: dict[str, Any] | None = None,
    ) -> ActionResult:
        """Replace a verified semantic text control without foreground input."""
        return ActionResult(
            action="set_text_control", driver=self.name, executed=False,
            confidence="not_supported",
            notes=[f"{self.name} does not support set_text_control"],
        )

    def drag(
        self,
        target: ComputerTarget,
        x1: int = 0,
        y1: int = 0,
        x2: int = 0,
        y2: int = 0,
    ) -> ActionResult:
        """Drag from one point to another. Optional – not all drivers support this.

        Args:
            target: The target application/window.
            x1: Start X coordinate.
            y1: Start Y coordinate.
            x2: End X coordinate.
            y2: End Y coordinate.

        Returns:
            ActionResult describing what happened.
        """
        return ActionResult(
            action="drag", driver=self.name, executed=False,
            confidence="not_supported",
            notes=[f"{self.name} does not support drag"],
        )

    @abstractmethod
    def is_available(self) -> bool:
        """Check whether this driver is available on the current system.

        Returns:
            True if the driver can be used.
        """
        ...
