"""
cli_adapter.py — CLI adapter sharing backend with frontend UI.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CLIAdapter:
    """Provides CLI access to the same backend services as the web UI."""

    def __init__(self) -> None:
        self._session_id: Optional[str] = None

    def set_session(self, session_id: str) -> None:
        self._session_id = session_id

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def send_message(self, chat_id: str, content: str) -> Dict[str, Any]:
        from ..chat.chat_manager import Message, get_chat_manager
        mgr = get_chat_manager()
        return mgr.add_message(chat_id, Message(role="user", content=content)).to_dict()

    def list_conversations(self) -> List[Dict[str, Any]]:
        from ..chat.chat_manager import get_chat_manager
        return get_chat_manager().list_conversations()

    def list_tools(self) -> List[Dict[str, Any]]:
        from ..tool.tool_manager import get_tool_manager
        return [tool.to_dict() for tool in get_tool_manager().list_all()]

    def list_prompts(self) -> List[Dict[str, Any]]:
        from ..prompt.prompt_manager import get_prompt_manager
        return get_prompt_manager().list_all()

    def get_history(self, chat_id: str) -> Dict[str, Any]:
        from ..chat.chat_manager import get_chat_manager
        return get_chat_manager().get_history_json(chat_id)

    def invoke_tool(self, tool_id: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from ..tool.tool_manager import get_tool_manager
        t = get_tool_manager().get(tool_id)
        if t is None:
            return {"error": f"Tool not found: {tool_id}", "status_code": 404}
        return {"invoked": True, "tool_id": tool_id, "note": "Stub: real invocation via function registry"}


_global_cli_adapter: Optional[CLIAdapter] = None


def get_cli_adapter() -> CLIAdapter:
    global _global_cli_adapter
    if _global_cli_adapter is None:
        _global_cli_adapter = CLIAdapter()
    return _global_cli_adapter
