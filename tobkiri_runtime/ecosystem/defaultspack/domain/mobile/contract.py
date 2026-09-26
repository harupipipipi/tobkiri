from __future__ import annotations

import re
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class MobileRouteContract:
    method: str
    pattern: str
    block_module: str = ""
    flow_id: str = ""
    fallback_block_module: str = ""
    path_inject: dict[str, str] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    device_scope: str = ""
    feature: str = ""
    pc_equivalent: str = ""


_FEATURE_FLAG_ENV = {
    "credential_transfer": "RUMI_MOBILE_CREDENTIAL_TRANSFER",
    "credentials_admin": "RUMI_MOBILE_CREDENTIAL_TRANSFER",
}


def mobile_feature_enabled(feature: str) -> bool:
    env_name = _FEATURE_FLAG_ENV.get(str(feature or ""))
    if not env_name:
        return True
    return str(os.environ.get(env_name) or "").strip().lower() in {"1", "true", "yes", "on"}


MOBILE_ROUTE_CONTRACTS: tuple[MobileRouteContract, ...] = (
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/bootstrap",
        block_module="blocks.mobile.bootstrap",
        device_scope="chat.read",
        feature="bootstrap",
    ),
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/manifest",
        block_module="blocks.mobile.manifest",
        device_scope="chat.read",
        feature="manifest",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/control-states/query",
        block_module="blocks.mobile.runtime_controls",
        defaults={"_mobile_control_operation": "query_states"},
        device_scope="chat.read",
        feature="runtime_controls",
        pc_equivalent="POST /api/command-protocol/v1/states/query",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/control-commands/invoke",
        block_module="blocks.mobile.runtime_controls",
        defaults={"_mobile_control_operation": "invoke"},
        device_scope="chat.write",
        feature="runtime_controls",
        pc_equivalent="POST /api/command-protocol/v1/invoke",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/pairings/{id}/claim",
        block_module="blocks.mobile.pairing",
        path_inject={"id": "pairing_id"},
        defaults={"action": "claim"},
        feature="pairing",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/pairings/{id}/approve",
        block_module="blocks.mobile.pairing",
        path_inject={"id": "pairing_id"},
        defaults={"action": "approve"},
        feature="pairing_admin",
    ),
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/pairings/{id}/review",
        block_module="blocks.mobile.pairing",
        path_inject={"id": "pairing_id"},
        defaults={"action": "review"},
        feature="pairing_admin",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/pairings/{id}/reject",
        block_module="blocks.mobile.pairing",
        path_inject={"id": "pairing_id"},
        defaults={"action": "reject"},
        feature="pairing_admin",
    ),
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/pairings/{id}/status",
        block_module="blocks.mobile.pairing",
        path_inject={"id": "pairing_id"},
        defaults={"action": "status"},
        feature="pairing",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/pairings/{id}/token/pickup",
        block_module="blocks.mobile.pairing",
        path_inject={"id": "pairing_id"},
        defaults={"action": "pickup_token_delivery"},
        feature="pairing",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/pairings/{id}/token/ack",
        block_module="blocks.mobile.pairing",
        path_inject={"id": "pairing_id"},
        defaults={"action": "ack_token_delivery"},
        feature="pairing",
    ),
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/devices",
        block_module="blocks.mobile.pairing",
        defaults={"action": "list_devices"},
        feature="device_admin",
    ),
    MobileRouteContract(
        "PATCH",
        "/api/mobile/v1/devices/{id}",
        block_module="blocks.mobile.pairing",
        path_inject={"id": "device_id"},
        defaults={"action": "patch_device"},
        feature="device_admin",
    ),
    MobileRouteContract(
        "DELETE",
        "/api/mobile/v1/devices/{id}",
        block_module="blocks.mobile.pairing",
        path_inject={"id": "device_id"},
        defaults={"action": "delete_device"},
        feature="device_admin",
    ),
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/conversations",
        block_module="blocks.mobile.conversations",
        defaults={"action": "list"},
        device_scope="chat.read",
        feature="chat",
        pc_equivalent="GET /api/chat/conversations",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/conversations",
        block_module="blocks.mobile.conversations",
        defaults={"action": "create"},
        device_scope="chat.write",
        feature="chat",
        pc_equivalent="POST /api/chat/conversations",
    ),
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/conversations/{id}",
        block_module="blocks.mobile.conversations",
        path_inject={"id": "conversation_id"},
        defaults={"action": "get"},
        device_scope="chat.read",
        feature="chat",
        pc_equivalent="GET /api/chat/conversations/{id}",
    ),
    MobileRouteContract(
        "PUT",
        "/api/mobile/v1/conversations/{id}",
        block_module="blocks.chat.update_conversation",
        path_inject={"id": "conversation_id"},
        device_scope="chat.write",
        feature="chat",
        pc_equivalent="PUT /api/chat/conversations/{id}",
    ),
    MobileRouteContract(
        "DELETE",
        "/api/mobile/v1/conversations/{id}",
        block_module="blocks.chat.delete_conversation",
        path_inject={"id": "conversation_id"},
        device_scope="chat.write",
        feature="chat",
        pc_equivalent="DELETE /api/chat/conversations/{id}",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/conversations/{id}/messages",
        flow_id="defaultspack.chat_turn",
        fallback_block_module="blocks.chat.send",
        path_inject={"id": "conversation_id"},
        device_scope="chat.write",
        feature="chat",
        pc_equivalent="POST /api/chat/conversations/{id}/messages",
    ),
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/conversations/{id}/tool-preferences",
        block_module="blocks.chat.tool_preferences",
        path_inject={"id": "conversation_id"},
        device_scope="chat.read",
        feature="chat",
        pc_equivalent="GET /api/chat/conversations/{id}/tool-preferences",
    ),
    MobileRouteContract(
        "PUT",
        "/api/mobile/v1/conversations/{id}/tool-preferences",
        block_module="blocks.chat.tool_preferences",
        path_inject={"id": "conversation_id"},
        device_scope="chat.write",
        feature="chat",
        pc_equivalent="PUT /api/chat/conversations/{id}/tool-preferences",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/conversations/{id}/stream",
        flow_id="defaultspack.chat_stream_turn",
        fallback_block_module="blocks.chat.stream",
        path_inject={"id": "conversation_id"},
        device_scope="chat.write",
        feature="chat",
        pc_equivalent="POST /api/chat/conversations/{id}/stream",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/conversations/{id}/stop",
        block_module="blocks.chat.stop",
        path_inject={"id": "conversation_id"},
        device_scope="chat.write",
        feature="chat",
        pc_equivalent="POST /api/chat/conversations/{id}/stop",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/conversations/{id}/export",
        block_module="blocks.chat.export_conversation",
        path_inject={"id": "conversation_id"},
        device_scope="chat.read",
        feature="chat",
        pc_equivalent="POST /api/chat/conversations/{id}/export",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/conversations/{id}/summarize",
        block_module="blocks.chat.summarize_and_trim",
        path_inject={"id": "conversation_id"},
        device_scope="chat.write",
        feature="chat",
        pc_equivalent="POST /api/chat/conversations/{id}/summarize",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/conversations/{id}/auto-trim",
        block_module="blocks.chat.auto_trim",
        path_inject={"id": "conversation_id"},
        device_scope="chat.write",
        feature="chat",
        pc_equivalent="POST /api/chat/conversations/{id}/auto-trim",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/conversations/{id}/compact",
        block_module="blocks.chat.compact",
        path_inject={"id": "conversation_id"},
        device_scope="chat.write",
        feature="chat",
        pc_equivalent="POST /api/chat/conversations/{id}/compact",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/conversations/{id}/auto-compact",
        block_module="blocks.chat.auto_compact",
        path_inject={"id": "conversation_id"},
        device_scope="chat.write",
        feature="chat",
        pc_equivalent="POST /api/chat/conversations/{id}/auto-compact",
    ),
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/conversations/{id}/run-results/{run_id}/browser-screenshots",
        block_module="blocks.chat.browser_screenshots",
        path_inject={"id": "conversation_id", "run_id": "run_id"},
        device_scope="chat.read",
        feature="chat",
        pc_equivalent="GET /api/chat/conversations/{id}/run-results/{run_id}/browser-screenshots",
    ),
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/conversations/{id}/artifact-file",
        block_module="blocks.chat.artifact_file",
        path_inject={"id": "conversation_id"},
        device_scope="chat.read",
        feature="chat",
        pc_equivalent="GET /api/chat/conversations/{id}/artifact-file",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/conversations/{id}/branch",
        block_module="blocks.mobile.conversations",
        path_inject={"id": "conversation_id"},
        defaults={"action": "branch"},
        device_scope="chat.write",
        feature="chat",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/conversations/import-branch",
        block_module="blocks.mobile.conversations",
        defaults={"action": "import_branch"},
        device_scope="chat.write",
        feature="chat",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/credential-transfers",
        block_module="blocks.mobile.credentials",
        defaults={"action": "create"},
        feature="credentials_admin",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/credential-transfers/{id}/confirm",
        block_module="blocks.mobile.credentials",
        path_inject={"id": "transfer_id"},
        defaults={"action": "confirm"},
        feature="credentials_admin",
    ),
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/credential-transfers/{id}/status",
        block_module="blocks.mobile.credentials",
        path_inject={"id": "transfer_id"},
        defaults={"action": "status"},
        feature="credentials_admin",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/credential-transfers/{id}/cancel",
        block_module="blocks.mobile.credentials",
        path_inject={"id": "transfer_id"},
        defaults={"action": "cancel"},
        feature="credentials_admin",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/credential-transfers/{id}/revoke",
        block_module="blocks.mobile.credentials",
        path_inject={"id": "transfer_id"},
        defaults={"action": "revoke"},
        feature="credentials_admin",
    ),
    MobileRouteContract(
        "GET",
        "/api/mobile/v1/credential-transfers",
        block_module="blocks.mobile.credentials",
        defaults={"action": "list"},
        device_scope="credentials.request",
        feature="credential_transfer",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/credential-transfers/{id}",
        block_module="blocks.mobile.credentials",
        path_inject={"id": "transfer_id"},
        defaults={"action": "redeem"},
        device_scope="credentials.request",
        feature="credential_transfer",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/credential-transfers/{id}/ack",
        block_module="blocks.mobile.credentials",
        path_inject={"id": "transfer_id"},
        defaults={"action": "ack"},
        device_scope="credentials.request",
        feature="credential_transfer",
    ),
    MobileRouteContract(
        "POST",
        "/api/mobile/v1/credential-transfers/{id}/reject",
        block_module="blocks.mobile.credentials",
        path_inject={"id": "transfer_id"},
        defaults={"action": "reject"},
        device_scope="credentials.request",
        feature="credential_transfer",
    ),
)


_PARAM_RE = re.compile(r"\{(\w+)\}")


@lru_cache(maxsize=None)
def _compiled(pattern: str) -> re.Pattern[str]:
    regex = _PARAM_RE.sub(
        lambda match: rf"(?P<{match.group(1)}>[^/]+)",
        str(pattern or ""),
    )
    return re.compile("^" + regex + "$")


def iter_mobile_route_contracts() -> tuple[MobileRouteContract, ...]:
    return tuple(route for route in MOBILE_ROUTE_CONTRACTS if mobile_feature_enabled(route.feature))


def match_mobile_route(method: str, path: str) -> MobileRouteContract | None:
    method_upper = str(method or "").upper()
    normalized = str(path or "").rstrip("/") or "/"
    for route in iter_mobile_route_contracts():
        if route.method.upper() != method_upper:
            continue
        if _compiled(route.pattern).match(normalized):
            return route
    return None


def required_device_scope(method: str, path: str) -> str:
    route = match_mobile_route(method, path)
    if route is None:
        return ""
    return route.device_scope


def mobile_capability_flags() -> dict[str, bool]:
    routes = iter_mobile_route_contracts()
    features = {route.feature for route in routes}
    scopes = {route.device_scope for route in routes if route.device_scope}
    return {
        "chat": "chat" in features and {"chat.read", "chat.write"} <= scopes,
        "tools": "tools" in features or "tools.observe" in scopes,
        "tool_invoke": "tools.invoke.basic" in scopes,
        "cloud_delegation": "tools.invoke.cloud" in scopes,
        "approvals": True,
        "credential_transfer": "credential_transfer" in features
        and "credentials.request" in scopes,
    }


def mobile_route_manifest() -> list[dict[str, Any]]:
    return [
        {
            "method": route.method,
            "pattern": route.pattern,
            "device_scope": route.device_scope,
            "feature": route.feature,
            "pc_equivalent": route.pc_equivalent,
            "block_module": route.block_module,
            "flow_id": route.flow_id,
            "fallback_block_module": route.fallback_block_module,
        }
        for route in iter_mobile_route_contracts()
    ]
