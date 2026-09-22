from __future__ import annotations

import hashlib
import time
from typing import Any

from blocks._common import timestamp
from domain.agent.scheduler import Scheduler
from domain.chat.store import ChatStore
from domain.company.runtime_store import CompanyRuntimeStore


DEFAULT_BLOCKER_CHANNEL_ID = "ops-company"
SIGNAL_SOURCE = "mimo_blocker_signal"
TERMINAL_SIGNAL_TASK_STATUSES = {"completed", "done", "cancelled", "canceled", "failed"}
UNANSWERED_CHILD_GRACE_MS = 60_000


def sync_mimo_blocker_signals(
    state: dict[str, Any],
    *,
    company_id: str,
    profile_id: str,
    channel_id: str = DEFAULT_BLOCKER_CHANNEL_ID,
) -> list[dict[str, Any]]:
    """Collect MiMo runtime blocker evidence and mirror it into Company Workspace."""

    runtime_store = CompanyRuntimeStore()
    active_tasks = _active_signal_tasks_by_key(runtime_store, company_id)
    try:
        signals = collect_mimo_blocker_signals(
            state, company_id=company_id, profile_id=profile_id
        )
    except Exception:
        # Missing observations cannot prove that previously blocked work recovered.
        return [_unobserved_signal(task) for task in active_tasks.values()]
    current_keys = {str(signal.get("signal_key") or "") for signal in signals if signal.get("signal_key")}
    synced: list[dict[str, Any]] = []

    for signal in signals:
        signal_key = str(signal.get("signal_key") or "").strip()
        if not signal_key:
            continue
        active_task = active_tasks.get(signal_key)
        metadata = _signal_metadata(signal, company_id=company_id, profile_id=profile_id)
        if active_task:
            task = runtime_store.update_task(
                str(active_task.get("id") or active_task.get("task_id")),
                {
                    "title": signal["title"],
                    "description": signal["description"],
                    "status": "blocked",
                    "priority": "high",
                    "target_agent_ids": list(signal.get("target_agent_ids") or []),
                    "metadata": metadata,
                },
                company_id=company_id,
            ) or active_task
            if not task.get("message_id"):
                message = _create_signal_message(runtime_store, company_id, channel_id, signal, metadata)
                task = runtime_store.update_task(
                    str(task.get("id") or task.get("task_id")),
                    {
                        "channel_id": message.get("channel_id"),
                        "thread_id": message.get("thread_id"),
                        "message_id": message.get("message_id"),
                    },
                    company_id=company_id,
                ) or task
                runtime_store.update_message_tasks(str(message.get("message_id")), [str(task.get("id") or task.get("task_id"))])
        else:
            message = _create_signal_message(runtime_store, company_id, channel_id, signal, metadata)
            task = runtime_store.create_task(
                company_id,
                title=signal["title"],
                description=signal["description"],
                target_agent_ids=list(signal.get("target_agent_ids") or []),
                source=SIGNAL_SOURCE,
                status="blocked",
                priority="high",
                channel_id=str(message.get("channel_id") or channel_id),
                thread_id=str(message.get("thread_id") or ""),
                message_id=str(message.get("message_id") or ""),
                metadata=metadata,
            )
            runtime_store.update_message_tasks(str(message.get("message_id")), [str(task.get("id") or task.get("task_id"))])

        synced.append(
            {
                **signal,
                "task_id": str(task.get("id") or task.get("task_id") or ""),
                "message_id": str(task.get("message_id") or ""),
                "thread_id": str(task.get("thread_id") or _thread_id_for_signal(signal_key)),
            }
        )

    _resolve_absent_signal_tasks(runtime_store, company_id, current_keys)
    return synced


def collect_mimo_blocker_signals(
    state: dict[str, Any],
    *,
    company_id: str,
    profile_id: str,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    signals.extend(_scheduler_failure_signals(state, company_id=company_id, profile_id=profile_id))
    signals.extend(_unanswered_subagent_signals(state, company_id=company_id, profile_id=profile_id))
    return signals


def _scheduler_failure_signals(
    state: dict[str, Any],
    *,
    company_id: str,
    profile_id: str,
) -> list[dict[str, Any]]:
    schedule_ids = state.get("schedule_ids") if isinstance(state.get("schedule_ids"), dict) else {}
    if not schedule_ids:
        return []
    scheduler = Scheduler()
    signals: list[dict[str, Any]] = []
    for loop_key, schedule_id in schedule_ids.items():
        schedule_id = str(schedule_id or "").strip()
        if not schedule_id:
            continue
        schedule = scheduler.get_schedule(schedule_id)
        history = scheduler.get_history(schedule_id, limit=1, offset=0)
        entries = history.get("entries") if isinstance(history, dict) else []
        latest = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else None
        if latest is None:
            continue
        status = str(latest.get("status") or "").strip().lower()
        if status not in {"error", "failed"}:
            continue
        error_text = _clean_preview(latest.get("error") or "scheduler execution failed", limit=1000)
        schedule_name = str((schedule or {}).get("name") or loop_key or schedule_id)
        evidence = {
            "source": "scheduler_history",
            "profile_id": profile_id,
            "company_id": company_id,
            "loop_key": str(loop_key),
            "schedule_id": schedule_id,
            "schedule_name": schedule_name,
            "execution_id": str(latest.get("execution_id") or ""),
            "execution_status": status,
            "error": error_text,
            "started_at": str(latest.get("started_at") or ""),
            "completed_at": str(latest.get("completed_at") or ""),
        }
        title = "Scheduler failure: " + schedule_name
        description = (
            "Latest MiMo schedule execution failed.\n\n"
            "Schedule: " + schedule_name + "\n"
            "Schedule ID: " + schedule_id + "\n"
            "Execution ID: " + evidence["execution_id"] + "\n"
            "Error: " + error_text
        )
        signals.append(
            {
                "signal_key": "scheduler_failure:" + schedule_id,
                "signal_type": "scheduler_failure",
                "severity": "blocker",
                "title": title,
                "description": description,
                "target_agent_ids": ["project_manager", "scheduler"],
                "evidence": evidence,
                "detected_at": timestamp(),
            }
        )
    return signals


def _unanswered_subagent_signals(
    state: dict[str, Any],
    *,
    company_id: str,
    profile_id: str,
) -> list[dict[str, Any]]:
    parent_id = str(state.get("conversation_id") or "").strip()
    if not parent_id:
        return []
    store = ChatStore()
    parent = store.get_conversation(parent_id)
    if not isinstance(parent, dict):
        return []
    children = _child_subagent_conversations(store, parent)
    signals: list[dict[str, Any]] = []
    for child in children:
        verdict = _unanswered_child_verdict(child)
        if verdict is None:
            continue
        child_id = str(child.get("id") or "")
        title_text = str(child.get("title") or child_id or "subagent")
        agent_id = str(child.get("agent_id") or "subagent")
        task_text = _child_task_text(child)
        evidence = {
            "source": "chat_child_conversation",
            "profile_id": profile_id,
            "company_id": company_id,
            "parent_conversation_id": parent_id,
            "child_conversation_id": child_id,
            "child_title": title_text,
            "agent_id": agent_id,
            "latest_user_message_id": verdict["latest_user_message_id"],
            "latest_user_message_preview": verdict["latest_user_message_preview"],
            "latest_user_message_created_at": verdict["latest_user_message_created_at"],
            "message_count": verdict["message_count"],
            "assistant_messages_after_latest_user": 0,
            "task": task_text,
        }
        title = "Unanswered subagent conversation: " + _clean_preview(title_text, limit=96)
        description = (
            "A MiMo subagent child conversation has a user assignment but no assistant reply after it.\n\n"
            "Child conversation: " + child_id + "\n"
            "Agent: " + agent_id + "\n"
            "Assignment: " + (task_text or verdict["latest_user_message_preview"])
        )
        signals.append(
            {
                "signal_key": "subagent_unanswered:" + child_id,
                "signal_type": "subagent_unanswered",
                "severity": "blocker",
                "title": title,
                "description": description,
                "target_agent_ids": _subagent_target_agent_ids(agent_id),
                "evidence": evidence,
                "detected_at": timestamp(),
            }
        )
    return signals


def _child_subagent_conversations(store: ChatStore, parent: dict[str, Any]) -> list[dict[str, Any]]:
    parent_id = str(parent.get("id") or "")
    seen: set[str] = set()
    children: list[dict[str, Any]] = []
    for child_id in parent.get("child_conversation_ids") if isinstance(parent.get("child_conversation_ids"), list) else []:
        child = store.get_conversation(str(child_id))
        if isinstance(child, dict) and str(child.get("conversation_kind") or "") == "subagent":
            seen.add(str(child.get("id") or child_id))
            children.append(child)
    listed, _ = store.list_conversations(
        limit=500,
        offset=0,
        conversation_kind="subagent",
        group_id=parent.get("group_id"),
        include_messages=True,
    )
    for child in listed:
        if not isinstance(child, dict):
            continue
        child_id = str(child.get("id") or "")
        if child_id in seen:
            continue
        if str(child.get("parent_conversation_id") or "") != parent_id:
            continue
        seen.add(child_id)
        children.append(child)
    return children


def _unanswered_child_verdict(child: dict[str, Any]) -> dict[str, Any] | None:
    messages = child.get("messages") if isinstance(child.get("messages"), list) else []
    latest_user_index = -1
    latest_user: dict[str, Any] | None = None
    for index, message in enumerate(messages):
        if isinstance(message, dict) and str(message.get("role") or "").lower() == "user":
            latest_user_index = index
            latest_user = message
    if latest_user is None:
        return None
    for message in messages[latest_user_index + 1 :]:
        if isinstance(message, dict) and str(message.get("role") or "").lower() == "assistant":
            return None
    created_at = latest_user.get("created_at")
    if _message_age_ms(created_at) < UNANSWERED_CHILD_GRACE_MS:
        return None
    return {
        "latest_user_message_id": str(latest_user.get("id") or ""),
        "latest_user_message_preview": _clean_preview(latest_user.get("raw_text") or _extract_text(latest_user.get("content")), limit=1000),
        "latest_user_message_created_at": created_at,
        "message_count": len(messages),
    }


def _message_age_ms(created_at: Any) -> int:
    try:
        value = int(created_at)
    except (TypeError, ValueError):
        return UNANSWERED_CHILD_GRACE_MS
    if value <= 0:
        return UNANSWERED_CHILD_GRACE_MS
    return max(0, int(time.time() * 1000) - value)


def _child_task_text(child: dict[str, Any]) -> str:
    metadata = child.get("metadata") if isinstance(child.get("metadata"), dict) else {}
    subagent = metadata.get("subagent") if isinstance(metadata.get("subagent"), dict) else {}
    return _clean_preview(subagent.get("task") or "", limit=1000)


def _subagent_target_agent_ids(agent_id: str) -> list[str]:
    cleaned = str(agent_id or "").strip()
    targets = ["project_manager"]
    if cleaned and cleaned not in {"subagent", "project_manager"}:
        targets.append(cleaned)
    else:
        targets.append("toolsmith")
    return targets


def _signal_metadata(signal: dict[str, Any], *, company_id: str, profile_id: str) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "company_id": company_id,
        "source": SIGNAL_SOURCE,
        "signal_key": str(signal.get("signal_key") or ""),
        "signal_type": str(signal.get("signal_type") or ""),
        "severity": str(signal.get("severity") or "blocker"),
        "evidence": signal.get("evidence") if isinstance(signal.get("evidence"), dict) else {},
        "detected_at": str(signal.get("detected_at") or timestamp()),
    }


def _create_signal_message(
    runtime_store: CompanyRuntimeStore,
    company_id: str,
    channel_id: str,
    signal: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    signal_key = str(signal.get("signal_key") or "")
    return runtime_store.add_message(
        company_id,
        channel_id=channel_id,
        thread_id=_thread_id_for_signal(signal_key),
        sender_id="mimo_status",
        content=_signal_message_content(signal),
        mentions=list(signal.get("target_agent_ids") or []),
        metadata=metadata,
    )


def _signal_message_content(signal: dict[str, Any]) -> str:
    evidence = signal.get("evidence") if isinstance(signal.get("evidence"), dict) else {}
    lines = [
        "MiMo blocker signal: " + str(signal.get("title") or signal.get("signal_type") or "blocker"),
        "",
        "Evidence:",
    ]
    if signal.get("signal_type") == "scheduler_failure":
        lines.extend(
            [
                "- Signal: scheduler_failure",
                "- Schedule: " + str(evidence.get("schedule_name") or evidence.get("schedule_id") or ""),
                "- Execution: " + str(evidence.get("execution_id") or ""),
                "- Error: " + str(evidence.get("error") or ""),
            ]
        )
    elif signal.get("signal_type") == "subagent_unanswered":
        lines.extend(
            [
                "- Signal: subagent_unanswered",
                "- Parent conversation: " + str(evidence.get("parent_conversation_id") or ""),
                "- Child conversation: " + str(evidence.get("child_conversation_id") or ""),
                "- Latest user message: " + str(evidence.get("latest_user_message_preview") or ""),
            ]
        )
    else:
        lines.append("- Signal: " + str(signal.get("signal_type") or "unknown"))
    lines.extend(["", "Next action: triage this as blocked work in the Company Workspace."])
    return "\n".join(lines).strip()


def _active_signal_tasks_by_key(runtime_store: CompanyRuntimeStore, company_id: str) -> dict[str, dict[str, Any]]:
    tasks, _ = runtime_store.list_tasks(company_id, limit=1000, offset=0)
    by_key: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        if metadata.get("source") != SIGNAL_SOURCE:
            continue
        status = str(task.get("status") or "").strip().lower()
        if status in TERMINAL_SIGNAL_TASK_STATUSES:
            continue
        signal_key = str(metadata.get("signal_key") or "").strip()
        if signal_key and signal_key not in by_key:
            by_key[signal_key] = task
    return by_key


def _unobserved_signal(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    return {
        "signal_key": metadata.get("signal_key"),
        "signal_type": metadata.get("signal_type"),
        "severity": metadata.get("severity", "blocker"),
        "title": task.get("title"),
        "description": str(task.get("description") or "")
        + "\n\nCurrent status is unavailable; this prior blocker is retained.",
        "target_agent_ids": list(task.get("target_agent_ids") or []),
        "evidence": metadata.get("evidence", {}),
        "detected_at": metadata.get("detected_at"),
        "task_id": task.get("id") or task.get("task_id"),
        "message_id": task.get("message_id"),
        "thread_id": task.get("thread_id"),
        "observation_status": "unavailable",
    }


def _resolve_absent_signal_tasks(runtime_store: CompanyRuntimeStore, company_id: str, current_keys: set[str]) -> None:
    active_tasks = _active_signal_tasks_by_key(runtime_store, company_id)
    for signal_key, task in active_tasks.items():
        if signal_key in current_keys:
            continue
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        runtime_store.update_task(
            str(task.get("id") or task.get("task_id")),
            {
                "status": "completed",
                "metadata": {
                    **metadata,
                    "resolved_at": timestamp(),
                    "resolution": "MiMo status no longer reports this blocker signal.",
                },
            },
            company_id=company_id,
        )


def _thread_id_for_signal(signal_key: str) -> str:
    digest = hashlib.sha1(str(signal_key or "mimo-blocker").encode("utf-8")).hexdigest()[:16]
    return "thread_mimo_blocker_" + digest


def _clean_preview(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("text"):
                    parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part)
