from __future__ import annotations

import os
import re
import threading
from typing import Any, Iterable

from tobkiri_protocol.settings_state import SettingsOwnerPort

from core_runtime.runtime_audit_helpers import audit_event, redact_sensitive
from core_runtime.runtime_events import utc_now
from domain.agent_runtime.run_store import AgentRunStore
from domain.company.models import DEFAULT_CHANNEL_ID, DEFAULT_COMPANY_ID
from domain.company.run_dispatcher import CompanyRunDispatcher
from domain.company.runtime_store import ACTIVE_RUN_STATUSES, CompanyRuntimeStore
from domain.company.service import CompanyService
from domain.company.store import CompanyStore


MAX_REMOTE_INPUT_CHARS = 20_000
NEXT_POLL_MS = 1500
REMOTE_METADATA_KEY = "remote_gateway"
SAFE_REMOTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
REMOTE_STATES = {
    "queued",
    "running",
    "waiting_approval",
    "blocked",
    "completed",
    "cancelled",
    "stale",
}


class RemoteTaskGatewayError(ValueError):
    def __init__(self, message: str, code: str = "INVALID_INPUT", *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class RemoteTaskGateway:
    """Authenticated LAN/PC/mobile gateway over the existing team workspace runtime."""

    def __init__(
        self,
        *,
        company_store: CompanyStore | None = None,
        company_service: CompanyService | None = None,
        runtime_store: CompanyRuntimeStore | None = None,
        run_store: AgentRunStore | None = None,
        run_dispatcher: CompanyRunDispatcher | None = None,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> None:
        self.company_store = company_store or CompanyStore()
        self.company_service = company_service or CompanyService(
            self.company_store,
            settings_owner=settings_owner,
        )
        self.runtime_store = runtime_store or CompanyRuntimeStore()
        self.run_store = run_store or AgentRunStore()
        self.run_dispatcher = run_dispatcher or CompanyRunDispatcher(
            company_store=self.company_store,
            runtime_store=self.runtime_store,
            settings_owner=settings_owner,
        )

    def create_task(self, args: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = _require_dict(args)
        input_text = _remote_input(payload)
        company = self._resolve_company(payload.get("company_id"))
        company_id = str(company["id"])
        target_agent_ids = self._target_agent_ids(company, payload.get("target_agent_ids"))
        client = redact_sensitive(payload.get("client") if isinstance(payload.get("client"), dict) else {})
        request_metadata = redact_sensitive(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {})
        priority = _clean_text(payload.get("priority"), default="normal", limit=40)
        title = _clean_text(payload.get("title"), default=_title_from_input(input_text), limit=160)
        remote_metadata = {
            "source": "remote_gateway",
            "client": client,
            "metadata": request_metadata,
            "policy": {
                "mode": "agent_delegate",
                "direct_tool_execution": False,
            },
            "input_chars": len(input_text),
        }
        message = self.runtime_store.add_message(
            company_id,
            channel_id=DEFAULT_CHANNEL_ID,
            sender_id=_sender_id(client),
            content=input_text,
            mentions=[],
            metadata=remote_metadata,
        )
        task = self.runtime_store.create_task(
            company_id,
            title=title,
            description=input_text,
            target_agent_ids=target_agent_ids,
            source="remote",
            status="queued",
            priority=priority,
            channel_id=message.get("channel_id") or DEFAULT_CHANNEL_ID,
            thread_id=message.get("thread_id"),
            message_id=message.get("message_id"),
            metadata=remote_metadata,
        )
        self.runtime_store.update_message_tasks(str(message["message_id"]), [str(task["task_id"])])
        self._append_event(
            company_id,
            str(task["task_id"]),
            "task.created",
            "Remote task created",
            message_id=message.get("message_id"),
            thread_id=message.get("thread_id"),
            status="queued",
            agent_ids=target_agent_ids,
        )
        routes: list[dict[str, Any]] = []
        dispatch_result: dict[str, Any] | None = None
        dispatch_requested = payload.get("dispatch", True) is not False
        if dispatch_requested:
            dispatch_result = {"status": "queued"}
            self._append_event(
                company_id,
                str(task["task_id"]),
                "task.dispatch_queued",
                "Remote task dispatch queued",
                status="queued",
                agent_ids=target_agent_ids,
            )

        audit_event(
            context,
            "remote_task.create",
            {
                "company_id": company_id,
                "task_id": task.get("task_id"),
                "target_agent_ids": target_agent_ids,
                "dispatch": dispatch_requested,
            },
        )
        snapshot = self._snapshot(str(task["task_id"]), company_id=company_id)
        snapshot.update(
            {
                "message_id": message.get("message_id"),
                "thread_id": message.get("thread_id"),
                "routes": routes,
                "dispatch": dispatch_result.get("dispatch") if isinstance(dispatch_result, dict) else None,
            }
        )
        if dispatch_requested:
            snapshot["dispatch"] = dispatch_result
            self._start_dispatch_thread(company_id, str(task["task_id"]), context or {}, client)
        return snapshot

    def get_task(self, task_id: str, args: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        del args, context
        return self._snapshot(_safe_task_id(task_id))

    def list_events(self, task_id: str, args: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        del context
        payload = args if isinstance(args, dict) else {}
        snapshot = self._snapshot(_safe_task_id(task_id))
        task = snapshot["task"]
        all_events = self._remote_events(task)
        after = str(payload.get("after") or "").strip()
        limit = _int_between(payload.get("limit"), default=100, minimum=1, maximum=500)
        events = [event for event in all_events if not after or str(event.get("cursor") or "") > after]
        selected = events[:limit]
        next_cursor = str(selected[-1].get("cursor")) if selected else after
        if not next_cursor and all_events:
            next_cursor = str(all_events[-1].get("cursor") or "")
        return {
            "events": selected,
            "next_cursor": next_cursor,
            "next_poll_ms": NEXT_POLL_MS,
        }

    def cancel_task(self, task_id: str, args: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = args if isinstance(args, dict) else {}
        task = self._get_task_or_raise(_safe_task_id(task_id), company_id=payload.get("company_id"))
        company_id = str(task["company_id"])
        reason = _clean_text(payload.get("reason"), default="Remote task cancelled", limit=500)
        run_links = self.runtime_store.list_run_links(company_id, task_id=str(task["task_id"]), limit=1000)
        updated_task = self.runtime_store.update_task(
            str(task["task_id"]),
            {
                "status": "cancelled",
                "metadata": {
                    "cancelled_by": "remote_gateway",
                    "cancel_reason": reason,
                    "cancelled_at": utc_now(),
                },
            },
            company_id=company_id,
        ) or task
        for link in run_links:
            run_id = str(link.get("run_id") or "")
            if not run_id:
                continue
            self.runtime_store.update_run_link_status(run_id, "cancelled")
            if self.run_store.get_run(run_id) is not None:
                self.run_store.update_status(
                    run_id,
                    "cancelled",
                    result={"cancelled_by": "remote_gateway", "reason": reason},
                    completed=True,
                )
                self.run_store.add_event(run_id, "remote_task.cancelled", {"company_id": company_id, "task_id": task["task_id"], "reason": reason})
        self.runtime_store.add_inbox_item(
            company_id,
            agent_id="operations_manager",
            task_id=str(task["task_id"]),
            kind="remote_task_cancelled",
            content="Remote task cancelled: " + str(updated_task.get("title") or task["task_id"]),
            status="open",
            priority="high",
            metadata={"reason": reason, "run_ids": [link.get("run_id") for link in run_links if link.get("run_id")]},
        )
        self._append_event(company_id, str(task["task_id"]), "task.cancelled", "Remote task cancelled", status="cancelled", reason=reason)
        for link in self.runtime_store.list_run_links(company_id, task_id=str(task["task_id"]), limit=1000):
            self._append_run_status_event(company_id, str(task["task_id"]), link)
        audit_event(context, "remote_task.cancel", {"company_id": company_id, "task_id": task["task_id"], "reason": reason})
        return self._snapshot(str(task["task_id"]), company_id=company_id)

    def host_status(self, args: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        del context
        payload = args if isinstance(args, dict) else {}
        company = self.company_service.bootstrap_default_company()
        company_id = str(company["id"])
        run_links = self.runtime_store.list_run_links(company_id, limit=10000)
        run_ids = _run_ids(run_links)
        stale_after_seconds = _int_between(payload.get("stale_after_seconds"), default=600, minimum=1, maximum=86_400)
        waiting_approvals = self.run_store.list_waiting_approval(run_ids=run_ids, limit=10000)
        stale_runs = self.run_store.list_stale(stale_after_seconds=stale_after_seconds, run_ids=run_ids, limit=10000)
        active_runs = self._active_run_count(run_links)
        return {
            "host": {
                "remote_gateway": True,
                "api": str(os.environ.get("RUMI_API_PORT") or "8765"),
                "auth": "bearer",
            },
            "company": {
                "company_id": company_id,
                "bootstrapped": True,
            },
            "runtime": {
                "open_tasks": len(self.runtime_store.list_open_tasks(company_id, limit=10000)),
                "active_runs": active_runs,
                "waiting_approvals": len(waiting_approvals),
                "stale_runs": len(stale_runs),
            },
        }

    def _resolve_company(self, raw_company_id: Any) -> dict[str, Any]:
        company_id = _safe_company_id(raw_company_id or DEFAULT_COMPANY_ID)
        if company_id == DEFAULT_COMPANY_ID:
            return self.company_service.bootstrap_default_company()
        company = self.company_service.get_company(company_id)
        if company is None:
            raise RemoteTaskGatewayError("company not found: " + company_id, "NOT_FOUND")
        return company

    def _target_agent_ids(self, company: dict[str, Any], raw_value: Any) -> list[str]:
        requested = _string_list(raw_value)
        if not requested:
            requested = ["operations_manager"]
        agents = company.get("agents") if isinstance(company.get("agents"), dict) else {}
        missing = [agent_id for agent_id in requested if agent_id not in agents]
        if missing:
            raise RemoteTaskGatewayError(
                "unknown target agent(s): " + ", ".join(missing),
                "UNKNOWN_TARGET_AGENT",
                details={"target_agent_ids": requested, "unknown": missing},
            )
        return requested

    def _snapshot(self, task_id: str, *, company_id: str | None = None) -> dict[str, Any]:
        task = self._get_task_or_raise(task_id, company_id=company_id)
        company_id = str(task["company_id"])
        run_links = self._synced_run_links(company_id, task_id)
        raw_agent_runs = [
            run
            for run in (self.run_store.get_run(str(link.get("run_id") or "")) for link in run_links)
            if isinstance(run, dict)
        ]
        agent_runs = [_remote_run_dto(run) for run in raw_agent_runs]
        run_ids = _run_ids(run_links)
        inbox = [
            item
            for item in self.runtime_store.list_inbox(company_id, limit=500)
            if str(item.get("task_id") or "") == task_id or (item.get("run_id") and str(item.get("run_id")) in run_ids)
        ][:100]
        waiting_approvals = self._pending_approvals(run_ids)
        state = self._normalized_state(task, run_links, raw_agent_runs, waiting_approvals)
        if state not in REMOTE_STATES:
            state = "queued"
        task = self._persist_normalized_task_state(company_id, task_id, state, task)
        self._sync_task_state_event(company_id, task_id, state, task)
        task = self._get_task_or_raise(task_id, company_id=company_id)
        return {
            "remote_task_id": task_id,
            "company_id": company_id,
            "task_id": task_id,
            "message_id": task.get("message_id"),
            "thread_id": task.get("thread_id"),
            "state": state,
            "task": task,
            "run_links": run_links,
            "agent_runs": agent_runs,
            "inbox": inbox,
            "waiting_approvals": waiting_approvals,
            "updated_at": task.get("updated_at"),
            "next_poll_ms": NEXT_POLL_MS,
        }

    def _get_task_or_raise(self, task_id: str, *, company_id: Any = None) -> dict[str, Any]:
        clean_company_id = _safe_company_id(company_id) if company_id else None
        task = self.runtime_store.get_task(task_id, company_id=clean_company_id)
        if task is None:
            raise RemoteTaskGatewayError("remote task not found: " + task_id, "NOT_FOUND")
        return task

    def _start_dispatch_thread(
        self,
        company_id: str,
        task_id: str,
        context: dict[str, Any],
        client: dict[str, Any],
    ) -> None:
        thread = threading.Thread(
            target=self._dispatch_task_background,
            args=(company_id, task_id, dict(context or {}), dict(client or {})),
            name=f"remote-task-dispatch-{task_id}",
            daemon=True,
        )
        thread.start()

    def _dispatch_task_background(
        self,
        company_id: str,
        task_id: str,
        context: dict[str, Any],
        client: dict[str, Any],
    ) -> None:
        try:
            dispatch_result = self.run_dispatcher.dispatch_task(
                company_id,
                task_id,
                requested_by="remote_gateway",
                policy={"mode": "agent_delegate", "direct_tool_execution": False},
                context={**context, "remote_gateway": True, "remote_client": client},
            )
            if not isinstance(dispatch_result, dict):
                return
            task = dispatch_result.get("task") if isinstance(dispatch_result.get("task"), dict) else {}
            routes = _routes_from_run_links(dispatch_result.get("run_links"))
            self._append_event(
                company_id,
                task_id,
                "task.dispatched",
                "Remote task dispatched",
                status=str((dispatch_result.get("dispatch") or {}).get("status") or task.get("status") or "running"),
                routes=routes,
            )
            for link in dispatch_result.get("run_links") or []:
                if isinstance(link, dict):
                    self._append_run_status_event(company_id, task_id, link)
        except Exception as exc:
            self.runtime_store.update_task(
                task_id,
                {"status": "blocked", "metadata": {"dispatch_error": str(exc)}},
                company_id=company_id,
            )
            self._append_event(
                company_id,
                task_id,
                "task.dispatch_failed",
                "Remote task dispatch failed",
                status="blocked",
                error=str(exc),
            )

    def _synced_run_links(self, company_id: str, task_id: str) -> list[dict[str, Any]]:
        links = self.runtime_store.list_run_links(company_id, task_id=task_id, limit=1000)
        changed = False
        for link in links:
            run_id = str(link.get("run_id") or "")
            if not run_id:
                continue
            run = self.run_store.get_run(run_id)
            if isinstance(run, dict):
                status = str(run.get("status") or link.get("status") or "").lower()
                heartbeat = run.get("heartbeat_at") if isinstance(run.get("heartbeat_at"), str) else None
                if status and status != str(link.get("status") or "").lower():
                    self.runtime_store.update_run_link_status(run_id, status, heartbeat_at=heartbeat)
                    changed = True
        if changed:
            links = self.runtime_store.list_run_links(company_id, task_id=task_id, limit=1000)
        for link in links:
            self._append_run_status_event(company_id, task_id, link)
        return links

    def _pending_approvals(self, run_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = [run_id for run_id in _string_list(list(run_ids)) if run_id]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.run_store.conn.execute(
            f"""
            SELECT approval_id, run_id, tool_call_id, reviewer, status, reason, requested_at, decided_at
            FROM agent_approvals
            WHERE status = 'pending' AND run_id IN ({placeholders})
            ORDER BY requested_at ASC
            LIMIT 100
            """,
            ids,
        ).fetchall()
        return [_remote_approval_dto(dict(row)) for row in rows]

    def _normalized_state(
        self,
        task: dict[str, Any],
        run_links: list[dict[str, Any]],
        agent_runs: list[dict[str, Any]],
        waiting_approvals: list[dict[str, Any]],
    ) -> str:
        task_status = str(task.get("status") or "").lower()
        statuses = {
            str(item.get("status") or "").lower()
            for item in [*run_links, *agent_runs]
            if isinstance(item, dict) and item.get("status")
        }
        completed_statuses = {"completed", "complete", "done", "success", "succeeded"}
        blocked_statuses = {"blocked", "failed", "error", "cancel_failed"}
        if task_status in {"cancelled", "canceled"} or "cancelled" in statuses or "canceled" in statuses:
            return "cancelled"
        if task_status in {"completed", "complete", "done"}:
            return "completed"
        if run_links and statuses and all(status in completed_statuses for status in statuses):
            return "completed"
        if task_status == "stale" or "stale" in statuses:
            return "stale"
        if waiting_approvals or task_status in {"waiting_approval", "waiting_user_input"} or statuses & {"waiting_approval", "waiting_user_input"}:
            return "waiting_approval"
        if task_status in blocked_statuses or statuses & blocked_statuses:
            return "blocked"
        if task_status in {"running", "assigned"} or statuses & {"created", "queued", "running", "paused", "resumable"}:
            return "running"
        return "queued"

    def _persist_normalized_task_state(
        self,
        company_id: str,
        task_id: str,
        state: str,
        task: dict[str, Any],
    ) -> dict[str, Any]:
        task_status = str(task.get("status") or "").lower()
        persisted_state = "cancelled" if state == "cancelled" else state
        if state not in {"completed", "blocked", "cancelled", "stale"}:
            return task
        if task_status == persisted_state:
            return task
        return self.runtime_store.update_task(
            task_id,
            {"status": persisted_state, "metadata": {"remote_state_synced_at": utc_now()}},
            company_id=company_id,
        ) or task

    def _active_run_count(self, run_links: list[dict[str, Any]]) -> int:
        active: set[str] = set()
        for link in run_links:
            run_id = str(link.get("run_id") or "")
            if not run_id:
                continue
            run = self.run_store.get_run(run_id)
            status = str((run or {}).get("status") or link.get("status") or "").lower()
            if status in ACTIVE_RUN_STATUSES:
                active.add(run_id)
        return len(active)

    def _remote_events(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        remote = metadata.get(REMOTE_METADATA_KEY) if isinstance(metadata.get(REMOTE_METADATA_KEY), dict) else {}
        events = remote.get("events") if isinstance(remote.get("events"), list) else []
        return [event for event in events if isinstance(event, dict)]

    def _append_event(self, company_id: str, task_id: str, event_type: str, message: str, **fields: Any) -> None:
        task = self.runtime_store.get_task(task_id, company_id=company_id)
        if task is None:
            return
        metadata = dict(task.get("metadata") if isinstance(task.get("metadata"), dict) else {})
        remote = dict(metadata.get(REMOTE_METADATA_KEY) if isinstance(metadata.get(REMOTE_METADATA_KEY), dict) else {})
        events = [event for event in remote.get("events", []) if isinstance(event, dict)]
        next_seq = int(remote.get("next_event_seq") or len(events) + 1)
        event = {
            "cursor": f"{next_seq:06d}",
            "type": str(event_type),
            "message": str(message),
            "task_id": task_id,
            "created_at": utc_now(),
            **redact_sensitive(fields),
        }
        events.append(event)
        remote["events"] = events
        remote["next_event_seq"] = next_seq + 1
        self.runtime_store.update_task(task_id, {"metadata": {REMOTE_METADATA_KEY: remote}}, company_id=company_id)

    def _append_run_status_event(self, company_id: str, task_id: str, link: dict[str, Any]) -> None:
        run_id = str(link.get("run_id") or "")
        if not run_id:
            return
        status = str(link.get("status") or "running")
        task = self.runtime_store.get_task(task_id, company_id=company_id)
        if task is None:
            return
        if _last_event_value(self._remote_events(task), "run.status", run_id=run_id, key="status") == status:
            return
        self._append_event(
            company_id,
            task_id,
            "run.status",
            "Run status updated",
            run_id=run_id,
            agent_id=link.get("agent_id"),
            status=status,
            heartbeat_at=link.get("heartbeat_at"),
        )

    def _sync_task_state_event(self, company_id: str, task_id: str, state: str, task: dict[str, Any]) -> None:
        if _last_event_value(self._remote_events(task), "task.state", key="status") == state:
            return
        events = self._remote_events(task)
        if not events:
            return
        if events[-1].get("type") == "task.created" and events[-1].get("status") == state:
            return
        self._append_event(company_id, task_id, "task.state", "Remote task state updated", status=state)


def _require_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RemoteTaskGatewayError("input_data must be a dict", "INVALID_INPUT")
    return value


def _remote_input(payload: dict[str, Any]) -> str:
    text = str(payload.get("input") or "").strip()
    if not text:
        raise RemoteTaskGatewayError("input is required", "INVALID_INPUT")
    if len(text) > MAX_REMOTE_INPUT_CHARS:
        raise RemoteTaskGatewayError(
            f"input must be {MAX_REMOTE_INPUT_CHARS} characters or fewer",
            "INPUT_TOO_LARGE",
            details={"max_chars": MAX_REMOTE_INPUT_CHARS},
        )
    return text


def _safe_company_id(value: Any) -> str:
    company_id = str(value or "").strip()
    if not company_id or not SAFE_REMOTE_ID_RE.match(company_id):
        raise RemoteTaskGatewayError("invalid company_id", "INVALID_INPUT")
    return company_id


def _safe_task_id(value: Any) -> str:
    task_id = str(value or "").strip()
    if not task_id or not SAFE_REMOTE_ID_RE.match(task_id):
        raise RemoteTaskGatewayError("invalid task_id", "INVALID_INPUT")
    return task_id


def _string_list(value: Any) -> list[str]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _clean_text(value: Any, *, default: str = "", limit: int = 160) -> str:
    text = str(value or "").strip() or default
    return text[:limit]


def _remote_run_dto(run: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "run_id",
        "agent_id",
        "status",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "heartbeat_at",
    )
    dto = {key: run.get(key) for key in allowed if run.get(key) not in (None, "")}
    if run.get("error") not in (None, ""):
        dto["error"] = _clean_text(redact_sensitive(run.get("error")), limit=500)
    return dto


def _remote_approval_dto(approval: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "approval_id",
        "run_id",
        "tool_call_id",
        "reviewer",
        "status",
        "requested_at",
        "decided_at",
    )
    dto = {key: approval.get(key) for key in allowed if approval.get(key) not in (None, "")}
    if approval.get("reason") not in (None, ""):
        dto["reason"] = _clean_text(redact_sensitive(approval.get("reason")), limit=500)
    return dto


def _title_from_input(input_text: str) -> str:
    first_line = input_text.strip().splitlines()[0] if input_text.strip() else "Remote task"
    return first_line[:120] or "Remote task"


def _sender_id(client: dict[str, Any]) -> str:
    kind = _clean_text(client.get("kind"), default="remote", limit=32)
    name = _clean_text(client.get("name"), default="", limit=48)
    return "remote:" + kind + (":" + name if name else "")


def _int_between(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _run_ids(run_links: Iterable[dict[str, Any]]) -> list[str]:
    return _string_list([link.get("run_id") for link in run_links if isinstance(link, dict)])


def _routes_from_run_links(run_links: Any) -> list[dict[str, Any]]:
    if not isinstance(run_links, list):
        return []
    routes: list[dict[str, Any]] = []
    for link in run_links:
        if not isinstance(link, dict):
            continue
        routes.append(
            {
                "route": "agent.delegate",
                "run_id": link.get("run_id"),
                "agent_id": link.get("agent_id"),
                "status": link.get("status"),
            }
        )
    return routes


def _last_event_value(events: list[dict[str, Any]], event_type: str, *, key: str, run_id: str | None = None) -> str | None:
    for event in reversed(events):
        if event.get("type") != event_type:
            continue
        if run_id is not None and str(event.get("run_id") or "") != run_id:
            continue
        return str(event.get(key)) if event.get(key) is not None else None
    return None
