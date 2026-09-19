"""Ask Tobkiri Launcher to open the dedicated authority approval window.

The presentation Shell has no Host command surface, so Defaultspack surfaces
reach the Launcher through the authenticated host broker instead of a Tauri
invoke.  The request only opens the operator window; approval decisions still
require the operator gesture inside that window.
"""

from __future__ import annotations

from typing import Any

from blocks._common import error, ok


def _valid_request_id(request_id: str) -> bool:
    """Mirror the Launcher's authority request id shape."""
    if not request_id or len(request_id) > 160:
        return False
    return all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in request_id
    )


def run(input_data: Any, context: Any = None) -> dict[str, Any]:
    del context
    payload = input_data if isinstance(input_data, dict) else {}
    request_id = str(payload.get("request_id") or "").strip()
    if not _valid_request_id(request_id):
        response = error("'request_id' is invalid", code="INVALID_REQUEST_ID")
        response["_http_status"] = 400
        return response

    from domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    client = ViewerBrokerClient.from_environment()
    if not client.available():
        response = error(
            "Viewer host broker is unavailable", code="HOST_BROKER_UNAVAILABLE"
        )
        response["_http_status"] = 503
        return response

    try:
        result = client.open_authority_approval_window(request_id)
    except Exception as exc:
        response = error(str(exc), code="APPROVAL_WINDOW_FAILED")
        response["_http_status"] = 502
        return response

    if not result.get("ok"):
        response = error(
            str((result.get("error") or {}).get("message") or "approval window failed"),
            code=str((result.get("error") or {}).get("code") or "APPROVAL_WINDOW_FAILED"),
        )
        response["_http_status"] = 502
        return response

    return ok({"opened": True, "request_id": request_id})
