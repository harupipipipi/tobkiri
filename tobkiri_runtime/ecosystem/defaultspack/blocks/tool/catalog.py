import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok
from domain.mobile.tools import annotate_mobile_tool_record
from domain.tool.cloudflare_coverage import cloudflare_tool_record, cloudflare_tool_summary
from domain.tool.permission_resolver import ToolPermissionResolver
from domain.tool.registry import ToolRegistry
from domain.tool.runtime_profile import tool_runtime_profile, tool_runtime_profile_summary
from domain.tool.service_catalog import ToolServiceCatalog


def run(input_data, context):
    registry = ToolRegistry()
    tools = registry.list_tools()
    catalog = ToolServiceCatalog(tools)
    resolver = ToolPermissionResolver()
    records = []
    for tool in tools:
        record = annotate_mobile_tool_record(catalog.compact_record(tool), tool)
        cloudflare = cloudflare_tool_record(tool, record=record)
        runtime_profile = tool_runtime_profile(tool, record=record)
        records.append(
            {
                **record,
                "cloudflare": cloudflare,
                "runtime_profile": runtime_profile,
                "permission": resolver.resolve(tool, context=context if isinstance(context, dict) else {}),
            }
        )
    cloudflare_summary = cloudflare_tool_summary([record["cloudflare"] for record in records])
    runtime_profile_summary = tool_runtime_profile_summary(
        [record["runtime_profile"] for record in records]
    )
    return ok(
        {
            "services": catalog.services(),
            "tools": records,
            "cloudflare": cloudflare_summary,
            "cloudflare_summary": cloudflare_summary,
            "runtime_profiles": runtime_profile_summary,
            "runtime_profile_summary": runtime_profile_summary,
            "count": len(records),
        }
    )
