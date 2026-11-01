from __future__ import annotations

import shutil
import subprocess
from typing import Any

from ._agent_os_common import err, ok

SENSITIVE_KEY_PARTS = ("api_key", "authorization", "bearer", "credential", "password", "secret", "token")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _dry_or_command(arguments: dict[str, Any], command: list[str], label: str) -> dict[str, Any]:
    return _dry_or_commands(arguments, [command], label)


def _dry_or_commands(
    arguments: dict[str, Any],
    commands: list[list[str]],
    label: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    commands = [command for command in commands if command]
    data = {
        "dry_run": arguments.get("execute") is not True,
        "commands": commands,
        "tool": label,
    }
    if commands:
        data["command"] = commands[0]
    if payload is not None:
        data["payload"] = _redact(payload)
    if arguments.get("execute") is not True:
        return ok(data)
    if not commands:
        return err(f"{label} has no command to execute", "EMPTY_COMMAND")
    results = []
    for command in commands:
        if shutil.which(command[0]) is None:
            return err(f"{command[0]} is not available", "MISSING_CLI")
        completed = subprocess.run(command, text=True, capture_output=True, timeout=int(arguments.get("timeout") or 60))
        results.append(
            {
                "command": command,
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
        if completed.returncode != 0 and arguments.get("continue_on_error") is not True:
            break
    data["dry_run"] = False
    data["results"] = results
    if results:
        data["exit_code"] = results[-1]["exit_code"]
        data["stdout"] = results[-1]["stdout"]
        data["stderr"] = results[-1]["stderr"]
        if any(result.get("exit_code") for result in results):
            message = str(data["stderr"] or data["stdout"] or f"{label} command failed").strip()
            return err(
                message,
                "COMMAND_FAILED",
                data=data,
            )
    return ok(data)


def _first_text(arguments: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = arguments.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _as_text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None or not str(value).strip():
        return []
    return [str(value).strip()]


def _add_repo(command: list[str], arguments: dict[str, Any]) -> None:
    repo = _first_text(arguments, "repo", "repository")
    if repo:
        command.extend(["--repo", repo])


def _status_label(status: str) -> str:
    return "status:" + status.strip().lower().replace(" ", "-")


def _issue_reference(arguments: dict[str, Any]) -> str:
    return _first_text(arguments, "issue", "issue_number", "number", "issue_id", "id", "key")


def github_search(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    query = str(arguments.get("query") or "")
    kind = str(arguments.get("kind") or "repos")
    return _dry_or_command(arguments, ["gh", "search", kind, query, "--limit", str(arguments.get("limit") or 10)], "github_search")


def github_pr_create(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    command = ["gh", "pr", "create", "--title", str(arguments.get("title") or "PR"), "--body", str(arguments.get("body") or "")]
    if arguments.get("draft", True):
        command.append("--draft")
    return _dry_or_command(arguments, command, "github_pr_create")


def github_issue_create(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _dry_or_command(arguments, ["gh", "issue", "create", "--title", str(arguments.get("title") or "Issue"), "--body", str(arguments.get("body") or "")], "github_issue_create")


def github_issue_update(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    issue = _issue_reference(arguments)
    if not issue:
        return err("GitHub issue update requires issue, issue_number, id, or key", "MISSING_ISSUE")

    command = ["gh", "issue", "edit", issue]
    _add_repo(command, arguments)
    has_edit = False
    title = _first_text(arguments, "title", "summary")
    body = _first_text(arguments, "body", "description")
    if title:
        command.extend(["--title", title])
        has_edit = True
    if body:
        command.extend(["--body", body])
        has_edit = True
    for assignee in _as_text_list(arguments.get("assignee") or arguments.get("assignees")):
        command.extend(["--add-assignee", assignee])
        has_edit = True

    commands = []
    status = _first_text(arguments, "status", "state")
    status_lower = status.lower()
    if status_lower in {"closed", "close", "done", "resolved"}:
        close_command = ["gh", "issue", "close", issue]
        _add_repo(close_command, arguments)
        commands.append(close_command)
    elif status_lower in {"open", "opened", "reopen", "reopened"}:
        reopen_command = ["gh", "issue", "reopen", issue]
        _add_repo(reopen_command, arguments)
        commands.append(reopen_command)
    elif status:
        command.extend(["--add-label", _status_label(status)])
        has_edit = True

    if has_edit:
        commands.insert(0, command)

    comment = _first_text(arguments, "comment", "note")
    if comment:
        comment_command = ["gh", "issue", "comment", issue, "--body", comment]
        _add_repo(comment_command, arguments)
        commands.append(comment_command)
    if not commands:
        return err("GitHub issue update needs a title, body, status, assignee, or comment", "EMPTY_UPDATE")

    payload = {
        "issue": issue,
        "title": title or None,
        "status": status or None,
        "assignee": arguments.get("assignee") or arguments.get("assignees"),
        "comment": comment or None,
    }
    return _dry_or_commands(arguments, commands, "github_issue_update", payload=payload)


def github_issue_list(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    command = ["gh", "issue", "list"]
    _add_repo(command, arguments)
    state = _first_text(arguments, "state")
    status = _first_text(arguments, "status")
    if not state and status.lower() in {"open", "opened", "closed"}:
        state = "closed" if status.lower() == "closed" else "open"
    if state:
        command.extend(["--state", state])
    limit = arguments.get("limit") or 30
    command.extend(["--limit", str(limit)])
    assignee = _first_text(arguments, "assignee")
    if assignee:
        command.extend(["--assignee", assignee])
    for label in _as_text_list(arguments.get("label") or arguments.get("labels")):
        command.extend(["--label", label])
    if status and not state:
        command.extend(["--label", _status_label(status)])
    search = _first_text(arguments, "search", "query")
    if search:
        command.extend(["--search", search])
    return _dry_or_command(arguments, command, "github_issue_list")


def _third_party_issue_sync(
    arguments: dict[str, Any],
    *,
    tool_label: str,
    connector_name: str,
    executable: str,
) -> dict[str, Any]:
    issue = _issue_reference(arguments)
    if not issue:
        return err(f"{tool_label} requires issue, issue_id, id, or key", "MISSING_ISSUE")

    title = _first_text(arguments, "title", "summary")
    description = _first_text(arguments, "body", "description")
    status = _first_text(arguments, "status", "state")
    assignee = _first_text(arguments, "assignee")
    comment = _first_text(arguments, "comment", "note")
    payload = {
        "connector_required": connector_name,
        "issue": issue,
        "title": title or None,
        "description": description or None,
        "status": status or None,
        "assignee": assignee or None,
        "comment": comment or None,
        "external_url": arguments.get("external_url") or arguments.get("url"),
        "metadata": arguments.get("metadata") if isinstance(arguments.get("metadata"), dict) else None,
    }

    update_command = [executable, "issue", "update", issue]
    has_update = False
    if title:
        update_command.extend(["--title", title])
        has_update = True
    if description:
        update_command.extend(["--description", description])
        has_update = True
    if status:
        update_command.extend(["--status", status])
        has_update = True
    if assignee:
        update_command.extend(["--assignee", assignee])
        has_update = True

    commands = [update_command] if has_update else []
    if comment:
        commands.append([executable, "issue", "comment", issue, "--body", comment])
    if not commands:
        return err(f"{tool_label} needs a title, description, status, assignee, or comment", "EMPTY_UPDATE")

    return _dry_or_commands(arguments, commands, tool_label, payload=payload)


def linear_issue_sync(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _third_party_issue_sync(
        arguments,
        tool_label="linear_issue_sync",
        connector_name="linear",
        executable="linear",
    )


def jira_issue_sync(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _third_party_issue_sync(
        arguments,
        tool_label="jira_issue_sync",
        connector_name="jira",
        executable="jira",
    )


def gmail_search(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return ok({"connector_required": "gmail", "dry_run": True, "query": arguments.get("query")})


def gmail_draft(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return ok({"connector_required": "gmail", "dry_run": True, "draft": {k: arguments.get(k) for k in ("to", "subject", "body")}})


def calendar_create(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return ok({"connector_required": "calendar", "dry_run": True, "event": _redact(dict(arguments))})


def drive_create(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return ok({"connector_required": "drive", "dry_run": True, "file": _redact(dict(arguments))})


def drive_export(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return ok({"connector_required": "drive", "dry_run": True, "export": _redact(dict(arguments))})


def slack_send(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return ok({"connector_required": "slack", "dry_run": True, "message": _redact(dict(arguments))})


def discord_send(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return ok({"connector_required": "discord", "dry_run": True, "message": _redact(dict(arguments))})


def line_push(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return ok({"connector_required": "line", "dry_run": True, "message": _redact(dict(arguments))})
