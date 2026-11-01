

from blocks._common import error, ok
from domain.agent_runtime.run_store import AgentRunStore


def run(input_data, context=None):
    input_data = input_data if isinstance(input_data, dict) else {}
    run_id = input_data.get("run_id") or input_data.get("id")
    store = AgentRunStore()
    if run_id:
        run_data = store.get_run(run_id)
        if not run_data:
            return error("run not found", "NOT_FOUND")
        return ok(run_data)
    return ok({"runs": store.list_runs(limit=int(input_data.get("limit", 100)))})
