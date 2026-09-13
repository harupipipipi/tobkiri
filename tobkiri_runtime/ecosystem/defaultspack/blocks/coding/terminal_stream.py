"""defaults.coding.terminal_stream — ターミナルストリーム実行ブロック"""

from blocks._common import error, ok
from blocks.coding._approval import approval_required
from blocks.coding._workspace import canonical_mutation_guard
from domain.coding.contract_adapter import (
    SHELL_INSPECT,
    TERMINAL_CONTROL,
    authorize_legacy_coding_operation,
    invoke_coding_contract,
    service_payload,
    workspace_id,
)
from domain.safety.audit import record_attempt, record_execution, record_failure


def run(input_data, context=None):
    """コマンドをストリーム実行する（スタブ）。

    input_data:
        command (str): 実行するコマンド
        cwd (str|null, optional): 作業ディレクトリ

    returns:
        {"status":"ok","data":{"command":str,"stream_id":str,"started":true}}
    """
    command = input_data.get("command")
    if not command:
        return error("'command' is required", code="INVALID_INPUT")

    cwd = input_data.get("cwd")

    try:
        operation = "terminal.stream"
        selected_workspace_id = workspace_id(input_data)
        arguments = {
            "command": command,
            "cwd": str(cwd or "."),
            "shell": bool(input_data.get("shell", False)),
        }
        risk = invoke_coding_contract(SHELL_INSPECT, "classify", arguments)
        risk_level = str(risk.get("risk_level") or "high")
        record_attempt(operation, risk_level, {"command": command, "cwd": cwd})
        authorization = authorize_legacy_coding_operation(
            legacy_operation=operation,
            service_pack_id="rumi_terminal_session_pack",
            service_operation="terminal.session.start",
            authority="terminal.session.control",
            arguments=arguments,
            input_data=input_data,
            context=context,
            selected_workspace_id=selected_workspace_id,
            mutation_guard=canonical_mutation_guard,
            allow_without_approval=not bool(risk.get("approval_required")),
        )
        if not authorization.get("authorized"):
            if authorization.get("reason") != "approval_required":
                return error(
                    str(
                        authorization.get("message")
                        or authorization.get("reason")
                    ),
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
                    started=False,
                )
            )
        result = invoke_coding_contract(
            TERMINAL_CONTROL,
            "start",
            service_payload(authorization, arguments),
        )
        record_execution(
            operation,
            risk_level,
            {"command": command, "cwd": cwd},
        )
        return ok(
            {
                **result,
                "stream_id": str(result.get("id") or ""),
                "started": result.get("status") == "running",
            }
        )
    except PermissionError as e:
        record_failure(
            "terminal.stream",
            "medium",
            str(e),
            {"command": command, "cwd": cwd},
        )
        return error(str(e), code="PATH_RESTRICTED")
    except Exception as e:
        record_failure(
            "terminal.stream",
            "medium",
            str(e),
            {"command": command, "cwd": cwd},
        )
        return error(str(e), code="STREAM_ERROR")
