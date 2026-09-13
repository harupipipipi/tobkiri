from blocks._common import error
from blocks.coding.workspace._contract import (
    get_mount,
    mutate,
    project_result,
    snapshot,
)


def run(input_data, context=None):
    workspace_id = str(input_data.get("workspace_id") or "").strip()
    if not workspace_id:
        return error("'workspace_id' is required", code="INVALID_INPUT")
    updates = {
        key: input_data.get(key)
        for key in ("label", "root_path", "metadata")
        if key in input_data
    }
    if "workspace_root" in input_data and "root_path" not in updates:
        updates["root_path"] = input_data.get("workspace_root")
    if not updates:
        return error("no workspace updates provided", code="INVALID_INPUT")
    try:
        current = get_mount(workspace_id)
        if current is None:
            return error(f"workspace not found: {workspace_id}", code="WORKSPACE_NOT_FOUND")
        state = snapshot()
        current_metadata = dict(current.get("metadata") or {})
        if "label" in updates:
            current_metadata["label"] = str(updates.get("label") or "").strip()
        if isinstance(updates.get("metadata"), dict):
            current_metadata["metadata"] = {
                **dict(current_metadata.get("metadata") or {}),
                **updates["metadata"],
            }
        if updates.get("root_path"):
            current_metadata["trusted"] = False
            current_metadata["trust_granted_at"] = None
        response = mutate(
            input_data=input_data,
            context=context,
            legacy_operation="workspace.update",
            action="update",
            arguments={
                "workspace_id": workspace_id,
                "root_path": str(updates.get("root_path") or ""),
                "expected_revision": int(state.get("revision") or 0),
                "metadata": current_metadata,
            },
        )
        return project_result(response)
    except (TypeError, ValueError) as exc:
        return error(str(exc), code="INVALID_INPUT")
    except Exception as exc:
        return error(str(exc), code="WORKSPACE_UPDATE_ERROR")
