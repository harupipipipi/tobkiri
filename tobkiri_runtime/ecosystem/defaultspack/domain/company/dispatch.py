from __future__ import annotations

from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from .run_dispatcher import CompanyRunDispatcher
from .store import CompanyStore


class CompanyDispatchService:
    """Dispatch metadata plus AgentEngine run creation. This service never executes tools."""

    def __init__(
        self,
        store: CompanyStore | None = None,
        *,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> None:
        self.store = store or CompanyStore()
        self.dispatcher = CompanyRunDispatcher(
            company_store=self.store,
            settings_owner=settings_owner,
        )

    def dispatch_task(
        self,
        company_id: str,
        task_id: str,
        *,
        requested_by: str = "system",
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self.dispatcher.dispatch_task(
            company_id,
            task_id,
            requested_by=requested_by,
            policy=policy,
        )
