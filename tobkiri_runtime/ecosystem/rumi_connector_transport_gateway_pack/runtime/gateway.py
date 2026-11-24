"""Contract-only connector ingress, egress, and status gateway."""

from __future__ import annotations

from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
REGISTRY = "rumi.resource.connector.registry.v1"
INBOUND = "rumi.transport.connector.inbound.v1"
OUTBOUND_RESOURCE = "rumi.resource.connector.outbound.v1"
OUTBOUND_ACTION = "rumi.action.connector.outbound.v1"
SERVICE_PACK_ID = "rumi_connector_transport_gateway_pack"
OUTBOUND_PACK_ID = "rumi_connector_outbound_broker_pack"


def create_gateway_transport(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the public connector ingress projection."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "receive":
            raise ValueError(f"unknown connector gateway transport operation: {name}")
        return client.invoke(INBOUND, "receive", dict(payload))

    return operation


def create_gateway_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create public connector and redacted delivery status reads."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        profile_id = str(payload.get("profile_id") or "default")
        if name == "status":
            registry = client.invoke(REGISTRY, "list", {"profile_id": profile_id})
            connectors = (
                registry.get("connectors")
                if isinstance(registry, Mapping)
                else []
            )
            connectors = connectors if isinstance(connectors, list) else []
            return {
                "profile_id": profile_id,
                "registry_revision": int(registry.get("revision") or 0),
                "connectors": [_public_connector(item) for item in connectors],
            }
        if name == "delivery_status":
            return client.invoke(
                OUTBOUND_RESOURCE,
                "status",
                {
                    "profile_id": profile_id,
                    "delivery_id": str(payload.get("delivery_id") or ""),
                },
            )
        raise ValueError(f"unknown connector gateway resource operation: {name}")

    return operation


def create_gateway_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated connector egress projections."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        arguments = _arguments(name, payload)
        profile_id = str(payload.get("profile_id") or "default")
        caller = _caller(payload)
        _redeem(client, name, profile_id, caller, arguments, payload)
        receipt = _authorize_outbound(
            client,
            name,
            profile_id,
            caller,
            arguments,
        )
        return client.invoke(
            OUTBOUND_ACTION,
            name,
            {
                **arguments,
                "profile_id": profile_id,
                "authority_receipt": receipt,
                "caller_id": caller["caller_id"],
                "caller_pack_id": SERVICE_PACK_ID,
                "caller_function_id": f"connector.gateway.{name}",
                "session_id": caller["session_id"],
            },
        )

    return operation


def _arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if name == "send":
        message = payload.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("connector message must be an object")
        return {
            "connector_id": str(payload.get("connector_id") or ""),
            "delivery_id": str(payload.get("delivery_id") or ""),
            "message": dict(message),
        }
    if name in {"retry", "cancel"}:
        return {"delivery_id": str(payload.get("delivery_id") or "")}
    raise ValueError(f"unknown connector gateway action: {name}")


def _caller(payload: Mapping[str, Any]) -> dict[str, str]:
    caller_id = str(payload.get("caller_id") or "")
    caller_pack_id = str(payload.get("caller_pack_id") or "")
    if not caller_id or not caller_pack_id:
        raise ValueError("connector gateway caller scope is incomplete")
    return {
        "caller_id": caller_id,
        "caller_pack_id": caller_pack_id,
        "caller_function_id": str(payload.get("caller_function_id") or ""),
        "session_id": str(payload.get("session_id") or ""),
    }


def _redeem(
    client: Any,
    name: str,
    profile_id: str,
    caller: Mapping[str, str],
    arguments: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    result = client.invoke(
        AUTHORITY,
        "redeem",
        {
            "receipt": str(payload.get("authority_receipt") or ""),
            "service_pack_id": SERVICE_PACK_ID,
            "operation": f"connector.gateway.{name}",
            "authority": "connector.gateway.control",
            **dict(caller),
            "profile_id": profile_id,
            "workspace_id": "",
            "arguments": dict(arguments),
        },
    )
    if not result.get("authorized"):
        raise PermissionError(str(result.get("reason") or "connector gateway denied"))


def _authorize_outbound(
    client: Any,
    name: str,
    profile_id: str,
    caller: Mapping[str, str],
    arguments: Mapping[str, Any],
) -> str:
    result = client.invoke(
        AUTHORITY,
        "authorize",
        {
            "service_pack_id": OUTBOUND_PACK_ID,
            "operation": f"connector.outbound.{name}",
            "authority": "connector.outbound.control",
            "caller_id": caller["caller_id"],
            "caller_pack_id": SERVICE_PACK_ID,
            "caller_function_id": f"connector.gateway.{name}",
            "profile_id": profile_id,
            "workspace_id": "",
            "session_id": caller["session_id"],
            "arguments": dict(arguments),
            "approval_required": False,
        },
    )
    if not result.get("authorized") or not result.get("receipt"):
        raise PermissionError(str(result.get("reason") or "connector egress denied"))
    return str(result["receipt"])


def _public_connector(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        "id": str(value.get("id") or ""),
        "adapter_id": str(value.get("adapter_id") or ""),
        "display_name": str(value.get("display_name") or ""),
        "config": _public_config(value.get("config")),
        "enabled": bool(value.get("enabled")),
        "credential_configured": bool(value.get("credential_ref")),
        "updated_at_ms": int(value.get("updated_at_ms") or 0),
    }


def _public_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    secret_parts = ("credential", "oauth", "password", "secret", "signature", "token")
    return {
        str(key): _public_value(item)
        for key, item in value.items()
        if not any(part in str(key).casefold() for part in secret_parts)
    }


def _public_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _public_config(value)
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)

