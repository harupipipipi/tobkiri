"""Tobkiri CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class CliError(RuntimeError):
    pass


_SECRET_KEY_MARKERS = ("authorization", "cookie", "password", "secret", "token")


def _redact_output(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]"
                if any(marker in str(key).lower() for marker in _SECRET_KEY_MARKERS)
                else _redact_output(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_output(item) for item in value]
    return value


def _app_data_dir() -> Path:
    configured = os.environ.get("TOBKIRI_APP_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "dev.tobkiri.launcher"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home()) / "dev.tobkiri.launcher"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / (
        "dev.tobkiri.launcher"
    )


def _connection_path() -> Path:
    configured = os.environ.get("RUMI_VIEWER_HOST_BROKER_CONNECTION", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else _app_data_dir() / "user_data" / "host_broker" / "connection.json"
    )


def _session_path() -> Path:
    return _app_data_dir() / "user_data" / "debug_cli" / "session.json"


def _safe_json_file(path: Path, *, secret: bool = False) -> dict[str, Any]:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CliError(f"refusing unsafe file: {path}")
        if secret and info.st_mode & 0o077:
            raise CliError(f"refusing overly permissive secret file: {path}")
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except CliError:
        raise
    except Exception as exc:
        raise CliError(f"cannot read {path}") from exc
    if not isinstance(decoded, dict):
        raise CliError(f"invalid JSON object: {path}")
    return decoded


def _safe_secret_text(path: Path) -> str:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise CliError(f"refusing unsafe secret file: {path}")
        return path.read_text(encoding="utf-8").strip()
    except CliError:
        raise
    except Exception as exc:
        raise CliError(f"cannot read secret file: {path}") from exc


def _write_session(value: dict[str, Any] | None) -> None:
    path = _session_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if value is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _loopback_url(value: Any) -> str:
    url = str(value or "").rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in {"", "/"}:
        raise CliError("broker URL is not strict IPv4 loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port is None:
        raise CliError("broker URL is invalid")
    return url


def _broker_request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    connection = _safe_json_file(_connection_path(), secret=True)
    if connection.get("version") != 1 or connection.get("host") != "127.0.0.1":
        raise CliError("Launcher broker connection is invalid")
    url = _loopback_url(connection.get("url"))
    token = str(connection.get("token") or "")
    if not token:
        raise CliError("Launcher broker token is unavailable")
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url + path,
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Rumi-Viewer-Broker-Token": token,
        },
    )
    return _open_json(request, "Launcher broker")


def _open_json(request: urllib.request.Request, label: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            decoded = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        message = ""
        try:
            failure = json.loads(exc.read().decode("utf-8") or "{}")
            if isinstance(failure, dict):
                candidate = failure.get("error")
                if isinstance(candidate, dict):
                    message = str(candidate.get("message") or candidate.get("code") or "")
                elif candidate is not None:
                    message = str(candidate)
                if not message:
                    data = failure.get("data")
                    if isinstance(data, dict):
                        message = str(data.get("message") or data.get("error") or "")
        except Exception:
            pass
        suffix = f": {message}" if message else ""
        raise CliError(f"{label} returned HTTP {exc.code}{suffix}") from exc
    except Exception as exc:
        raise CliError(f"{label} is unavailable") from exc
    if not isinstance(decoded, dict):
        raise CliError(f"{label} returned invalid JSON")
    error = decoded.get("error")
    if decoded.get("ok") is False or decoded.get("status") == "error":
        message = error.get("message") if isinstance(error, dict) else error
        raise CliError(str(message or f"{label} rejected the request"))
    return decoded


def _latest_manifest() -> dict[str, Any]:
    configured = os.environ.get("TOBKIRI_DEBUG_LAUNCH_MANIFEST", "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend(
        [
            Path.cwd() / ".tmp" / "rumi-viewer-defaultspack-debug" / "latest.json",
            Path(__file__).resolve().parents[2]
            / ".tmp"
            / "rumi-viewer-defaultspack-debug"
            / "latest.json",
        ]
    )
    for path in candidates:
        if path.is_file():
            return _safe_json_file(path)
    raise CliError("Defaultspack debug launch was not found; set TOBKIRI_DEBUG_LAUNCH_MANIFEST")


def _session() -> dict[str, Any]:
    return _safe_json_file(_session_path())


def _debug_binding_query() -> dict[str, Any]:
    session = _session()
    status = _broker_request("GET", "/api/host/debug/status").get("status") or {}
    expected = {
        "debug_session_id": str(session.get("session_id") or ""),
        "lease_epoch": int(session.get("lease_epoch") or 0),
        "debug_run_id": str(session.get("run_id") or ""),
        "workspace_identity_digest": str(session.get("workspace_digest") or ""),
        "pack_id": str(session.get("pack_id") or ""),
        "profile_id": str(session.get("profile_id") or ""),
    }
    if (
        status.get("state") != "active"
        or str(status.get("session_id") or "") != expected["debug_session_id"]
        or int(status.get("lease_epoch") or 0) != expected["lease_epoch"]
        or str(status.get("run_id") or "") != expected["debug_run_id"]
        or str(status.get("workspace_digest") or "") != expected["workspace_identity_digest"]
        or str(status.get("pack_id") or "") != expected["pack_id"]
        or str(status.get("profile_id") or "") != expected["profile_id"]
    ):
        raise CliError("Launcher debug session binding is no longer active")
    return expected


def _api_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session = _session()
    token_path = Path(str(session.get("api_token_file") or ""))
    token = _safe_secret_text(token_path)
    if not token:
        raise CliError("Defaultspack API token is empty")
    url = str(session.get("defaultspack_url") or "").rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Rumi-CSRF": f"debug-cli-{uuid.uuid4().hex}",
        },
    )
    decoded = _open_json(request, "Defaultspack")
    data = decoded.get("data") if decoded.get("status") == "ok" else decoded
    if not isinstance(data, dict):
        raise CliError("Defaultspack returned invalid data")
    return data


def _api_resume(
    conversation_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Resume an approved conversation without exposing its one-shot token."""
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    followup = (
        metadata.get("approval_followup")
        if isinstance(metadata.get("approval_followup"), dict)
        else {}
    )
    tool_name = str(followup.get("tool_name") or "").strip()
    if tool_name.startswith("coding_"):
        resumed = _api_request(
            "POST",
            "/api/coding/approvals/resume",
            {
                "conversation_id": conversation_id,
                "request_id": str(followup.get("request_id") or ""),
                "resume_id": str(followup.get("resume_id") or ""),
            },
        )
        if resumed.get("resumed") is not True:
            raise CliError("Defaultspack rejected delegated coding replay")
        return resumed

    session = _session()
    token = _safe_secret_text(Path(str(session.get("api_token_file") or "")))
    if not token:
        raise CliError("Defaultspack API token is empty")
    base_url = _loopback_url(session.get("defaultspack_url"))
    request = urllib.request.Request(
        base_url + f"/api/chat/conversations/{urllib.parse.quote(conversation_id, safe='')}/stream",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Rumi-CSRF": f"debug-cli-{uuid.uuid4().hex}",
        },
    )
    terminal_event = ""
    approval_requested = False
    try:
        with urllib.request.urlopen(request, timeout=30 * 60) as response:
            content_type = str(response.headers.get("Content-Type") or "")
            if "text/event-stream" not in content_type:
                decoded = json.loads(response.read().decode("utf-8") or "{}")
                if not isinstance(decoded, dict) or decoded.get("status") == "error":
                    raise CliError("Defaultspack rejected delegated debug resume")
                return {"resumed": True, "terminal_event": "response"}
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type") or "")
                if event_type:
                    terminal_event = event_type
                if event_type == "approval_requested":
                    approval_requested = True
                if event_type == "error":
                    error = event.get("error")
                    message = error.get("message") if isinstance(error, dict) else error
                    raise CliError(str(message or "delegated debug resume failed"))
                if event_type == "tool_call_completed" and event.get("approval_replay") is True:
                    if event.get("is_error") is True:
                        raise CliError(
                            str(event.get("message") or "delegated debug replay failed")
                        )
                    return {
                        "resumed": True,
                        "terminal_event": "tool_call_completed",
                        "approval_requested": approval_requested,
                    }
    except CliError:
        raise
    except urllib.error.HTTPError as exc:
        raise CliError(f"Defaultspack resume returned HTTP {exc.code}") from exc
    except Exception as exc:
        raise CliError("Defaultspack delegated debug resume is unavailable") from exc
    return {
        "resumed": True,
        "terminal_event": terminal_event or "stream_closed",
        "approval_requested": approval_requested,
    }


def _resume_payload(
    request: dict[str, Any],
    decision: dict[str, Any],
    *,
    source: str,
) -> tuple[str, dict[str, Any]] | None:
    resume_id = str(decision.get("resume_id") or "").strip()
    conversation_id = str(request.get("conversation_id") or "").strip()
    details = request.get("details") if isinstance(request.get("details"), dict) else {}
    if not conversation_id:
        conversation_id = str(details.get("conversation_id") or "").strip()
    if source == "authority":
        return None
    if not resume_id or not conversation_id:
        return None
    request_id = str(request.get("request_id") or "")
    idempotency_key = f"debug-resume-{request_id}-{uuid.uuid4().hex}"
    tool = _request_field(request, ("function_id", "tool", "action"), "")
    action = _request_field(request, ("action", "function_id"), str(request.get("operation") or ""))
    approved_payload = details.get("payload")
    if not isinstance(approved_payload, dict):
        approved_payload = details.get("arguments")
    if not isinstance(approved_payload, dict):
        approved_payload = {}
    metadata = {
        "approval_followup": {
            "action": action,
            "operation": str(request.get("operation") or action),
            "resume_id": resume_id,
            "payload": approved_payload,
            "request_id": request_id,
            "tool_call_id": str(details.get("tool_call_id") or ""),
            "tool_name": tool,
        },
        "runtime_content": "\n".join(
            [
                "A Launcher-authorized delegated debug operator approved the pending operation.",
                "Continue by calling the exact pending tool once with these approved arguments.",
                f"Tool: {tool}",
                f"Operation: {request.get('operation') or action}",
                f"Approval request id: {request_id}",
                "Approved arguments JSON:",
                json.dumps(approved_payload, ensure_ascii=False, sort_keys=True),
            ]
        ),
        "selected_tools": [tool] if tool else [],
    }
    return conversation_id, {
        "idempotency_key": idempotency_key,
        "message": {
            "role": "user",
            "content": "Internal delegated debug approval resume.",
            "metadata": metadata,
        },
        "tools": [tool] if tool else None,
        "params": {
            "tool_choice": "required",
            "tool_policy": {
                **({"selected_tools": [tool]} if tool else {}),
            },
        },
    }


def _request_by_id(request_id: str) -> dict[str, Any]:
    debug_binding = _debug_binding_query()
    result = _api_request(
        "GET",
        "/api/coding/approvals",
        query={
            "include_expired": "true",
            "limit": 500,
            **debug_binding,
        },
    )
    for item in result.get("requests") or []:
        if isinstance(item, dict) and str(item.get("request_id") or "") == request_id:
            return {**item, "_approval_source": "runtime"}
    authority = _api_request(
        "GET",
        f"/api/authority/requests/{request_id}",
        query=debug_binding,
    )
    if authority:
        return {**authority, "_approval_source": "authority"}
    raise CliError("approval request not found")


def _request_field(request: dict[str, Any], keys: tuple[str, ...], fallback: str) -> str:
    details = request.get("details") if isinstance(request.get("details"), dict) else {}
    for key in keys:
        value = str(details.get(key) or "").strip()
        if value:
            return value
    return fallback


def _safe_approval_view(request: dict[str, Any], *, expected_digest: str) -> dict[str, Any]:
    details = request.get("details") if isinstance(request.get("details"), dict) else {}
    return {
        "request_id": str(request.get("request_id") or ""),
        "approval_source": str(
            request.get("approval_source") or request.get("_approval_source") or "runtime"
        ),
        "operation": str(request.get("operation") or request.get("permission_id") or ""),
        "risk_level": str(request.get("risk_level") or ""),
        "status": str(request.get("status") or ""),
        "created_at": request.get("created_at"),
        "expires_at": request.get("expires_at"),
        "expected_digest": expected_digest,
        "tool": _request_field(request, ("function_id", "tool", "action"), ""),
        "conversation_id": str(
            request.get("conversation_id") or details.get("conversation_id") or ""
        ),
        "pack_id": str(request.get("pack_id") or details.get("pack_id") or ""),
        "profile_id": str(request.get("profile_id") or details.get("profile_id") or ""),
    }


def _authority_snapshot(request: dict[str, Any]) -> tuple[str, str]:
    resource = request.get("resource") if isinstance(request.get("resource"), dict) else {}
    target_digest = hashlib.sha256(
        json.dumps(resource, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    snapshot = {
        key: request.get(key)
        for key in (
            "request_id",
            "permission_id",
            "principal_id",
            "resource",
            "reason",
            "risk_level",
            "created_at",
            "expires_at",
            "conversation_id",
            "profile_id",
            "node_id",
            "graph_id",
            "debug_session_id",
            "lease_epoch",
            "debug_run_id",
            "workspace_identity_digest",
            "pack_id",
            "debug_profile_id",
            "operation_owner",
        )
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return digest, target_digest


def _epoch(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError as exc:
        raise CliError("approval request expiry is invalid") from exc


def _signed_operator(request: dict[str, Any]) -> tuple[dict[str, Any], str]:
    decision = str(request.get("_debug_decision") or "")
    if decision not in {"approve", "deny"}:
        raise CliError("debug approval decision is invalid")
    session = _session()
    status = _broker_request("GET", "/api/host/debug/status").get("status") or {}
    if status.get("state") != "active":
        raise CliError("Launcher debug approval is not active")
    request_id = str(request.get("request_id") or "")
    source = str(request.get("_approval_source") or "runtime")
    if source == "authority":
        permission_id = str(request.get("permission_id") or "")
        operation = f"authority.{permission_id}"
        digest, target_digest = _authority_snapshot(request)
        tool = str(
            request.get("node_id") or (request.get("resource") or {}).get("kind") or "authority"
        )
        action = permission_id
        conversation_id = str(
            request.get("conversation_id")
            or request.get("profile_id")
            or request.get("principal_id")
            or "local"
        )
        operation_owner = str(request.get("principal_id") or session.get("pack_id") or "defaultspack")
        expires_at = _epoch(request.get("expires_at"))
    else:
        operation = str(request.get("operation") or "")
        details = request.get("details") if isinstance(request.get("details"), dict) else {}
        permission_id = _request_field(
            request, ("permission_id", "approval_permission_id", "request_id"), request_id
        )
        tool = _request_field(request, ("function_id", "tool", "action"), operation)
        action = _request_field(request, ("action", "function_id"), operation)
        conversation_id = _request_field(
            request, ("conversation_owner", "conversation_id", "profile_id"), "local"
        )
        operation_owner = _request_field(
            request, ("operation_owner", "owner_pack", "pack_id"), str(session.get("pack_id") or "")
        )
        digest = str(request.get("args_hash") or "")
        target_digest = str(details.get("target_digest") or details.get("snapshot_digest") or "")
        expires_at = _epoch(request.get("expires_at"))
    payload = {
        "session_id": session["session_id"],
        "run_id": session["run_id"],
        "workspace_digest": status["workspace_digest"],
        "pack_id": session["pack_id"],
        "profile_id": session["profile_id"],
        "lease_epoch": session["lease_epoch"],
        "session_secret": session["session_secret"],
        "request_id": request_id,
        "permission_id": permission_id,
        "tool": tool,
        "action": action,
        "operation": operation,
        "decision": decision,
        "canonical_arguments_digest": digest,
        "target_digest": target_digest or None,
        "conversation_id": conversation_id,
        "operation_owner": operation_owner,
        "request_expires_at": expires_at,
    }
    result = _broker_request("POST", "/api/host/debug/approval/operator", payload)
    operator = result.get("debug_cli_operator")
    if not isinstance(operator, dict):
        raise CliError("Launcher did not return a debug operator")
    return operator, digest


def _debug_status(_args: argparse.Namespace) -> dict[str, Any]:
    return _broker_request("GET", "/api/host/debug/status")


def _pack_approval_request(args: argparse.Namespace) -> dict[str, Any]:
    _debug_binding_query()
    return _api_request(
        "POST",
        "/api/coding/packs/approval/request",
        {"pack_id": args.pack_id},
    )


def _pack_status(args: argparse.Namespace) -> dict[str, Any]:
    _debug_binding_query()
    return _api_request(
        "GET",
        "/api/coding/packs/status",
        query={"pack_id": args.pack_id},
    )


def _session_start(args: argparse.Namespace) -> dict[str, Any]:
    launcher_status = _broker_request("GET", "/api/host/debug/status").get("status") or {}
    if launcher_status.get("state") not in {"disabled", "pending"}:
        raise CliError("another Launcher debug approval request is already pending or active")
    workspace = Path(args.workspace).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise CliError("workspace must be a directory")
    guardian = _broker_request("GET", "/api/host/debug/guardian").get("guardian") or {}
    run_id = str(guardian.get("run_id") or "")
    if not run_id or guardian.get("guardian_owned") is not True:
        raise CliError("Launcher-owned Defaultspack guardian is unavailable")
    guardian_workspace = Path(str(guardian.get("workspace") or "")).resolve()
    if guardian_workspace != workspace:
        raise CliError("--workspace does not match the Launcher-owned Defaultspack launch")
    token_file = Path(str(guardian.get("api_token_file") or ""))
    port = int(guardian.get("http_port") or 0)
    if not token_file.is_file() or not (1 <= port <= 65535):
        raise CliError("Launcher-owned Defaultspack connection is incomplete")
    _write_session(None)
    session_id = "dbg-" + uuid.uuid4().hex
    claim_secret = secrets.token_urlsafe(48)
    request = {
        "session_id": session_id,
        "run_id": run_id,
        "workspace": str(workspace),
        "pack_id": args.pack_id,
        "profile_id": args.profile_id,
        "claim_secret": claim_secret,
    }
    _broker_request("POST", "/api/host/debug/session/request", request)
    deadline = time.monotonic() + 60 * 60
    while time.monotonic() < deadline:
        status = _broker_request("GET", "/api/host/debug/status").get("status") or {}
        if status.get("state") == "armed" and status.get("session_id") == session_id:
            break
        if status.get("state") == "disabled":
            raise CliError("Launcher rejected or revoked the pending debug session")
        time.sleep(0.5)
    else:
        raise CliError("timed out waiting for native Launcher confirmation")
    result = _broker_request("POST", "/api/host/debug/session/start", request)
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    session_secret = str(result.get("session_secret") or "")
    if status.get("state") != "active" or not session_secret:
        raise CliError("Launcher did not return an active session credential")
    _write_session(
        {
            **{key: value for key, value in request.items() if key != "claim_secret"},
            "session_secret": session_secret,
            "lease_epoch": status.get("lease_epoch"),
            "workspace_digest": hashlib.sha256(str(workspace).encode()).hexdigest(),
            "defaultspack_url": f"http://127.0.0.1:{port}",
            "api_token_file": str(token_file.resolve()),
            "started_at": int(time.time()),
            "expires_at": status.get("expires_at"),
            "duration": status.get("duration"),
        }
    )
    return result


def _session_stop(_args: argparse.Namespace) -> dict[str, Any]:
    session = _session()
    result = _broker_request(
        "POST",
        "/api/host/debug/session/stop",
        {
            "session_id": session["session_id"],
            "run_id": session["run_id"],
            "session_secret": session["session_secret"],
        },
    )
    _write_session(None)
    return result


def _approvals_list(_args: argparse.Namespace) -> dict[str, Any]:
    debug_binding = _debug_binding_query()
    runtime = _api_request(
        "GET",
        "/api/coding/approvals",
        query={
            "status": "pending",
            "include_expired": "false",
            "limit": 100,
            **debug_binding,
        },
    )
    authority = _api_request(
        "GET",
        "/api/authority/requests",
        query={"status": "pending", **debug_binding},
    )
    runtime_pending = [
        _safe_approval_view(
            {**item, "approval_source": "runtime"},
            expected_digest=str(item.get("args_hash") or ""),
        )
        for item in runtime.get("pending") or []
        if isinstance(item, dict)
    ]
    authority_pending = [
        _safe_approval_view(
            {**item, "approval_source": "authority"},
            expected_digest=_authority_snapshot(item)[0],
        )
        for item in authority.get("requests") or authority.get("items") or []
        if isinstance(item, dict) and item.get("status") == "pending"
    ]
    pending = runtime_pending + authority_pending
    return {"pending": pending, "count": len(pending)}


def _approval_show(args: argparse.Namespace) -> dict[str, Any]:
    request = _request_by_id(args.request_id)
    expected_digest = (
        _authority_snapshot(request)[0]
        if request.get("_approval_source") == "authority"
        else str(request.get("args_hash") or "")
    )
    return {
        "request": _safe_approval_view(request, expected_digest=expected_digest),
        "expected_digest": expected_digest,
    }


def _approval_decide(args: argparse.Namespace) -> dict[str, Any]:
    request = _request_by_id(args.request_id)
    source = str(request.get("_approval_source") or "runtime")
    digest = (
        _authority_snapshot(request)[0]
        if source == "authority"
        else str(request.get("args_hash") or "")
    )
    if args.decision == "approve" and args.expected_digest != digest:
        raise CliError("expected digest does not match the current approval snapshot")
    operator, signed_digest = _signed_operator({**request, "_debug_decision": args.decision})
    if signed_digest != digest:
        raise CliError("approval request changed while signing")
    payload = {
        "approval_request_id": args.request_id,
        "expected_digest": digest,
        "debug_cli_operator": operator,
    }
    if args.decision == "deny":
        payload["reason"] = args.reason
    if source == "authority":
        payload["scope"] = "once"
        payload["persist"] = False
        decision = _api_request(
            "POST",
            f"/api/authority/requests/{args.request_id}/{args.decision}",
            payload,
        )
    else:
        decision = _api_request("POST", f"/api/coding/approvals/{args.decision}", payload)
    _broker_request(
        "POST",
        "/api/host/debug/approval/settle",
        {"debug_cli_operator": operator, "outcome": "settled"},
    )
    if args.decision != "approve":
        return decision
    resume = _resume_payload(request, decision, source=source)
    if resume is None:
        return {**decision, "resumed": False, "resume_reason": "no_conversation"}
    conversation_id, resume_request = resume
    try:
        resumed = _api_resume(conversation_id, resume_request)
    except Exception:
        # Preserve the actual resume failure.  The best-effort settlement may
        # itself fail (for example after the operator was already consumed),
        # but that secondary failure must never mask the actionable cause.
        try:
            _broker_request(
                "POST",
                "/api/host/debug/approval/settle",
                {"debug_cli_operator": operator, "outcome": "resume_failed"},
            )
        except CliError:
            pass
        raise
    return {**decision, **resumed}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tobkiri")
    top = parser.add_subparsers(dest="command", required=True)
    debug = top.add_parser("debug")
    commands = debug.add_subparsers(dest="debug_command", required=True)
    status = commands.add_parser("status")
    status.set_defaults(handler=_debug_status)
    session = commands.add_parser("session")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    start = session_commands.add_parser("start")
    start.add_argument("--workspace", required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--pack-id", default="defaultspack")
    start.add_argument("--profile-id", default="debug-cli")
    start.set_defaults(handler=_session_start)
    stop = session_commands.add_parser("stop")
    stop.set_defaults(handler=_session_stop)
    approvals = commands.add_parser("approvals")
    approval_commands = approvals.add_subparsers(dest="approval_command", required=True)
    list_parser = approval_commands.add_parser("list")
    list_parser.set_defaults(handler=_approvals_list)
    show = approval_commands.add_parser("show")
    show.add_argument("request_id")
    show.set_defaults(handler=_approval_show)
    approve = approval_commands.add_parser("approve")
    approve.add_argument("request_id")
    approve.add_argument("--expected-digest", required=True)
    approve.set_defaults(handler=_approval_decide, decision="approve")
    deny = approval_commands.add_parser("deny")
    deny.add_argument("request_id")
    deny.add_argument("--reason", default="denied by delegated debug CLI")
    deny.set_defaults(handler=_approval_decide, decision="deny")
    packs = commands.add_parser("packs")
    pack_commands = packs.add_subparsers(dest="pack_command", required=True)
    pack_request = pack_commands.add_parser("request")
    pack_request.add_argument("pack_id")
    pack_request.set_defaults(handler=_pack_approval_request)
    pack_status = pack_commands.add_parser("status")
    pack_status.add_argument("pack_id")
    pack_status.set_defaults(handler=_pack_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(raw_argv)
        result = args.handler(args)
        print(json.dumps(_redact_output(result), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except CliError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
