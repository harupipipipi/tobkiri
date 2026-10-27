"""Sandbox compatibility exports."""

from typing import TYPE_CHECKING, Any

__all__ = ["SandboxManager"]

if TYPE_CHECKING:
    from .sandbox_manager import SandboxManager


def __getattr__(name: str) -> Any:
    """Load the legacy manager only when a caller explicitly requests it."""

    if name == "SandboxManager":
        from .sandbox_manager import SandboxManager

        return SandboxManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
