"""Authority-owned approval snapshots for MCP server connections."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from domain.coding.workspace_store import WorkspaceStore
from domain.safety import approval


MCP_CONNECT_OPERATION = "tool.mcp_connect"
MCP_APPROVAL_TTL_SECONDS = 120
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRET_ARG_RE = re.compile(
    r"(?i)(--?(?:api[-_]?key|token|secret|password|authorization)(?:=|\s+)).+"
)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _expand_placeholders(value: Any) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER_RE.sub(
            lambda match: os.environ.get(match.group(1), ""),
            value,
        )
    if isinstance(value, list):
        return [_expand_placeholders(item) for item in value]
    if isinstance(value, tuple):
        return [_expand_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _expand_placeholders(item) for key, item in value.items()}
    return value


def _command_parts(command: Any, args: Any = None) -> list[str]:
    if isinstance(command, (list, tuple)):
        parts = [str(item) for item in command if str(item)]
    else:
        text = str(command or "").strip()
        if not text:
            return []
        if args is not None or os.path.exists(text):
            parts = [text]
        else:
            parts = shlex.split(text, posix=os.name != "nt")
    if isinstance(args, (list, tuple)):
        parts.extend(str(item) for item in args)
    elif args is not None:
        parts.append(str(args))
    return parts


def _workspace_scope(
    input_data: dict[str, Any],
    context: dict[str, Any] | None,
) -> dict[str, str]:
    context = context if isinstance(context, dict) else {}
    workspace_id = str(input_data.get("workspace_id") or context.get("workspace_id") or "").strip()
    store = WorkspaceStore()
    if workspace_id:
        record = store.get(workspace_id)
        if record is None:
            raise ValueError(f"workspace not found: {workspace_id}")
        root = str(Path(str(record.get("root_path") or "")).expanduser().resolve())
        return {"workspace_id": workspace_id, "workspace_root": root}

    root_claim = input_data.get("workspace_root") or context.get("workspace_root")
    if root_claim:
        root = str(Path(str(root_claim)).expanduser().resolve(strict=True))
        record = store.find_by_root(root)
        stable_id = str(record.get("workspace_id") or "") if record else ""
        if not stable_id:
            stable_id = "path_" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
        return {"workspace_id": stable_id, "workspace_root": root}

    return {"workspace_id": "global", "workspace_root": ""}


def _safe_url(value: Any) -> str:
    text = str(value or "")
    try:
        parsed = urlsplit(text)
    except ValueError:
        return "<invalid-url>"
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _redact_argument(value: Any, secrets: list[str]) -> str:
    text = str(value)
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    if _SECRET_ARG_RE.search(text):
        text = _SECRET_ARG_RE.sub(r"\1<redacted>", text)
    return text


def build_mcp_snapshot(
    server_id: str,
    config: dict[str, Any],
    *,
    server_source: str,
    input_data: dict[str, Any],
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the exact execution config and a secret-free review snapshot."""
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    transport = str(config.get("transport") or "stdio").strip().lower()
    if transport not in {"stdio", "sse"}:
        raise ValueError("config.transport must be 'stdio' or 'sse'")

    workspace = _workspace_scope(input_data, context)
    expanded = _expand_placeholders(dict(config))
    effective_config = dict(expanded)
    effective_config["server_id"] = str(server_id)
    effective_config["name"] = str(server_id)
    effective_config["transport"] = transport

    configured_env = expanded.get("env") if isinstance(expanded.get("env"), dict) else {}
    configured_headers = (
        expanded.get("headers") if isinstance(expanded.get("headers"), dict) else {}
    )
    secret_values = [str(value) for value in configured_env.values()]
    secret_values.extend(str(value) for value in configured_headers.values())

    if transport == "stdio":
        parts = _command_parts(expanded.get("command"), expanded.get("args"))
        if not parts:
            raise ValueError("config.command is required for stdio transport")
        cwd_value = expanded.get("cwd") or workspace.get("workspace_root") or os.getcwd()
        cwd = str(Path(str(cwd_value)).expanduser().resolve(strict=True))
        if not Path(cwd).is_dir():
            raise ValueError("config.cwd must be a directory")
        effective_config["command"] = parts[0]
        effective_config["args"] = parts[1:]
        effective_config["cwd"] = cwd
        effective_config["env"] = {str(key): str(value) for key, value in configured_env.items()}
        executable = _redact_argument(parts[0], secret_values)
        arguments = [_redact_argument(item, secret_values) for item in parts[1:]]
        endpoint = None
        review_config = {
            "server_id": str(server_id),
            "name": str(server_id),
            "transport": transport,
            "command": shlex.join([executable, *arguments]),
            "args": arguments,
            "cwd": cwd,
            "env": {str(key): "<redacted>" for key in configured_env},
        }
    else:
        url = str(expanded.get("url") or "").strip()
        if not url:
            raise ValueError("config.url is required for sse transport")
        effective_config["url"] = url
        effective_config["headers"] = {
            str(key): str(value) for key, value in configured_headers.items()
        }
        executable = None
        arguments = []
        cwd = None
        endpoint = _safe_url(url)
        review_config = {
            "server_id": str(server_id),
            "name": str(server_id),
            "transport": transport,
            "url": endpoint,
            "headers": {str(key): "<redacted>" for key in configured_headers},
        }

    config_digest = _canonical_digest(effective_config)
    scope_fields = {
        "operation": MCP_CONNECT_OPERATION,
        "server_id": str(server_id),
        "workspace": workspace,
        "config_digest": config_digest,
    }
    scope_digest = _canonical_digest(scope_fields)
    review = {
        "kind": "mcp_connect",
        "server_id": str(server_id),
        "server_source": str(server_source),
        "transport": transport,
        "executable": executable,
        "args": arguments,
        "cwd": cwd,
        "endpoint": endpoint,
        "env": {str(key): "<redacted>" for key in configured_env},
        "headers": {str(key): "<redacted>" for key in configured_headers},
        "capabilities": config.get("capabilities") or [],
        "tools": config.get("tools") or [],
        "network": {
            "access": "remote"
            if transport == "sse"
            else str(config.get("network") or "process-defined"),
            "endpoint": endpoint,
        },
        "filesystem": config.get("filesystem")
        or {
            "workspace_root": workspace.get("workspace_root") or None,
            "access": "process-defined",
        },
        "persistence": {
            "registry": True,
            "tools_registered": True,
            "survives_reconnect": True,
        },
        "consequences": [
            "Starts or contacts the configured MCP server.",
            "Discovers and registers server-provided tools.",
            "May grant those tools network or filesystem access available to the server process.",
        ],
        "workspace": workspace,
        "config_digest": config_digest,
        "config": review_config,
    }
    binding_args = {
        "server_id": str(server_id),
        "workspace": workspace,
        "config_digest": config_digest,
        "approval_scope_digest": scope_digest,
    }
    return {
        "effective_config": effective_config,
        "review": review,
        "binding_args": binding_args,
        "scope_digest": scope_digest,
        "workspace": workspace,
        "config_digest": config_digest,
    }


def create_mcp_approval_request(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Enqueue a shared, secret-free MCP connection approval request."""
    review = dict(snapshot["review"])
    for existing in approval.list_approval_requests(
        status="pending",
        include_expired=False,
        limit=500,
    ):
        existing_details = (
            existing.get("details") if isinstance(existing.get("details"), dict) else {}
        )
        if existing_details.get("approval_kind") != "mcp_connect":
            continue
        if str(existing_details.get("approval_scope_digest") or "") != str(
            snapshot["scope_digest"]
        ):
            continue
        return _approval_payload(existing, review)
    details = {
        "approval_kind": "mcp_connect",
        "server_id": review["server_id"],
        "workspace": dict(snapshot["workspace"]),
        "config_digest": snapshot["config_digest"],
        "approval_scope_digest": snapshot["scope_digest"],
        "review": review,
    }
    request = approval.create_approval_request(
        MCP_CONNECT_OPERATION,
        "high",
        dict(snapshot["binding_args"]),
        expires_in=MCP_APPROVAL_TTL_SECONDS,
        details=details,
    )
    return _approval_payload(request, review)


def _approval_payload(
    request: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    return {
        "approval_required": True,
        "risk_level": "high",
        "operation": MCP_CONNECT_OPERATION,
        "approval_request_id": request["request_id"],
        "args_hash": request["args_hash"],
        "expires_at": request["expires_at"],
        "display_summary": f"Connect MCP server: {review['server_id']}",
        "review": review,
        **review,
    }


def verify_mcp_approval(token: str, snapshot: dict[str, Any]):
    """Validate and consume an MCP approval against the authority snapshot."""
    return approval.verify_execution_token(
        token,
        MCP_CONNECT_OPERATION,
        approval.hash_arguments(snapshot["binding_args"]),
        scope_digest=str(snapshot["scope_digest"]),
    )


def obsolete_mcp_approvals(
    server_id: str,
    *,
    keep_scope_digest: str = "",
    reason: str = "MCP configuration changed",
) -> None:
    """Invalidate unsettled reviews for a server after configuration changes."""
    for request in approval.list_approval_requests(include_expired=True, limit=500):
        details = request.get("details") if isinstance(request.get("details"), dict) else {}
        if details.get("approval_kind") != "mcp_connect":
            continue
        if str(details.get("server_id") or "") != str(server_id):
            continue
        if request.get("status") not in {"pending", "approved", "expired"}:
            continue
        current_scope = str(details.get("approval_scope_digest") or "")
        if keep_scope_digest and current_scope == keep_scope_digest:
            continue
        approval.mark_obsolete(str(request.get("request_id") or ""), reason)
