"""defaults.coding.git_commit — Gitコミットブロック"""

from blocks._common import ok, error
from blocks.coding._approval import approval_required
from blocks.coding._workspace import canonical_mutation_guard
from domain.coding.contract_adapter import (
    GIT_WRITE,
    authorize_legacy_coding_operation,
    git_snapshot,
    invoke_coding_contract,
    preflight_legacy_coding_operation,
    service_payload,
    workspace_id,
)
from domain.safety.audit import record_attempt, record_execution, record_failure


def run(input_data, context=None):
    """コミットを実行する。

    input_data:
        message (str): コミットメッセージ
        paths (list[str]): コミット対象ファイル (optional)
        files (list[str]): paths のエイリアス (optional)
        all_tracked (bool): git add -u (optional)

    returns:
        {"status":"ok","data":{"commit_hash":str,"message":str}}
    """
    message = input_data.get("message")
    if not message:
        return error("'message' is required", code="INVALID_INPUT")

    paths = input_data.get("paths") or input_data.get("files") or None
    all_tracked = bool(input_data.get("all_tracked", False))

    if paths is not None and all_tracked:
        return error(
            "paths/files and all_tracked=True cannot be used together",
            code="INVALID_INPUT",
        )

    operation = "git.commit"
    record_attempt(operation, "high", {"message": message, "paths": paths, "all_tracked": all_tracked})
    try:
        selected_workspace_id = workspace_id(input_data)
        base_arguments = {
            "message": str(message),
            "paths": [str(item) for item in (paths or [])],
            "all_tracked": all_tracked,
        }
        preflight = preflight_legacy_coding_operation(
            legacy_operation=operation,
            input_data=input_data,
            context=context,
            selected_workspace_id=selected_workspace_id,
            mutation_guard=canonical_mutation_guard,
        )
        if not preflight.get("authorized"):
            if preflight.get("reason") == "approval_required":
                return ok(approval_required(operation, "high", args=input_data, message=message))
            denied = error(
                str(preflight.get("message") or preflight.get("reason")),
                code=str(preflight.get("code") or "APPROVAL_INVALID"),
                details=preflight.get("details"),
            )
            denied["_http_status"] = 409 if preflight.get("code") == "ADAPTIVE_LEASE_HELD" else 403
            return denied
        arguments = {
            **base_arguments,
            **git_snapshot(
                selected_workspace_id,
                paths=base_arguments["paths"] if paths is not None else None,
                capture_commit=True,
                all_tracked=all_tracked,
            ),
        }
        for key in (
            "expected_head",
            "expected_tree",
            "expected_status_hash",
            "expected_mount_revision",
        ):
            if key in input_data:
                arguments[key] = input_data[key]
        missing_snapshot = [
            key
            for key in (
                "expected_head",
                "expected_tree",
                "expected_status_hash",
                "expected_mount_revision",
            )
            if str(arguments.get(key) or "").strip() == ""
        ]
        if missing_snapshot:
            return error(
                "Git commit requires an explicit repository snapshot: "
                + ", ".join(missing_snapshot),
                code="INVALID_INPUT",
            )
        authorization = authorize_legacy_coding_operation(
            legacy_operation=operation,
            service_pack_id="rumi_git_write_pack",
            service_operation="git.commit",
            authority="git.write",
            arguments=arguments,
            input_data=input_data,
            context=context,
            selected_workspace_id=selected_workspace_id,
            mutation_guard=canonical_mutation_guard,
        )
        if not authorization.get("authorized"):
            if authorization.get("reason") == "approval_required":
                return ok(approval_required(operation, "high", args=input_data, message=message))
            return error(
                str(authorization.get("message") or authorization.get("reason")),
                code=str(authorization.get("code") or "APPROVAL_INVALID"),
            )
        result = invoke_coding_contract(
            GIT_WRITE,
            "commit",
            service_payload(authorization, arguments),
        )
        record_execution(operation, "high", {"message": message, "paths": paths}, commit_hash=result.get("commit_hash"))
        return ok(result)
    except ValueError as e:
        return error(str(e), code="INVALID_INPUT")
    except Exception as e:
        record_failure(operation, "high", str(e), {"message": message, "paths": paths})
        return error(str(e), code="GIT_ERROR")
