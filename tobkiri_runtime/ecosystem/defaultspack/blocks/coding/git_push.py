"""defaults.coding.git_push — Gitプッシュブロック"""

import hashlib

from blocks._common import ok, error
from blocks.coding._approval import approval_required
from blocks.coding._workspace import canonical_mutation_guard
from domain.coding.contract_adapter import (
    GIT_PUBLISH,
    GIT_READ,
    authorize_legacy_coding_operation,
    invoke_coding_contract,
    service_payload,
    workspace_id,
)
from domain.safety.audit import record_attempt, record_execution, record_failure


def run(input_data, context=None):
    """プッシュを実行する。

    input_data:
        remote (str, optional): リモート名（デフォルト: "origin"）
        branch (str|null, optional): ブランチ名

    returns:
        {"status":"ok","data":{"remote":str,"branch":str,"pushed":true}}
    """
    remote = input_data.get("remote", "origin")
    branch = input_data.get("branch")

    operation = "git.push"
    record_attempt(operation, "high", {"remote": remote, "branch": branch})
    try:
        selected_workspace_id = workspace_id(input_data)
        remote_result = invoke_coding_contract(
            GIT_READ,
            "remote",
            {"workspace_id": selected_workspace_id},
        )
        remote_url = _remote_url(str(remote_result.get("output") or ""), str(remote))
        if not branch:
            branch_result = invoke_coding_contract(
                GIT_READ,
                "branch",
                {"workspace_id": selected_workspace_id},
            )
            branch = _current_branch(str(branch_result.get("output") or ""))
        if not branch:
            return error("branch is required", code="INVALID_INPUT")
        dry_run = bool(input_data.get("dry_run", False))
        arguments = {
            "remote": str(remote),
            "branch": str(branch),
            "force_with_lease": bool(input_data.get("force_with_lease", False)),
            "set_upstream": bool(input_data.get("set_upstream", False)),
            "dry_run": dry_run,
            "expected_remote_url_hash": hashlib.sha256(
                remote_url.encode("utf-8")
            ).hexdigest(),
        }
        service_name = "dry_run" if dry_run else "push"
        service_operation = f"git.publish.{service_name}"
        authorization = authorize_legacy_coding_operation(
            legacy_operation=operation,
            service_pack_id="rumi_git_publish_pack",
            service_operation=service_operation,
            authority="git.publish",
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
                        remote=remote,
                        branch=branch,
                    )
                )
            return error(
                str(authorization.get("message") or authorization.get("reason")),
                code=str(authorization.get("code") or "APPROVAL_INVALID"),
            )
        result = invoke_coding_contract(
            GIT_PUBLISH,
            service_name,
            service_payload(authorization, arguments),
        )
        result["pushed"] = bool(result.get("published"))
        record_execution(operation, "high", {"remote": remote, "branch": branch})
        return ok(result)
    except Exception as e:
        record_failure(operation, "high", str(e), {"remote": remote, "branch": branch})
        return error(str(e), code="GIT_ERROR")


def _remote_url(output, remote):
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == remote and "(push)" in line:
            return fields[1]
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == remote:
            return fields[1]
    raise ValueError("Git remote is unavailable")


def _current_branch(output):
    for line in output.splitlines():
        marker, _, branch = line.partition("\t")
        if marker.strip() == "*" and branch.strip():
            return branch.strip()
    return ""
