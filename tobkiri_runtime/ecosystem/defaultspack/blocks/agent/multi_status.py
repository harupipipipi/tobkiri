

from blocks._common import error, ok
from blocks.agent._state import get_multi_session
from domain.company.models import DEFAULT_COMPANY_ID
from domain.company.runtime_store import CompanyRuntimeStore


def run(input_data, context):
    """Compatibility wrapper for reading a CompanySlackRuntime thread."""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")
    session_id = str(input_data.get("session_id") or "").strip()
    if not session_id:
        return error("session_id is required")
    session = get_multi_session(session_id) or {}
    company_id = str(input_data.get("company_id") or session.get("company_id") or DEFAULT_COMPANY_ID)
    store = CompanyRuntimeStore()
    thread = store.get_thread(session_id)
    messages, message_total = store.list_messages(company_id, thread_id=session_id, limit=100)
    tasks, task_total = store.list_tasks(company_id, thread_id=session_id, limit=100)
    return ok(
        {
            "session_id": session_id,
            "company_id": company_id,
            "thread": thread,
            "messages": messages,
            "message_total": message_total,
            "tasks": tasks,
            "task_total": task_total,
            "status": "routed" if thread else "not_found",
            "legacy": True,
            "runtime": "CompanySlackRuntime",
            "deprecation_warning": "/api/agent/multi/* is compatibility-only.",
        }
    )
