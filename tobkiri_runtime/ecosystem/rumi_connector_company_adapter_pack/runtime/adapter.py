"""Project normalized connector events into Company contracts."""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
COMPANY_RESOURCE = "rumi.resource.company.v1"
COMPANY_ACTION = "rumi.action.company.state.v1"
COMPANY_COORDINATOR = "rumi.action.company.coordinator.v1"
SERVICE_PACK_ID = "rumi_connector_company_adapter_pack"
STATE_PACK_ID = "rumi_company_state_store_pack"
COORDINATOR_PACK_ID = "rumi_company_coordinator_pack"


class ConnectorCompanyAdapter:
    """Append one safe inbound record and request Company routing."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def route(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Route only explicitly Company-enabled connector events."""

        profile_id = str(payload.get("profile_id") or "default")
        connector = _mapping(payload.get("connector"))
        config = _mapping(connector.get("config"))
        routes = config.get("routes")
        enabled_routes = (
            {str(item) for item in routes} if isinstance(routes, list) else set()
        )
        if "company" not in enabled_routes:
            return {"status": "skipped", "reason": "company route is not enabled"}
        company_id = str(config.get("company_id") or "")
        if not company_id:
            raise ValueError("company_id is required for the company route")
        event = _mapping(payload.get("event"))
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ValueError("connector event_id is required")
        connector_id = str(payload.get("connector_id") or connector.get("id") or "")
        if not connector_id:
            raise ValueError("connector_id is required")
        inbound_id = "connector-" + _hash(
            f"{company_id}\0{connector_id}\0{event_id}"
        )[:40]
        record = {
            "id": inbound_id,
            "type": str(event.get("type") or "message"),
            "actor_id": str(event.get("actor_id") or ""),
            "channel_id": str(event.get("channel_id") or ""),
            "text": str(event.get("text") or ""),
            "metadata": {
                "source": "connector",
                "adapter_id": str(event.get("adapter_id") or ""),
                "connector_id": connector_id,
                "event_id": event_id,
                "received_at_ms": max(0, int(event.get("received_at_ms") or 0)),
                "event": _safe_event(event),
            },
        }
        appended = self._append(profile_id, company_id, record)
        routed = self._coordinate(profile_id, company_id, inbound_id)
        return {
            "status": "accepted",
            "company_id": company_id,
            "inbound_id": inbound_id,
            "deduplicated": bool(appended.get("deduplicated")),
            "routing": routed,
        }

    def _append(
        self,
        profile_id: str,
        company_id: str,
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot = self.client.invoke(
            COMPANY_RESOURCE,
            "list",
            {"profile_id": profile_id},
        )
        arguments = {
            "company_id": company_id,
            "expected_revision": int(snapshot.get("revision") or 0),
            "record": dict(record),
        }
        receipt = self._authorize(
            STATE_PACK_ID,
            "company.state.inbound.append",
            "company.state.manage",
            "company.connector.inbound",
            profile_id,
            arguments,
        )
        return self.client.invoke(
            COMPANY_ACTION,
            "inbound.append",
            {
                **arguments,
                "profile_id": profile_id,
                "authority_receipt": receipt,
                "caller_id": "company.connector.adapter",
                "caller_pack_id": SERVICE_PACK_ID,
                "caller_function_id": "company.connector.inbound",
                "session_id": "",
            },
        )

    def _coordinate(
        self,
        profile_id: str,
        company_id: str,
        inbound_id: str,
    ) -> dict[str, Any]:
        arguments = {"company_id": company_id, "inbound_id": inbound_id}
        receipt = self._authorize(
            COORDINATOR_PACK_ID,
            "company.coordinator.route_inbound",
            "company.coordinate",
            "company.connector.route",
            profile_id,
            arguments,
        )
        return self.client.invoke(
            COMPANY_COORDINATOR,
            "route_inbound",
            {
                **arguments,
                "profile_id": profile_id,
                "authority_receipt": receipt,
                "caller_id": "company.connector.adapter",
                "caller_pack_id": SERVICE_PACK_ID,
                "caller_function_id": "company.connector.route",
                "session_id": "",
            },
        )

    def _authorize(
        self,
        target_pack_id: str,
        operation: str,
        authority: str,
        caller_function_id: str,
        profile_id: str,
        arguments: Mapping[str, Any],
    ) -> str:
        result = self.client.invoke(
            AUTHORITY,
            "authorize",
            {
                "service_pack_id": target_pack_id,
                "operation": operation,
                "authority": authority,
                "caller_id": "company.connector.adapter",
                "caller_pack_id": SERVICE_PACK_ID,
                "caller_function_id": caller_function_id,
                "profile_id": profile_id,
                "workspace_id": "",
                "session_id": "",
                "arguments": dict(arguments),
                "approval_required": False,
            },
        )
        if not result.get("authorized") or not result.get("receipt"):
            raise PermissionError(str(result.get("reason") or "Company route denied"))
        return str(result["receipt"])


def create_connector_route(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the connector-to-Company route projection."""

    adapter = ConnectorCompanyAdapter(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "route":
            raise ValueError(f"unknown connector Company operation: {name}")
        return adapter.route(payload)

    return operation


def _safe_event(value: Any) -> Any:
    secret_parts = ("credential", "oauth", "password", "secret", "signature", "token")
    if isinstance(value, Mapping):
        return {
            str(key): _safe_event(item)
            for key, item in value.items()
            if not any(part in str(key).casefold() for part in secret_parts)
        }
    if isinstance(value, list):
        return [_safe_event(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

