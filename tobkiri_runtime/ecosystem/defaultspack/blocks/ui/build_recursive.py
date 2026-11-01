
from blocks._common import error


from domain.tool.ui_compiler_tools import ui_build_recursive
from domain.tool_policy.internal_context import (
    internal_tool_decision_allows,
    tool_server_approval_context_is_internal,
)


def run(input_data, context):
    if not _authorized(context):
        return error(
            "build-recursive requires local approved tool context",
            "APPROVAL_REQUIRED",
        )
    return ui_build_recursive(input_data, context if isinstance(context, dict) else {})


def _authorized(context):
    return tool_server_approval_context_is_internal(context) or internal_tool_decision_allows(context)
