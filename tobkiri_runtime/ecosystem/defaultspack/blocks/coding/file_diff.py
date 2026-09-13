"""defaults.coding.file_diff — write前のdiff preview."""

import difflib

from blocks._common import error, ok
from domain.coding.contract_adapter import FILE_INSPECT, invoke_coding_contract, workspace_id


def run(input_data, context=None):
    path = input_data.get("path")
    if not path:
        return error("'path' is required", code="INVALID_INPUT")
    if "content" not in input_data:
        return error("'content' is required", code="INVALID_INPUT")
    try:
        selected_workspace_id = workspace_id(input_data)
        current = invoke_coding_contract(
            FILE_INSPECT,
            "read",
            {"workspace_id": selected_workspace_id, "path": path, "max_bytes": 4 * 1024 * 1024},
        )
        before = str(current.get("content") or "")
        after = str(input_data.get("content") or "")
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
            )
        )
        return ok({"path": path, "diff": diff, "has_changes": bool(diff), "workspace_id": selected_workspace_id})
    except PermissionError as exc:
        return error(str(exc), code="PATH_RESTRICTED")
    except ValueError as exc:
        return error(str(exc), code="PATH_TRAVERSAL")
    except Exception as exc:
        return error(str(exc), code="DIFF_ERROR")
