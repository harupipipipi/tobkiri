

from blocks._common import ok, error
from domain.chat.tool_selection_trace import ToolSelectionTraceAccessError, ToolSelectionTraceStore


def run(input_data, context):
    trace_id = str((input_data or {}).get("trace_id") or "").strip()
    if not trace_id:
        return error("trace_id is required", "INVALID_INPUT")
    try:
        trace = ToolSelectionTraceStore().get_authorized(trace_id, context if isinstance(context, dict) else {})
    except ToolSelectionTraceAccessError as exc:
        return error(str(exc), exc.code)
    return ok(trace)
