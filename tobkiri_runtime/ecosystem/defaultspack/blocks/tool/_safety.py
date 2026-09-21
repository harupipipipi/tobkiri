from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from blocks._common import ok
from blocks.coding._approval import (
    approval_invalid_response,
    approval_required,
    is_server_approved,
)
from domain.safety.audit import record_attempt, record_execution, record_failure


def approved_or_request(input_data: dict[str, Any], context: dict[str, Any] | None, operation: str, risk: str = "high"):
    invalid = approval_invalid_response(operation, input_data, _error_passthrough)
    if invalid:
        return invalid
    if is_server_approved(context, operation, input_data):
        return None
    return ok(
        approval_required(
            operation,
            risk,
            args=input_data,
            **_approval_details(operation, input_data),
        )
    )


def record_tool_attempt(operation: str, risk: str, input_data: dict[str, Any]) -> None:
    record_attempt(operation, risk, _audit_args(input_data))


def record_tool_execution(operation: str, risk: str, input_data: dict[str, Any], **extra: Any) -> None:
    record_execution(operation, risk, _audit_args(input_data), **extra)


def record_tool_failure(operation: str, risk: str, input_data: dict[str, Any], reason: str, **extra: Any) -> None:
    record_failure(operation, risk, reason, _audit_args(input_data), **extra)


def _approval_details(operation: str, input_data: dict[str, Any]) -> dict[str, Any]:
    del operation
    details = _audit_args(input_data)
    return details


def _audit_args(input_data: dict[str, Any]) -> dict[str, Any]:
    args = input_data if isinstance(input_data, dict) else {}
    summary: dict[str, Any] = {}
    for key in (
        "name",
        "tool_name",
        "server_id",
        "server_name",
        "config_digest",
        "action",
        "id",
        "transport",
        "image",
        "command",
        "type",
    ):
        if key in args:
            summary[key] = args.get(key)
    if "config" in args and isinstance(args.get("config"), dict):
        config = args["config"]
        config_summary = {
            key: config.get(key)
            for key in ("server_id", "name", "transport")
            if key in config
        }
        if "command" in config:
            command = str(config.get("command") or "")
            config_summary["command"] = "[redacted command]" if _looks_sensitive(command) else command
        if "url" in config:
            config_summary["url"] = _redacted_url(config.get("url"))
        summary["config"] = config_summary
    if "parameters" in args:
        summary["has_parameters"] = True
    if "handler_code" in args:
        summary["has_handler_code"] = True
    if "updates" in args and isinstance(args.get("updates"), dict):
        summary["update_keys"] = sorted(str(key) for key in args["updates"].keys())
    if "payload" in args and isinstance(args.get("payload"), dict):
        payload = args["payload"]
        summary["payload_keys"] = sorted(str(key) for key in payload.keys())
    return summary


def _looks_sensitive(value: str) -> bool:
    marker = value.lower()
    return any(token in marker for token in ("token", "secret", "password", "apikey", "api-key", "authorization", "cookie"))


def _redacted_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname or ""
        if not hostname:
            return "[redacted endpoint]"
        netloc = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "[redacted]" if parsed.query else "", ""))
    except ValueError:
        return "[redacted endpoint]"


def _error_passthrough(message: str, code: str = "ERROR"):
    return {
        "status": "error",
        "error": {
            "code": code,
            "message": message,
        },
    }
