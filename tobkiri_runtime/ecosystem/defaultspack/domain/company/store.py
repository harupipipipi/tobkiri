from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .models import (
    DEFAULT_SETTINGS,
    SCHEMA_VERSION,
    default_agents,
    default_channel,
    gen_id,
    normalize_agent,
    normalize_company,
    public_company,
    timestamp,
)


class CompanyStore:
    """Durable JSON store for team workspaces."""

    _instance = None
    _class_lock = threading.RLock()

    def __new__(cls):
        storage_file = cls._default_storage_file()
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._lock = threading.RLock()
                cls._instance._storage_file = storage_file
                cls._instance._companies = cls._instance._load_companies()
            elif cls._instance._storage_file != storage_file:
                cls._instance._storage_file = storage_file
                cls._instance._companies = cls._instance._load_companies()
            return cls._instance

    @staticmethod
    def _default_storage_file() -> Path:
        override = os.environ.get("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", "").strip()
        if override:
            path = Path(override)
            return path if path.suffix == ".json" else path / "companies.json"
        user_data = os.environ.get("RUMI_USER_DATA", "").strip()
        if user_data:
            return (
                Path(user_data).expanduser()
                / "defaultspack"
                / "shared"
                / "companies"
                / "companies.json"
            )
        return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "companies" / "companies.json"

    @property
    def storage_file(self) -> Path:
        return self._storage_file

    def _load_companies(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._storage_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            return {}
        raw = data.get("companies") if isinstance(data, dict) else data
        if not isinstance(raw, dict):
            return {}
        loaded: dict[str, dict[str, Any]] = {}
        for company_id, company in raw.items():
            if not isinstance(company, dict):
                continue
            item = normalize_company({"id": str(company_id), **company})
            loaded[item["id"]] = item
        return loaded

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp_name).replace(path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _save(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": timestamp(),
            "companies": self._companies,
        }
        self._atomic_write_json(self._storage_file, payload)

    def create_company(
        self,
        *,
        name: str,
        description: str = "",
        settings: dict[str, Any] | None = None,
        agents: list[dict[str, Any]] | dict[str, dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        company_id: str | None = None,
        conversation_group_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            cid = str(company_id or gen_id("company_"))
            if cid in self._companies:
                raise ValueError("company already exists: " + cid)
            now = timestamp()
            agent_map: dict[str, dict[str, Any]] = {}
            if agents is None:
                for agent in default_agents():
                    agent_map[agent["agent_id"]] = agent
            elif isinstance(agents, dict):
                for agent_id, agent in agents.items():
                    normalized = normalize_agent(agent if isinstance(agent, dict) else {"agent_id": str(agent_id)})
                    agent_map[normalized["agent_id"]] = normalized
            else:
                for agent in agents:
                    if isinstance(agent, dict):
                        normalized = normalize_agent(agent)
                        agent_map[normalized["agent_id"]] = normalized
            channel = default_channel(now)
            company = normalize_company(
                {
                    "id": cid,
                    "name": name,
                    "description": description,
                    "settings": {**copy.deepcopy(DEFAULT_SETTINGS), **(settings or {})},
                    "metadata": metadata if isinstance(metadata, dict) else {},
                    "agents": agent_map,
                    "channels": {channel["id"]: channel},
                    "conversation_group_id": conversation_group_id or "company:" + cid,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            self._companies[cid] = company
            self._save()
            return public_company(company)

    def ensure_company(
        self,
        *,
        company_id: str,
        name: str,
        description: str = "",
        settings: dict[str, Any] | None = None,
        agents: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        conversation_group_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            existing = self._companies.get(company_id)
            if existing is None:
                return self.create_company(
                    company_id=company_id,
                    name=name,
                    description=description,
                    settings=settings,
                    agents=agents,
                    metadata=metadata,
                    conversation_group_id=conversation_group_id,
                )
            company = normalize_company(existing)
            company["name"] = name or company.get("name")
            if description:
                company["description"] = description
            if settings:
                company["settings"] = {**company.get("settings", {}), **settings}
            if metadata:
                company["metadata"] = {**company.get("metadata", {}), **metadata}
            if conversation_group_id:
                company["conversation_group_id"] = conversation_group_id
            for agent in agents or []:
                normalized = normalize_agent(agent)
                current = company["agents"].get(normalized["agent_id"], {})
                company["agents"][normalized["agent_id"]] = {**current, **normalized}
            if "ops-company" not in company["channels"]:
                company["channels"]["ops-company"] = default_channel()
            company["updated_at"] = timestamp()
            self._companies[company_id] = company
            self._save()
            return public_company(company)

    def get_company(self, company_id: str) -> dict[str, Any] | None:
        with self._lock:
            company = self._companies.get(str(company_id))
            return public_company(company) if company is not None else None

    def list_companies(self, *, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            items = [public_company(company) for company in self._companies.values()]
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        total = len(items)
        return items[offset: offset + limit], total

    def find_company_by_conversation_id(self, conversation_id: str) -> dict[str, Any] | None:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return None
        with self._lock:
            for company in self._companies.values():
                metadata = company.get("metadata") if isinstance(company.get("metadata"), dict) else {}
                if str(metadata.get("conversation_id") or "").strip() == conversation_id:
                    return public_company(company)
        return None

    def update_company(self, company_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(updates, dict):
            return None
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None:
                return None
            protected = {"id", "created_at", "agents", "channels", "messages", "tasks", "inbound_routes"}
            for key, value in updates.items():
                if key in protected:
                    continue
                if key in {"settings", "metadata"} and isinstance(value, dict):
                    company[key] = {**company.get(key, {}), **value}
                else:
                    company[key] = value
            company["updated_at"] = timestamp()
            self._companies[str(company_id)] = normalize_company(company)
            self._save()
            return public_company(self._companies[str(company_id)])

    def delete_company(self, company_id: str) -> bool:
        with self._lock:
            cid = str(company_id)
            if cid not in self._companies:
                return False
            del self._companies[cid]
            self._save()
            return True

    def get_settings(self, company_id: str) -> dict[str, Any] | None:
        company = self.get_company(company_id)
        if company is None:
            return None
        return copy.deepcopy(company.get("settings", {}))

    def update_settings(self, company_id: str, settings: dict[str, Any], *, replace: bool = False) -> dict[str, Any] | None:
        if not isinstance(settings, dict):
            return None
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None:
                return None
            company["settings"] = copy.deepcopy(settings) if replace else {**company.get("settings", {}), **settings}
            company["updated_at"] = timestamp()
            self._save()
            return copy.deepcopy(company["settings"])

    def list_agents(self, company_id: str) -> list[dict[str, Any]] | None:
        company = self.get_company(company_id)
        if company is None:
            return None
        agents = list(company.get("agents", {}).values())
        agents.sort(key=lambda agent: agent.get("role_key", ""))
        return copy.deepcopy(agents)

    def get_agent(self, company_id: str, agent_id: str) -> dict[str, Any] | None:
        company = self.get_company(company_id)
        if company is None:
            return None
        return copy.deepcopy(company.get("agents", {}).get(str(agent_id)))

    def upsert_agent(self, company_id: str, agent: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None or not isinstance(agent, dict):
                return None
            normalized = normalize_agent(agent)
            current = company.setdefault("agents", {}).get(normalized["agent_id"], {})
            company["agents"][normalized["agent_id"]] = {**current, **normalized}
            company["updated_at"] = timestamp()
            self._save()
            return copy.deepcopy(company["agents"][normalized["agent_id"]])

    def remove_agent(self, company_id: str, agent_id: str) -> bool:
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None or str(agent_id) not in company.get("agents", {}):
                return False
            del company["agents"][str(agent_id)]
            company["updated_at"] = timestamp()
            self._save()
            return True

    def list_channels(self, company_id: str) -> list[dict[str, Any]] | None:
        company = self.get_company(company_id)
        if company is None:
            return None
        channels = list(company.get("channels", {}).values())
        channels.sort(key=lambda channel: channel.get("updated_at", ""), reverse=True)
        return copy.deepcopy(channels)

    def get_channel(self, company_id: str, channel_id: str) -> dict[str, Any] | None:
        company = self.get_company(company_id)
        if company is None:
            return None
        return copy.deepcopy(company.get("channels", {}).get(str(channel_id)))

    def upsert_channel(self, company_id: str, channel: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None or not isinstance(channel, dict):
                return None
            now = timestamp()
            channel_id = str(channel.get("id") or channel.get("channel_id") or gen_id("channel_"))
            current = company.setdefault("channels", {}).get(channel_id, {})
            item = {
                "id": channel_id,
                "name": channel.get("name") or current.get("name") or channel_id,
                "description": channel.get("description", current.get("description", "")),
                "visibility": channel.get("visibility", current.get("visibility", "team")),
                "members": list(channel.get("members", current.get("members", []))),
                "mentions": bool(channel.get("mentions", current.get("mentions", True))),
                "append_only": bool(channel.get("append_only", current.get("append_only", True))),
                "message_count": int(current.get("message_count", 0)),
                "last_message_at": current.get("last_message_at"),
                "metadata": {**current.get("metadata", {}), **(channel.get("metadata") or {})},
                "created_at": current.get("created_at", now),
                "updated_at": now,
            }
            company["channels"][channel_id] = item
            company["updated_at"] = now
            self._save()
            return copy.deepcopy(item)

    def delete_channel(self, company_id: str, channel_id: str) -> bool:
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None or str(channel_id) not in company.get("channels", {}):
                return False
            del company["channels"][str(channel_id)]
            company["updated_at"] = timestamp()
            self._save()
            return True

    def add_message(
        self,
        company_id: str,
        *,
        channel_id: str,
        sender_id: str,
        content: str,
        mentions: list[str] | None = None,
        task_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None:
                return None
            channel = company.setdefault("channels", {}).get(str(channel_id))
            if channel is None:
                channel = self.upsert_channel(company_id, {"id": str(channel_id), "name": str(channel_id)})
                company = self._companies.get(str(company_id))
            now = timestamp()
            message_id = gen_id("msg_")
            message = {
                "id": message_id,
                "company_id": str(company_id),
                "channel_id": str(channel_id),
                "sender_id": str(sender_id),
                "content": str(content),
                "mentions": list(mentions or []),
                "task_ids": list(task_ids or []),
                "metadata": metadata if isinstance(metadata, dict) else {},
                "created_at": now,
                "updated_at": now,
            }
            company.setdefault("messages", {})[message_id] = message
            channel = company["channels"][str(channel_id)]
            channel["message_count"] = int(channel.get("message_count", 0)) + 1
            channel["last_message_at"] = now
            channel["updated_at"] = now
            company["updated_at"] = now
            self._save()
            return copy.deepcopy(message)

    def list_messages(self, company_id: str, *, channel_id: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int] | None:
        company = self.get_company(company_id)
        if company is None:
            return None
        messages = list(company.get("messages", {}).values())
        if channel_id:
            messages = [message for message in messages if message.get("channel_id") == str(channel_id)]
        messages.sort(key=lambda message: message.get("created_at", ""))
        total = len(messages)
        return copy.deepcopy(messages[offset: offset + limit]), total

    def get_message(self, company_id: str, message_id: str) -> dict[str, Any] | None:
        company = self.get_company(company_id)
        if company is None:
            return None
        return copy.deepcopy(company.get("messages", {}).get(str(message_id)))

    def create_task(
        self,
        company_id: str,
        *,
        title: str,
        description: str = "",
        target_agent_ids: list[str] | None = None,
        source: str = "manual",
        status: str = "queued",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None:
                return None
            now = timestamp()
            task_id = gen_id("task_")
            task = {
                "id": task_id,
                "company_id": str(company_id),
                "title": str(title),
                "description": str(description),
                "target_agent_ids": list(target_agent_ids or []),
                "source": str(source),
                "status": str(status),
                "dispatches": [],
                "metadata": metadata if isinstance(metadata, dict) else {},
                "created_at": now,
                "updated_at": now,
            }
            company.setdefault("tasks", {})[task_id] = task
            company["updated_at"] = now
            self._save()
            return copy.deepcopy(task)

    def update_task(self, company_id: str, task_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None:
                return None
            task = company.get("tasks", {}).get(str(task_id))
            if task is None or not isinstance(updates, dict):
                return None
            protected = {"id", "company_id", "created_at"}
            for key, value in updates.items():
                if key not in protected:
                    task[key] = value
            task["updated_at"] = timestamp()
            company["updated_at"] = task["updated_at"]
            self._save()
            return copy.deepcopy(task)

    def get_task(self, company_id: str, task_id: str) -> dict[str, Any] | None:
        company = self.get_company(company_id)
        if company is None:
            return None
        return copy.deepcopy(company.get("tasks", {}).get(str(task_id)))

    def list_tasks(
        self,
        company_id: str,
        *,
        status: str | None = None,
        target_agent_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int] | None:
        company = self.get_company(company_id)
        if company is None:
            return None
        tasks = list(company.get("tasks", {}).values())
        if status:
            tasks = [task for task in tasks if task.get("status") == status]
        if target_agent_id:
            tasks = [task for task in tasks if str(target_agent_id) in task.get("target_agent_ids", [])]
        tasks.sort(key=lambda task: task.get("created_at", ""), reverse=True)
        total = len(tasks)
        return copy.deepcopy(tasks[offset: offset + limit]), total

    def append_task_dispatch(self, company_id: str, task_id: str, dispatch: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None:
                return None
            task = company.get("tasks", {}).get(str(task_id))
            if task is None:
                return None
            task.setdefault("dispatches", []).append(copy.deepcopy(dispatch))
            task["status"] = "queued"
            task["updated_at"] = timestamp()
            company["updated_at"] = task["updated_at"]
            self._save()
            return copy.deepcopy(task)

    def list_inbound_routes(self, company_id: str) -> list[dict[str, Any]] | None:
        company = self.get_company(company_id)
        if company is None:
            return None
        routes = list(company.get("inbound_routes", {}).values())
        routes.sort(key=lambda route: route.get("updated_at", ""), reverse=True)
        return copy.deepcopy(routes)

    def upsert_inbound_route(self, company_id: str, route: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None or not isinstance(route, dict):
                return None
            now = timestamp()
            route_id = str(route.get("id") or route.get("route_id") or gen_id("route_"))
            current = company.setdefault("inbound_routes", {}).get(route_id, {})
            item = {
                "id": route_id,
                "provider": route.get("provider", current.get("provider", "local")),
                "source": route.get("source", current.get("source", "")),
                "channel_id": route.get("channel_id", current.get("channel_id", "ops-company")),
                "enabled": bool(route.get("enabled", current.get("enabled", True))),
                "metadata": {**current.get("metadata", {}), **(route.get("metadata") or {})},
                "created_at": current.get("created_at", now),
                "updated_at": now,
            }
            company["inbound_routes"][route_id] = item
            company["updated_at"] = now
            self._save()
            return copy.deepcopy(item)

    def delete_inbound_route(self, company_id: str, route_id: str) -> bool:
        with self._lock:
            company = self._companies.get(str(company_id))
            if company is None or str(route_id) not in company.get("inbound_routes", {}):
                return False
            del company["inbound_routes"][str(route_id)]
            company["updated_at"] = timestamp()
            self._save()
            return True
