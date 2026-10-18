from blocks._common import error, ok
from blocks.coding.workspace._contract import project_mount, snapshot


def run(input_data, context=None):
    del input_data, context
    try:
        state = snapshot()
        return ok(
            {
                "workspaces": [project_mount(item) for item in state.get("mounts", [])],
                "selected_workspace_id": state.get("selected_workspace_id"),
                "revision": state.get("revision"),
            }
        )
    except Exception as exc:
        return error(str(exc), code="WORKSPACE_LIST_ERROR")
