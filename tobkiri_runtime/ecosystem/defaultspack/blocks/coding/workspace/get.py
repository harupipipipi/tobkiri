from blocks._common import error, ok
from blocks.coding.workspace._contract import get_mount, project_mount


def run(input_data, context=None):
    del context
    workspace_id = str(input_data.get("workspace_id") or "").strip()
    if not workspace_id:
        return error("'workspace_id' is required", code="INVALID_INPUT")
    try:
        record = get_mount(workspace_id)
        if record is None:
            return error(f"workspace not found: {workspace_id}", code="WORKSPACE_NOT_FOUND")
        return ok({"workspace": project_mount(record)})
    except Exception as exc:
        return error(str(exc), code="WORKSPACE_GET_ERROR")
