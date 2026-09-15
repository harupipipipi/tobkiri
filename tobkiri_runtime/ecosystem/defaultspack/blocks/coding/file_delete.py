"""defaults.coding.file_delete — ファイル削除ブロック"""

from blocks._common import ok, error
from blocks.coding._approval import approval_required
from blocks.coding._workspace import canonical_mutation_guard
from domain.coding.contract_adapter import (
    FILE_MUTATE,
    authorize_legacy_coding_operation,
    invoke_coding_contract,
    service_payload,
    workspace_id,
)
from domain.safety.audit import record_attempt, record_execution, record_failure


def run(input_data, context=None):
    """ファイルを削除する。

    input_data:
        path (str): 削除するファイルのパス

    returns:
        {"status":"ok","data":{"path":str,"deleted":true}}
    """
    path = input_data.get("path")
    if not path:
        return error("'path' is required", code="INVALID_INPUT")
    operation = "file.delete"
    record_attempt(operation, "high", {"path": path})
    try:
        selected_workspace_id = workspace_id(input_data)
        arguments = {
            "path": str(path),
            "expected_sha256": str(input_data.get("expected_sha256") or ""),
        }
        authorization = authorize_legacy_coding_operation(
            legacy_operation=operation,
            service_pack_id="rumi_file_mutation_pack",
            service_operation="file.delete",
            authority="file.delete",
            arguments=arguments,
            input_data=input_data,
            context=context,
            selected_workspace_id=selected_workspace_id,
            mutation_guard=canonical_mutation_guard,
        )
        if not authorization.get("authorized"):
            if authorization.get("reason") == "approval_required":
                return ok(approval_required(operation, "high", args=input_data, path=path))
            return error(
                str(authorization.get("message") or authorization.get("reason")),
                code=str(authorization.get("code") or "APPROVAL_INVALID"),
            )
        data = invoke_coding_contract(
            FILE_MUTATE,
            "delete",
            service_payload(authorization, arguments),
        )
        record_execution(operation, "high", {"path": path})
        return ok(data)
    except FileNotFoundError as e:
        return error(str(e), code="FILE_NOT_FOUND")
    except PermissionError as e:
        record_failure(operation, "high", str(e), {"path": path})
        return error(str(e), code="PATH_RESTRICTED")
    except ValueError as e:
        return error(str(e), code="PATH_TRAVERSAL")
    except Exception as e:
        record_failure(operation, "high", str(e), {"path": path})
        return error(str(e), code="DELETE_ERROR")
