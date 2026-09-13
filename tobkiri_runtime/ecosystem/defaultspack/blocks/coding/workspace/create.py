"""Create canonical workspace mount metadata."""

from __future__ import annotations

import uuid

from blocks._common import error
from blocks.coding.workspace._contract import mutate, project_result, snapshot


def run(input_data, context=None):
    root_path = input_data.get("root_path") or input_data.get("workspace_root")
    if not root_path:
        return error("'root_path' is required", code="INVALID_INPUT")
    try:
        state = snapshot()
        workspace_id = str(input_data.get("workspace_id") or uuid.uuid4())
        metadata = {
            "label": str(input_data.get("label") or "").strip() or workspace_id,
            "trusted": bool(input_data.get("trusted", False)),
            "trust_granted_at": None,
            "metadata": dict(input_data.get("metadata") or {}),
        }
        response = mutate(
            input_data=input_data,
            context=context,
            legacy_operation="workspace.create",
            action="mount",
            arguments={
                "workspace_id": workspace_id,
                "root_path": str(root_path),
                "expected_revision": int(state.get("revision") or 0),
                "metadata": metadata,
            },
        )
        return project_result(response)
    except (TypeError, ValueError) as exc:
        return error(str(exc), code="INVALID_INPUT")
    except Exception as exc:
        return error(str(exc), code="WORKSPACE_CREATE_ERROR")
