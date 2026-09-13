"""defaults.coding.git_branch — Git branch operations."""

from blocks._common import ok, error
from domain.coding.contract_adapter import (
    GIT_READ,
    invoke_coding_contract,
    workspace_id,
)
from domain.safety.audit import record_attempt, record_failure


def _mutation_operation(action, create):
    if action == "switch":
        return "git.branch.create" if create else "git.branch.switch"
    return ""


def run(input_data, context=None):
    """Read, list, switch, or create/switch git branches."""
    action = input_data.get("action", "current")
    name = input_data.get("name") or input_data.get("branch")
    create = bool(input_data.get("create", False))
    operation = _mutation_operation(action, create)
    audit_args = {"action": action, "branch": name, "create": create}

    try:
        if not operation:
            selected_workspace_id = workspace_id(input_data)
            read = invoke_coding_contract(
                GIT_READ,
                "branch",
                {"workspace_id": selected_workspace_id},
            )
            branches = []
            current = ""
            for line in str(read.get("output") or "").splitlines():
                marker, _, branch = line.partition("\t")
                branch = branch.strip()
                if not branch:
                    continue
                branches.append(branch)
                if marker.strip() == "*":
                    current = branch
            return ok(
                {
                    "action": action,
                    "current": current,
                    "branches": branches,
                    "workspace_id": selected_workspace_id,
                }
            )
        if not name:
            return error("branch is required", code="INVALID_INPUT")
        record_attempt(operation, "high", audit_args)
        message = (
            "Git branch create/switch is unavailable until the Host provides "
            "an exclusive workspace mutation lease"
        )
        record_failure(operation, "high", message, audit_args)
        return error(message, code="GIT_UNAVAILABLE")
    except ValueError as e:
        if operation:
            record_failure(operation, "high", str(e), audit_args)
        return error(str(e), code="INVALID_INPUT")
    except Exception as e:
        if operation:
            record_failure(operation, "high", str(e), audit_args)
        return error(str(e), code="GIT_ERROR")
