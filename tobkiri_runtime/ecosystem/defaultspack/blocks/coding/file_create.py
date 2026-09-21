"""defaults.coding.file_create — ファイル新規作成ブロック"""

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
    """ファイルを新規作成する。

    input_data:
        path (str): 作成するファイルのパス
        content (str, optional): 初期内容（デフォルト: ""）

    returns:
        {"status":"ok","data":{"path":str,"created":true}}
    """
    path = input_data.get("path")
    if not path:
        return error("'path' is required", code="INVALID_INPUT")

    content = input_data.get("content", "")
    operation = "file.create"
    record_attempt(operation, "medium", {"path": path})
    try:
        selected_workspace_id = workspace_id(input_data)
        arguments = {
            "path": str(path),
            "content": str(content),
            "encoding": str(input_data.get("encoding") or "utf-8"),
            "expected_sha256": "",
        }
        authorization = authorize_legacy_coding_operation(
            legacy_operation=operation,
            service_pack_id="rumi_file_mutation_pack",
            service_operation="file.create",
            authority="file.create",
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
        data = invoke_coding_contract(
            FILE_MUTATE,
            "create",
            service_payload(authorization, arguments),
        )
        record_execution(operation, "medium", {"path": path})
        return ok(data)
    except FileExistsError as e:
        return error(str(e), code="FILE_EXISTS")
    except PermissionError as e:
        record_failure(operation, "medium", str(e), {"path": path})
        return error(str(e), code="PATH_RESTRICTED")
    except ValueError as e:
        return error(str(e), code="PATH_TRAVERSAL")
    except Exception as e:
        record_failure(operation, "medium", str(e), {"path": path})
        return error(str(e), code="CREATE_ERROR")
