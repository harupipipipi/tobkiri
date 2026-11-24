"""defaults.coding.file_patch — old/new replacement patch."""

from blocks._common import error, ok
from blocks.coding._approval import approval_required
from blocks.coding._workspace import canonical_mutation_guard
from domain.coding.contract_adapter import (
    FILE_PATCH,
    authorize_legacy_coding_operation,
    invoke_coding_contract,
    service_payload,
    workspace_id,
)
from domain.safety.audit import record_attempt, record_execution, record_failure


def run(input_data, context=None):
    path = input_data.get("path")
    old = input_data.get("old")
    new = input_data.get("new")
    if not path:
        return error("'path' is required", code="INVALID_INPUT")
    if old is None or new is None:
        return error("'old' and 'new' are required", code="INVALID_INPUT")
    operation = "file.patch"
    record_attempt(operation, "medium", {"path": path})
    try:
        selected_workspace_id = workspace_id(input_data)
        arguments = {
            "path": str(path),
            "old": str(old),
            "new": str(new),
            "encoding": str(input_data.get("encoding") or "utf-8"),
            "expected_sha256": str(input_data.get("expected_sha256") or ""),
        }
        authorization = authorize_legacy_coding_operation(
            legacy_operation=operation,
            service_pack_id="rumi_file_patch_pack",
            service_operation="file.patch",
            authority="file.patch",
            arguments=arguments,
            input_data=input_data,
            context=context,
            selected_workspace_id=selected_workspace_id,
            mutation_guard=canonical_mutation_guard,
        )
        if not authorization.get("authorized"):
            if authorization.get("reason") == "approval_required":
                return ok(approval_required(operation, "medium", args=input_data, path=path))
            return error(
                str(authorization.get("message") or authorization.get("reason")),
                code=str(authorization.get("code") or "APPROVAL_INVALID"),
            )
        result = invoke_coding_contract(
            FILE_PATCH,
            "apply",
            service_payload(authorization, arguments),
        )
        record_execution(operation, "medium", {"path": path})
        return ok(result)
    except PermissionError as exc:
        record_failure(operation, "medium", str(exc), {"path": path})
        return error(str(exc), code="PATH_RESTRICTED")
    except ValueError as exc:
        record_failure(operation, "medium", str(exc), {"path": path})
        return error(str(exc), code="PATCH_ERROR")
    except Exception as exc:
        return error(str(exc), code="PATCH_ERROR")
