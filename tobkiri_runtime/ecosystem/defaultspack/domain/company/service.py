from __future__ import annotations

import hashlib
import re
from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from .dispatch import CompanyDispatchService
from .inbound_routes import CompanyInboundRouteService
from .mention import CompanyMentionService
from .migration import migrate_operations_company_state
from .models import (
    DEFAULT_COMPANY_DESCRIPTION,
    DEFAULT_COMPANY_ID,
    DEFAULT_COMPANY_NAME,
    DEFAULT_CONVERSATION_GROUP_ID,
    default_agents,
)
from .runtime_store import CompanyRuntimeStore
from .store import CompanyStore


class CompanyService:
    def __init__(
        self,
        store: CompanyStore | None = None,
        *,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> None:
        self.store = store or CompanyStore()
        self.runtime_store = CompanyRuntimeStore()
        self.settings_owner = settings_owner

    def create_company(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        return self.store.create_company(
            company_id=data.get("id") or data.get("company_id"),
            name=name,
            description=str(data.get("description") or ""),
            settings=data.get("settings") if isinstance(data.get("settings"), dict) else None,
            agents=data.get("agents") if isinstance(data.get("agents"), (list, dict)) else None,
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else None,
            conversation_group_id=data.get("conversation_group_id"),
        )

    def get_company(self, company_id: str) -> dict[str, Any] | None:
        return self._with_runtime_summary(self.store.get_company(company_id))

    def list_companies(self, *, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        companies, total = self.store.list_companies(limit=limit, offset=offset)
        return [self._with_runtime_summary(company) for company in companies], total

    def update_company(self, company_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        return self.store.update_company(company_id, updates)

    def delete_company(self, company_id: str) -> bool:
        return self.store.delete_company(company_id)

    def bootstrap_default_company(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.store.ensure_company(
            company_id=DEFAULT_COMPANY_ID,
            name=DEFAULT_COMPANY_NAME,
            description=DEFAULT_COMPANY_DESCRIPTION,
            agents=default_agents(),
            metadata=metadata or {"profile_id": "defaultspack.operations_company"},
            conversation_group_id=(metadata or {}).get("conversation_group_id") or DEFAULT_CONVERSATION_GROUP_ID,
        )

    def bootstrap_conversation_company(self, conversation_id: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        existing = self.store.find_company_by_conversation_id(conversation_id)
        employee_model = _conversation_employee_model(conversation_id, metadata)
        merged_metadata = {
            "profile_id": "defaultspack.operations_company",
            "conversation_id": conversation_id,
            "source": "chat",
            "surface": "main_chat",
            "user_role": "president",
            "employee_model": employee_model,
            **(metadata or {}),
        }
        company_id = str(existing.get("id") if existing else _conversation_company_id(conversation_id))
        guard_settings = _subagent_team_company_guard_settings() if _is_subagent_team_company_metadata(merged_metadata) else None
        if existing:
            return self.store.ensure_company(
                company_id=company_id,
                name=str(existing.get("name") or "Executive Team"),
                description=str(existing.get("description") or "Employee group delegated from the current chat."),
                settings=guard_settings,
                agents=None,
                metadata=merged_metadata,
                conversation_group_id=str(
                    existing.get("conversation_group_id")
                    or (metadata or {}).get("conversation_group_id")
                    or "company:" + company_id
                ),
            )
        return self.store.ensure_company(
            company_id=company_id,
            name=str((metadata or {}).get("name") or "Executive Team"),
            description="Employee group delegated from the current chat.",
            settings=guard_settings,
            agents=_default_agents_for_model(employee_model),
            metadata=merged_metadata,
            conversation_group_id=(metadata or {}).get("conversation_group_id") or "company:" + company_id,
        )

    def status(self, company_id: str | None = None) -> dict[str, Any]:
        target_id = company_id or DEFAULT_COMPANY_ID
        company = self.store.get_company(target_id)
        if company is None and target_id == DEFAULT_COMPANY_ID:
            company = migrate_operations_company_state(store=self.store) or self.bootstrap_default_company()
        runtime_store = self.runtime_store
        company = self._with_runtime_summary(company, runtime_store=runtime_store)
        reporting = self._runtime_reporting(target_id, company, runtime_store)
        return {
            "bootstrapped": company is not None,
            "company_id": target_id,
            "company": company,
            "storage_file": str(self.store.storage_file),
            "runtime_db_path": str(runtime_store.db_path),
            "runtime": runtime_store.stats(target_id) if company is not None else {},
            "reporting": reporting,
        }

    def status_for_conversation(self, conversation_id: str, *, bootstrap: bool = False) -> dict[str, Any]:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return self.status(None)
        company = self.store.find_company_by_conversation_id(conversation_id)
        if company is None and bootstrap:
            company = self.bootstrap_conversation_company(conversation_id)
        runtime_store = self.runtime_store
        company_id = str(company.get("id") or _conversation_company_id(conversation_id)) if company else _conversation_company_id(conversation_id)
        company = self._with_runtime_summary(company, runtime_store=runtime_store)
        reporting = self._runtime_reporting(company_id, company, runtime_store)
        return {
            "bootstrapped": company is not None,
            "company_id": company_id,
            "conversation_id": conversation_id,
            "company": company,
            "storage_file": str(self.store.storage_file),
            "runtime_db_path": str(runtime_store.db_path),
            "runtime": runtime_store.stats(company_id) if company is not None else {},
            "reporting": reporting,
        }

    def mention(self, company_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        content = str(data.get("content") or data.get("message") or "")
        return CompanyMentionService(
            self.store,
            settings_owner=self.settings_owner,
        ).create_message_task(
            company_id,
            content=content,
            sender_id=str(data.get("sender_id") or "user"),
            channel_id=str(data.get("channel_id") or "ops-company"),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else None,
        )

    def dispatch(self, company_id: str, task_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        return CompanyDispatchService(
            self.store,
            settings_owner=self.settings_owner,
        ).dispatch_task(
            company_id,
            task_id,
            requested_by=str(data.get("requested_by") or "system"),
            policy=data.get("policy") if isinstance(data.get("policy"), dict) else None,
        )

    def inbound_routes(self) -> CompanyInboundRouteService:
        return CompanyInboundRouteService(
            self.store,
            settings_owner=self.settings_owner,
        )

    def _with_runtime_summary(
        self,
        company: dict[str, Any] | None,
        *,
        runtime_store: CompanyRuntimeStore | None = None,
    ) -> dict[str, Any] | None:
        if company is None:
            return None
        runtime = runtime_store or self.runtime_store
        company_id = str(company.get("id") or "")
        if not company_id:
            return company
        enriched = dict(company)
        try:
            stats = runtime.stats(company_id)
        except Exception:
            stats = {}
        if stats:
            enriched["runtime_counts"] = stats
            enriched["message_count"] = max(int(enriched.get("message_count", 0) or 0), int(stats.get("messages", 0) or 0))
            enriched["task_count"] = max(int(enriched.get("task_count", 0) or 0), int(stats.get("tasks", 0) or 0))
            enriched["run_count"] = int(stats.get("runs", 0) or 0)
            enriched["thread_count"] = int(stats.get("threads", 0) or 0)
            enriched["inbox_count"] = int(stats.get("inbox", 0) or 0)
        try:
            blocker_signals = runtime.blocker_signal_summary(company_id, limit=20)
        except Exception:
            blocker_signals = {}
        if blocker_signals:
            enriched["runtime_blocker_signals"] = blocker_signals
        channels = enriched.get("channels")
        if isinstance(channels, dict):
            next_channels: dict[str, Any] = {}
            for channel_id, channel in channels.items():
                next_channels[str(channel_id)] = self._with_runtime_channel_count(
                    company_id,
                    str(channel_id),
                    channel if isinstance(channel, dict) else {},
                    runtime,
                )
            for channel_id in self._runtime_channel_ids(company_id, runtime):
                if channel_id not in next_channels:
                    next_channels[channel_id] = self._with_runtime_channel_count(
                        company_id,
                        channel_id,
                        {
                            "id": channel_id,
                            "name": channel_id,
                            "description": "Runtime channel",
                            "visibility": "team",
                            "members": [],
                        },
                        runtime,
                    )
            enriched["channels"] = next_channels
            enriched["channel_count"] = len(next_channels)
        return enriched

    @staticmethod
    def _runtime_reporting(
        company_id: str,
        company: dict[str, Any] | None,
        runtime_store: CompanyRuntimeStore,
    ) -> dict[str, Any]:
        if company is None:
            return {}
        try:
            return {
                "blocker_signals": runtime_store.blocker_signal_summary(company_id, limit=20),
            }
        except Exception:
            return {"blocker_signals": {"blocker_count": 0, "latest_signal": None, "signals": []}}

    @staticmethod
    def _runtime_channel_ids(company_id: str, runtime_store: CompanyRuntimeStore) -> list[str]:
        try:
            return runtime_store.list_channel_ids(company_id)
        except Exception:
            return []

    @staticmethod
    def _with_runtime_channel_count(
        company_id: str,
        channel_id: str,
        channel: dict[str, Any],
        runtime_store: CompanyRuntimeStore,
    ) -> dict[str, Any]:
        enriched = dict(channel)
        try:
            messages, total = runtime_store.list_messages(company_id, channel_id=channel_id, limit=1, offset=0)
        except Exception:
            return enriched
        enriched["message_count"] = max(int(enriched.get("message_count", 0) or 0), int(total))
        if total:
            try:
                latest, _latest_total = runtime_store.list_messages(
                    company_id,
                    channel_id=channel_id,
                    limit=1,
                    offset=max(int(total) - 1, 0),
                )
            except Exception:
                latest = []
            if latest:
                enriched["last_message_at"] = latest[0].get("created_at") or enriched.get("last_message_at")
            elif messages:
                enriched["last_message_at"] = messages[0].get("created_at") or enriched.get("last_message_at")
        return enriched


def _conversation_company_id(conversation_id: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(conversation_id or "").strip()).strip("-").lower()
    if len(clean) > 40:
        clean = clean[:40].strip("-")
    digest = hashlib.sha1(str(conversation_id).encode("utf-8")).hexdigest()[:10]
    return "chat-team-" + (clean or digest) + "-" + digest


def _conversation_employee_model(conversation_id: str, metadata: dict[str, Any] | None) -> str:
    for key in ("employee_model", "model", "preferred_model"):
        candidate = str((metadata or {}).get(key) or "").strip()
        if candidate:
            return candidate
    try:
        from domain.chat.store import ChatStore

        conversation = ChatStore().get_conversation(conversation_id) or {}
        candidate = str(conversation.get("model") or "").strip()
        if candidate:
            return candidate
    except Exception:
        pass
    return ""


def _default_agents_for_model(model: str) -> list[dict[str, Any]]:
    model = str(model or "").strip()
    agents = default_agents()
    if not model:
        return agents
    for agent in agents:
        agent["model"] = model
    return agents


def _subagent_team_company_guard_settings() -> dict[str, Any]:
    return {
        "subagent_team": {
            "company_api_write_guard": True,
            "guard_owner": "subagent_team_workspace",
            "managed_workspace": True,
        }
    }


def _is_subagent_team_company_metadata(metadata: dict[str, Any] | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    return (
        bool(metadata.get("subagent_team"))
        or bool(metadata.get("subagent_team_workspace"))
        or str(metadata.get("surface") or "") == "subagent_team_workspace"
        or str(metadata.get("workspace_kind") or "") == "subagent_team"
        or str(metadata.get("frontend_surface") or "") == "subagent_team_workspace"
    )
