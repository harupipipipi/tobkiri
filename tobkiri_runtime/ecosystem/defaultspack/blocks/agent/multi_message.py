import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from blocks.agent._state import get_multi_session, set_multi_session
from domain.company.message_router import CompanySlackRuntime
from domain.company.models import DEFAULT_COMPANY_ID
from domain.company.store import CompanyStore


def run(input_data, context, *, settings_owner=None):
    """Compatibility wrapper for posting into a CompanySlackRuntime thread."""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")
    session_id = str(input_data.get("session_id") or "").strip()
    if not session_id:
        return error("session_id is required")
    message = str(input_data.get("message") or "").strip()
    if not message:
        return error("message is required")

    session = get_multi_session(session_id) or {}
    company_id = str(input_data.get("company_id") or session.get("company_id") or DEFAULT_COMPANY_ID)
    if CompanyStore().get_company(company_id) is None:
        return error("team workspace not found: " + company_id, "NOT_FOUND")
    target_agent = str(input_data.get("target_agent") or "").strip()
    target_agent_ids = [_slug(target_agent)] if target_agent else None
    result = CompanySlackRuntime(settings_owner=settings_owner).post_message(
        company_id,
        content=message,
        sender_id=str(input_data.get("sender_id") or "legacy_multi"),
        channel_id=str(input_data.get("channel_id") or "ops-company"),
        thread_id=session_id,
        target_agent_ids=target_agent_ids,
        metadata={
            "compatibility_endpoint": "/api/agent/multi/{id}/message",
            "legacy_multi": True,
        },
        context=context if isinstance(context, dict) else {},
    )
    if result is None:
        return error("team workspace not found", "NOT_FOUND")
    if not session:
        set_multi_session(
            session_id,
            {
                "session_id": session_id,
                "company_id": company_id,
                "thread_id": session_id,
                "status": "routed",
                "legacy": True,
                "runtime": "CompanySlackRuntime",
            },
        )
    return ok(
        {
            "session_id": session_id,
            "status": "routed",
            "result": result,
            "deprecation_warning": (
                "/api/agent/multi/* is a compatibility wrapper; messages are routed through CompanySlackRuntime."
            ),
        }
    )


def _slug(value):
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    return slug or value
