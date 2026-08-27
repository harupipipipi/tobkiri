"""Finite compatibility facade for the selected Company state owner."""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    captured_profile_id,
    invoke_global_contract,
)
from domain.safety import approval
from domain.tool_policy.internal_context import tool_server_approval_context_is_internal

AUTHORITY = "rumi.service.host.authorize.v1"
RESOURCE = "rumi.resource.company.v1"
ACTION = "rumi.action.company.state.v1"
STATE_PACK_ID = "rumi_company_state_store_pack"
COORDINATOR = "rumi.action.company.coordinator.v1"
COORDINATOR_PACK_ID = "rumi_company_coordinator_pack"


class CompanyFacadeError(RuntimeError):
    """Expose a stable compatibility diagnostic without falling back to SQLite."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = status


class CompanyContractFacade:
    """Translate the finite Company CRUD legacy routes into global contracts."""

    def __init__(
        self,
        input_data: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> None:
        self.input = dict(input_data)
        self.context = dict(context)
        self.profile_id = _profile_id()

    def run(self, operation: str) -> dict[str, Any]:
        """Execute one compatibility operation through the selected owner."""

        if operation == "list":
            return self._list()
        if operation == "get":
            return self._get(_company_id(self.input))
        if operation == "create":
            return self._create()
        if operation == "update":
            return self._update(_company_id(self.input))
        if operation == "delete":
            return self._delete(_company_id(self.input))
        if operation == "get_settings":
            return self._get_settings(_company_id(self.input))
        if operation == "update_settings":
            return self._update_settings(_company_id(self.input))
        if operation == "list_agents":
            return self._list_agents(_company_id(self.input))
        if operation == "get_agent":
            return self._get_agent(
                _company_id(self.input),
                _required_id(self.input, "agent_id"),
            )
        if operation == "upsert_agent":
            return self._upsert_agent(_company_id(self.input))
        if operation == "delete_agent":
            return self._delete_agent(
                _company_id(self.input),
                _required_id(self.input, "agent_id"),
            )
        if operation == "list_channels":
            return self._list_channels(_company_id(self.input))
        if operation == "get_channel":
            return self._get_channel(
                _company_id(self.input),
                _required_id(self.input, "channel_id"),
            )
        if operation == "upsert_channel":
            return self._upsert_channel(_company_id(self.input))
        if operation == "delete_channel":
            return self._delete_channel(
                _company_id(self.input),
                _required_id(self.input, "channel_id"),
            )
        if operation == "list_tasks":
            return self._list_tasks(_company_id(self.input))
        if operation == "get_task":
            return self._get_task(_company_id(self.input), _required_id(self.input, "task_id"))
        if operation == "upsert_task":
            return self._upsert_task(_company_id(self.input))
        if operation == "delete_task":
            return self._delete_task(_company_id(self.input), _required_id(self.input, "task_id"))
        if operation == "dispatch_task":
            return self._dispatch_task(_company_id(self.input), _required_id(self.input, "task_id"))
        if operation == "list_routes":
            return self._list_records(_company_id(self.input), "routes")
        if operation == "upsert_route":
            return self._upsert_named(_company_id(self.input), "route")
        if operation == "delete_route":
            return self._delete_named(
                _company_id(self.input), "route", _required_id(self.input, "route_id")
            )
        if operation == "append_inbound":
            return self._append_inbound(_company_id(self.input))
        if operation == "list_messages":
            return self._list_timeline(_company_id(self.input), "messages")
        if operation == "get_message":
            return self._get_message(
                _company_id(self.input), _required_id(self.input, "message_id")
            )
        if operation == "append_message":
            return self._append_message(_company_id(self.input))
        if operation == "status":
            return self._status()
        if operation == "bootstrap":
            return self._bootstrap()
        if operation == "resolve_mentions":
            return self._resolve_mentions(_company_id(self.input))
        if operation == "mention":
            return self._mention(_company_id(self.input))
        raise CompanyFacadeError(
            "INVALID_INPUT",
            f"unsupported company compatibility operation: {operation}",
        )

    def _list(self) -> dict[str, Any]:
        snapshot = self._resource("list", {})
        if not isinstance(snapshot, Mapping):
            raise CompanyFacadeError(
                "COMPANY_OWNER_UNAVAILABLE",
                "Company owner returned invalid data",
                503,
            )
        companies = snapshot.get("companies")
        if not isinstance(companies, list):
            companies = []
        offset = _nonnegative_int(self.input.get("offset"), 0)
        limit = _bounded_limit(self.input.get("limit"), 50)
        projected = [_legacy_company(value) for value in companies]
        return {
            "companies": projected[offset : offset + limit],
            "total": len(projected),
            "revision": int(snapshot.get("revision") or 0),
        }

    def _get(self, company_id: str) -> dict[str, Any] | None:
        value = self._resource("get", {"company_id": company_id})
        return _legacy_company(value) if isinstance(value, Mapping) else None

    def _create(self) -> dict[str, Any]:
        name = str(self.input.get("name") or "").strip()
        if not name:
            raise CompanyFacadeError("INVALID_INPUT", "name is required")
        company_id = str(
            self.input.get("company_id") or self.input.get("id") or "company-" + uuid.uuid4().hex
        ).strip()
        result = self._mutate(
            "company.create",
            {
                "company_id": company_id,
                "name": name,
                "description": str(self.input.get("description") or ""),
                "settings": _object(self.input.get("settings"), "settings"),
                "metadata": _object(self.input.get("metadata"), "metadata"),
                "conversation_group_id": str(self.input.get("conversation_group_id") or ""),
            },
        )
        value = result.get("company")
        return _legacy_company(value) if isinstance(value, Mapping) else self._required(company_id)

    def _update(self, company_id: str) -> dict[str, Any] | None:
        updates = self.input.get("updates")
        if updates is None:
            updates = {
                key: value
                for key, value in self.input.items()
                if key not in {"id", "company_id", "approval_token", "_headers"}
            }
        if not isinstance(updates, Mapping):
            raise CompanyFacadeError("INVALID_INPUT", "updates must be a dict")
        permitted = {
            "name",
            "status",
            "settings",
            "description",
            "metadata",
            "conversation_group_id",
        }
        unsupported = sorted(set(updates) - permitted)
        if unsupported:
            raise CompanyFacadeError(
                "COMPANY_LEGACY_FIELD_DEPRECATED",
                "use Company member, role, channel, or task routes for: " + ", ".join(unsupported),
                410,
            )
        normalized = dict(updates)
        for key in {"settings", "metadata"} & set(normalized):
            normalized[key] = _object(normalized[key], key)
        if self._get(company_id) is None:
            return None
        result = self._mutate(
            "company.update",
            {"company_id": company_id, "updates": normalized},
        )
        value = result.get("company")
        return _legacy_company(value) if isinstance(value, Mapping) else self._required(company_id)

    def _delete(self, company_id: str) -> bool:
        if self._get(company_id) is None:
            return False
        self._mutate("company.delete", {"company_id": company_id})
        return True

    def _get_settings(self, company_id: str) -> dict[str, Any] | None:
        company = self._get(company_id)
        if company is None:
            return None
        return dict(company.get("settings") or {})

    def _update_settings(self, company_id: str) -> dict[str, Any] | None:
        settings = _object(self.input.get("settings"), "settings")
        company = self._get(company_id)
        if company is None:
            return None
        _reject_subagent_team_write(company)
        result = self._mutate(
            "company.update",
            {
                "company_id": company_id,
                "updates": {"settings": settings},
                "replace_settings": bool(self.input.get("replace", False)),
            },
        )
        value = result.get("company")
        if not isinstance(value, Mapping):
            value = self._required(company_id)
        return dict(value.get("settings") or {})

    def _list_agents(self, company_id: str) -> list[dict[str, Any]] | None:
        company = self._raw_company(company_id)
        if company is None:
            return None
        return _legacy_agents(company)

    def _get_agent(
        self,
        company_id: str,
        agent_id: str,
    ) -> dict[str, Any] | None:
        company = self._raw_company(company_id)
        if company is None:
            return None
        return next(
            (agent for agent in _legacy_agents(company) if agent["agent_id"] == agent_id),
            None,
        )

    def _upsert_agent(
        self, company_id: str, supplied_agent: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | None:
        agent = _object(supplied_agent or self.input.get("agent"), "agent")
        company = self._raw_company(company_id)
        if company is None:
            return None
        _reject_subagent_team_write(_legacy_company(company))
        agent_id = str(agent.get("agent_id") or agent.get("id") or "").strip()
        if not agent_id:
            agent_id = "agent-" + uuid.uuid4().hex
        operation_id = str(self.input.get("operation_id") or "").strip()
        role_id = str(agent.get("role_key") or agent_id).strip()
        display_name = str(agent.get("display_name") or agent.get("agent_name") or agent_id).strip()
        legacy_metadata = {
            key: value
            for key, value in agent.items()
            if key
            not in {
                "id",
                "agent_id",
                "role_key",
                "agent_name",
                "display_name",
                "model",
                "agent_profile_id",
                "aliases",
                "enabled",
                "status",
                "metadata",
            }
        }
        metadata = _object(agent.get("metadata"), "agent.metadata")
        if operation_id:
            metadata["operation_id"] = operation_id
        metadata["legacy_agent"] = {
            **legacy_metadata,
            "model": str(agent.get("model") or ""),
            "status": str(agent.get("status") or "idle"),
        }
        role = {
            "id": role_id,
            "name": str(agent.get("agent_name") or display_name),
            "work_type": str(agent.get("work_type") or "agent"),
            "metadata": {"legacy_agent_role": role_id},
        }
        member = {
            "id": agent_id,
            "display_name": display_name,
            "role_id": role_id,
            "agent_profile_id": _profile_identifier(agent),
            "mentions": _agent_mentions(agent),
            "enabled": bool(agent.get("enabled", True)),
            "metadata": metadata,
        }
        result = self._mutate(
            "agent.upsert",
            {"company_id": company_id, "role": role, "member": member},
        )
        value = result.get("agent")
        role_value = result.get("role")
        if not isinstance(value, Mapping) or not isinstance(role_value, Mapping):
            return self._get_agent(company_id, agent_id)
        return _legacy_agent(value, role_value)

    def _delete_agent(self, company_id: str, agent_id: str) -> bool:
        if self._get_agent(company_id, agent_id) is None:
            return False
        self._mutate(
            "agent.delete",
            {"company_id": company_id, "agent_id": agent_id},
        )
        return True

    def _list_channels(self, company_id: str) -> list[dict[str, Any]] | None:
        company = self._raw_company(company_id)
        if company is None:
            return None
        channels = company.get("channels")
        channels = dict(channels) if isinstance(channels, Mapping) else {}
        return [
            dict(value)
            for _channel_id, value in sorted(channels.items())
            if isinstance(value, Mapping)
        ]

    def _get_channel(
        self,
        company_id: str,
        channel_id: str,
    ) -> dict[str, Any] | None:
        company = self._raw_company(company_id)
        if company is None:
            return None
        channels = company.get("channels")
        channels = dict(channels) if isinstance(channels, Mapping) else {}
        value = channels.get(channel_id)
        return dict(value) if isinstance(value, Mapping) else None

    def _upsert_channel(self, company_id: str) -> dict[str, Any] | None:
        channel = self.input.get("channel")
        if channel is None:
            channel = {
                key: value
                for key, value in self.input.items()
                if key
                not in {
                    "company_id",
                    "action",
                    "approval_token",
                    "_headers",
                }
            }
        channel = _object(channel, "channel")
        channel_id = str(channel.get("channel_id") or channel.get("id") or "").strip()
        if not channel_id:
            channel_id = "channel-" + uuid.uuid4().hex
        company = self._raw_company(company_id)
        if company is None:
            return None
        _reject_subagent_team_write(_legacy_company(company))
        result = self._mutate(
            "channel.upsert",
            {
                "company_id": company_id,
                "record": {"id": channel_id, **channel},
            },
        )
        value = result.get("channel")
        return (
            dict(value)
            if isinstance(value, Mapping)
            else self._get_channel(
                company_id,
                channel_id,
            )
        )

    def _delete_channel(self, company_id: str, channel_id: str) -> bool:
        company = self._raw_company(company_id)
        if company is None or self._get_channel(company_id, channel_id) is None:
            return False
        _reject_subagent_team_write(_legacy_company(company))
        self._mutate(
            "channel.delete",
            {"company_id": company_id, "record_id": channel_id},
        )
        return True

    def _list_tasks(self, company_id: str) -> list[dict[str, Any]] | None:
        company = self._raw_company(company_id)
        if company is None:
            return None
        tasks = company.get("tasks")
        tasks = dict(tasks) if isinstance(tasks, Mapping) else {}
        return [
            _legacy_task(item) for _key, item in sorted(tasks.items()) if isinstance(item, Mapping)
        ]

    def _get_task(self, company_id: str, task_id: str) -> dict[str, Any] | None:
        company = self._raw_company(company_id)
        if company is None:
            return None
        tasks = company.get("tasks")
        value = tasks.get(task_id) if isinstance(tasks, Mapping) else None
        return _legacy_task(value) if isinstance(value, Mapping) else None

    def _upsert_task(self, company_id: str) -> dict[str, Any] | None:
        company = self._raw_company(company_id)
        if company is None:
            return None
        _reject_subagent_team_write(_legacy_company(company))
        task = _object(self.input.get("task") or self.input.get("updates"), "task")
        if not task:
            task = {
                key: value
                for key, value in self.input.items()
                if key
                not in {
                    "company_id",
                    "task_id",
                    "id",
                    "action",
                    "approval_token",
                    "_headers",
                }
            }
        operation_id = str(self.input.get("operation_id") or "").strip()
        task_id = str(
            self.input.get("task_id")
            or task.get("id")
            or (
                "task-"
                + hashlib.sha256(f"{company_id}\0{operation_id}".encode("utf-8")).hexdigest()[:40]
                if operation_id
                else "task-" + uuid.uuid4().hex
            )
        )
        existing = self._get_task(company_id, task_id) or {}
        metadata = _object(task.get("metadata"), "task.metadata")
        if operation_id:
            metadata["operation_id"] = operation_id
        task = {**task, "metadata": metadata, "idempotency_key": operation_id or task_id}
        source = {**existing, **task, "id": task_id}
        record = _state_task(source)
        result = self._mutate("task.upsert", {"company_id": company_id, "record": record})
        value = result.get("task")
        return (
            _legacy_task(value)
            if isinstance(value, Mapping)
            else self._get_task(company_id, task_id)
        )

    def _delete_task(self, company_id: str, task_id: str) -> bool:
        company = self._raw_company(company_id)
        if company is None or self._get_task(company_id, task_id) is None:
            return False
        _reject_subagent_team_write(_legacy_company(company))
        self._mutate("task.delete", {"company_id": company_id, "task_id": task_id})
        return True

    def _dispatch_task(self, company_id: str, task_id: str) -> dict[str, Any]:
        company = self._raw_company(company_id)
        if company is None:
            raise CompanyFacadeError("NOT_FOUND", "company not found", 404)
        _reject_subagent_team_write(_legacy_company(company))
        arguments = {"company_id": company_id, "task_id": task_id}
        receipt = _receipt_for(
            self.input,
            self.context,
            self.profile_id,
            "dispatch_task",
            arguments,
            service_pack_id=COORDINATOR_PACK_ID,
            authority="company.coordinate",
            operation="company.coordinator.dispatch_task",
        )
        result = _invoke(
            COORDINATOR,
            "dispatch_task",
            {**arguments, **receipt, "profile_id": self.profile_id},
        )
        return dict(result) if isinstance(result, Mapping) else {}

    def _required(self, company_id: str) -> dict[str, Any]:
        value = self._get(company_id)
        if value is None:
            raise CompanyFacadeError(
                "COMPANY_OWNER_UNAVAILABLE",
                "Company mutation lost state",
                503,
            )
        return value

    def _mutate(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = self._resource("list", {})
        if not isinstance(snapshot, Mapping):
            raise CompanyFacadeError(
                "COMPANY_OWNER_UNAVAILABLE",
                "Company owner is unavailable",
                503,
            )
        current_revision = int(snapshot.get("revision") or 0)
        supplied_revision = self.input.get("expected_revision")
        if supplied_revision is not None:
            try:
                expected_revision = int(supplied_revision)
            except (TypeError, ValueError) as exc:
                raise CompanyFacadeError(
                    "INVALID_INPUT", "expected_revision must be an integer"
                ) from exc
            if expected_revision != current_revision:
                raise CompanyFacadeError(
                    "REVISION_CONFLICT",
                    "Company resource revision changed; refresh before retrying",
                    409,
                )
        else:
            expected_revision = current_revision
        exact = {
            "expected_revision": expected_revision,
            **dict(arguments),
        }
        receipt = _receipt(self.input, self.context, self.profile_id, name, exact)
        result = _invoke(
            ACTION,
            name,
            {**exact, **receipt, "profile_id": self.profile_id},
        )
        return dict(result) if isinstance(result, Mapping) else {}

    def _resource(self, name: str, payload: Mapping[str, Any]) -> Any:
        return _invoke(RESOURCE, name, {"profile_id": self.profile_id, **dict(payload)})

    def _raw_company(self, company_id: str) -> dict[str, Any] | None:
        value = self._resource("get", {"company_id": company_id})
        return dict(value) if isinstance(value, Mapping) else None

    def _list_records(
        self,
        company_id: str,
        key: str,
    ) -> list[dict[str, Any]] | None:
        company = self._raw_company(company_id)
        if company is None:
            return None
        records = company.get(key)
        records = dict(records) if isinstance(records, Mapping) else {}
        return [
            dict(value)
            for _record_id, value in sorted(records.items())
            if isinstance(value, Mapping)
        ]

    def _upsert_named(self, company_id: str, kind: str) -> dict[str, Any] | None:
        record = _object(self.input.get(kind), kind)
        if not record:
            record = {
                key: value
                for key, value in self.input.items()
                if key not in {"company_id", "action", "approval_token", "_headers"}
            }
        operation_id = str(self.input.get("operation_id") or "").strip()
        record_id = str(
            record.get("id")
            or record.get(f"{kind}_id")
            or (
                f"{kind}-"
                + hashlib.sha256(
                    f"{company_id}\0{operation_id}".encode("utf-8")
                ).hexdigest()[:40]
                if operation_id
                else f"{kind}-" + uuid.uuid4().hex
            )
        )
        company = self._raw_company(company_id)
        if company is None:
            return None
        metadata = _object(record.get("metadata"), f"{kind}.metadata")
        if operation_id:
            metadata["operation_id"] = operation_id
        record = {**record, "metadata": metadata}
        result = self._mutate(
            f"{kind}.upsert", {"company_id": company_id, "record": {"id": record_id, **record}}
        )
        value = result.get(kind)
        return dict(value) if isinstance(value, Mapping) else None

    def _delete_named(self, company_id: str, kind: str, record_id: str) -> bool:
        company = self._raw_company(company_id)
        records = company.get(kind + "s") if isinstance(company, Mapping) else None
        if not isinstance(records, Mapping) or record_id not in records:
            return False
        self._mutate(f"{kind}.delete", {"company_id": company_id, "record_id": record_id})
        return True

    def _append_inbound(self, company_id: str) -> dict[str, Any] | None:
        if self._raw_company(company_id) is None:
            return None
        metadata = _object(self.input.get("metadata"), "metadata")
        route_id = str(self.input.get("route_id") or "").strip()
        if route_id:
            metadata["route_id"] = route_id
        record = {
            "id": "inbound-" + uuid.uuid4().hex,
            "type": "inbound",
            "actor_id": str(self.input.get("sender_id") or "external"),
            "channel_id": str(self.input.get("channel_id") or ""),
            "text": str(self.input.get("content") or ""),
            "metadata": metadata,
        }
        result = self._mutate("inbound.append", {"company_id": company_id, "record": record})
        return dict(result.get("inbound") or {})

    def _list_timeline(self, company_id: str, key: str) -> dict[str, Any] | None:
        company = self._raw_company(company_id)
        if company is None:
            return None
        records = company.get(key)
        if not isinstance(records, list):
            records = []
        channel_id = str(self.input.get("channel_id") or "")
        thread_id = str(self.input.get("thread_id") or "")
        projected = [dict(item) for item in records if isinstance(item, Mapping)]
        if channel_id:
            projected = [
                item for item in projected if str(item.get("channel_id") or "") == channel_id
            ]
        if thread_id:
            projected = [
                item
                for item in projected
                if str(_object(item.get("metadata"), "metadata").get("thread_id") or "")
                == thread_id
            ]
        descending = str(self.input.get("order") or "").strip().lower() in {
            "desc",
            "descending",
            "latest",
            "newest",
        }
        projected.sort(key=lambda item: int(item.get("created_at_ms") or 0), reverse=descending)
        total = len(projected)
        limit = _bounded_limit(self.input.get("limit"), 50)
        offset = _nonnegative_int(self.input.get("offset"), 0)
        if _enabled(self.input.get("tail")) or _enabled(self.input.get("latest")):
            offset = max(total - limit, 0)
        return {"messages": projected[offset : offset + limit], "total": total}

    def _get_message(self, company_id: str, message_id: str) -> dict[str, Any] | None:
        company = self._raw_company(company_id)
        if company is None:
            return None
        records = company.get("messages")
        if not isinstance(records, list):
            return None
        for record in records:
            if isinstance(record, Mapping) and str(record.get("id") or "") == message_id:
                return dict(record)
        return None

    def _append_message(self, company_id: str) -> dict[str, Any] | None:
        if self._raw_company(company_id) is None:
            return None
        metadata = _object(self.input.get("metadata"), "metadata")
        metadata["thread_id"] = str(self.input.get("thread_id") or "")
        metadata["target_agent_ids"] = list(self.input.get("target_agent_ids") or [])
        metadata["task_ids"] = list(self.input.get("task_ids") or [])
        operation_id = str(self.input.get("operation_id") or "").strip()
        message_id = (
            "message-"
            + hashlib.sha256(f"{company_id}\0{operation_id}".encode("utf-8")).hexdigest()[:40]
            if operation_id
            else "message-" + uuid.uuid4().hex
        )
        if operation_id:
            metadata["operation_id"] = operation_id
        record = {
            "id": message_id,
            "type": "message",
            "actor_id": str(self.input.get("sender_id") or "user"),
            "channel_id": str(self.input.get("channel_id") or "ops-company"),
            "text": str(self.input.get("content") or ""),
            "metadata": metadata,
        }
        result = self._mutate("message.append", {"company_id": company_id, "record": record})
        return dict(result.get("message") or {})

    def _status(self) -> dict[str, Any]:
        """Project Company state into the legacy status response without SQLite."""

        conversation_id = str(self.input.get("conversation_id") or "").strip()
        if conversation_id:
            company = self._company_for_conversation(conversation_id)
            if company is None and _enabled(self.input.get("bootstrap")):
                company = self._bootstrap_company(conversation_id)
            company_id = (
                str(company.get("id") or "")
                if isinstance(company, Mapping)
                else _conversation_company_id(conversation_id)
            )
        else:
            company_id = str(self.input.get("company_id") or "operations-company")
            company = self._raw_company(company_id)
            if company is None and _enabled(self.input.get("bootstrap")):
                company = self._bootstrap_company("")
        snapshot = self._resource("list", {})
        return {
            "bootstrapped": company is not None,
            "company_id": company_id,
            "conversation_id": conversation_id,
            "company": _legacy_company(company) if isinstance(company, Mapping) else None,
            "runtime": _state_runtime_counts(company),
            "reporting": {"blocker_signals": _state_blocker_summary(company)},
            "revision": int(snapshot.get("revision") or 0) if isinstance(snapshot, Mapping) else 0,
        }

    def _bootstrap(self) -> dict[str, Any]:
        """Create the selected Company state record and default member projection."""

        conversation_id = str(self.input.get("conversation_id") or "").strip()
        company = self._bootstrap_company(conversation_id)
        return {"bootstrapped": True, "company": _legacy_company(company)}

    def _resolve_mentions(self, company_id: str) -> dict[str, Any] | None:
        company = self._raw_company(company_id)
        if company is None:
            return None
        content = str(self.input.get("content") or self.input.get("message") or "")
        mentions = _mention_values(content)
        agents = _legacy_agents(company)
        resolved: list[dict[str, Any]] = []
        unresolved: list[str] = []
        seen: set[str] = set()
        for mention in mentions:
            if mention in {"team", "channel"}:
                continue
            target = _MENTION_ALIASES.get(mention, mention)
            candidates = (
                agents
                if target == "all"
                else [agent for agent in agents if target in _agent_mention_keys(agent)]
            )
            if not candidates:
                unresolved.append(mention)
                continue
            for agent in candidates:
                agent_id = str(agent.get("agent_id") or "")
                if agent_id and agent_id not in seen:
                    seen.add(agent_id)
                    resolved.append(agent)
        return {
            "mentions": mentions,
            "resolved_agents": resolved,
            "resolved_agent_ids": [agent["agent_id"] for agent in resolved],
            "unresolved": unresolved,
        }

    def _mention(self, company_id: str) -> dict[str, Any] | None:
        resolution = self._resolve_mentions(company_id)
        if resolution is None:
            return None
        targets = list(resolution["resolved_agent_ids"])
        self.input["target_agent_ids"] = targets
        task_ids = [
            "mention-"
            + hashlib.sha256(
                f"{company_id}\0{self.input.get('content') or self.input.get('message') or ''}\0{agent_id}".encode(
                    "utf-8"
                )
            ).hexdigest()[:40]
            for agent_id in targets
        ]
        self.input["task_ids"] = task_ids
        message = self._append_message(company_id)
        if message is None:
            return None
        tasks: list[dict[str, Any]] = []
        content = str(self.input.get("content") or self.input.get("message") or "")
        for agent_id, task_id in zip(targets, task_ids):
            result = self._mutate(
                "task.upsert",
                {
                    "company_id": company_id,
                    "record": {
                        "id": task_id,
                        "title": content[:120] or "Mentioned Company task",
                        "description": content,
                        "status": "queued",
                        "assignee_member_id": agent_id,
                        "channel_id": str(message.get("channel_id") or ""),
                        "idempotency_key": task_id,
                        "metadata": {
                            "source": "company_mention",
                            "message_id": str(message.get("id") or ""),
                            "legacy_task": {
                                "company_id": company_id,
                                "target_agent_ids": [agent_id],
                                "source": "mention",
                                "dispatches": [],
                            },
                        },
                    },
                },
            )
            task = result.get("task")
            if isinstance(task, Mapping):
                tasks.append(_legacy_task(task))
        return {"message": message, "tasks": tasks, **resolution}

    def _company_for_conversation(self, conversation_id: str) -> Mapping[str, Any] | None:
        snapshot = self._resource("list", {})
        companies = snapshot.get("companies") if isinstance(snapshot, Mapping) else []
        for company in companies if isinstance(companies, list) else []:
            if not isinstance(company, Mapping):
                continue
            metadata = company.get("metadata")
            if (
                str(company.get("conversation_group_id") or "")
                == "company:" + _conversation_company_id(conversation_id)
                or isinstance(metadata, Mapping)
                and str(metadata.get("conversation_id") or "") == conversation_id
            ):
                return company
        return None

    def _bootstrap_company(self, conversation_id: str) -> dict[str, Any]:
        from domain.company.models import (
            DEFAULT_COMPANY_DESCRIPTION,
            DEFAULT_COMPANY_ID,
            DEFAULT_COMPANY_NAME,
            DEFAULT_SETTINGS,
            default_agents,
        )

        company_id = (
            _conversation_company_id(conversation_id) if conversation_id else DEFAULT_COMPANY_ID
        )
        existing = self._raw_company(company_id)
        if isinstance(existing, Mapping):
            return dict(existing)
        metadata = _object(self.input.get("metadata"), "metadata")
        employee_model = _conversation_employee_model(conversation_id, metadata)
        metadata = {
            "profile_id": "defaultspack.operations_company",
            **({"conversation_id": conversation_id, "source": "chat"} if conversation_id else {}),
            **metadata,
        }
        if employee_model:
            metadata["employee_model"] = employee_model
        result = self._mutate(
            "company.create",
            {
                "company_id": company_id,
                "name": str(
                    metadata.get("name")
                    or ("Executive Team" if conversation_id else DEFAULT_COMPANY_NAME)
                ),
                "description": str(
                    metadata.get("description")
                    or (
                        "Employee group delegated from the current chat."
                        if conversation_id
                        else DEFAULT_COMPANY_DESCRIPTION
                    )
                ),
                "settings": dict(DEFAULT_SETTINGS),
                "metadata": metadata,
                "conversation_group_id": "company:" + company_id,
            },
        )
        company = result.get("company")
        if not isinstance(company, Mapping):
            raise CompanyFacadeError(
                "COMPANY_OWNER_UNAVAILABLE", "Company creation returned invalid data", 503
            )
        agents = default_agents()
        if employee_model:
            for agent in agents:
                agent["model"] = employee_model
        for agent in agents:
            self._upsert_agent(company_id, agent)
        value = self._raw_company(company_id)
        return dict(value) if isinstance(value, Mapping) else dict(company)


def _receipt(
    input_data: Mapping[str, Any],
    context: Mapping[str, Any],
    profile_id: str,
    name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    return _receipt_for(
        input_data,
        context,
        profile_id,
        name,
        arguments,
        service_pack_id=STATE_PACK_ID,
        authority="company.state.manage",
        operation=f"company.state.{name}",
    )


def _receipt_for(
    input_data: Mapping[str, Any],
    context: Mapping[str, Any],
    profile_id: str,
    name: str,
    arguments: Mapping[str, Any],
    *,
    service_pack_id: str,
    authority: str,
    operation: str,
) -> dict[str, Any]:
    if not tool_server_approval_context_is_internal(dict(context)):
        token = _approval_token(input_data)
        if not token:
            raise CompanyFacadeError(
                "APPROVAL_REQUIRED",
                "approval token is required",
                403,
            )
        verified = approval.verify_execution_token(
            token,
            name,
            approval.hash_arguments(dict(input_data)),
            consume=True,
        )
        if not verified.valid:
            raise CompanyFacadeError(
                "APPROVAL_INVALID",
                "approval token is invalid",
                403,
            )
    caller_id = str(
        context.get("principal_id") or context.get("user_id") or "defaultspack.local_user"
    )
    scope = {
        "service_pack_id": service_pack_id,
        "operation": operation,
        "authority": authority,
        "caller_id": caller_id,
        "caller_pack_id": "defaultspack",
        "caller_function_id": f"domain.company.contract_facade.{name}",
        "profile_id": profile_id,
        "workspace_id": "",
        "session_id": str(context.get("session_id") or ""),
        "arguments": dict(arguments),
        "approval_required": False,
    }
    issued = _invoke(AUTHORITY, "authorize", scope)
    if not isinstance(issued, Mapping) or not issued.get("authorized"):
        raise CompanyFacadeError(
            "COMPANY_AUTHORITY_DENIED",
            str((issued or {}).get("reason") or "Company state denied"),
            403,
        )
    return {
        "authority_receipt": str(issued.get("receipt") or ""),
        "caller_id": caller_id,
        "caller_pack_id": "defaultspack",
        "caller_function_id": scope["caller_function_id"],
        "session_id": scope["session_id"],
    }


def _legacy_company(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project the Company state record into the established route shape."""

    company = dict(value)
    members = company.get("members") if isinstance(company.get("members"), Mapping) else {}
    roles = company.get("roles") if isinstance(company.get("roles"), Mapping) else {}
    agents = {
        agent["agent_id"]: agent for agent in _legacy_agents({"members": members, "roles": roles})
    }
    return {
        "id": str(company.get("id") or ""),
        "name": str(company.get("name") or "Company"),
        "description": str(company.get("description") or ""),
        "status": str(company.get("status") or "active"),
        "conversation_group_id": str(company.get("conversation_group_id") or ""),
        "settings": dict(company.get("settings") or {}),
        "metadata": dict(company.get("metadata") or {}),
        "agents": agents,
        "channels": dict(company.get("channels") or {}),
        "created_at_ms": company.get("created_at_ms"),
        "updated_at_ms": company.get("updated_at_ms"),
    }


def _company_id(input_data: Mapping[str, Any]) -> str:
    company_id = str(input_data.get("company_id") or input_data.get("id") or "").strip()
    if not company_id:
        raise CompanyFacadeError("INVALID_INPUT", "company_id is required")
    return company_id


def _object(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CompanyFacadeError("INVALID_INPUT", f"{name} must be a dict")
    return dict(value)


def _legacy_agents(company: Mapping[str, Any]) -> list[dict[str, Any]]:
    members = company.get("members")
    members = dict(members) if isinstance(members, Mapping) else {}
    roles = company.get("roles")
    roles = dict(roles) if isinstance(roles, Mapping) else {}
    agents = []
    for member_id, member in members.items():
        if not isinstance(member, Mapping):
            continue
        role = roles.get(str(member.get("role_id") or ""))
        role_data = dict(role) if isinstance(role, Mapping) else {}
        agents.append(_legacy_agent(member, role_data, fallback_id=str(member_id)))
    return sorted(agents, key=lambda agent: agent["agent_id"])


def _legacy_agent(
    member: Mapping[str, Any],
    role: Mapping[str, Any],
    *,
    fallback_id: str = "",
) -> dict[str, Any]:
    agent_id = str(member.get("id") or fallback_id)
    metadata = member.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    legacy = metadata.get("legacy_agent")
    legacy = dict(legacy) if isinstance(legacy, Mapping) else {}
    return {
        "id": agent_id,
        "agent_id": agent_id,
        "role_key": str(member.get("role_id") or ""),
        "agent_name": str(role.get("name") or member.get("display_name") or agent_id),
        "display_name": str(member.get("display_name") or agent_id),
        "model": str(legacy.get("model") or member.get("agent_profile_id") or ""),
        "aliases": list(member.get("mentions") or []),
        "enabled": bool(member.get("enabled", True)),
        "status": str(legacy.get("status") or "idle"),
        "work_type": str(role.get("work_type") or "agent"),
        "metadata": metadata,
        **{key: value for key, value in legacy.items() if key not in {"model", "status"}},
    }


def _legacy_task(value: Mapping[str, Any]) -> dict[str, Any]:
    metadata = value.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    legacy = metadata.get("legacy_task")
    legacy = dict(legacy) if isinstance(legacy, Mapping) else {}
    return {
        "id": str(value.get("id") or ""),
        "company_id": str(legacy.get("company_id") or ""),
        "title": str(value.get("title") or "Task"),
        "description": str(value.get("description") or ""),
        "status": str(value.get("status") or "queued"),
        "target_agent_ids": list(legacy.get("target_agent_ids") or []),
        "source": str(legacy.get("source") or "manual"),
        "dispatches": list(legacy.get("dispatches") or []),
        "metadata": metadata,
        "created_at_ms": value.get("created_at_ms"),
        "updated_at_ms": value.get("updated_at_ms"),
    }


def _state_task(value: Mapping[str, Any]) -> dict[str, Any]:
    targets = value.get("target_agent_ids")
    targets = [str(item) for item in targets] if isinstance(targets, list) else []
    metadata = _object(value.get("metadata"), "task.metadata")
    metadata["legacy_task"] = {
        "company_id": str(value.get("company_id") or ""),
        "target_agent_ids": targets,
        "source": str(value.get("source") or "manual"),
        "dispatches": list(value.get("dispatches") or []),
    }
    return {
        "id": str(value.get("id") or ""),
        "title": str(value.get("title") or "Task"),
        "description": str(value.get("description") or ""),
        "status": str(value.get("status") or "queued"),
        "assignee_member_id": targets[0] if targets else "",
        "idempotency_key": str(value.get("idempotency_key") or value.get("id") or ""),
        "metadata": metadata,
    }


def _profile_identifier(agent: Mapping[str, Any]) -> str:
    candidate = str(agent.get("agent_profile_id") or "").strip()
    if not candidate:
        candidate = str(agent.get("model") or "").strip()
    if candidate and candidate.replace("_", "a").replace("-", "a").isalnum():
        return candidate[:255]
    return "default"


def _agent_mentions(agent: Mapping[str, Any]) -> list[str]:
    aliases = agent.get("aliases")
    aliases = aliases if isinstance(aliases, list) else []
    agent_id = str(agent.get("agent_id") or agent.get("id") or "").strip()
    return [agent_id, *[str(value).lstrip("@") for value in aliases]]


def _required_id(input_data: Mapping[str, Any], key: str) -> str:
    value = str(input_data.get(key) or "").strip()
    if not value:
        raise CompanyFacadeError("INVALID_INPUT", f"{key} is required")
    return value


def _reject_subagent_team_write(company: Mapping[str, Any]) -> None:
    metadata = company.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    settings = company.get("settings")
    settings = dict(settings) if isinstance(settings, Mapping) else {}
    nested = settings.get("subagent_team")
    nested = dict(nested) if isinstance(nested, Mapping) else {}
    if (
        bool(metadata.get("subagent_team"))
        or bool(metadata.get("subagent_team_workspace"))
        or metadata.get("surface") == "subagent_team_workspace"
        or metadata.get("workspace_kind") == "subagent_team"
        or metadata.get("frontend_surface") == "subagent_team_workspace"
        or nested.get("guard_owner") == "subagent_team_workspace"
        or nested.get("surface") == "subagent_team_workspace"
        or nested.get("workspace_kind") == "subagent_team"
        or nested.get("frontend_surface") == "subagent_team_workspace"
    ):
        raise CompanyFacadeError(
            "SUBAGENT_TEAM_POLICY_REQUIRED",
            "use /api/subagent-team for subagent team writes",
            403,
        )


def _approval_token(input_data: Mapping[str, Any]) -> str:
    token = str(input_data.get("approval_token") or "").strip()
    if token:
        return token
    headers = input_data.get("_headers")
    if not isinstance(headers, Mapping):
        return ""
    return str(headers.get("X-Rumi-Approval") or headers.get("x-rumi-approval") or "").strip()


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "tail", "latest"}
    return False


def _conversation_company_id(conversation_id: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", conversation_id).strip("-").lower()
    digest = hashlib.sha1(conversation_id.encode("utf-8")).hexdigest()[:10]
    return "chat-team-" + (clean[:40].strip("-") or digest) + "-" + digest


def _state_runtime_counts(company: Mapping[str, Any] | None) -> dict[str, int]:
    if not isinstance(company, Mapping):
        return {}
    return {
        "messages": len(company.get("messages") or []),
        "tasks": len(company.get("tasks") or {}),
        "threads": 0,
        "runs": 0,
        "inbox": 0,
        "summaries": 0,
    }


def _state_blocker_summary(company: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(company, Mapping):
        return {"blocker_count": 0, "latest_signal": None, "signals": []}
    tasks = company.get("tasks")
    blocked = (
        [
            dict(task)
            for task in tasks.values()
            if isinstance(task, Mapping) and task.get("status") == "blocked"
        ]
        if isinstance(tasks, Mapping)
        else []
    )
    blocked.sort(key=lambda task: int(task.get("updated_at_ms") or 0), reverse=True)
    return {
        "blocker_count": len(blocked),
        "latest_signal": blocked[0] if blocked else None,
        "signals": blocked[:20],
    }


def _mention_values(content: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for value in re.findall(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_.:-]{1,100})", content):
        normalized = value.casefold()
        if normalized not in seen:
            seen.add(normalized)
            values.append(normalized)
    return values


def _agent_mention_keys(agent: Mapping[str, Any]) -> set[str]:
    metadata = agent.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    aliases = agent.get("aliases")
    aliases = aliases if isinstance(aliases, list) else []
    keys = {
        str(agent.get("agent_id") or "").casefold(),
        str(agent.get("id") or "").casefold(),
        str(agent.get("role_key") or "").casefold(),
        str(metadata.get("short_id") or "").casefold(),
    }
    keys.update(str(alias).lstrip("@").casefold() for alias in aliases)
    return keys


_MENTION_ALIASES = {
    "pm": "project_manager",
    "ops_manager": "operations_manager",
}


def _bounded_limit(value: Any, default: int) -> int:
    limit = _nonnegative_int(value, default)
    return min(max(limit, 1), 200)


def _nonnegative_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return default


def _profile_id() -> str:
    session = get_container().get_or_none("v4_dispatch_session")
    if session is None:
        raise CompanyFacadeError(
            "COMPANY_OWNER_UNAVAILABLE",
            "resolved profile is unavailable",
            503,
        )
    return captured_profile_id(session)


def _conversation_employee_model(
    conversation_id: str,
    metadata: Mapping[str, Any] | None,
) -> str:
    """Resolve the employee model from the canonical conversation owner."""

    supplied = metadata if isinstance(metadata, Mapping) else {}
    for key in ("employee_model", "model", "preferred_model"):
        candidate = str(supplied.get(key) or "").strip()
        if candidate:
            return candidate
    if not conversation_id:
        return ""
    try:
        from domain.chat.store import ChatStore

        conversation = ChatStore().get_conversation(conversation_id) or {}
    except Exception:
        return ""
    return str(conversation.get("model") or "").strip()


def _invoke(contract: str, operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise CompanyFacadeError(
            "COMPANY_OWNER_UNAVAILABLE",
            "Company owner is unavailable",
            503,
        )
    return invoke_global_contract(registry, contract, operation, payload)
