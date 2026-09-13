from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..extensions.runtime import get_extension_registry
from .executor import SubagentFactory, ToolExecutor
from .schema_adapter import adapt_tool_definitions, tool_name_from_definition


class ToolBroker:
    """Bridge layer between provider capabilities and tool execution."""

    def __init__(
        self,
        executor: Optional[ToolExecutor] = None,
        *,
        subagent_factory: Optional[SubagentFactory] = None,
    ) -> None:
        self._executor = executor or ToolExecutor(
            subagent_factory=subagent_factory
        )

    @staticmethod
    def supports_native_tool_calling(provider_manifest: Dict[str, Any]) -> bool:
        capabilities = provider_manifest.get("capabilities", {}) or {}
        return bool(
            capabilities.get("native_tool_calling")
            or capabilities.get("tool_calling")
        )

    def select_strategy(
        self,
        provider_manifest: Dict[str, Any],
        tools: List[Dict[str, Any]],
    ) -> str:
        if tools and self.supports_native_tool_calling(provider_manifest):
            return "native"
        return "prompt_fallback"

    def prepare_provider_tools(
        self,
        provider_manifest: Dict[str, Any],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        strategy = self.select_strategy(provider_manifest, tools)
        if strategy == "native":
            return {"strategy": "native", "tools": adapt_tool_definitions(tools)}
        return {
            "strategy": "prompt_fallback",
            "tool_names": [tool_name_from_definition(tool) for tool in tools],
        }

    def execute_tool_intent(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._executor.execute(tool_name, arguments, context)

    def list_tool_extensions(self) -> List[Dict[str, Any]]:
        return get_extension_registry().tools().list(enabled_only=True)
