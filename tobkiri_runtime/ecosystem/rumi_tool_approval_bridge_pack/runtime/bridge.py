"""Bind tool invocation scopes to core authority one-shot approvals."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from core_runtime.authority.request_store import AuthorityRequestStore


def create_authorize_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a bridge that verifies authority but never self-approves."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"authorize", "verify"}:
            raise ValueError(f"unknown tool approval operation: {name}")
        scope = _scope(payload)
        if not bool(payload.get("approval_required", False)):
            return {
                "authorized": True,
                "approval_required": False,
                "scope": scope,
                "consumed": False,
            }
        token = str(payload.get("approval_token") or "")
        request_id = str(payload.get("approval_request_id") or "").strip()
        if not token or not request_id:
            return {
                "authorized": False,
                "approval_required": True,
                "reason": "approval_required",
                "scope": scope,
                "request": {
                    "principal_id": scope["caller_id"],
                    "permission_id": scope["authority"],
                    "profile_id": scope["profile_id"],
                    "resource": scope,
                    "risk_level": str(payload.get("risk") or "high"),
                },
            }
        consumed = AuthorityRequestStore().consume_one_shot(
            request_id=request_id,
            principal_id=scope["caller_id"],
            permission_id=scope["authority"],
            resource=scope,
            token=token,
        )
        return {
            "authorized": consumed,
            "approval_required": True,
            "scope": scope,
            "consumed": consumed,
            "reason": "authorized" if consumed else "approval_invalid_or_used",
        }

    return operation


def _scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    caller_id = str(payload.get("caller_id") or "").strip()
    profile_id = str(payload.get("profile_id") or "").strip()
    authority = str(payload.get("authority") or "").strip()
    tool_id = str(payload.get("tool_id") or "").strip()
    if not caller_id or not profile_id or not authority or not tool_id:
        raise ValueError("tool approval scope is incomplete")
    arguments = payload.get("arguments")
    if not isinstance(arguments, Mapping):
        raise ValueError("tool approval arguments must be an object")
    args_hash = hashlib.sha256(
        json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "operation": f"tool.invoke:{tool_id}",
        "tool_id": tool_id,
        "authority": authority,
        "args_hash": args_hash,
        "caller_id": caller_id,
        "profile_id": profile_id,
        "replay_policy": "one_shot",
    }

