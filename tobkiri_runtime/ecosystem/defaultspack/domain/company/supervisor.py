from __future__ import annotations

from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.agent_runtime.run_store import AgentRunStore

from .models import DEFAULT_COMPANY_ID
from .run_dispatcher import CompanyRunDispatcher
from .runtime_store import CompanyRuntimeStore
from .summary_worker import CompanySummaryWorker


class CompanySupervisor:
    """Operations manager tick for team workspace health."""

    def __init__(
        self,
        *,
        runtime_store: CompanyRuntimeStore | None = None,
        run_store: AgentRunStore | None = None,
        run_dispatcher: CompanyRunDispatcher | None = None,
        summary_worker: CompanySummaryWorker | None = None,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> None:
        self.runtime_store = runtime_store or CompanyRuntimeStore()
        self.run_store = run_store or AgentRunStore()
        self.run_dispatcher = run_dispatcher or CompanyRunDispatcher(
            runtime_store=self.runtime_store,
            settings_owner=settings_owner,
        )
        self.summary_worker = summary_worker or CompanySummaryWorker(runtime_store=self.runtime_store, run_store=self.run_store)

    def tick(
        self,
        company_id: str = DEFAULT_COMPANY_ID,
        *,
        stale_after_seconds: int = 600,
        auto_dispatch: bool = False,
        auto_summarize: bool = False,
        auto_mark_stale: bool = False,
    ) -> dict[str, Any]:
        open_tasks = self.runtime_store.list_open_tasks(company_id)
        unassigned_mentions = self.runtime_store.list_unassigned_mentions(company_id)
        run_links = self.runtime_store.list_run_links(company_id, limit=10000)
        link_by_run_id = {str(link.get("run_id")): link for link in run_links if link.get("run_id")}
        company_run_ids = list(link_by_run_id)
        stale_runs = self.run_store.list_stale(stale_after_seconds=stale_after_seconds, run_ids=company_run_ids)
        waiting_approvals = self.run_store.list_waiting_approval(run_ids=company_run_ids)
        failed_runs = self.run_store.list_runs(status="error", run_ids=company_run_ids, limit=50) + self.run_store.list_runs(
            status="failed",
            run_ids=company_run_ids,
            limit=50,
        )
        dirty_summaries = self.runtime_store.list_dirty_summaries(company_id)

        actions: list[dict[str, Any]] = []
        performed_actions: list[dict[str, Any]] = []
        for task in open_tasks:
            actions.append({"type": "open_task", "task_id": task.get("task_id"), "status": task.get("status")})
        for item in unassigned_mentions:
            actions.append({"type": "unassigned_mention", "inbox_id": item.get("inbox_id"), "content": item.get("content")})
        for run in stale_runs:
            link = link_by_run_id.get(str(run.get("run_id") or ""))
            actions.append({"type": "stale_run", "run_id": run.get("run_id"), "agent_id": run.get("agent_id"), "task_id": (link or {}).get("task_id")})
        for run in waiting_approvals:
            actions.append({"type": "waiting_approval", "run_id": run.get("run_id"), "agent_id": run.get("agent_id")})
        for run in failed_runs:
            actions.append({"type": "failed_run", "run_id": run.get("run_id"), "error": run.get("error")})
        for summary in dirty_summaries:
            actions.append({"type": "dirty_summary", "scope_type": summary.get("scope_type"), "scope_id": summary.get("scope_id")})

        if auto_dispatch:
            for task in open_tasks:
                if str(task.get("status") or "") != "queued" or not task.get("target_agent_ids"):
                    continue
                dispatch = self.run_dispatcher.dispatch_task(company_id, str(task["task_id"]), requested_by="operations_manager")
                performed_actions.append(
                    {
                        "type": "dispatch_task",
                        "task_id": task.get("task_id"),
                        "status": (dispatch or {}).get("dispatch", {}).get("status") if isinstance(dispatch, dict) else None,
                    }
                )
        if auto_mark_stale:
            for run in stale_runs:
                link = link_by_run_id.get(str(run.get("run_id") or ""))
                performed = self._mark_stale_run(company_id, run, link)
                if performed:
                    performed_actions.append(performed)
        if auto_summarize:
            for summary in dirty_summaries:
                refreshed = self.summary_worker.summarize_scope(company_id, str(summary["scope_type"]), str(summary["scope_id"]))
                performed_actions.append(
                    {
                        "type": "summarize",
                        "scope_type": refreshed.get("scope_type"),
                        "scope_id": refreshed.get("scope_id"),
                    }
                )

        if actions:
            self.runtime_store.add_inbox_item(
                company_id,
                agent_id="operations_manager",
                kind="supervisor_tick",
                content="Supervisor found " + str(len(actions)) + " team workspace item(s).",
                priority="normal",
                metadata={"actions": actions},
            )

        return {
            "company_id": company_id,
            "open_tasks": open_tasks,
            "unassigned_mentions": unassigned_mentions,
            "stale_runs": stale_runs,
            "waiting_approvals": waiting_approvals,
            "failed_runs": failed_runs,
            "dirty_summaries": dirty_summaries,
            "actions": actions,
            "performed_actions": performed_actions,
        }

    def _mark_stale_run(self, company_id: str, run: dict[str, Any], link: dict[str, Any] | None) -> dict[str, Any] | None:
        run_id = str(run.get("run_id") or "")
        if not run_id:
            return None
        self.run_store.touch(run_id, status="stale", event_type="company_supervisor.stale", payload={"company_id": company_id})
        self.runtime_store.update_run_link_status(run_id, "stale")
        task_id = str((link or {}).get("task_id") or "")
        if task_id:
            self.runtime_store.update_task(task_id, {"status": "stale"}, company_id=company_id)
        self.runtime_store.add_inbox_item(
            company_id,
            agent_id="operations_manager",
            run_id=run_id,
            task_id=task_id or None,
            kind="stale_run",
            content="Run " + run_id + " is stale and needs operations manager review.",
            status="open",
            priority="high",
            metadata={"run": run, "link": link or {}},
        )
        return {"type": "mark_stale_run", "run_id": run_id, "task_id": task_id or None}
