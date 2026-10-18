from __future__ import annotations

import base64
from typing import Any

from ._agent_os_common import err, now_slug
from .sandbox_tools import _require_server_side_approval
from .schema_adapter import list_or_empty, mapping_or_empty


def desktop_list(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    del arguments
    desktop_context, _owner_id, context_error = _trusted_desktop_context(context)
    if context_error is not None:
        return context_error
    return _sandbox_api().run({"_handler": "desktops_list"}, desktop_context)


def desktop_create(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    approval_error = _require_server_side_approval(context)
    if approval_error is not None:
        return approval_error
    payload = dict(arguments or {})
    desktop_context, owner_id, context_error = _trusted_desktop_context(context)
    if context_error is not None:
        return context_error
    _apply_trusted_owner(payload, owner_id)
    payload["_handler"] = "desktops_create"
    return _sandbox_api().run(payload, desktop_context)


def desktop_frame(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(arguments or {})
    seat_id = str(payload.get("seat_id") or payload.get("desktop_id") or "").strip()
    if not seat_id:
        return err("'seat_id' is required", "INVALID_INPUT")
    payload["seat_id"] = seat_id
    desktop_context, owner_id, context_error = _trusted_desktop_context(context)
    if context_error is not None:
        return context_error
    _apply_trusted_owner(payload, owner_id)
    payload["_handler"] = "desktop_frame"
    result = _sandbox_api().run(payload, desktop_context)
    if not isinstance(result, dict) or result.get("_binary") is not True:
        return result
    body = result.get("body") or b""
    if not isinstance(body, (bytes, bytearray)):
        return err("desktop frame returned an invalid binary payload", "DESKTOP_FRAME_INVALID")
    headers = mapping_or_empty(result.get("headers"))
    artifacts = list_or_empty(result.get("artifacts"))
    artifact_paths = [
        artifact.get("path")
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("path")
    ]
    return {
        "status": "ok",
        "data": {
            "seat_id": seat_id,
            "content_type": result.get("content_type") or "image/png",
            "data_base64": base64.b64encode(bytes(body)).decode("ascii"),
            "frame_seq": _header_int(headers.get("X-Rumi-Frame-Seq")),
            "width": _header_int(headers.get("X-Rumi-Frame-Width")),
            "height": _header_int(headers.get("X-Rumi-Frame-Height")),
            "captured_at": headers.get("X-Rumi-Captured-At"),
            "artifact_paths": artifact_paths,
        },
        "artifacts": artifacts,
        "artifact_paths": artifact_paths,
    }


def desktop_frame_evidence(
    arguments: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit, export, or delete approved visual QA frame evidence."""
    approval_error = _require_server_side_approval(context)
    if approval_error is not None:
        return approval_error
    payload = dict(arguments or {})
    seat_id = str(payload.get("seat_id") or payload.get("desktop_id") or "").strip()
    if not seat_id:
        return err("'seat_id' is required", "INVALID_INPUT")
    payload["seat_id"] = seat_id
    desktop_context, owner_id, context_error = _trusted_desktop_context(context)
    if context_error is not None:
        return context_error
    _apply_trusted_owner(payload, owner_id)
    payload["_handler"] = "desktop_frame_evidence"
    return _sandbox_api().run(payload, desktop_context)


def desktop_input(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    approval_error = _require_server_side_approval(context)
    if approval_error is not None:
        return approval_error
    payload = dict(arguments or {})
    seat_id = str(payload.get("seat_id") or payload.get("desktop_id") or "").strip()
    if not seat_id:
        return err("'seat_id' is required", "INVALID_INPUT")
    payload["seat_id"] = seat_id
    payload.setdefault("client_action_id", f"desktop-input-{now_slug()}")
    desktop_context, owner_id, context_error = _trusted_desktop_context(context)
    if context_error is not None:
        return context_error
    _apply_trusted_owner(payload, owner_id)
    _default_agent(payload, context)
    payload["_handler"] = "desktop_ai_input"
    return _sandbox_api().run(payload, desktop_context)


def desktop_control_acquire(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _desktop_control(arguments, context, handler="desktop_control_acquire", require_token=False)


def desktop_control_renew(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _desktop_control(arguments, context, handler="desktop_control_renew", require_token=True)


def desktop_control_release(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _desktop_control(arguments, context, handler="desktop_control_release", require_token=True)


def desktop_rules_update(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    approval_error = _require_server_side_approval(context)
    if approval_error is not None:
        return approval_error
    payload = dict(arguments or {})
    seat_id = str(payload.get("seat_id") or payload.get("desktop_id") or "").strip()
    if not seat_id:
        return err("'seat_id' is required", "INVALID_INPUT")
    payload["seat_id"] = seat_id
    desktop_context, owner_id, context_error = _trusted_desktop_context(context)
    if context_error is not None:
        return context_error
    _apply_trusted_owner(payload, owner_id)
    payload["_handler"] = "desktop_rules_update"
    return _sandbox_api().run(payload, desktop_context)


def desktop_access_request(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(arguments or {})
    seat_id = str(payload.get("seat_id") or payload.get("desktop_id") or "").strip()
    if not seat_id:
        return err("'seat_id' is required", "INVALID_INPUT")
    payload["seat_id"] = seat_id
    desktop_context, owner_id, context_error = _trusted_desktop_context(context)
    if context_error is not None:
        return context_error
    _apply_trusted_owner(payload, owner_id)
    payload["_handler"] = "desktop_access_request"
    return _sandbox_api().run(payload, desktop_context)


def desktop_access_grant(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    approval_error = _require_server_side_approval(context)
    if approval_error is not None:
        return approval_error
    payload = dict(arguments or {})
    seat_id = str(payload.get("seat_id") or payload.get("desktop_id") or "").strip()
    request_id = str(payload.get("request_id") or "").strip()
    if not seat_id:
        return err("'seat_id' is required", "INVALID_INPUT")
    if not request_id:
        return err("'request_id' is required", "INVALID_INPUT")
    payload["seat_id"] = seat_id
    payload["request_id"] = request_id
    desktop_context, owner_id, context_error = _trusted_desktop_context(context)
    if context_error is not None:
        return context_error
    _apply_trusted_owner(payload, owner_id)
    payload["_handler"] = "desktop_access_grant"
    return _sandbox_api().run(payload, desktop_context)


def _desktop_control(
    arguments: dict[str, Any],
    context: dict[str, Any] | None,
    *,
    handler: str,
    require_token: bool,
) -> dict[str, Any]:
    approval_error = _require_server_side_approval(context)
    if approval_error is not None:
        return approval_error
    payload = dict(arguments or {})
    seat_id = str(payload.get("seat_id") or payload.get("desktop_id") or "").strip()
    if not seat_id:
        return err("'seat_id' is required", "INVALID_INPUT")
    if require_token and not str(payload.get("lease_token") or "").strip():
        return err("'lease_token' is required", "INVALID_INPUT")
    payload["seat_id"] = seat_id
    desktop_context, owner_id, context_error = _trusted_desktop_context(context)
    if context_error is not None:
        return context_error
    _apply_trusted_owner(payload, owner_id)
    payload["_handler"] = handler
    return _sandbox_api().run(payload, desktop_context)


def _trusted_desktop_context(context: dict[str, Any] | None) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    desktop_context = dict(context) if isinstance(context, dict) else {}
    owner_id = _trusted_owner_id(desktop_context)
    if not owner_id:
        return {}, "", err("desktop tools require a server-derived principal context", "DESKTOP_PRINCIPAL_REQUIRED")
    desktop_context["principal_id"] = owner_id
    return desktop_context, owner_id, None


def _trusted_owner_id(context: dict[str, Any]) -> str:
    if context.get("flow_id") == "transport_direct" or context.get("owner_pack") == "defaultspack":
        return "local-user"
    if context.get("source") == "defaultspack_local_ui":
        return "local-user"
    for key in (
        "principal_id",
        "actor_id",
        "user_id",
        "session_id",
        "client_id",
        "authenticated_agent_id",
        "agent_id",
        "actor_agent_id",
    ):
        text = str(context.get(key) or "").strip()
        if text:
            return text[:160]
    return ""


def _apply_trusted_owner(payload: dict[str, Any], owner_id: str) -> None:
    owner_id = str(owner_id or "").strip()[:160]
    if not owner_id:
        return
    payload["owner_id"] = owner_id
    payload["access_owner_id"] = owner_id
    access = payload.get("access") if isinstance(payload.get("access"), dict) else None
    if access is not None:
        access["owner_id"] = owner_id


def _default_agent(payload: dict[str, Any], context: dict[str, Any] | None) -> None:
    if payload.get("agent_id") or payload.get("actor_agent_id"):
        return
    context = context if isinstance(context, dict) else {}
    agent_id = str(
        context.get("agent_id")
        or context.get("actor_id")
        or context.get("user_id")
        or ""
    ).strip()
    if agent_id:
        payload["agent_id"] = agent_id[:160]


def _header_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _sandbox_api():
    try:
        from ecosystem.defaultspack.blocks.sandbox import api
    except ModuleNotFoundError:
        from blocks.sandbox import api  # type: ignore
    return api
