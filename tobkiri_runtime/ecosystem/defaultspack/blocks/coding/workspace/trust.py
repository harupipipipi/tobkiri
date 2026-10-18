from blocks._common import error
from blocks.coding.workspace._contract import mutate, project_result, snapshot


def run(input_data, context=None):
    workspace_id = str(input_data.get("workspace_id") or "").strip()
    if not workspace_id:
        return error("'workspace_id' is required", code="INVALID_INPUT")
    try:
        state = snapshot()
        return project_result(
            mutate(
                input_data=input_data,
                context=context,
                legacy_operation="workspace.trust",
                action="trust",
                arguments={
                    "workspace_id": workspace_id,
                    "expected_revision": int(state.get("revision") or 0),
                },
            )
        )
    except KeyError:
        return error(f"workspace not found: {workspace_id}", code="WORKSPACE_NOT_FOUND")
    except (TypeError, ValueError) as exc:
        return error(str(exc), code="INVALID_INPUT")
    except Exception as exc:
        return error(str(exc), code="WORKSPACE_TRUST_ERROR")
