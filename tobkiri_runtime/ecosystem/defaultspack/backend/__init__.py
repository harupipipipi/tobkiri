"""defaultspack backend compatibility package."""

from typing import TYPE_CHECKING, Any

from .agent import AgentOrchestrator, AgentRole, AgentSpec, TaskStatus, VisibilityScope
from .chat import ChatManager, ChatMessage
from .frontend_support import LayoutConfig, LayoutEngine, PaneConfig
from .knowledge import KnowledgeEntry, KnowledgeManager, KnowledgeStore
from .memory import MemoryEntry, MemoryManager, MemorySurface, MemoryStore, MemoryType, UserModel
from .migration import DefaultsMigrator
from .pack_extension import ExtensionManager, ExtensionRequest, PatchMode
from .cli.cli_adapter import CLIAdapter, get_cli_adapter

__all__ = [
    "AgentOrchestrator",
    "AgentRole",
    "AgentSpec",
    "ChatManager",
    "ChatMessage",
    "CLIAdapter",
    "DefaultsMigrator",
    "ExtensionManager",
    "ExtensionRequest",
    "get_cli_adapter",
    "KnowledgeEntry",
    "KnowledgeManager",
    "KnowledgeStore",
    "LayoutConfig",
    "LayoutEngine",
    "MemoryEntry",
    "MemoryManager",
    "MemoryStore",
    "MemorySurface",
    "MemoryType",
    "PaneConfig",
    "PatchMode",
    "SandboxManager",
    "TaskStatus",
    "UserModel",
    "VisibilityScope",
]

if TYPE_CHECKING:
    from .sandbox import SandboxManager


def __getattr__(name: str) -> Any:
    """Load compatibility exports without side effects during submodule imports."""

    if name == "SandboxManager":
        from .sandbox import SandboxManager

        return SandboxManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
