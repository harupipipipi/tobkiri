import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok
from domain.tool.cloudflare_coverage import cloudflare_tool_record, cloudflare_tool_summary
from domain.tool.permission_resolver import ToolPermissionResolver
from domain.tool.registry import ToolRegistry
from domain.tool.service_catalog import ToolServiceCatalog


def run(input_data, context):
    registry = ToolRegistry()
    tools = registry.list_tools()
    catalog = ToolServiceCatalog(tools)
    resolver = ToolPermissionResolver()
    records = []
    for tool in tools:
        record = catalog.compact_record(tool)
        cloudflare = cloudflare_tool_record(tool, record=record)
        records.append(
            {
                **record,
                "cloudflare": cloudflare,
                "permission": resolver.resolve(tool, context=context if isinstance(context, dict) else {}),
            }
        )
    return ok(
        {
            "services": catalog.services(),
            "tools": records,
            "cloudflare": cloudflare_tool_summary([record["cloudflare"] for record in records]),
            "count": len(records),
        }
    )
