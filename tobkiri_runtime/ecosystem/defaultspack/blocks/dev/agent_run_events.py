

from blocks._common import error, ok
from domain.agent_runtime.run_store import AgentRunStore


def run(input_data, context=None):
    run_id = input_data.get("run_id") or input_data.get("id")
    if not run_id:
        return error("run_id is required", "INVALID_INPUT")
    return ok({"events": AgentRunStore().events(run_id, limit=int(input_data.get("limit", 100)))})
