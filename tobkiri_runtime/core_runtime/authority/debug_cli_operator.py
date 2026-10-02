"""Verification bridge for Launcher-signed delegated debug decisions."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .models import AuthorityRequest
from .principal import UNATTRIBUTED_PRINCIPAL_ID


def active_authority_debug_binding(
    *,
    profile_id: str | None,
    conversation_id: str | None,
    principal_id: str,
) -> dict[str, Any]:
    """Return a complete active Launcher binding or no debug binding."""
    try:
        connection_path = Path(os.environ.get("RUMI_VIEWER_HOST_BROKER_CONNECTION") or "")
        file_info = connection_path.lstat()
        if (
            stat.S_ISLNK(file_info.st_mode)
            or not stat.S_ISREG(file_info.st_mode)
            or file_info.st_mode & 0o077
        ):
            return {}
        connection = json.loads(connection_path.read_text(encoding="utf-8"))
        url = str(connection.get("url") or "").rstrip("/")
        token = str(connection.get("token") or "")
        parsed = urllib.parse.urlsplit(url)
        port = int(connection.get("port") or 0)
        if (
            connection.get("version") != 1
            or connection.get("host") != "127.0.0.1"
            or parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port != port
            or not 1 <= port <= 65535
            or not token
        ):
            return {}
        broker_request = urllib.request.Request(
            url + "/api/host/debug/status",
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "X-Rumi-Viewer-Broker-Token": token,
            },
        )
        with urllib.request.urlopen(broker_request, timeout=2) as response:
            result = json.loads(response.read().decode("utf-8") or "{}")
    except Exception:
        return {}
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, dict) or status.get("state") != "active":
        return {}
    active_profile = str(status.get("profile_id") or "")
    if profile_id and str(profile_id) != active_profile:
        return {}
    binding: dict[str, Any] = {
        "debug_session_id": str(status.get("session_id") or ""),
        "lease_epoch": int(status.get("lease_epoch") or 0),
        "debug_run_id": str(status.get("run_id") or ""),
        "workspace_identity_digest": str(status.get("workspace_digest") or ""),
        "pack_id": str(status.get("pack_id") or ""),
        "debug_profile_id": active_profile,
        "operation_owner": str(principal_id or status.get("pack_id") or ""),
    }
    if (
        not binding["debug_session_id"]
        or binding["lease_epoch"] <= 0
        or not binding["debug_run_id"]
        or len(binding["workspace_identity_digest"]) != 64
        or not binding["pack_id"]
        or not binding["debug_profile_id"]
        or not (conversation_id or profile_id)
    ):
        return {}
    return binding


def authority_snapshot(request: AuthorityRequest) -> dict[str, Any]:
    resource = dict(request.resource or {})
    target_digest = hashlib.sha256(
        json.dumps(resource, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    snapshot = {
        "request_id": request.request_id,
        "permission_id": request.permission_id,
        "principal_id": request.principal_id,
        "resource": resource,
        "reason": request.reason,
        "risk_level": request.risk_level,
        "created_at": request.created_at,
        "expires_at": request.expires_at,
        "conversation_id": request.conversation_id,
        "profile_id": request.profile_id,
        "node_id": request.node_id,
        "graph_id": request.graph_id,
        "debug_session_id": request.debug_session_id,
        "lease_epoch": request.lease_epoch,
        "debug_run_id": request.debug_run_id,
        "workspace_identity_digest": request.workspace_identity_digest,
        "pack_id": request.pack_id,
        "debug_profile_id": request.debug_profile_id,
        "operation_owner": request.operation_owner,
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"snapshot": snapshot, "digest": digest, "target_digest": target_digest}


def verify_authority_debug_operator(
    request: AuthorityRequest,
    expected_digest: str,
    operator: dict[str, Any] | None,
    *,
    decision: str = "",
) -> tuple[bool, str, dict[str, Any]]:
    if not isinstance(operator, dict):
        return False, "debug_cli_operator is required", {}
    decision = decision or str(operator.get("decision") or "") or "approve"
    snapshot = authority_snapshot(request)
    expected = {
        "kind": "debug_cli_operator",
        "origin": "launcher_debug_cli",
        "scope": "once",
        "decision": decision,
        "request_id": request.request_id,
        "permission_id": request.permission_id,
        "operation": f"authority.{request.permission_id}",
        "tool": request.node_id or str(request.resource.get("kind") or "authority"),
        "action": request.permission_id,
        "conversation_id": (
            request.conversation_id or request.profile_id or request.principal_id or "local"
        ),
        "operation_owner": request.principal_id or UNATTRIBUTED_PRINCIPAL_ID,
        "canonical_arguments_digest": snapshot["digest"],
        "target_digest": snapshot["target_digest"],
        "session_id": request.debug_session_id,
        "lease_epoch": request.lease_epoch,
        "run_id": request.debug_run_id,
        "workspace_digest": request.workspace_identity_digest,
        "pack_id": request.pack_id,
        "profile_id": request.debug_profile_id,
    }
    if not request.debug_session_id:
        return False, "Authority request is not bound to this debug session", {}
    if not hmac.compare_digest(str(expected_digest or ""), snapshot["digest"]):
        return False, "Authority approval request digest changed", {}
    for key, value in expected.items():
        if str(operator.get(key) or "") != str(value or ""):
            return False, f"debug operator {key} mismatch", {}
    try:
        connection_path = Path(os.environ.get("RUMI_VIEWER_HOST_BROKER_CONNECTION") or "")
        file_info = connection_path.lstat()
        if (
            stat.S_ISLNK(file_info.st_mode)
            or not stat.S_ISREG(file_info.st_mode)
            or file_info.st_mode & 0o077
        ):
            raise ValueError("unsafe broker connection file")
        connection = json.loads(connection_path.read_text(encoding="utf-8"))
        url = str(connection.get("url") or "").rstrip("/")
        token = str(connection.get("token") or "")
        parsed = urllib.parse.urlsplit(url)
        port = int(connection.get("port") or 0)
        if (
            connection.get("version") != 1
            or connection.get("host") != "127.0.0.1"
            or parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port != port
            or not 1 <= port <= 65535
            or not token
        ):
            raise ValueError("invalid broker connection")
        body = json.dumps(
            {"debug_cli_operator": operator, "expected_decision": decision}
        ).encode()
        broker_request = urllib.request.Request(
            url + "/api/host/debug/approval/verify",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "X-Rumi-Viewer-Broker-Token": token,
            },
        )
        with urllib.request.urlopen(broker_request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8") or "{}")
    except Exception:
        return False, "Launcher rejected debug operator", {}
    if result.get("ok") is not True or result.get("verified") is not True:
        return False, "Launcher rejected debug operator", {}
    return (
        True,
        "",
        {
            "operator_kind": "debug_cli_operator",
            "operator_origin": "launcher_debug_cli",
            "decision_source": "delegated_debug_cli",
            "human_approved": False,
            "session_id": str(operator.get("session_id") or ""),
            "run_id": str(operator.get("run_id") or ""),
            "workspace_digest": str(operator.get("workspace_digest") or ""),
        },
    )
