"""
domain/agent/scheduler.py - In-memory scheduler engine

Manages scheduled agent executions using threading.Timer.
Supports three schedule types:
  - interval: execute every N seconds/minutes/hours
  - cron: execute according to a cron expression (min hour dom month dow)
  - once: execute at a specific datetime then auto-disable

No external dependencies. Pure stdlib.
"""

import sys
import os
import hashlib
import json
import threading
import time
import math
import re
from itertools import count
from typing import Any
from datetime import datetime, timezone, timedelta

from tobkiri_protocol.settings_state import SettingsOwnerPort

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import gen_id, timestamp
from domain.agent.schedule_store import (
    current_schedules_dir,
    save_schedule,
    load_schedule,
    load_all_schedules,
    delete_schedule as store_delete,
    append_history,
    load_history,
)
from domain.tool.scheduled_approval import (
    approve_schedule_pending_approval,
    obsolete_superseded_scheduled_approvals,
)


_APPROVAL_REQUIRED_FINISH_REASONS = {"approval_required", "authority_approval_required"}
_SCHEDULE_AUTO_APPROVAL_DEFAULT_FOLLOWUPS = 3
_SCHEDULE_AUTO_APPROVAL_MAX_FOLLOWUPS = 64
_SCHEDULE_AUTO_APPROVAL_UNLIMITED_VALUES = {"none", "null", "unlimited", "infinite", "infinity"}
_SCHEDULE_TASK_DEFAULT_TIMEOUT_SECONDS = 300.0
_SCHEDULE_ONCE_ALREADY_RUNNING_RETRY_SECONDS = 30.0
_SCHEDULED_CONVERSATION_LOCK_WAIT_SECONDS = 1.0
_SCHEDULED_CONVERSATION_LOCK_MAX_WAIT_SECONDS = 5.0
_SCHEDULED_CHAT_ERROR_TEXT_LIMIT = 700
_SCHEDULED_CHAT_ERROR_USER_COMMIT_WAIT_SECONDS = 0.5
_SCHEDULE_AI_REQUEST_TIMEOUT_RESERVE_SECONDS = 5.0
_SCHEDULE_AI_REQUEST_TIMEOUT_MIN_SECONDS = 2.0
_SCHEDULE_AI_REQUEST_TIMEOUT_MAX_SECONDS = 3600.0
_SCHEDULED_CHAT_SECRET_VALUE_RE = re.compile(
    r"\b(AIza[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z_-]{20,}|gh[pousr]_[0-9A-Za-z_]{20,})\b"
)


class _SchedulerTaskTimedOut(TimeoutError):
    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        super().__init__(_scheduler_timeout_error(timeout_seconds))


class _SchedulerConversationBusy(RuntimeError):
    def __init__(self, conversation_id: str):
        self.conversation_id = conversation_id
        super().__init__("conversation is already running: " + conversation_id)


def _format_timeout_seconds(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _scheduler_timeout_error(timeout_seconds: float) -> str:
    return (
        "scheduled task timed out after "
        + _format_timeout_seconds(timeout_seconds)
        + " seconds"
    )


def _task_timeout_seconds(raw_value: Any) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return _SCHEDULE_TASK_DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        return _SCHEDULE_TASK_DEFAULT_TIMEOUT_SECONDS
    return value


def _wait_timeout_seconds(value: float) -> float:
    max_timeout = getattr(threading, "TIMEOUT_MAX", value)
    return max(0.0, min(float(value), float(max_timeout)))


def _remaining_timeout_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _scheduler_request_timeout_for_execution_timeout(timeout_seconds: Any) -> float | None:
    try:
        value = float(timeout_seconds)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return max(
        _SCHEDULE_AI_REQUEST_TIMEOUT_MIN_SECONDS,
        min(
            _SCHEDULE_AI_REQUEST_TIMEOUT_MAX_SECONDS,
            value - _SCHEDULE_AI_REQUEST_TIMEOUT_RESERVE_SECONDS,
        ),
    )


def _apply_scheduler_execution_timeout_to_params(
    params: dict[str, Any],
    timeout_seconds: Any,
) -> dict[str, Any]:
    if "request_timeout" in params or "timeout" in params:
        return params
    request_timeout = _scheduler_request_timeout_for_execution_timeout(timeout_seconds)
    if request_timeout is not None:
        params["request_timeout"] = request_timeout
    return params


def _scheduled_conversation_lock_wait_seconds(task_cfg: dict[str, Any], remaining_timeout: float) -> float:
    policy = task_cfg.get("tool_policy") if isinstance(task_cfg.get("tool_policy"), dict) else {}
    raw_value = policy.get("schedule_conversation_lock_wait_seconds", _SCHEDULED_CONVERSATION_LOCK_WAIT_SECONDS)
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = _SCHEDULED_CONVERSATION_LOCK_WAIT_SECONDS
    if not math.isfinite(value) or value < 0:
        value = _SCHEDULED_CONVERSATION_LOCK_WAIT_SECONDS
    return max(0.0, min(value, _SCHEDULED_CONVERSATION_LOCK_MAX_WAIT_SECONDS, remaining_timeout))


def _retry_once_after_running_skip() -> str:
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=_SCHEDULE_ONCE_ALREADY_RUNNING_RETRY_SECONDS)
    return retry_at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_with_timeout(call, timeout_seconds: float, *, task_timeout_seconds: float, cancel_event=None):
    if timeout_seconds <= 0:
        if cancel_event is not None:
            cancel_event.set()
        raise _SchedulerTaskTimedOut(task_timeout_seconds)

    done = threading.Event()
    outcome: dict[str, Any] = {}

    def target():
        try:
            outcome["result"] = call()
        except BaseException as exc:
            outcome["exception"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=target, name="scheduler-task-runner", daemon=True)
    worker.start()
    if not done.wait(_wait_timeout_seconds(timeout_seconds)):
        if cancel_event is not None:
            cancel_event.set()
        raise _SchedulerTaskTimedOut(task_timeout_seconds)

    exc = outcome.get("exception")
    if exc is not None:
        if isinstance(exc, Exception):
            raise exc
        raise RuntimeError(str(exc))
    return outcome.get("result")


def _chat_result_data(result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("status") != "ok":
        return {}
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _chat_result_finish_reason(result: dict[str, Any] | None) -> str:
    data = _chat_result_data(result)
    return str(data.get("finish_reason") or "").strip()


def _chat_result_content(result: dict[str, Any] | None) -> str:
    data = _chat_result_data(result)
    if not data:
        return ""
    content = data.get("content", data.get("text", ""))
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("text")
        )
    if isinstance(content, str):
        return content
    return str(content)


def _chat_result_message_id(result: dict[str, Any] | None) -> str:
    data = _chat_result_data(result)
    return str(data.get("id") or "").strip() if data else ""


def _redact_scheduler_error_text(value: Any) -> str:
    text = str(value or "scheduled chat failed")
    text = _SCHEDULED_CHAT_SECRET_VALUE_RE.sub("[redacted]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|authorization|bearer|credential|password|secret|token)(\s*[:=]\s*)[^\s,}]+",
        r"\1\2[redacted]",
        text,
    )
    text = text.strip() or "scheduled chat failed"
    if len(text) > _SCHEDULED_CHAT_ERROR_TEXT_LIMIT:
        text = text[:_SCHEDULED_CHAT_ERROR_TEXT_LIMIT].rstrip() + "... (truncated)"
    return text


def _scheduled_chat_message_has_visible_text(message: dict[str, Any]) -> bool:
    raw_text = message.get("raw_text")
    if isinstance(raw_text, str) and raw_text.strip():
        return True
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    return True
            elif isinstance(block, str) and block.strip():
                return True
    return False


def _scheduled_chat_message_text(message: dict[str, Any]) -> str:
    raw_text = message.get("raw_text")
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            elif isinstance(block, str) and block.strip():
                parts.append(block.strip())
        return "\n".join(parts).strip()
    return ""


def _scheduled_chat_assistant_is_terminal(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    if metadata.get("durable_scheduler_error") is True:
        return True
    finish_reason = str(message.get("finish_reason") or "").strip().lower()
    if finish_reason in {"streaming", "in_progress", "pending"}:
        return False
    thinking = metadata.get("thinking") if isinstance(metadata.get("thinking"), dict) else {}
    thinking_state = str(thinking.get("state") or "").strip().lower()
    if thinking_state in {"running", "streaming", "thinking"}:
        return False
    if finish_reason in {
        "stop",
        "error",
        "cancelled",
        "canceled",
        "approval_required",
        "tool_calls",
        "length",
        "ai_error",
        "ai_error_after_tool_use",
    }:
        return True
    return _scheduled_chat_message_has_visible_text(message)


def _ensure_scheduled_chat_error_message(
    *,
    conversation_id: str,
    schedule_id: str,
    exec_id: str,
    task_cfg: dict[str, Any],
    trigger: str,
    error_text: Any,
) -> dict[str, Any] | None:
    conversation_id = str(conversation_id or "").strip()
    schedule_id = str(schedule_id or "").strip()
    exec_id = str(exec_id or "").strip()
    if not conversation_id or not schedule_id or not exec_id:
        return None
    try:
        from domain.chat.store import ChatStore
    except Exception:
        return None

    scheduled_user: dict[str, Any] | None = None
    store = ChatStore()
    deadline = time.monotonic() + _SCHEDULED_CHAT_ERROR_USER_COMMIT_WAIT_SECONDS
    while True:
        conversation = store.get_conversation(conversation_id)
        if isinstance(conversation, dict):
            messages = conversation.get("messages")
            if isinstance(messages, list):
                for message in reversed(messages):
                    if not isinstance(message, dict) or str(message.get("role") or "") != "user":
                        continue
                    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
                    source = str(metadata.get("source") or "").strip()
                    if source not in {"scheduler", "scheduler_approval_followup"}:
                        continue
                    if str(metadata.get("schedule_id") or "").strip() != schedule_id:
                        continue
                    if str(metadata.get("schedule_execution_id") or "").strip() != exec_id:
                        continue
                    scheduled_user = message
                    break
        if isinstance(scheduled_user, dict):
            break
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)
    if not isinstance(scheduled_user, dict):
        return None

    parent_id = str(scheduled_user.get("id") or "").strip()
    if not parent_id:
        return None
    for message in messages:
        if (
            isinstance(message, dict)
            and str(message.get("role") or "") == "assistant"
            and str(message.get("parent_id") or "").strip() == parent_id
            and _scheduled_chat_assistant_is_terminal(message)
        ):
            return message

    safe_error = _redact_scheduler_error_text(error_text)
    content_text = (
        "Scheduled chat did not complete. "
        + safe_error
        + "\n\nThe failure was saved so this scheduled conversation does not remain user-only."
    )
    try:
        sequence_number = int(scheduled_user.get("sequence_number") or len(messages) or 1) + 1
    except (TypeError, ValueError):
        sequence_number = len(messages) + 1
    model = str(task_cfg.get("model") or "").strip() or str(conversation.get("model") or "stub/default")
    metadata = {
        "model": model,
        "source": "scheduler",
        "schedule_id": schedule_id,
        "schedule_execution_id": exec_id,
        "trigger": trigger,
        "profile_id": task_cfg.get("profile_id"),
        "agent_id": task_cfg.get("agent_id"),
        "status": "error",
        "error_code": "SCHEDULED_CHAT_EXECUTION_FAILED",
        "durable_scheduler_error": True,
        "provider_invocation_started": False,
        "thinking": {"state": "failed"},
    }
    task_metadata = task_cfg.get("metadata") if isinstance(task_cfg.get("metadata"), dict) else {}
    if task_metadata.get("company_id"):
        metadata["company_id"] = task_metadata.get("company_id")
    return store.add_message(
        conversation_id,
        {
            "role": "assistant",
            "parent_id": parent_id,
            "sequence_number": sequence_number,
            "content": [{"type": "text", "text": content_text}],
            "raw_text": content_text,
            "finish_reason": "error",
            "usage": {},
            "widget": None,
            "metadata": metadata,
            "events": [
                {
                    "type": "task_failed",
                    "message": safe_error,
                    "terminal": True,
                    "source": "scheduler",
                    "schedule_id": schedule_id,
                    "schedule_execution_id": exec_id,
                }
            ],
            "tool_logs": [],
            "model": model,
        },
    )


def _pending_approval_from_chat_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    data = _chat_result_data(result)
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    for pending in (
        metadata.get("pending_approval"),
        metadata.get("pendingApproval"),
        metadata.get("pendingAuthorityApproval"),
        data.get("pending_approval"),
        data.get("pendingApproval"),
        data.get("pendingAuthorityApproval"),
    ):
        if isinstance(pending, dict):
            return pending
    events = data.get("events") if isinstance(data.get("events"), list) else []
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != "approval_requested":
            continue
        event_data = event.get("data")
        if isinstance(event_data, dict):
            return event_data
        details = event.get("details")
        if isinstance(details, dict):
            return details
        if event.get("approval_required") or event.get("requires_approval"):
            return event
    return None


def _scheduler_trigger_name(manual: bool) -> str:
    return "manual" if manual else "scheduled"


def _scheduler_chat_payload(
    *,
    conversation_id: str,
    content: str,
    task_cfg: dict[str, Any],
    schedule_id: str,
    exec_id: str,
    trigger: str,
    params: dict[str, Any],
    tools: list[Any] | None,
    parent_id: str | None = None,
    metadata_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = task_cfg.get("metadata") if isinstance(task_cfg.get("metadata"), dict) else {}
    message_metadata = {
        **metadata,
        "source": "scheduler",
        "schedule_id": schedule_id,
        "schedule_execution_id": exec_id,
        "trigger": trigger,
        "profile_id": task_cfg.get("profile_id"),
        "agent_id": task_cfg.get("agent_id"),
    }
    if isinstance(metadata_extra, dict):
        message_metadata.update(metadata_extra)
    task_message = str(task_cfg.get("message") or "").strip()
    if task_message:
        message_metadata.setdefault("scheduled_task_message", task_message)
    message = {
        "role": "user",
        "content": content,
        "metadata": message_metadata,
    }
    if parent_id is not None:
        message["parent_id"] = parent_id
    return {
        "conversation_id": conversation_id,
        "message": message,
        "params": dict(params),
        "tools": tools,
    }


def _current_conversation_node_id(conversation_id: str) -> str | None:
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return None
    try:
        from domain.chat.store import ChatStore

        conversation = ChatStore().get_conversation(conversation_id)
    except Exception:
        return None
    if not isinstance(conversation, dict):
        return None
    current_id = str(conversation.get("current_node_id") or "").strip()
    return current_id or None


def _schedule_auto_approval_limit(task_cfg: dict[str, Any]) -> int | None:
    policy = task_cfg.get("tool_policy") if isinstance(task_cfg.get("tool_policy"), dict) else {}
    if "schedule_auto_approve_max_followups" not in policy:
        raw_value: Any = _SCHEDULE_AUTO_APPROVAL_DEFAULT_FOLLOWUPS
    else:
        raw_value = policy.get("schedule_auto_approve_max_followups")
        if raw_value is None:
            return None
    if isinstance(raw_value, str):
        text = raw_value.strip().lower()
        if text in _SCHEDULE_AUTO_APPROVAL_UNLIMITED_VALUES:
            return None
        if not text:
            raw_value = _SCHEDULE_AUTO_APPROVAL_DEFAULT_FOLLOWUPS
    try:
        value = int(raw_value)
    except Exception:
        value = _SCHEDULE_AUTO_APPROVAL_DEFAULT_FOLLOWUPS
    return max(0, min(value, _SCHEDULE_AUTO_APPROVAL_MAX_FOLLOWUPS))


def _schedule_auto_approval_attempts(task_cfg: dict[str, Any]):
    limit = _schedule_auto_approval_limit(task_cfg)
    if limit is None:
        return count()
    return range(limit)


def _initial_tool_choice(task_cfg: dict[str, Any]) -> Any:
    policy = task_cfg.get("tool_policy") if isinstance(task_cfg.get("tool_policy"), dict) else {}
    value = policy.get("schedule_initial_tool_choice")
    if isinstance(value, dict):
        return value
    if str(value or "").strip().lower() in {"auto", "none", "required"}:
        return str(value).strip().lower()
    return None


def _followup_params(params: dict[str, Any]) -> dict[str, Any]:
    followup = dict(params)
    followup.pop("tool_choice", None)
    return followup


def _scheduled_approval_followup_content(
    *,
    task_cfg: dict[str, Any],
    approved: dict[str, Any],
) -> str:
    summary = approved.get("summary") if isinstance(approved.get("summary"), dict) else {}
    task_message = str(task_cfg.get("message") or "").strip()
    operation = str(summary.get("operation") or "").strip()
    tool_name = str(summary.get("tool_name") or "").strip()
    lines = ["Continue this approved scheduled task."]
    if task_message:
        lines.extend(["", "Scheduled task:", task_message])
    if tool_name or operation:
        lines.extend(
            [
                "",
                "Approved tool request:",
                " / ".join(part for part in (tool_name, operation) if part),
            ]
        )
    lines.extend(
        [
            "",
            "Use the approved tool result to continue only this scheduled task and summarize what happened.",
        ]
    )
    return "\n".join(lines)


def _scheduler_chat_params_and_tools(
    task_cfg: dict[str, Any],
    *,
    timeout_seconds: Any = None,
) -> tuple[dict[str, Any], list[Any] | None]:
    params: dict[str, Any] = {}
    if task_cfg.get("model"):
        params["model"] = task_cfg.get("model")
    if isinstance(task_cfg.get("tool_policy"), dict):
        params["tool_policy"] = task_cfg["tool_policy"]
    if task_cfg.get("thinking_level"):
        params["thinking_level"] = task_cfg.get("thinking_level")
    tools = task_cfg.get("tools") if isinstance(task_cfg.get("tools"), list) else None
    if tools and "tool_choice" not in params:
        initial_tool_choice = _initial_tool_choice(task_cfg)
        if initial_tool_choice is not None:
            params["tool_choice"] = initial_tool_choice
    _apply_scheduler_execution_timeout_to_params(params, timeout_seconds)
    return params, tools


def _scheduler_chat_context(task_cfg: dict[str, Any], *, cancel_event=None) -> dict[str, Any]:
    policy = task_cfg.get("tool_policy") if isinstance(task_cfg.get("tool_policy"), dict) else {}
    context: dict[str, Any] = {"profile_policy": policy}
    if cancel_event is not None:
        context["is_cancelled"] = cancel_event.is_set
    metadata = task_cfg.get("metadata") if isinstance(task_cfg.get("metadata"), dict) else {}
    profile_id = str(task_cfg.get("profile_id") or policy.get("profile_id") or metadata.get("profile_id") or "").strip()
    if profile_id:
        context["profile_id"] = profile_id
        context["authority_principal_id"] = "profile:" + profile_id
        context["principal_id"] = "profile:" + profile_id
    company_id = str(metadata.get("company_id") or "").strip()
    if profile_id == "defaultspack.mimo_coding_company" and company_id == "mimo-coding-company":
        context["owner_pack"] = "defaultspack"
        context["source"] = "scheduler"
    return context


def _resume_scheduled_chat_approvals(
    *,
    result: dict[str, Any],
    send_chat,
    conversation_id: str,
    task_cfg: dict[str, Any],
    schedule_id: str,
    exec_id: str,
    trigger: str,
    params: dict[str, Any],
    tools: list[Any] | None,
    cancel_event=None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    auto_approvals: list[dict[str, Any]] = []
    for _idx in _schedule_auto_approval_attempts(task_cfg):
        if cancel_event is not None and cancel_event.is_set():
            break
        if _chat_result_finish_reason(result) not in _APPROVAL_REQUIRED_FINISH_REASONS:
            break
        pending = _pending_approval_from_chat_result(result)
        if not isinstance(pending, dict):
            break
        approved = approve_schedule_pending_approval(task_cfg, pending, conversation_id=conversation_id)
        if not approved:
            break
        if cancel_event is not None and cancel_event.is_set():
            break
        auto_approvals.append(approved["summary"])
        followup_parent_id = _chat_result_message_id(result) or None
        result = send_chat(
            _scheduler_chat_payload(
                conversation_id=conversation_id,
                content=_scheduled_approval_followup_content(
                    task_cfg=task_cfg,
                    approved=approved,
                ),
                task_cfg=task_cfg,
                schedule_id=schedule_id,
                exec_id=exec_id,
                trigger=trigger,
                params=_followup_params(params),
                tools=tools,
                parent_id=followup_parent_id,
                metadata_extra={
                    "source": "scheduler_approval_followup",
                    "approval_followup": approved["followup"],
                    "scheduled_task_message": str(task_cfg.get("message") or ""),
                    "scheduled_task_model": str(task_cfg.get("model") or ""),
                    "scheduled_task_agent_id": str(task_cfg.get("agent_id") or ""),
                },
            ),
            _scheduler_chat_context(task_cfg, cancel_event=cancel_event),
        )
    current_request_ids: set[str] = set()
    if _chat_result_finish_reason(result) in _APPROVAL_REQUIRED_FINISH_REASONS:
        pending = _pending_approval_from_chat_result(result)
        if isinstance(pending, dict):
            request_id = str(pending.get("approval_request_id") or pending.get("request_id") or "").strip()
            if request_id:
                current_request_ids.add(request_id)
    obsolete_superseded_scheduled_approvals([conversation_id], current_request_ids)
    return result, auto_approvals


def _current_scheduled_approval_result(
    conversation: dict[str, Any] | None,
    *,
    schedule_id: str,
) -> dict[str, Any] | None:
    if not isinstance(conversation, dict):
        return None
    messages = conversation.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    messages_by_id = {
        str(message.get("id") or ""): message
        for message in messages
        if isinstance(message, dict) and str(message.get("id") or "").strip()
    }
    current_id = str(conversation.get("current_node_id") or "").strip()
    current = messages_by_id.get(current_id)
    if not isinstance(current, dict):
        return None
    if str(current.get("role") or "") != "assistant":
        return None
    if str(current.get("finish_reason") or "").strip() not in _APPROVAL_REQUIRED_FINISH_REASONS:
        return None

    result = {"status": "ok", "data": current}
    pending = _pending_approval_from_chat_result(result)
    if not isinstance(pending, dict):
        return None
    request_id = str(pending.get("approval_request_id") or pending.get("request_id") or "").strip()
    if request_id and _has_scheduled_approval_followup_child(
        messages,
        parent_id=str(current.get("id") or "").strip(),
        request_id=request_id,
        schedule_id=schedule_id,
    ):
        return None

    parent_id = str(current.get("parent_id") or "").strip()
    parent = messages_by_id.get(parent_id)
    if not isinstance(parent, dict) or str(parent.get("role") or "") != "user":
        return None
    source_metadata = parent.get("metadata") if isinstance(parent.get("metadata"), dict) else {}
    source = str(source_metadata.get("source") or "").strip()
    if source not in {"scheduler", "scheduler_approval_followup"}:
        return None
    if str(source_metadata.get("schedule_id") or "").strip() != str(schedule_id or "").strip():
        return None
    return {"result": result, "source_metadata": dict(source_metadata)}


def _has_scheduled_approval_followup_child(
    messages: list[Any],
    *,
    parent_id: str,
    request_id: str,
    schedule_id: str,
) -> bool:
    if not parent_id or not request_id:
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("parent_id") or "").strip() != parent_id:
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if str(metadata.get("source") or "").strip() != "scheduler_approval_followup":
            continue
        if str(metadata.get("schedule_id") or "").strip() != str(schedule_id or "").strip():
            continue
        followup = metadata.get("approval_followup") if isinstance(metadata.get("approval_followup"), dict) else {}
        followup_request_id = str(
            followup.get("request_id")
            or followup.get("approval_request_id")
            or ""
        ).strip()
        if followup_request_id == request_id:
            return True
    return False


# ---------------------------------------------------------------------------
# Minimal cron expression parser (5-field: minute hour day month weekday)
# ---------------------------------------------------------------------------

def _parse_cron_field(field, min_val, max_val):
    """Parse a single cron field into a set of integers.

    Supports: *, N, N-M, N-M/S, */S, N,M,O
    """
    result = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        # Handle */N
        if part.startswith("*/"):
            step_str = part[2:]
            if not step_str.isdigit():
                raise ValueError("invalid cron step: " + part)
            step = int(step_str)
            if step < 1:
                raise ValueError("cron step must be >= 1")
            for v in range(min_val, max_val + 1, step):
                result.add(v)
            continue
        # Handle *
        if part == "*":
            for v in range(min_val, max_val + 1):
                result.add(v)
            continue
        # Handle N-M or N-M/S
        range_match = re.match(r"^(\d+)-(\d+)(?:/(\d+))?$", part)
        if range_match:
            lo = int(range_match.group(1))
            hi = int(range_match.group(2))
            step = int(range_match.group(3)) if range_match.group(3) else 1
            if lo < min_val or hi > max_val or lo > hi or step < 1:
                raise ValueError("invalid cron range: " + part)
            for v in range(lo, hi + 1, step):
                result.add(v)
            continue
        # Handle plain number
        if part.isdigit():
            v = int(part)
            if v < min_val or v > max_val:
                raise ValueError("cron value out of range: " + part)
            result.add(v)
            continue
        raise ValueError("invalid cron token: " + part)
    return result


def parse_cron_expression(expr):
    """Parse a 5-field cron expression. Returns dict with sets for each field.

    Fields: minute(0-59) hour(0-23) day(1-31) month(1-12) weekday(0-6, 0=Sun)
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError("cron expression must have exactly 5 fields, got " + str(len(parts)))
    return {
        "minute": _parse_cron_field(parts[0], 0, 59),
        "hour": _parse_cron_field(parts[1], 0, 23),
        "day": _parse_cron_field(parts[2], 1, 31),
        "month": _parse_cron_field(parts[3], 1, 12),
        "weekday": _parse_cron_field(parts[4], 0, 6),
    }


def cron_matches(parsed_cron, dt):
    """Check if a datetime matches a parsed cron expression."""
    # weekday: Python Monday=0 ... Sunday=6; cron Sunday=0
    cron_dow = (dt.weekday() + 1) % 7  # Convert Python weekday to cron weekday
    return (
        dt.minute in parsed_cron["minute"]
        and dt.hour in parsed_cron["hour"]
        and dt.day in parsed_cron["day"]
        and dt.month in parsed_cron["month"]
        and cron_dow in parsed_cron["weekday"]
    )


def next_cron_time(parsed_cron, from_dt):
    """Find the next datetime after from_dt that matches the cron expression.

    Searches up to 366 days ahead. Returns a datetime or None.
    """
    # Start from the next minute
    candidate = from_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = from_dt + timedelta(days=366)
    while candidate < limit:
        if cron_matches(parsed_cron, candidate):
            return candidate
        candidate += timedelta(minutes=1)
        # Optimisation: if current hour is not in cron hours, skip to next hour
        cron_dow = (candidate.weekday() + 1) % 7
        if candidate.month not in parsed_cron["month"]:
            # Skip to first day of next month
            if candidate.month == 12:
                candidate = candidate.replace(year=candidate.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                candidate = candidate.replace(month=candidate.month + 1, day=1, hour=0, minute=0)
            continue
        if candidate.day not in parsed_cron["day"] or cron_dow not in parsed_cron["weekday"]:
            # Skip to next day
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if candidate.hour not in parsed_cron["hour"]:
            # Skip to next hour
            candidate = (candidate + timedelta(hours=1)).replace(minute=0)
            continue
    return None


def _interval_to_seconds(value, unit):
    """Convert an interval value+unit to seconds."""
    multipliers = {"seconds": 1, "minutes": 60, "hours": 3600}
    if unit not in multipliers:
        raise ValueError("unit must be one of: seconds, minutes, hours")
    return value * multipliers[unit]


def _parse_iso_datetime(dt_str):
    """Parse an ISO 8601 datetime string to a UTC datetime object."""
    # Handle Z suffix
    s = dt_str.replace("Z", "+00:00")
    # Python 3.7+ fromisoformat handles timezone offsets
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        # Fallback: try strptime for common formats
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(dt_str.rstrip("Z"), fmt.rstrip("%z"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
        raise ValueError("cannot parse datetime: " + dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fingerprint_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _fingerprint_json_value(item)
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_fingerprint_json_value(item) for item in value]
    if isinstance(value, set):
        return [_fingerprint_json_value(item) for item in sorted(value, key=repr)]
    return str(value)


def _schedule_execution_input_fingerprint(sched: dict[str, Any]) -> str:
    task = sched.get("task") if isinstance(sched.get("task"), dict) else {}
    payload = {"task": _fingerprint_json_value(task)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _running_execution_details(sched: dict[str, Any]) -> dict[str, Any] | None:
    running = sched.get("running_execution")
    if isinstance(running, dict):
        return running
    return None


def _running_execution_started_at(sched: dict[str, Any]) -> tuple[str | None, datetime | None]:
    running = _running_execution_details(sched) or {}
    for raw_value in (
        running.get("started_at"),
        sched.get("running_started_at"),
        running.get("created_at"),
        sched.get("updated_at"),
    ):
        if raw_value is None:
            continue
        raw_text = str(raw_value).strip()
        if not raw_text:
            continue
        try:
            return raw_text, _parse_iso_datetime(raw_text)
        except ValueError:
            continue
    return None, None


def _running_execution_has_parseable_start_marker(sched: dict[str, Any]) -> bool:
    _started_at, started_dt = _running_execution_started_at(sched)
    return started_dt is not None


def _running_execution_timeout_seconds(sched: dict[str, Any]) -> float:
    running = _running_execution_details(sched) or {}
    task_cfg = sched.get("task") if isinstance(sched.get("task"), dict) else {}
    if running.get("timeout_seconds") is not None:
        return _task_timeout_seconds(running.get("timeout_seconds"))
    return _task_timeout_seconds(task_cfg.get("timeout", 300))


def _running_execution_trigger(sched: dict[str, Any]) -> str:
    running = _running_execution_details(sched) or {}
    trigger = str(running.get("trigger") or "").strip()
    if trigger in {"manual", "scheduled"}:
        return trigger
    return "scheduled"


def _stale_running_execution(sched: dict[str, Any], *, now_dt: datetime | None = None) -> dict[str, Any] | None:
    running = _running_execution_details(sched)
    if running is None:
        return None
    started_at, started_dt = _running_execution_started_at(sched)
    if started_dt is None:
        return None
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    timeout_seconds = _running_execution_timeout_seconds(sched)
    if (now_dt - started_dt).total_seconds() < timeout_seconds:
        return None
    execution_id = str(running.get("execution_id") or "").strip()
    if not execution_id:
        execution_id = "sexec_recovered_" + gen_id()
    return {
        "execution_id": execution_id,
        "started_at": started_at or started_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trigger": _running_execution_trigger(sched),
        "timeout_seconds": timeout_seconds,
    }


def _scheduled_user_message_for_running_execution(
    sched: dict[str, Any],
    running: dict[str, Any],
) -> dict[str, Any] | None:
    task_cfg = sched.get("task") if isinstance(sched.get("task"), dict) else {}
    conversation_id = str(task_cfg.get("conversation_id") or "").strip()
    schedule_id = str(sched.get("id") or running.get("schedule_id") or "").strip()
    execution_id = str(running.get("execution_id") or "").strip()
    if not conversation_id or not schedule_id or not execution_id:
        return None
    try:
        from domain.chat.store import ChatStore
    except Exception:
        return None

    try:
        conversation = ChatStore().get_conversation(conversation_id)
    except Exception:
        return None
    if not isinstance(conversation, dict):
        return None
    messages = conversation.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or str(message.get("role") or "") != "user":
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if str(metadata.get("schedule_id") or "").strip() != schedule_id:
            continue
        if str(metadata.get("schedule_execution_id") or "").strip() != execution_id:
            continue
        source = str(metadata.get("source") or "").strip()
        if source != "scheduler":
            continue
        return message
    return None


def _obsolete_running_execution(sched: dict[str, Any]) -> dict[str, Any] | None:
    running = _running_execution_details(sched)
    if running is None:
        return None

    reason = str(running.get("obsolete_reason") or "").strip()
    running_fingerprint = str(
        running.get("input_fingerprint")
        or running.get("task_fingerprint")
        or ""
    ).strip()
    current_fingerprint = _schedule_execution_input_fingerprint(sched)
    if not reason and running_fingerprint and running_fingerprint != current_fingerprint:
        reason = "execution_input_changed"

    message_id = ""
    if not reason and not _running_execution_has_parseable_start_marker(sched):
        task_cfg = sched.get("task") if isinstance(sched.get("task"), dict) else {}
        current_message = str(task_cfg.get("message") or "").strip()
        scheduled_user = _scheduled_user_message_for_running_execution(sched, running)
        if isinstance(scheduled_user, dict):
            previous_message = _scheduled_chat_message_text(scheduled_user)
            if previous_message and current_message and previous_message != current_message:
                reason = "execution_input_message_changed"
                message_id = str(scheduled_user.get("id") or "").strip()

    if not reason:
        return None

    started_at, started_dt = _running_execution_started_at(sched)
    execution_id = str(running.get("execution_id") or "").strip()
    if not execution_id:
        execution_id = "sexec_recovered_" + gen_id()
    details = {
        "execution_id": execution_id,
        "started_at": started_at or (started_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if started_dt else timestamp()),
        "trigger": _running_execution_trigger(sched),
        "timeout_seconds": _running_execution_timeout_seconds(sched),
        "obsolete_reason": reason,
        "input_fingerprint": running_fingerprint,
        "current_input_fingerprint": current_fingerprint,
    }
    if message_id:
        details["scheduled_user_message_id"] = message_id
    return details


def _recoverable_running_execution(sched: dict[str, Any]) -> dict[str, Any] | None:
    obsolete = _obsolete_running_execution(sched)
    if obsolete is not None:
        obsolete["recovery_kind"] = "obsolete"
        return obsolete
    stale = _stale_running_execution(sched)
    if stale is not None:
        stale["recovery_kind"] = "stale"
        return stale
    return None


def _active_running_execution(sched: dict[str, Any]) -> dict[str, Any] | None:
    running = _running_execution_details(sched)
    if running is None:
        return None
    if _recoverable_running_execution(sched) is not None:
        return None
    started_at, _started_dt = _running_execution_started_at(sched)
    execution_id = str(running.get("execution_id") or "").strip()
    return {
        "execution_id": execution_id,
        "schedule_id": str(running.get("schedule_id") or sched.get("id") or "").strip(),
        "started_at": started_at,
        "trigger": _running_execution_trigger(sched),
        "timeout_seconds": _running_execution_timeout_seconds(sched),
    }


# ---------------------------------------------------------------------------
# Scheduler singleton
# ---------------------------------------------------------------------------

class Scheduler:
    """In-memory scheduler backed by threading.Timer.

    Each schedule has a corresponding Timer that fires at the next execution time.
    When the timer fires, the task is executed and the timer is re-armed for the
    next occurrence (unless the schedule type is 'once').
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, *, settings_owner: SettingsOwnerPort | None = None):
        del settings_owner
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialised = False
            return cls._instance

    def __init__(self, *, settings_owner: SettingsOwnerPort | None = None):
        if self._initialised:
            if settings_owner is not None:
                self._settings_owner = settings_owner
            return
        self._initialised = True
        self._settings_owner = settings_owner
        self._lock = threading.Lock()
        self._timers = {}        # schedule_id -> threading.Timer
        self._schedules = {}     # schedule_id -> schedule dict (in-memory cache)
        self._conversation_locks = {}  # conversation_id -> threading.Lock
        self._conversation_lock_holders = {}  # conversation_id -> in-process holder metadata
        self._active_execution_ids = set()
        self._stale_recovered_execution_ids = set()
        self._loaded = False
        self._loaded_schedules_dir = None

    # ---- public API ----

    def ensure_loaded(self):
        """Load schedules once and keep active schedule timers armed."""
        should_load = False
        timers_to_cancel = []
        schedules_dir = current_schedules_dir()
        with self._lock:
            if self._loaded_schedules_dir != schedules_dir:
                timers_to_cancel = list(self._timers.values())
                self._timers.clear()
                self._schedules.clear()
                self._stale_recovered_execution_ids.clear()
                self._conversation_lock_holders.clear()
                self._loaded = False
                self._loaded_schedules_dir = schedules_dir
            if not self._loaded:
                self._loaded = True
                should_load = True
        for timer in timers_to_cancel:
            timer.cancel()
        if should_load:
            all_scheds = load_all_schedules()
            for sd in all_scheds:
                sid = sd.get("id")
                if not sid:
                    continue
                with self._lock:
                    self._schedules[sid] = sd
        self._recover_stale_running_executions()
        self._ensure_active_timers()

    def create_schedule(self, schedule_type, task_config, schedule_config, name="", description=""):
        """Create and persist a new schedule.

        schedule_type: "interval" | "cron" | "once"
        task_config: {message, model, conversation_id, timeout}
        schedule_config: depends on type
          - interval: {value: int, unit: "seconds"|"minutes"|"hours"}
          - cron: {expression: "0 9 * * *"}
          - once: {run_at: "2025-03-01T09:00:00Z"}
        """
        self.ensure_loaded()

        # Validate schedule_type
        if schedule_type not in ("interval", "cron", "once"):
            raise ValueError("schedule_type must be one of: interval, cron, once")

        # Validate task_config
        if not isinstance(task_config, dict):
            raise ValueError("task_config must be a dict")
        if not task_config.get("message"):
            raise ValueError("task_config.message is required")

        # Validate schedule_config
        if not isinstance(schedule_config, dict):
            raise ValueError("schedule_config must be a dict")

        if schedule_type == "interval":
            val = schedule_config.get("value")
            unit = schedule_config.get("unit", "minutes")
            if not isinstance(val, (int, float)) or val <= 0:
                raise ValueError("schedule_config.value must be a positive number")
            _interval_to_seconds(val, unit)  # validates unit
        elif schedule_type == "cron":
            expr = schedule_config.get("expression")
            if not expr:
                raise ValueError("schedule_config.expression is required for cron type")
            parse_cron_expression(expr)  # validates expression
        elif schedule_type == "once":
            run_at = schedule_config.get("run_at")
            if not run_at:
                raise ValueError("schedule_config.run_at is required for once type")
            _parse_iso_datetime(run_at)  # validates datetime

        now = timestamp()
        sid = "sched_" + gen_id()

        task = {
            "message": task_config.get("message"),
            "model": task_config.get("model", "default"),
            "conversation_id": task_config.get("conversation_id"),
            "timeout": task_config.get("timeout", 300),
        }
        for key in ("profile_id", "agent_id", "tools", "tool_policy", "metadata", "thinking_level"):
            if key in task_config:
                task[key] = task_config.get(key)

        schedule = {
            "id": sid,
            "name": name if name else "Schedule " + sid[:12],
            "description": description,
            "type": schedule_type,
            "task": task,
            "config": schedule_config,
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": None,
            "created_at": now,
            "updated_at": now,
        }

        # Compute next execution time
        schedule["next_execution_at"] = self._compute_next_execution(schedule)

        save_schedule(schedule)
        with self._lock:
            self._schedules[sid] = schedule

        self._arm_timer(sid)
        return schedule

    def get_schedule(self, schedule_id):
        """Return a schedule dict or None."""
        self.ensure_loaded()
        with self._lock:
            return self._schedules.get(schedule_id)

    def list_schedules(self, status_filter=None):
        """Return list of all schedules, optionally filtered by status."""
        self.ensure_loaded()
        with self._lock:
            all_s = list(self._schedules.values())
        if status_filter:
            all_s = [s for s in all_s if s.get("status") == status_filter]
        all_s.sort(key=lambda s: s.get("created_at", ""), reverse=True)
        return all_s

    def update_schedule(self, schedule_id, updates):
        """Update a schedule. Allowed fields: name, description, task, config, type.

        Returns the updated schedule dict or None if not found.
        """
        self.ensure_loaded()
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return None

        allowed_keys = ("name", "description", "task", "config", "type")
        changed = False
        for key in allowed_keys:
            if key in updates:
                if key == "type":
                    if updates[key] not in ("interval", "cron", "once"):
                        raise ValueError("type must be one of: interval, cron, once")
                if key == "config":
                    # Re-validate config based on type
                    stype = updates.get("type", sched.get("type"))
                    cfg = updates["config"]
                    if stype == "interval":
                        val = cfg.get("value")
                        unit = cfg.get("unit", "minutes")
                        if not isinstance(val, (int, float)) or val <= 0:
                            raise ValueError("config.value must be a positive number")
                        _interval_to_seconds(val, unit)
                    elif stype == "cron":
                        expr = cfg.get("expression")
                        if not expr:
                            raise ValueError("config.expression is required for cron type")
                        parse_cron_expression(expr)
                    elif stype == "once":
                        run_at = cfg.get("run_at")
                        if not run_at:
                            raise ValueError("config.run_at is required for once type")
                        _parse_iso_datetime(run_at)
                if key == "task":
                    if not isinstance(updates["task"], dict):
                        raise ValueError("task must be a dict")
                    # Merge with existing task
                    old_task = dict(sched.get("task", {}))
                    old_input_fingerprint = _schedule_execution_input_fingerprint(sched)
                    merged_task = dict(old_task)
                    merged_task.update(updates["task"])
                    if not merged_task.get("message"):
                        raise ValueError("task.message cannot be empty")
                    if merged_task != old_task:
                        running = _running_execution_details(sched)
                        if isinstance(running, dict):
                            running.setdefault("input_fingerprint", old_input_fingerprint)
                            running["obsolete_reason"] = "execution_input_changed"
                            running["obsolete_at"] = timestamp()
                        sched["task"] = merged_task
                        changed = True
                    continue
                sched[key] = updates[key]
                changed = True

        if changed:
            sched["updated_at"] = timestamp()
            if sched.get("status") == "active":
                sched["next_execution_at"] = self._compute_next_execution(sched)
            save_schedule(sched)
            with self._lock:
                self._schedules[schedule_id] = sched
            if sched.get("status") == "active":
                self._cancel_timer(schedule_id)
                self._arm_timer(schedule_id)

        return sched

    def delete_schedule(self, schedule_id):
        """Delete a schedule. Returns True if deleted."""
        self.ensure_loaded()
        self._cancel_timer(schedule_id)
        with self._lock:
            removed = self._schedules.pop(schedule_id, None)
        store_delete(schedule_id)
        return removed is not None

    def pause_schedule(self, schedule_id):
        """Pause an active schedule. Returns updated schedule or None."""
        self.ensure_loaded()
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return None
        if sched.get("status") != "active":
            return sched  # already not active
        self._cancel_timer(schedule_id)
        sched["status"] = "paused"
        sched["next_execution_at"] = None
        sched["updated_at"] = timestamp()
        save_schedule(sched)
        with self._lock:
            self._schedules[schedule_id] = sched
        return sched

    def resume_schedule(self, schedule_id):
        """Resume a paused schedule. Returns updated schedule or None."""
        self.ensure_loaded()
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return None
        if sched.get("status") == "active":
            return sched  # already active
        if sched.get("status") == "completed":
            return sched  # once-type that already ran, cannot resume
        sched["status"] = "active"
        sched["next_execution_at"] = self._compute_next_execution(sched)
        sched["updated_at"] = timestamp()
        save_schedule(sched)
        with self._lock:
            self._schedules[schedule_id] = sched
        self._arm_timer(schedule_id)
        return sched

    def trigger_now(self, schedule_id):
        """Manually trigger a schedule execution immediately.

        Returns execution history entry.
        """
        self.ensure_loaded()
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return None
        return self._execute_task(schedule_id, manual=True)

    def get_history(self, schedule_id, limit=50, offset=0):
        """Return execution history for a schedule."""
        self.ensure_loaded()
        entries, total = load_history(schedule_id, limit=limit, offset=offset)
        return {"entries": entries, "total": total, "limit": limit, "offset": offset}

    def recover_scheduled_chat_approval(self, schedule_id):
        """Continue the current scheduled chat approval node after external approval."""
        self.ensure_loaded()
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return {"schedule_id": schedule_id, "continued_count": 0, "continued": [], "status": "not_found"}

        task_cfg = sched.get("task", {}) if isinstance(sched.get("task"), dict) else {}
        conversation_id = str(task_cfg.get("conversation_id") or "").strip()
        if not conversation_id:
            return {"schedule_id": schedule_id, "continued_count": 0, "continued": [], "status": "no_conversation"}

        try:
            from domain.chat.store import ChatStore
        except Exception:
            return {"schedule_id": schedule_id, "continued_count": 0, "continued": [], "status": "chat_store_unavailable"}

        conversation = ChatStore().get_conversation(conversation_id)
        current = _current_scheduled_approval_result(conversation, schedule_id=schedule_id)
        if not isinstance(current, dict):
            return {"schedule_id": schedule_id, "continued_count": 0, "continued": [], "status": "no_current_approval"}

        conversation_lock = self._conversation_execution_lock(conversation_id)
        if conversation_lock is None:
            return {"schedule_id": schedule_id, "continued_count": 0, "continued": [], "status": "no_conversation_lock"}
        if not conversation_lock.acquire(blocking=False):
            return {"schedule_id": schedule_id, "continued_count": 0, "continued": [], "status": "conversation_running"}

        started_at = timestamp()
        recovered_exec_id = "sexec_recovery_" + gen_id()
        source_metadata = current.get("source_metadata") if isinstance(current.get("source_metadata"), dict) else {}
        exec_id = str(source_metadata.get("schedule_execution_id") or recovered_exec_id).strip()
        trigger = str(source_metadata.get("trigger") or "scheduled").strip() or "scheduled"
        timeout_seconds = _task_timeout_seconds(task_cfg.get("timeout", 300))
        params, tools = _scheduler_chat_params_and_tools(task_cfg, timeout_seconds=timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        cancel_event = threading.Event()
        self._set_conversation_lock_holder(
            conversation_id,
            schedule_id=schedule_id,
            execution_id=recovered_exec_id,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            trigger="approval_recovery",
            cancel_event=cancel_event,
            orphan_releasable=False,
        )
        auto_approvals: list[dict[str, Any]] = []
        result = current["result"]
        history_entry = {
            "execution_id": recovered_exec_id,
            "schedule_id": schedule_id,
            "started_at": started_at,
            "completed_at": None,
            "status": "running",
            "trigger": "approval_recovery",
            "result": None,
            "error": None,
            "recovered_scheduled_approval": True,
            "recovered_execution_id": exec_id,
        }

        try:
            from blocks.chat.send import run as chat_send_run

            def send_chat(payload, context):
                if self._settings_owner is None:
                    return chat_send_run(payload, context)
                return chat_send_run(payload, context, settings_owner=self._settings_owner)

            def run_recovery():
                return _resume_scheduled_chat_approvals(
                    result=result,
                    send_chat=send_chat,
                    conversation_id=conversation_id,
                    task_cfg=task_cfg,
                    schedule_id=schedule_id,
                    exec_id=exec_id,
                    trigger=trigger,
                    params=params,
                    tools=tools,
                    cancel_event=cancel_event,
                )

            result, auto_approvals = _run_with_timeout(
                run_recovery,
                _remaining_timeout_seconds(deadline),
                task_timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
            if not auto_approvals:
                return {"schedule_id": schedule_id, "continued_count": 0, "continued": [], "status": "not_approved"}

            if isinstance(result, dict) and result.get("status") == "ok":
                data = result.get("data", {})
                if isinstance(data, dict):
                    content = _chat_result_content(result)
                    finish_reason = _chat_result_finish_reason(result)
                elif isinstance(data, str):
                    content = data
                    finish_reason = ""
                else:
                    content = str(data)
                    finish_reason = ""
                if finish_reason in _APPROVAL_REQUIRED_FINISH_REASONS:
                    content = (finish_reason + "\n" + content).strip()
                    history_entry["status"] = finish_reason
                else:
                    history_entry["status"] = "completed"
                history_entry["result"] = content
                if finish_reason:
                    history_entry["finish_reason"] = finish_reason
            else:
                err = result.get("error", {}) if isinstance(result, dict) else result
                if isinstance(err, dict):
                    err_msg = err.get("message", str(err))
                else:
                    err_msg = str(err)
                history_entry["status"] = "error"
                history_entry["error"] = err_msg
        except Exception as exc:
            history_entry["status"] = "error"
            history_entry["error"] = str(exc)
            if isinstance(exc, _SchedulerTaskTimedOut):
                history_entry["timeout_seconds"] = exc.timeout_seconds
        finally:
            self._release_conversation_execution_lock(conversation_id, conversation_lock, recovered_exec_id)

        if auto_approvals:
            history_entry["auto_approvals"] = auto_approvals
            history_entry["completed_at"] = timestamp()
            append_history(schedule_id, history_entry)
            return {
                "schedule_id": schedule_id,
                "continued_count": len(auto_approvals),
                "continued": auto_approvals,
                "status": history_entry["status"],
            }
        return {"schedule_id": schedule_id, "continued_count": 0, "continued": [], "status": "not_approved"}

    # ---- internal ----

    def _compute_next_execution(self, sched):
        """Compute the ISO timestamp of the next execution."""
        stype = sched.get("type")
        cfg = sched.get("config", {})
        now = datetime.now(timezone.utc)

        if stype == "interval":
            val = cfg.get("value", 60)
            unit = cfg.get("unit", "minutes")
            secs = _interval_to_seconds(val, unit)
            nxt = now + timedelta(seconds=secs)
            return nxt.strftime("%Y-%m-%dT%H:%M:%SZ")

        if stype == "cron":
            expr = cfg.get("expression", "* * * * *")
            parsed = parse_cron_expression(expr)
            nxt = next_cron_time(parsed, now)
            if nxt is None:
                return None
            return nxt.strftime("%Y-%m-%dT%H:%M:%SZ")

        if stype == "once":
            run_at = cfg.get("run_at")
            if not run_at:
                return None
            dt = _parse_iso_datetime(run_at)
            if dt <= now:
                return None  # already past
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        return None

    def _seconds_until_next(self, sched):
        """Return seconds until the next scheduled execution, or None."""
        nxt_str = sched.get("next_execution_at")
        if not nxt_str:
            return None
        try:
            nxt = _parse_iso_datetime(nxt_str)
        except ValueError:
            return None
        now = datetime.now(timezone.utc)
        delta = (nxt - now).total_seconds()
        if delta < 0:
            return 0.1  # fire immediately if overdue
        return max(delta, 0.1)

    def _arm_timer(self, schedule_id):
        """Set a threading.Timer for the next execution of a schedule."""
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None or sched.get("status") != "active":
            return
        delay = self._seconds_until_next(sched)
        if delay is None:
            return
        # Cap very long delays at 1 hour and re-check then
        max_delay = 3600.0
        if delay > max_delay:
            timer = threading.Timer(max_delay, self._recheck_and_arm, args=[schedule_id])
        else:
            timer = threading.Timer(delay, self._on_timer_fire, args=[schedule_id])
        timer.daemon = True
        with self._lock:
            old = self._timers.pop(schedule_id, None)
            if old is not None:
                old.cancel()
            self._timers[schedule_id] = timer
        timer.start()

    def _timer_needs_arm(self, schedule_id):
        with self._lock:
            timer = self._timers.get(schedule_id)
        if timer is None:
            return True
        is_alive = getattr(timer, "is_alive", None)
        if not callable(is_alive):
            return False
        return not is_alive()

    def _recover_stale_running_executions(self):
        with self._lock:
            schedule_ids = list(self._schedules.keys())
        for schedule_id in schedule_ids:
            self._recover_stale_running_execution(schedule_id)

    def _refresh_schedule_from_store(self, schedule_id):
        try:
            persisted = load_schedule(schedule_id)
        except Exception:
            return None
        with self._lock:
            if persisted is None:
                self._schedules.pop(schedule_id, None)
                return None
            self._schedules[schedule_id] = persisted
            return persisted

    def _recover_stale_running_execution(self, schedule_id):
        self._refresh_schedule_from_store(schedule_id)
        with self._lock:
            sched = self._schedules.get(schedule_id)
            if sched is None:
                return False
            stale = _recoverable_running_execution(sched)
            if stale is None:
                return False
            stale_execution_id = stale["execution_id"]
            if stale_execution_id in self._stale_recovered_execution_ids:
                return False
            self._stale_recovered_execution_ids.add(stale_execution_id)
            release_active_claim = False
            if stale_execution_id not in self._active_execution_ids:
                self._active_execution_ids.add(stale_execution_id)
                release_active_claim = True

        try:
            completed_at = timestamp()
            if stale.get("recovery_kind") == "obsolete":
                history_entry = {
                    "execution_id": stale["execution_id"],
                    "schedule_id": schedule_id,
                    "started_at": stale["started_at"],
                    "completed_at": completed_at,
                    "status": "obsolete",
                    "trigger": stale["trigger"],
                    "result": None,
                    "error": None,
                    "obsolete_reason": stale.get("obsolete_reason") or "execution_input_changed",
                    "recovered_obsolete_running_execution": True,
                    "input_fingerprint": stale.get("input_fingerprint") or "",
                    "current_input_fingerprint": stale.get("current_input_fingerprint") or "",
                }
                if stale.get("scheduled_user_message_id"):
                    history_entry["scheduled_user_message_id"] = stale.get("scheduled_user_message_id")
            else:
                history_entry = {
                    "execution_id": stale["execution_id"],
                    "schedule_id": schedule_id,
                    "started_at": stale["started_at"],
                    "completed_at": completed_at,
                    "status": "error",
                    "trigger": stale["trigger"],
                    "result": None,
                    "error": _scheduler_timeout_error(stale["timeout_seconds"]),
                    "timeout_seconds": stale["timeout_seconds"],
                    "recovered_stale_running_execution": True,
                }
            task_cfg = sched.get("task", {}) if isinstance(sched.get("task"), dict) else {}
            conversation_id = str(task_cfg.get("conversation_id") or "").strip()
            if conversation_id and stale.get("recovery_kind") != "obsolete":
                stored_error = _ensure_scheduled_chat_error_message(
                    conversation_id=conversation_id,
                    schedule_id=schedule_id,
                    exec_id=stale["execution_id"],
                    task_cfg=task_cfg,
                    trigger=stale["trigger"],
                    error_text=history_entry["error"],
                )
                if isinstance(stored_error, dict):
                    history_entry["conversation_id"] = conversation_id
                    history_entry["assistant_error_message_id"] = stored_error.get("id")
            append_history(schedule_id, history_entry)

            with self._lock:
                sched = self._schedules.get(schedule_id)
                if sched is None:
                    return True
                current = _running_execution_details(sched)
                if isinstance(current, dict):
                    current_execution_id = str(current.get("execution_id") or "").strip()
                    if current_execution_id and current_execution_id != stale["execution_id"]:
                        return True
                sched.pop("running_execution", None)
                sched.pop("running_started_at", None)
                try:
                    execution_count = int(sched.get("execution_count", 0))
                except (TypeError, ValueError):
                    execution_count = 0
                sched["execution_count"] = execution_count + 1
                sched["last_executed_at"] = completed_at
                if stale["trigger"] != "manual":
                    if sched.get("type") == "once":
                        sched["status"] = "completed"
                        sched["next_execution_at"] = None
                    elif sched.get("status") == "active":
                        sched["next_execution_at"] = self._compute_next_execution(sched)
                sched["updated_at"] = timestamp()
                save_schedule(sched)
                self._schedules[schedule_id] = sched
            return True
        except Exception:
            with self._lock:
                self._stale_recovered_execution_ids.discard(stale["execution_id"])
            raise
        finally:
            with self._lock:
                if release_active_claim:
                    self._active_execution_ids.discard(stale["execution_id"])
                    self._stale_recovered_execution_ids.discard(stale["execution_id"])

    def _ensure_active_timers(self):
        with self._lock:
            active_ids = [
                sid
                for sid, sched in self._schedules.items()
                if sched.get("status") == "active"
            ]
            inactive_ids = [
                sid
                for sid, sched in self._schedules.items()
                if sched.get("status") != "active"
            ]

        for schedule_id in inactive_ids:
            self._cancel_timer(schedule_id)
        for schedule_id in active_ids:
            if self._timer_needs_arm(schedule_id):
                self._arm_timer(schedule_id)

    def _recheck_and_arm(self, schedule_id):
        """Called when delay was capped; re-compute and re-arm."""
        self._recover_stale_running_execution(schedule_id)
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None or sched.get("status") != "active":
            return
        self._arm_timer(schedule_id)

    def _cancel_timer(self, schedule_id):
        """Cancel any running timer for a schedule."""
        with self._lock:
            timer = self._timers.pop(schedule_id, None)
        if timer is not None:
            timer.cancel()

    def _mark_schedule_running(self, schedule_id, execution_id, started_at, trigger, timeout_seconds):
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return
        marked_at = timestamp()
        sched["running_execution"] = {
            "execution_id": execution_id,
            "schedule_id": schedule_id,
            "started_at": started_at,
            "marked_at": marked_at,
            "trigger": trigger,
            "timeout_seconds": timeout_seconds,
            "input_fingerprint": _schedule_execution_input_fingerprint(sched),
        }
        sched["running_started_at"] = started_at
        sched["updated_at"] = marked_at
        save_schedule(sched)
        with self._lock:
            self._schedules[schedule_id] = sched

    def _already_running_entry(self, schedule_id, running, manual):
        now = timestamp()
        active_execution_id = str(running.get("execution_id") or "").strip()
        active_started_at = running.get("started_at")
        active_trigger = running.get("trigger")
        detail = "schedule already has a running execution"
        if active_execution_id:
            detail += ": " + active_execution_id
        return {
            "execution_id": "sexec_skipped_" + gen_id(),
            "schedule_id": schedule_id,
            "started_at": now,
            "completed_at": now,
            "status": "skipped",
            "trigger": "manual" if manual else "scheduled",
            "result": None,
            "error": detail,
            "skipped_reason": "already_running",
            "running_execution": {
                "execution_id": active_execution_id,
                "started_at": active_started_at,
                "trigger": active_trigger,
            },
        }

    def _last_history_is_duplicate_already_running_skip(self, schedule_id, running):
        active_execution_id = str(running.get("execution_id") or "").strip()
        if not active_execution_id:
            return False
        try:
            entries, _total = load_history(schedule_id, limit=1)
        except Exception:
            return False
        if not entries:
            return False
        latest = entries[0]
        if not isinstance(latest, dict):
            return False
        if latest.get("status") != "skipped" or latest.get("skipped_reason") != "already_running":
            return False
        latest_running = latest.get("running_execution") if isinstance(latest.get("running_execution"), dict) else {}
        return str(latest_running.get("execution_id") or "").strip() == active_execution_id

    def _conversation_running_entry(self, schedule_id, conversation_id, manual):
        now = timestamp()
        conversation_id = str(conversation_id or "").strip()
        detail = "conversation is already running"
        if conversation_id:
            detail += ": " + conversation_id
        return {
            "execution_id": "sexec_skipped_" + gen_id(),
            "schedule_id": schedule_id,
            "started_at": now,
            "completed_at": now,
            "status": "skipped" if not manual else "error",
            "trigger": "manual" if manual else "scheduled",
            "result": None,
            "error": detail,
            "error_code": "CONVERSATION_RUNNING",
            "skipped_reason": "conversation_running",
            "conversation_id": conversation_id,
        }

    def _last_history_is_duplicate_conversation_running_skip(self, schedule_id, conversation_id):
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return False
        try:
            entries, _total = load_history(schedule_id, limit=1)
        except Exception:
            return False
        if not entries:
            return False
        latest = entries[0]
        if not isinstance(latest, dict):
            return False
        if latest.get("status") != "skipped" or latest.get("skipped_reason") != "conversation_running":
            return False
        return str(latest.get("conversation_id") or "").strip() == conversation_id

    def _advance_after_skipped_scheduled_execution(self, schedule_id):
        with self._lock:
            sched = self._schedules.get(schedule_id)
            if sched is None:
                return
            if sched.get("type") == "once":
                sched["next_execution_at"] = _retry_once_after_running_skip()
            elif sched.get("status") == "active":
                sched["next_execution_at"] = self._compute_next_execution(sched)
            sched["updated_at"] = timestamp()
            save_schedule(sched)
            self._schedules[schedule_id] = sched

    def _conversation_execution_lock(self, conversation_id):
        key = str(conversation_id or "").strip()
        if not key:
            return None
        with self._lock:
            lock = self._conversation_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._conversation_locks[key] = lock
            return lock

    def _set_conversation_lock_holder(
        self,
        conversation_id,
        *,
        schedule_id,
        execution_id,
        started_at,
        timeout_seconds,
        trigger,
        cancel_event=None,
        orphan_releasable=False,
    ):
        key = str(conversation_id or "").strip()
        if not key:
            return None
        holder = {
            "conversation_id": key,
            "schedule_id": str(schedule_id or "").strip(),
            "execution_id": str(execution_id or "").strip(),
            "started_at": started_at,
            "timeout_seconds": timeout_seconds,
            "trigger": str(trigger or "").strip(),
            "cancel_event": cancel_event,
            "orphan_releasable": bool(orphan_releasable),
            "holder_marked_at": timestamp(),
        }
        with self._lock:
            self._conversation_lock_holders[key] = holder
        return holder

    def _release_conversation_execution_lock(self, conversation_id, conversation_lock, execution_id):
        key = str(conversation_id or "").strip()
        execution_id = str(execution_id or "").strip()
        should_release = False
        if key:
            with self._lock:
                holder = self._conversation_lock_holders.get(key)
                holder_execution_id = str(holder.get("execution_id") or "").strip() if isinstance(holder, dict) else ""
                if holder_execution_id == execution_id:
                    self._conversation_lock_holders.pop(key, None)
                    should_release = True
        if not should_release:
            return False
        try:
            conversation_lock.release()
            return True
        except RuntimeError:
            return False

    def _conversation_lock_orphan_reason(self, holder):
        if not isinstance(holder, dict) or not holder.get("orphan_releasable"):
            return None
        schedule_id = str(holder.get("schedule_id") or "").strip()
        holder_execution_id = str(holder.get("execution_id") or "").strip()
        if not schedule_id or not holder_execution_id:
            return None
        try:
            persisted = load_schedule(schedule_id)
        except Exception:
            return None
        with self._lock:
            if persisted is None:
                self._schedules.pop(schedule_id, None)
            else:
                self._schedules[schedule_id] = persisted
        if persisted is None:
            return "schedule_missing"
        running = _running_execution_details(persisted)
        if running is None:
            return "running_execution_missing"
        running_execution_id = str(running.get("execution_id") or "").strip()
        if not running_execution_id or running_execution_id != holder_execution_id:
            return "running_execution_changed"
        if _recoverable_running_execution(persisted) is not None:
            return "running_execution_recoverable"
        return None

    def _release_orphaned_conversation_lock(self, conversation_id):
        key = str(conversation_id or "").strip()
        if not key:
            return False
        with self._lock:
            holder = self._conversation_lock_holders.get(key)
            conversation_lock = self._conversation_locks.get(key)
        if not isinstance(holder, dict) or conversation_lock is None:
            return False
        reason = self._conversation_lock_orphan_reason(holder)
        if not reason:
            return False
        schedule_id = str(holder.get("schedule_id") or "").strip()
        execution_id = str(holder.get("execution_id") or "").strip()
        if reason == "running_execution_recoverable" and schedule_id:
            self._recover_stale_running_execution(schedule_id)
        cancel_event = holder.get("cancel_event")
        if hasattr(cancel_event, "set"):
            try:
                cancel_event.set()
            except Exception:
                pass
        should_release = False
        with self._lock:
            current = self._conversation_lock_holders.get(key)
            current_execution_id = str(current.get("execution_id") or "").strip() if isinstance(current, dict) else ""
            current_schedule_id = str(current.get("schedule_id") or "").strip() if isinstance(current, dict) else ""
            if current_execution_id == execution_id and current_schedule_id == schedule_id:
                self._conversation_lock_holders.pop(key, None)
                if execution_id in self._active_execution_ids:
                    self._stale_recovered_execution_ids.add(execution_id)
                should_release = True
        if not should_release:
            return False
        try:
            conversation_lock.release()
            return True
        except RuntimeError:
            return False

    def _on_timer_fire(self, schedule_id):
        """Called when a timer fires. Execute the task and re-arm."""
        self._recover_stale_running_execution(schedule_id)
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None or sched.get("status") != "active":
            return
        self._execute_task(schedule_id, manual=False)
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None or sched.get("status") != "active" or not sched.get("next_execution_at"):
            return
        self._arm_timer(schedule_id)

    def _execute_task(self, schedule_id, manual=False):
        """Execute the agent task for a schedule and record history."""
        self._refresh_schedule_from_store(schedule_id)
        self._recover_stale_running_execution(schedule_id)
        with self._lock:
            sched = self._schedules.get(schedule_id)
            running = _active_running_execution(sched) if sched is not None else None
        if sched is None:
            return None
        if running is not None:
            history_entry = self._already_running_entry(schedule_id, running, manual)
            if not manual:
                if not self._last_history_is_duplicate_already_running_skip(schedule_id, running):
                    append_history(schedule_id, history_entry)
                self._advance_after_skipped_scheduled_execution(schedule_id)
            return history_entry

        task_cfg = sched.get("task", {})
        message = task_cfg.get("message", "")
        model = task_cfg.get("model", "default")
        timeout_seconds = _task_timeout_seconds(task_cfg.get("timeout", 300))
        conversation_id = task_cfg.get("conversation_id")

        exec_id = "sexec_" + gen_id()
        started_at = timestamp()
        deadline = time.monotonic() + timeout_seconds
        cancel_event = threading.Event()

        history_entry = {
            "execution_id": exec_id,
            "schedule_id": schedule_id,
            "started_at": started_at,
            "completed_at": None,
            "status": "running",
            "trigger": "manual" if manual else "scheduled",
            "result": None,
            "error": None,
        }
        auto_approvals = []
        trigger = _scheduler_trigger_name(manual)
        conversation_lock = None
        conversation_lock_acquired = False
        history_finalized = False
        with self._lock:
            self._active_execution_ids.add(exec_id)
        try:
            if conversation_id:
                conversation_lock = self._conversation_execution_lock(conversation_id)
                if conversation_lock is None:
                    raise ValueError("task.conversation_id cannot be blank")
                self._release_orphaned_conversation_lock(conversation_id)
                if manual:
                    acquired = conversation_lock.acquire(blocking=False)
                else:
                    lock_timeout = _scheduled_conversation_lock_wait_seconds(
                        task_cfg,
                        _remaining_timeout_seconds(deadline),
                    )
                    acquired = conversation_lock.acquire(timeout=_wait_timeout_seconds(lock_timeout))
                if not acquired and self._release_orphaned_conversation_lock(conversation_id):
                    acquired = conversation_lock.acquire(blocking=False)
                if not acquired:
                    cancel_event.set()
                    if manual:
                        raise _SchedulerConversationBusy(str(conversation_id))
                    history_entry = self._conversation_running_entry(schedule_id, conversation_id, manual=False)
                    if not self._last_history_is_duplicate_conversation_running_skip(schedule_id, conversation_id):
                        append_history(schedule_id, history_entry)
                    self._advance_after_skipped_scheduled_execution(schedule_id)
                    history_finalized = True
                    return history_entry
                conversation_lock_acquired = True
                self._set_conversation_lock_holder(
                    conversation_id,
                    schedule_id=schedule_id,
                    execution_id=exec_id,
                    started_at=started_at,
                    timeout_seconds=timeout_seconds,
                    trigger=trigger,
                    cancel_event=cancel_event,
                    orphan_releasable=False,
                )
                self._mark_schedule_running(schedule_id, exec_id, started_at, trigger, timeout_seconds)
                self._set_conversation_lock_holder(
                    conversation_id,
                    schedule_id=schedule_id,
                    execution_id=exec_id,
                    started_at=started_at,
                    timeout_seconds=timeout_seconds,
                    trigger=trigger,
                    cancel_event=cancel_event,
                    orphan_releasable=True,
                )
                try:
                    from blocks.chat.send import run as chat_send_run

                    def send_chat(payload, context):
                        if self._settings_owner is None:
                            return chat_send_run(payload, context)
                        return chat_send_run(
                            payload, context, settings_owner=self._settings_owner
                        )

                    params, tools = _scheduler_chat_params_and_tools(task_cfg, timeout_seconds=timeout_seconds)

                    def run_chat_task():
                        initial_parent_id = _current_conversation_node_id(str(conversation_id))
                        chat_result = send_chat(
                            _scheduler_chat_payload(
                                conversation_id=conversation_id,
                                content=message,
                                task_cfg=task_cfg,
                                schedule_id=schedule_id,
                                exec_id=exec_id,
                                trigger=trigger,
                                params=params,
                                tools=tools,
                                parent_id=initial_parent_id,
                            ),
                            _scheduler_chat_context(task_cfg, cancel_event=cancel_event),
                        )
                        return _resume_scheduled_chat_approvals(
                            result=chat_result,
                            send_chat=send_chat,
                            conversation_id=conversation_id,
                            task_cfg=task_cfg,
                            schedule_id=schedule_id,
                            exec_id=exec_id,
                            trigger=trigger,
                            params=params,
                            tools=tools,
                            cancel_event=cancel_event,
                        )

                    result, auto_approvals = _run_with_timeout(
                        run_chat_task,
                        _remaining_timeout_seconds(deadline),
                        task_timeout_seconds=timeout_seconds,
                        cancel_event=cancel_event,
                    )
                finally:
                    self._release_conversation_execution_lock(conversation_id, conversation_lock, exec_id)
                    conversation_lock_acquired = False
            else:
                self._mark_schedule_running(schedule_id, exec_id, started_at, trigger, timeout_seconds)
                from blocks.ai.complete import run as ai_complete_run

                messages = []
                system_content = (
                    "You are a scheduled agent. Execute the following task. "
                    "Be concise and precise in your response."
                )
                messages.append({"role": "system", "content": system_content})
                messages.append({"role": "user", "content": message})

                scheduler_context = _scheduler_chat_context(task_cfg, cancel_event=cancel_event)
                completion_params: dict[str, Any] = {}
                _apply_scheduler_execution_timeout_to_params(completion_params, timeout_seconds)

                def run_completion_task():
                    payload = {"messages": messages, "model": model}
                    if completion_params:
                        payload["params"] = completion_params
                    if self._settings_owner is None:
                        return ai_complete_run(payload, scheduler_context)
                    return ai_complete_run(
                        payload, scheduler_context, settings_owner=self._settings_owner
                    )

                result = _run_with_timeout(
                    run_completion_task,
                    _remaining_timeout_seconds(deadline),
                    task_timeout_seconds=timeout_seconds,
                    cancel_event=cancel_event,
                )

            if result.get("status") == "ok":
                data = result.get("data", {})
                if isinstance(data, dict):
                    content = _chat_result_content(result)
                    finish_reason = _chat_result_finish_reason(result)
                elif isinstance(data, str):
                    content = data
                    finish_reason = ""
                else:
                    content = str(data)
                    finish_reason = ""
                if finish_reason in _APPROVAL_REQUIRED_FINISH_REASONS:
                    content = (finish_reason + "\n" + content).strip()
                    history_entry["status"] = finish_reason
                else:
                    history_entry["status"] = "completed"
                history_entry["result"] = content
                if finish_reason:
                    history_entry["finish_reason"] = finish_reason
                if auto_approvals:
                    history_entry["auto_approvals"] = auto_approvals
            else:
                err = result.get("error", {})
                if isinstance(err, dict):
                    err_msg = err.get("message", str(err))
                else:
                    err_msg = str(err)
                history_entry["status"] = "error"
                history_entry["error"] = err_msg

        except Exception as exc:
            history_entry["status"] = "error"
            history_entry["error"] = str(exc)
            if isinstance(exc, _SchedulerTaskTimedOut):
                history_entry["timeout_seconds"] = exc.timeout_seconds
            elif isinstance(exc, _SchedulerConversationBusy):
                history_entry["error_code"] = "CONVERSATION_RUNNING"
                history_entry["skipped_reason"] = "conversation_running"
            if conversation_id and not isinstance(exc, _SchedulerConversationBusy):
                history_entry["conversation_id"] = str(conversation_id)
                stored_error = _ensure_scheduled_chat_error_message(
                    conversation_id=str(conversation_id),
                    schedule_id=schedule_id,
                    exec_id=exec_id,
                    task_cfg=task_cfg if isinstance(task_cfg, dict) else {},
                    trigger=trigger,
                    error_text=history_entry["error"],
                )
                if isinstance(stored_error, dict):
                    history_entry["assistant_error_message_id"] = stored_error.get("id")
        finally:
            if conversation_lock_acquired and conversation_lock is not None:
                self._release_conversation_execution_lock(conversation_id, conversation_lock, exec_id)

            try:
                with self._lock:
                    recovered_as_stale = exec_id in self._stale_recovered_execution_ids
                if not recovered_as_stale and not history_finalized:
                    history_entry["completed_at"] = timestamp()
                    append_history(schedule_id, history_entry)

                    # Update schedule metadata
                    with self._lock:
                        sched = self._schedules.get(schedule_id)
                    if sched is not None:
                        current = _running_execution_details(sched)
                        if isinstance(current, dict):
                            current_execution_id = str(current.get("execution_id") or "").strip()
                            if not current_execution_id or current_execution_id == exec_id:
                                sched.pop("running_execution", None)
                                sched.pop("running_started_at", None)
                        else:
                            sched.pop("running_execution", None)
                            sched.pop("running_started_at", None)
                        sched["execution_count"] = sched.get("execution_count", 0) + 1
                        sched["last_executed_at"] = history_entry["completed_at"]
                        if not manual:
                            if sched.get("type") == "once":
                                sched["status"] = "completed"
                                sched["next_execution_at"] = None
                            elif sched.get("status") == "active":
                                sched["next_execution_at"] = self._compute_next_execution(sched)
                        sched["updated_at"] = timestamp()
                        save_schedule(sched)
                        with self._lock:
                            self._schedules[schedule_id] = sched
            finally:
                with self._lock:
                    self._active_execution_ids.discard(exec_id)
                    self._stale_recovered_execution_ids.discard(exec_id)
        return history_entry

    def shutdown(self):
        """Cancel all timers. Called on process exit."""
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
