
from blocks._common import ok, error
from blocks.agent._state import get_engine


def run(input_data, context):
    execution_id = input_data.get("execution_id") if isinstance(input_data, dict) else None
    if not execution_id:
        return error("execution_id is required")
    reason = input_data.get("reason", "Rejected by user")
    engine = get_engine(execution_id)
    if not engine:
        return error("execution not found")
    result = engine.reject(execution_id, reason)
    return ok(result)
