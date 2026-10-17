"""defaults.coding.terminal_exec — ターミナルコマンド実行ブロック"""

from blocks._common import error, ok
from blocks.coding._approval import approval_required
from blocks.coding._workspace import canonical_mutation_guard
from domain.coding.contract_adapter import (
    SHELL_EXECUTE,
    SHELL_INSPECT,
    authorize_legacy_coding_operation,
    invoke_coding_contract,
    service_payload,
    workspace_id,
)
from domain.safety.audit import record_attempt, record_execution, record_failure


def run(input_data, context=None):
    """コマンドを実行する。

    input_data:
        command (str): 実行するコマンド
        cwd (str|null, optional): 作業ディレクトリ
        timeout (int, optional): タイムアウト秒数（デフォルト: 30）

    returns:
        {"status":"ok","data":{"command":str,"exit_code":int,"stdout":str,"stderr":str}}
    """
    command = input_data.get("command")
    if not command:
        return error("'command' is required", code="INVALID_INPUT")

    cwd = input_data.get("cwd")
    timeout = input_data.get("timeout", 30)

    try:
        operation = "terminal.exec"
        selected_workspace_id = workspace_id(input_data)
        arguments = {
            "command": command,
            "cwd": str(cwd or "."),
            "timeout": max(1, min(900, int(timeout))),
            "shell": bool(input_data.get("shell", False)),
            "env": dict(input_data.get("env") or {}),
        }
        risk = invoke_coding_contract(
            SHELL_INSPECT,
            "classify",
            arguments,
        )
        risk_level = str(risk.get("risk_level") or "high")
        record_attempt(operation, risk_level, {"command": command, "cwd": cwd})
        authorization = authorize_legacy_coding_operation(
            legacy_operation=operation,
            service_pack_id="rumi_shell_execute_pack",
            service_operation="shell.execute",
            authority="shell.execute",
            arguments=arguments,
            input_data=input_data,
            context=context,
            selected_workspace_id=selected_workspace_id,
            mutation_guard=canonical_mutation_guard,
            allow_without_approval=not bool(risk.get("approval_required")),
        )
        if not authorization.get("authorized"):
            if authorization.get("reason") not in {"approval_required"}:
                return error(
                    str(authorization.get("message") or authorization.get("reason")),
                    code=str(authorization.get("code") or "APPROVAL_INVALID"),
                )
            return ok(
                approval_required(
                    operation,
                    risk_level,
                    args=input_data,
                    command=command,
                    cwd=cwd,
                    risk=risk,
                    classification=risk.get("classification", risk.get("risk_level")),
                    risk_reasons=risk.get("risk_reasons", [risk.get("reason", "command_execution")]),
                    exit_code=None,
                    stdout="",
                    stderr="",
                )
            )
        result = invoke_coding_contract(
            SHELL_EXECUTE,
            "execute",
            service_payload(authorization, arguments),
        )
        if result.get("exit_code") is None:
            record_failure(operation, risk_level, "not executed", {"command": command, "cwd": cwd})
        else:
            record_execution(
                operation,
                risk_level,
                {"command": command, "cwd": cwd},
                exit_code=result.get("exit_code"),
            )
        return ok(result)
    except PermissionError as e:
        record_failure("terminal.exec", "medium", str(e), {"command": command, "cwd": cwd})
        return error(str(e), code="PATH_RESTRICTED")
    except Exception as e:
        record_failure("terminal.exec", "medium", str(e), {"command": command, "cwd": cwd})
        return error(str(e), code="EXEC_ERROR")
