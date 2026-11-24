"""Pure command inspection and risk classification."""

from __future__ import annotations

import hashlib
import json
import ntpath
import shlex
from typing import Any, Callable, Mapping

_READ = {
    "pwd", "ls", "dir", "cat", "head", "tail", "rg", "grep", "find",
    "git status", "git diff", "git log", "git show", "git grep",
    "git ls-files", "git rev-parse", "git blame",
}
_NETWORK = {
    "curl", "wget", "ssh", "scp", "rsync", "git clone", "git fetch",
    "git pull", "git push", "gh", "npm publish", "cargo publish",
}
_INSTALL = {
    "npm install", "npm i", "pnpm install", "yarn add", "pip install",
    "pip3 install", "uv pip install", "cargo install", "brew install",
}
_DESTRUCTIVE = {
    "rm", "rmdir", "del", "erase", "git reset", "git clean",
    "git checkout", "git switch", "chmod", "chown", "sudo",
}
_CREDENTIAL = {
    "gh auth", "git credential", "security find-generic-password",
    "pass", "op read", "aws configure",
}
_METACHARS = (";", "&&", "||", "|", ">", "<", "`", "$(", "${")
_PACKVM_OPERATION = "rumi_shell_policy_pack.shell-inspect"
_PACKVM_SERVICE_OPERATION = "classify"


def create_shell_policy_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create pure shell inspect operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name == "classify":
            return classify(payload)
        if name == "tokenize":
            return {"argv": _argv(payload.get("command")), "executed": False}
        raise ValueError(f"unknown shell policy operation: {name}")

    return operation


def tobkiri_packvm_invoke(
    operation_id: object,
    payload: object,
) -> dict[str, Any]:
    """Run the sealed PackVM shell-policy ABI without Host authority.

    The V4 catalog grants this PackVM entrypoint only the canonical inspect
    operation.  The service action remains data so a caller cannot select a
    different legacy operation by changing the dispatch target.
    """

    if operation_id != _PACKVM_OPERATION:
        raise ValueError("PackVM shell policy operation is not permitted")
    if not isinstance(payload, Mapping):
        raise ValueError("PackVM shell policy payload must be an object")
    service_operation = payload.get("operation")
    if service_operation != _PACKVM_SERVICE_OPERATION:
        raise ValueError("PackVM shell policy service operation is invalid")
    result = create_shell_policy_operation(None)(service_operation, payload)
    if not isinstance(result, dict):
        raise ValueError("PackVM shell policy result must be an object")
    return dict(result)


def classify(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return deterministic risk without executing a command."""
    command = payload.get("command")
    normalized = _normalized(command)
    if not normalized:
        raise ValueError("command is required")
    reasons = []
    shell_syntax = any(marker in normalized for marker in _METACHARS)
    if shell_syntax:
        reasons.append("shell_syntax")
    if _prefix(normalized, _NETWORK):
        reasons.append("network")
    if _prefix(normalized, _INSTALL):
        reasons.append("install")
    if _prefix(normalized, _DESTRUCTIVE):
        reasons.append("destructive")
    if _prefix(normalized, _CREDENTIAL):
        reasons.append("credential")
    if _contains_absolute_path(command):
        reasons.append("outside_workspace_path")
    if any(flag in _argv(command) for flag in ("--fix", "--write", "--bless")):
        reasons.append("write_option")
    read_only = bool(_prefix(normalized, _READ)) and not reasons
    risk = "low" if read_only else "critical" if reasons else "medium"
    risk_reasons = reasons or (["read_only"] if read_only else ["command_execution"])
    return {
        "normalized_command": normalized,
        "command_hash": hashlib.sha256(
            json.dumps(
                {"command": command, "cwd": payload.get("cwd"), "shell": bool(payload.get("shell"))},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest(),
        "classification": risk,
        "risk_level": risk,
        "risk_reasons": risk_reasons,
        "reason": risk_reasons[0],
        "read_only": read_only,
        "approval_required": not read_only,
        "shell_syntax": shell_syntax,
        "executed": False,
    }


def _normalized(command: Any) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(str(item).strip() for item in command if str(item).strip())
    return " ".join(str(command or "").strip().split())


def _argv(command: Any) -> list[str]:
    if isinstance(command, (list, tuple)):
        return [str(item) for item in command]
    return shlex.split(str(command or ""), posix=True)


def _prefix(normalized: str, values: set[str]) -> str | None:
    lower = normalized.casefold()
    for candidate in sorted(values, key=len, reverse=True):
        folded = candidate.casefold()
        if lower == folded or lower.startswith(folded + " "):
            return candidate
    return None


def _contains_absolute_path(command: Any) -> bool:
    try:
        argv = _argv(command)
    except ValueError:
        return False
    for token in argv[1:]:
        if token.startswith("-"):
            continue
        # Home expansion belongs to execution, not inspection. Treat it as
        # outside the workspace without consulting environment or user records.
        if token.startswith(("~", "/", "\\")) or ntpath.isabs(token):
            return True
    return False
