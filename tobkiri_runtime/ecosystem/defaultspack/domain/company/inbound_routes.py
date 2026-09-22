from __future__ import annotations

from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from .message_router import CompanySlackRuntime
from .models import DEFAULT_CHANNEL_ID
from .store import CompanyStore


class CompanyInboundRouteService:
    def __init__(
        self,
        store: CompanyStore | None = None,
        *,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> None:
        self.store = store or CompanyStore()
        self.settings_owner = settings_owner

    def list_routes(self, company_id: str) -> list[dict[str, Any]] | None:
        return self.store.list_inbound_routes(company_id)

    def upsert_route(self, company_id: str, route: dict[str, Any]) -> dict[str, Any] | None:
        return self.store.upsert_inbound_route(company_id, route)

    def delete_route(self, company_id: str, route_id: str) -> bool:
        return self.store.delete_inbound_route(company_id, route_id)

    def ingest(
        self,
        company_id: str,
        *,
        content: str,
        sender_id: str = "external",
        route_id: str | None = None,
        channel_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        route = None
        if route_id:
            for candidate in self.store.list_inbound_routes(company_id) or []:
                if candidate.get("id") == route_id:
                    route = candidate
                    break
            if route is None or route.get("enabled") is False:
                return None
        target_channel_id = channel_id or (route or {}).get("channel_id") or DEFAULT_CHANNEL_ID
        return CompanySlackRuntime(
            company_store=self.store,
            settings_owner=self.settings_owner,
        ).post_message(
            company_id,
            content=content,
            sender_id=sender_id,
            channel_id=target_channel_id,
            metadata={"route_id": route_id, **(metadata or {})},
        )
