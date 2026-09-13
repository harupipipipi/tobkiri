import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from blocks.agent._state import set_multi_session
from domain.company.message_router import CompanySlackRuntime
from domain.company.models import DEFAULT_COMPANY_ID, DEFAULT_COMPANY_NAME, normalize_agent
from domain.company.store import CompanyStore


def run(input_data, context, *, settings_owner=None):
    """Compatibility wrapper for the legacy multi-agent execute endpoint."""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")
    context = context or {}
    task = str(input_data.get("task") or "").strip()
    if not task:
        return error("task is required")

    company_id = str(input_data.get("company_id") or DEFAULT_COMPANY_ID)
    agents = _normalize_legacy_agents(input_data.get("agents"))
    store = CompanyStore()
    if store.get_company(company_id) is None:
        store.ensure_company(
            company_id=company_id,
            name=DEFAULT_COMPANY_NAME,
            description="Compatibility team workspace for legacy multi-agent requests.",
            agents=agents or None,
            metadata={"compatibility": "agent.multi"},
        )
    else:
        for agent in agents:
            store.upsert_agent(company_id, agent)

    target_agent_ids = [agent["agent_id"] for agent in agents] if agents else ["operations_manager"]
    result = CompanySlackRuntime(
        company_store=store,
        settings_owner=settings_owner,
    ).post_message(
        company_id,
        content=task,
        sender_id=str(input_data.get("sender_id") or "legacy_multi"),
        channel_id=str(input_data.get("channel_id") or "ops-company"),
        target_agent_ids=target_agent_ids,
        metadata={
            "compatibility_endpoint": "/api/agent/multi/execute",
            "legacy_multi": True,
        },
        context=context if isinstance(context, dict) else {},
    )
    if result is None:
        return error("team workspace not found", "NOT_FOUND")

    session_id = str((result.get("message") or {}).get("thread_id") or (result.get("message") or {}).get("message_id") or "")
    session = {
        "session_id": session_id,
        "company_id": company_id,
        "thread_id": session_id,
        "status": "routed",
        "legacy": True,
        "runtime": "CompanySlackRuntime",
    }
    set_multi_session(session_id, session)
    return ok(
        {
            "session_id": session_id,
            "status": "routed",
            "result": result,
            "turn_results": [],
            "workspace": {},
            "deprecation_warning": (
                "/api/agent/multi/* is a compatibility wrapper. "
                "CompanySlackRuntime routes messages, mentions, tasks, and AgentEngine runs asynchronously."
            ),
        }
    )


def _normalize_legacy_agents(value):
    if not isinstance(value, list):
        return []
    agents = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("agent_id") or item.get("role") or "").strip()
        if not name:
            continue
        agent_id = _slug(name)
        agents.append(
            normalize_agent(
                {
                    "agent_id": agent_id,
                    "role_key": _slug(str(item.get("role") or name)),
                    "agent_name": name,
                    "display_name": name,
                    "model": str(item.get("model") or "default"),
                    "allowed_tools": list(item.get("tools") if isinstance(item.get("tools"), list) else []),
                    "aliases": [name, str(item.get("role") or "")],
                    "system_prompt": item.get("system_prompt") or item.get("role") or "",
                }
            )
        )
    return agents


def _slug(value):
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    return slug or "agent"
