from __future__ import annotations



from blocks._common import ok
from domain.tool.introspection import current_tool_names


def run(input_data, context):
    return ok(current_tool_names(input_data if isinstance(input_data, dict) else {}, context))
