from __future__ import annotations



from blocks._common import ok
from domain.chat.tool_recommender import search_tools
from domain.tool.registry import ToolRegistry


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    query = str(data.get("query") or data.get("text") or "").strip()
    try:
        limit = int(data.get("limit", 8))
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(24, limit))
    phase = str(data.get("phase") or "overview").strip().lower()
    include_schema = bool(data.get("include_schema")) or phase == "schema"

    tools = ToolRegistry().list_tools()
    requested_ids = data.get("tool_ids")
    if isinstance(requested_ids, list) and requested_ids:
        requested = {str(item).strip() for item in requested_ids if str(item).strip()}
        tools = [tool for tool in tools if str(tool.get("tool_id") or "") in requested]
        if not query:
            query = " ".join(requested)

    matches = search_tools(query, tools, limit=limit, threshold=0.0 if requested_ids else 0.06, include_schema=include_schema)
    return ok(
        {
            "query": query,
            "phase": "schema" if include_schema else "overview",
            "matches": matches,
            "selection_rule": (
                "First choose by capability from overview results. Only then request schema details "
                "for the concrete tool and produce JSON arguments that match that schema."
            ),
        }
    )
