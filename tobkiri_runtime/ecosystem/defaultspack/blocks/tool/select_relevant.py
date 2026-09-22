

from blocks._common import ok
from domain.chat.tool_recommender import search_tools
from domain.tool.loading import split_tools_by_loading
from domain.tool.registry import ToolRegistry


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    query = str(data.get("query") or message.get("content") or message.get("text") or "").strip()
    tools = data.get("tools")
    if tools is None and isinstance(message, dict):
        tools = message.get("tools") or message.get("available_tools")
    if not isinstance(tools, list):
        tools = ToolRegistry().list_tools()
    try:
        limit = int(data.get("limit", 8))
    except (TypeError, ValueError):
        limit = 8
    always_tools, vector_tools = split_tools_by_loading(tools)
    matches = search_tools(query, vector_tools, limit=max(1, min(24, limit)), threshold=0.06)
    selected_ids = {item["tool_id"] for item in matches}
    selected_tools = [
        *always_tools,
        *[tool for tool in vector_tools if str(tool.get("tool_id") or tool.get("name") or "") in selected_ids],
    ]
    return ok(
        {
            "profile_id": data.get("profile_id"),
            "tools": selected_tools,
            "matches": matches,
            "always_tools": [str(tool.get("tool_id") or tool.get("name") or "") for tool in always_tools],
            "selection_reason": "always_loading_plus_vector_docs_schema_skill_metadata",
        }
    )
