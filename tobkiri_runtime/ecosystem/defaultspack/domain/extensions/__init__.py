"""Manifest-driven extension discovery and registry APIs."""

from .entrypoints import import_entrypoint, import_module, normalize_module_name
from .activation import selected_extension_pack_ids
from .registry import (
    AgentModeRegistry,
    ChatModeRegistry,
    ExtensionRegistry,
    KnowledgeBackendRegistry,
    LLMRegistry,
    PolicyRegistry,
    PromptRegistry,
    ToolExtensionRegistry,
    TransportRegistry,
    UISurfaceRegistry,
)
from .runtime import get_extension_registry, get_extensions_root, get_extensions_roots

__all__ = [
    "AgentModeRegistry",
    "ChatModeRegistry",
    "ExtensionRegistry",
    "KnowledgeBackendRegistry",
    "LLMRegistry",
    "PolicyRegistry",
    "PromptRegistry",
    "import_entrypoint",
    "import_module",
    "normalize_module_name",
    "ToolExtensionRegistry",
    "TransportRegistry",
    "UISurfaceRegistry",
    "get_extension_registry",
    "get_extensions_root",
    "get_extensions_roots",
    "selected_extension_pack_ids",
]
