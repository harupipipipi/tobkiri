from __future__ import annotations

from typing import Any, Callable

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.input.dispatcher import dispatch_input
from domain.input.envelope import RumiInputEnvelope

from .mention import CompanyMentionService, extract_mentions
from .models import DEFAULT_CHANNEL_ID
from .run_dispatcher import CompanyRunDispatcher
from .runtime_store import CompanyRuntimeStore
from .store import CompanyStore


class CompanyMessageRouter:
    """Slack-like company message router.

    Mentions are routed as instructions to active runs, or as delegated tasks
    when no active run exists for the target agent.
    """

    def __init__(
        self,
        *,
        company_store: CompanyStore | None = None,
        runtime_store: CompanyRuntimeStore | None = None,
        run_dispatcher: CompanyRunDispatcher | None = None,
        input_dispatcher: Callable[[RumiInputEnvelope, dict[str, Any] | None], dict[str, Any]] | None = None,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> None:
        self.company_store = company_store or CompanyStore()
        self.runtime_store = runtime_store or CompanyRuntimeStore()
        self.run_dispatcher = run_dispatcher or CompanyRunDispatcher(
            company_store=self.company_store,
            runtime_store=self.runtime_store,
            settings_owner=settings_owner,
        )
        self.input_dispatcher = input_dispatcher or dispatch_input

    def post_message(
        self,
        company_id: str,
        *,
        content: str,
        sender_id: str = "user",
        channel_id: str = DEFAULT_CHANNEL_ID,
        thread_id: str | None = None,
        target_agent_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        company = self.company_store.get_company(company_id)
        if company is None:
            return None
        fallback_mentions = extract_mentions(content)
        resolution = CompanyMentionService(self.company_store).resolve(
            company_id,
            content,
        ) or {
            "mentions": fallback_mentions,
            "resolved_agents": [],
            "resolved_agent_ids": [],
            "unresolved": fallback_mentions,
        }
        mentions = list(resolution.get("mentions") or [])
        message = self.runtime_store.add_message(
            company_id,
            channel_id=channel_id,
            sender_id=sender_id,
            content=content,
            thread_id=thread_id,
            mentions=mentions,
            metadata=metadata or {},
        )
        explicit_targets = [str(item) for item in (target_agent_ids or []) if str(item).strip()]
        target_ids = _dedupe([*list(resolution.get("resolved_agent_ids") or []), *explicit_targets])
        routes: list[dict[str, Any]] = []
        task_ids: list[str] = []

        for unresolved in resolution.get("unresolved") or []:
            self.runtime_store.add_inbox_item(
                company_id,
                agent_id="operations_manager",
                message_id=message["message_id"],
                kind="unassigned_mention",
                content="Unresolved mention @" + str(unresolved) + " in company message.",
                priority="high",
                metadata={"mention": unresolved, "thread_id": message.get("thread_id")},
            )

        for agent_id in target_ids:
            route = self._route_target(
                company,
                message,
                agent_id,
                content=content,
                sender_id=sender_id,
                metadata=metadata or {},
                context=context or {},
            )
            routes.append(route)
            if route.get("task_id"):
                task_ids.append(str(route["task_id"]))

        if task_ids:
            message = self.runtime_store.update_message_tasks(str(message["message_id"]), task_ids) or message

        self._notify_manager(company_id, message, routes, resolution)
        self._mark_dirty(company_id, message, task_ids, routes)
        return {
            "message": message,
            "task": self.runtime_store.get_task(task_ids[0], company_id=company_id) if task_ids else None,
            "tasks": [self.runtime_store.get_task(task_id, company_id=company_id) for task_id in task_ids],
            "routes": routes,
            "resolution": {**resolution, "resolved_agent_ids": target_ids},
            "deprecation": None,
        }

    def _route_target(
        self,
        company: dict[str, Any],
        message: dict[str, Any],
        agent_id: str,
        *,
        content: str,
        sender_id: str,
        metadata: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        company_id = str(company["id"])
        active = self.runtime_store.find_active_run_for_agent(company_id, agent_id)
        inbox_item = self.runtime_store.add_inbox_item(
            company_id,
            agent_id=agent_id,
            message_id=message["message_id"],
            run_id=active.get("run_id") if active else None,
            kind="mention",
            content=content,
            status="routing" if active else "open",
            priority=str(metadata.get("priority") or "normal"),
            metadata={"sender_id": sender_id, "thread_id": message.get("thread_id")},
        )
        if active:
            instruction = self._inject_instruction(
                company_id,
                message,
                active,
                agent_id,
                content=content,
                sender_id=sender_id,
                context=context,
            )
            delivered = _instruction_ok(instruction)
            self.runtime_store.update_inbox_item(
                str(inbox_item["inbox_id"]),
                {
                    "status": "delivered" if delivered else "failed",
                    "metadata": {"instruction": instruction},
                },
            )
            if not delivered:
                self._notify_instruction_failure(company_id, message, agent_id, active, instruction)
            return {
                "agent_id": agent_id,
                "route": "run.instruction",
                "run_id": active.get("run_id"),
                "instruction": instruction,
                "status": instruction.get("status", "ok") if isinstance(instruction, dict) else "ok",
                "inbox_id": inbox_item.get("inbox_id"),
            }

        task = self.runtime_store.create_task(
            company_id,
            title="Mention request for " + agent_id,
            description=content,
            target_agent_ids=[agent_id],
            source="mention",
            channel_id=message.get("channel_id"),
            thread_id=message.get("thread_id"),
            message_id=message.get("message_id"),
            priority=str(metadata.get("priority") or "normal"),
            metadata={
                "sender_id": sender_id,
                "mentions": message.get("mentions", []),
                "message_id": message.get("message_id"),
                "thread_id": message.get("thread_id"),
                "subagent_team": metadata.get("subagent_team") if isinstance(metadata.get("subagent_team"), dict) else {},
                "channel_check": (metadata.get("subagent_team") if isinstance(metadata.get("subagent_team"), dict) else {}).get("channel_check"),
            },
        )
        dispatch = self.run_dispatcher.dispatch_task(
            company_id,
            str(task["task_id"]),
            requested_by=sender_id,
            policy=metadata.get("policy") if isinstance(metadata.get("policy"), dict) else None,
            context=context,
        )
        return {
            "agent_id": agent_id,
            "route": "agent.delegate",
            "task_id": task["task_id"],
            "dispatch": dispatch,
            "status": (dispatch or {}).get("dispatch", {}).get("status", "queued") if isinstance(dispatch, dict) else "queued",
            "inbox_id": inbox_item.get("inbox_id"),
        }

    def _inject_instruction(
        self,
        company_id: str,
        message: dict[str, Any],
        active_run: dict[str, Any],
        agent_id: str,
        *,
        content: str,
        sender_id: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(active_run.get("run_id") or "")
        envelope = RumiInputEnvelope(
            role="user",
            input=content,
            chat={"company_id": company_id, "thread_id": str(message.get("thread_id") or "")},
            source={"type": "company_slack_runtime", "provider": "company", "sender_id": sender_id},
            target={"execution_id": run_id, "agent_run_id": run_id, "agent_id": agent_id},
            delivery={"action_id": "run.instruction", "priority": "urgent"},
            metadata={
                "company_id": company_id,
                "message_id": message.get("message_id"),
                "thread_id": message.get("thread_id"),
                "agent_id": agent_id,
                "route": "run.instruction",
            },
            params={"instruction": content, "priority": "urgent"},
        )
        try:
            result = self.input_dispatcher(envelope, context or {})
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        return result if isinstance(result, dict) else {"status": "error", "error": str(result)}

    def _notify_instruction_failure(
        self,
        company_id: str,
        message: dict[str, Any],
        agent_id: str,
        active_run: dict[str, Any],
        instruction: dict[str, Any],
    ) -> None:
        self.runtime_store.add_inbox_item(
            company_id,
            agent_id="operations_manager",
            message_id=message.get("message_id"),
            run_id=active_run.get("run_id"),
            kind="instruction_failure",
            content="Failed to inject company mention into active run for " + str(agent_id) + ".",
            status="open",
            priority="high",
            metadata={
                "agent_id": agent_id,
                "thread_id": message.get("thread_id"),
                "instruction": instruction,
            },
        )

    def _notify_manager(
        self,
        company_id: str,
        message: dict[str, Any],
        routes: list[dict[str, Any]],
        resolution: dict[str, Any],
    ) -> None:
        if not routes and not resolution.get("unresolved"):
            return
        self.runtime_store.add_inbox_item(
            company_id,
            agent_id="operations_manager",
            message_id=message.get("message_id"),
            kind="manager_tick",
            content="Company message routed to " + ", ".join(str(route.get("agent_id")) for route in routes if route.get("agent_id")),
            status="open",
            priority="normal",
            metadata={"routes": routes, "resolution": resolution, "thread_id": message.get("thread_id")},
        )

    def _mark_dirty(self, company_id: str, message: dict[str, Any], task_ids: list[str], routes: list[dict[str, Any]]) -> None:
        self.runtime_store.mark_summary_dirty(company_id, "company", company_id)
        self.runtime_store.mark_summary_dirty(company_id, "channel", str(message.get("channel_id") or DEFAULT_CHANNEL_ID))
        if message.get("thread_id"):
            self.runtime_store.mark_summary_dirty(company_id, "thread", str(message["thread_id"]))
        for task_id in task_ids:
            self.runtime_store.mark_summary_dirty(company_id, "task", task_id)
        for route in routes:
            if route.get("run_id"):
                self.runtime_store.mark_summary_dirty(company_id, "run", str(route["run_id"]))


class CompanySlackRuntime(CompanyMessageRouter):
    """Primary asynchronous team workspace runtime."""


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = str(value).strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _instruction_ok(instruction: Any) -> bool:
    if not isinstance(instruction, dict):
        return False
    status = str(instruction.get("status") or "ok").lower()
    if status in {"error", "failed", "blocked"}:
        return False
    return not instruction.get("error")
