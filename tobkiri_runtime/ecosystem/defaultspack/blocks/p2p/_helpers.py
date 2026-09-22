from __future__ import annotations

from typing import Any


from domain.p2p.settings import P2PSettings


def settings_from(input_data: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> P2PSettings:
    input_data = input_data if isinstance(input_data, dict) else {}
    context = context if isinstance(context, dict) else {}
    overrides: dict[str, Any] = {}
    context_p2p = context.get("p2p")
    if isinstance(context_p2p, dict):
        overrides.update(context_p2p)
    # Client-supplied input may tune storage/ttl knobs, but it must never be
    # able to flip `enabled`: when P2P is off, request bodies cannot reopen the
    # inbound handshake/message paths (fail closed).
    input_p2p = input_data.get("p2p")
    if isinstance(input_p2p, dict):
        for key, value in input_p2p.items():
            if key != "enabled":
                overrides[key] = value
    for key in (
        "bind_host",
        "bind_port",
        "lan_discovery",
        "store_path",
        "envelope_ttl_seconds",
        "replay_ttl_seconds",
        "pairing_ttl_seconds",
    ):
        if key in input_data:
            overrides[key] = input_data[key]
    return P2PSettings.from_env(overrides)


def error_response(result: dict[str, Any]) -> dict[str, Any]:
    from blocks._common import error

    code = str(result.get("code") or "P2P_ERROR")
    message = str(result.get("error") or result.get("reason") or "P2P request failed")
    response = error(message, code)
    if code in {"SIGNATURE_INVALID", "SIGNATURE_MISSING", "SHARED_SECRET_MISSING"}:
        response["_http_status"] = 401
    elif code in {"P2P_DISABLED", "PEER_UNKNOWN", "PEER_UNAPPROVED", "PEER_BLOCKED", "CAPABILITY_DENIED", "COMPANY_DENIED"}:
        response["_http_status"] = 403
    elif code == "REPLAY_DETECTED":
        response["_http_status"] = 409
    return response
