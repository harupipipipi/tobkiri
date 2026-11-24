"""Project Company tasks into conversation and global agent job contracts."""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Mapping

CONVERSATION_RESOURCE = "rumi.resource.conversation.v1"
CONVERSATION_ACTION = "rumi.action.conversation.manage.v1"
MESSAGE_ACTION = "rumi.action.message.manage.v1"
JOB_ACTION = "rumi.action.job.v1"


class CompanyAgentAdapter:
    """Project one Company task without importing Company or agent runtimes."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch or cancel a deterministic Company agent job."""

        profile_id = str(payload.get("profile_id") or "default")
        task = _mapping(payload.get("task"))
        company = _mapping(payload.get("company"))
        member = _mapping(payload.get("member"))
        task_id = str(task.get("id") or "")
        company_id = str(company.get("id") or "")
        if not task_id or not company_id:
            raise ValueError("Company and task IDs are required")
        key = f"company:{_hash(company_id + chr(0) + task_id)[:40]}:agent"
        if name == "cancel":
            return self.client.invoke(
                JOB_ACTION,
                "cancel",
                {
                    "profile_id": profile_id,
                    "action_id": "agent.turn",
                    "idempotency_key": key,
                },
            )
        if name != "dispatch":
            raise ValueError(f"unknown Company agent adapter operation: {name}")
        conversation_id = "company-task-" + _hash(f"{company_id}\0{task_id}")[:40]
        conversation = self._conversation(profile_id, conversation_id)
        if conversation is None:
            conversation = self._create_conversation(
                profile_id,
                conversation_id,
                company,
                task,
                member,
            )
        message_id = "company-task-" + _hash(task_id)[:40]
        existing = next(
            (
                item
                for item in conversation.get("messages") or []
                if isinstance(item, Mapping) and item.get("id") == message_id
            ),
            None,
        )
        if existing is None:
            appended = self.client.invoke(
                MESSAGE_ACTION,
                "append",
                {
                    "profile_id": profile_id,
                    "conversation_id": conversation_id,
                    "expected_conversation_revision": conversation[
                        "conversation_revision"
                    ],
                    "message": {
                        "id": message_id,
                        "role": "user",
                        "content": _task_prompt(company, task, member),
                        "metadata": {
                            "source": "company",
                            "company_id": company_id,
                            "company_task_id": task_id,
                            "company_member_id": str(member.get("id") or ""),
                        },
                    },
                },
            )
            revision = int(appended["conversation_revision"])
        else:
            revision = int(conversation["conversation_revision"])
        result = self.client.invoke(
            JOB_ACTION,
            "dispatch",
            {
                "profile_id": profile_id,
                "action_id": "agent.turn",
                "idempotency_key": key,
                "payload": {
                    "agent_profile_id": str(
                        member.get("agent_profile_id") or "default"
                    ),
                    "conversation_id": conversation_id,
                    "conversation_revision": revision,
                    "company_id": company_id,
                    "company_task_id": task_id,
                },
            },
        )
        return {
            "status": "accepted",
            "conversation_id": conversation_id,
            "message_id": message_id,
            "agent": result,
        }

    def _conversation(
        self,
        profile_id: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        value = self.client.invoke(
            CONVERSATION_RESOURCE,
            "get",
            {"profile_id": profile_id, "conversation_id": conversation_id},
        )
        return dict(value) if isinstance(value, Mapping) else None

    def _create_conversation(
        self,
        profile_id: str,
        conversation_id: str,
        company: Mapping[str, Any],
        task: Mapping[str, Any],
        member: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot = self.client.invoke(
            CONVERSATION_RESOURCE,
            "list",
            {"profile_id": profile_id},
        )
        try:
            result = self.client.invoke(
                CONVERSATION_ACTION,
                "create",
                {
                    "profile_id": profile_id,
                    "expected_revision": int(snapshot.get("revision") or 0),
                    "conversation": {
                        "id": conversation_id,
                        "title": str(task.get("title") or "Company task")[:500],
                        "agent_id": str(member.get("agent_profile_id") or "default"),
                        "tags": ["company", "task"],
                        "conversation_kind": "company_task",
                        "group_id": str(company.get("id") or ""),
                        "metadata": {
                            "company_id": str(company.get("id") or ""),
                            "company_task_id": str(task.get("id") or ""),
                            "company_member_id": str(member.get("id") or ""),
                        },
                    },
                },
            )
            return dict(result["conversation"])
        except Exception:
            current = self._conversation(profile_id, conversation_id)
            if current is None:
                raise
            return current


def create_company_work_adapter(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the Company-to-agent work adapter."""

    adapter = CompanyAgentAdapter(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return adapter.invoke(name, payload)

    return operation


def _task_prompt(
    company: Mapping[str, Any],
    task: Mapping[str, Any],
    member: Mapping[str, Any],
) -> str:
    return (
        f"Company: {company.get('name') or company.get('id')}\n"
        f"Assigned member: {member.get('display_name') or member.get('id')}\n"
        f"Task: {task.get('title') or task.get('id')}\n\n"
        f"{task.get('description') or ''}"
    )[:100_000]


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

