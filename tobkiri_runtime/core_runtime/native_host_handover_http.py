"""Private Native Launcher route; never a public Pack or browser capability."""

from __future__ import annotations

from typing import Any

from .api.api_response import APIResponse


def handle_native_host_handover(handler: Any, method: str, path: str) -> bool:
    """Delegate only loopback Launcher-authenticated, native-consented work."""
    prefix = "/api/internal/native-host-handover/"
    if method != "POST" or path not in {
        prefix + item for item in ("prepare", "commit", "recovery-preview", "recover")
    }:
        return False
    if handler.headers.get("Origin") is not None or not handler._native_pack_launcher_authenticated():
        handler._discard_request_body()
        handler._send_response(APIResponse(False, error="Unauthorized"), 401)
        return True
    port = getattr(handler._packvm_lifecycle, "development_host_handover", None)
    if port is None:
        handler._discard_request_body()
        handler._send_response(
            APIResponse(False, error="Development Host handover is unavailable"), 409
        )
        return True
    body = handler._parse_object_body()
    if body is None:
        return True
    try:
        action = path.removeprefix(prefix)
        if action == "prepare":
            result = port.prepare(body)
        elif action == "recovery-preview":
            if body:
                raise ValueError("Host recovery preview fields are invalid")
            result = port.prepare_recovery()
        else:
            callback = port.commit if action == "commit" else port.recover
            result = callback(
                body, native_secret=handler.headers.get("X-Rumi-Desktop-Bootstrap", "")
            )
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        handler._send_response(
            APIResponse(
                False, error="Host handover changed or requires recovery; current data is retained"
            ),
            409,
        )
        return True
    handler._send_response(APIResponse(True, data=result))
    return True
