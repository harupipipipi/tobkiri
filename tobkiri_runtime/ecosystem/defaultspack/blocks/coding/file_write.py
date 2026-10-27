"""defaults.coding.file_write — ファイル書き込みブロック"""

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
    """ファイルに内容を書き込む。

    input_data:
        path (str): 書き込むファイルのパス
        content (str): 書き込む内容

    returns:
        {"status":"ok","data":{"path":str,"size":int,"written":true}}
    """
    path = input_data.get("path")
    if not path:
        return error("'path' is required", code="INVALID_INPUT")

    content = input_data.get("content")
    if content is None:
        return error("'content' is required", code="INVALID_INPUT")
    operation = "file.write"
    record_attempt(operation, "medium", {"path": path})
    try:
        arguments = {
            "path": str(path),
            "content": str(content),
            "encoding": str(input_data.get("encoding") or "utf-8"),
            "expected_sha256": str(input_data.get("expected_sha256") or ""),
        }
        authorization = authorize_legacy_coding_operation(
            legacy_operation=operation,
            service_pack_id="rumi_file_mutation_pack",
            service_operation="file.write",
            authority="file.write",
            arguments=arguments,
            input_data=input_data,
            context=context,
            # Approval is evaluated before workspace resolution so every
            # entrypoint has the same denial contract. A successful execution
            # still requires the canonical workspace_id below.
            selected_workspace_id=str(input_data.get("workspace_id") or ""),
            mutation_guard=canonical_mutation_guard,
        )
        if not authorization.get("authorized"):
            if authorization.get("reason") == "approval_required":
                return ok(approval_required(operation, "medium", args=input_data, path=path))
            denied = error(
                str(authorization.get("message") or authorization.get("reason")),
                code=str(authorization.get("code") or "APPROVAL_INVALID"),
                details=authorization.get("details"),
            )
            denied["_http_status"] = (
                409
                if authorization.get("code") == "ADAPTIVE_LEASE_HELD"
                else 403
            )
            return denied
        workspace_id(input_data)
        data = invoke_coding_contract(
            FILE_MUTATE,
            "write",
            service_payload(authorization, arguments),
        )
        record_execution(operation, "medium", {"path": path, "size": data.get("size")})
        return ok(data)
    except PermissionError as e:
        record_failure(operation, "medium", str(e), {"path": path})
        return error(str(e), code="PATH_RESTRICTED")
    except ValueError as e:
        return error(str(e), code="PATH_TRAVERSAL")
    except Exception as e:
        record_failure(operation, "medium", str(e), {"path": path})
        return error(str(e), code="WRITE_ERROR")
