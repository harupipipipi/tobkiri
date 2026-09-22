"""defaults.coding.git_branch — Git branch operations."""

from blocks._common import ok, error
from blocks.coding._approval import approval_required
from blocks.coding._workspace import canonical_mutation_guard
from domain.coding.contract_adapter import (
    GIT_READ,
    GIT_WRITE,
    authorize_legacy_coding_operation,
    git_snapshot,
    invoke_coding_contract,
    service_payload,
    workspace_id,
)
from domain.safety.audit import record_attempt, record_execution, record_failure


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
        selected_workspace_id = workspace_id(input_data)
        if not operation:
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
        service_name = "branch_create" if create else "branch_switch"
        service_operation = "git.branch_create" if create else "git.branch_switch"
        arguments = {"branch": str(name), **git_snapshot(selected_workspace_id)}
        authorization = authorize_legacy_coding_operation(
            legacy_operation=operation,
            service_pack_id="rumi_git_write_pack",
            service_operation=service_operation,
            authority="git.write",
            arguments=arguments,
            input_data=input_data,
            context=context,
            selected_workspace_id=selected_workspace_id,
            mutation_guard=canonical_mutation_guard,
        )
        if not authorization.get("authorized"):
            if authorization.get("reason") == "approval_required":
                return ok(
                    approval_required(
                        operation,
                        "high",
                        args=input_data,
                        action=action,
                        branch=name,
                        create=create,
                    )
                )
            return error(
                str(authorization.get("message") or authorization.get("reason")),
                code=str(authorization.get("code") or "APPROVAL_INVALID"),
            )
        result = invoke_coding_contract(
            GIT_WRITE,
            service_name,
            service_payload(authorization, arguments),
        )
        record_execution(operation, "high", audit_args)
        return ok(result)
    except ValueError as e:
        if operation:
            record_failure(operation, "high", str(e), audit_args)
        return error(str(e), code="INVALID_INPUT")
    except Exception as e:
        if operation:
            record_failure(operation, "high", str(e), audit_args)
        return error(str(e), code="GIT_ERROR")
