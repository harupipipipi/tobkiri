
from blocks._common import ok, error
from blocks.agent._state import get_engine, remove_engine


def run(input_data, context):
    execution_id = input_data.get("execution_id") if isinstance(input_data, dict) else None
    if not execution_id:
        return error("execution_id is required")
    engine = get_engine(execution_id)
    if not engine:
        return error("execution not found")
    result = engine.cancel(execution_id)
    remove_engine(execution_id)
    return ok(result)
