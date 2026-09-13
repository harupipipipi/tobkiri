"""defaults.coding.context — coding workspace context."""

from blocks._common import ok, error
from domain.coding.contract_adapter import FILE_INSPECT, GIT_READ, invoke_coding_contract, workspace_id


def run(input_data, context=None):
    """Return a compact workspace context for coding UI wiring."""
    directory = input_data.get("directory", ".")
    try:
        selected_workspace_id = workspace_id(input_data)
        listed = invoke_coding_contract(
            FILE_INSPECT,
            "list",
            {"workspace_id": selected_workspace_id, "directory": directory, "recursive": False},
        )
        entries = list(listed.get("items") or [])
        try:
            branch_result = invoke_coding_contract(
                GIT_READ, "branch", {"workspace_id": selected_workspace_id}
            )
            branch = _current_branch(str(branch_result.get("output") or ""))
        except Exception:
            branch = ""
        return ok({
            "branch": branch or None,
            "root_folder": None,
            "directory": directory,
            "files": [item["path"] for item in entries if not item.get("is_dir")],
            "entries": entries,
            "git": {"branch": branch} if branch else None,
            "workspace_id": selected_workspace_id,
        })
    except ValueError as e:
        return error(str(e), code="PATH_TRAVERSAL")
    except Exception as e:
        return error(str(e), code="CONTEXT_ERROR")


def _current_branch(output):
    for line in output.splitlines():
        marker, _, branch = line.partition("\t")
        if marker.strip() == "*":
            return branch.strip()
    return ""
