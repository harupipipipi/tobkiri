
from blocks._common import ok, error
from domain.agent.engine import AgentEngine
from blocks.agent._state import set_engine


def run(input_data, context):
    task = input_data.get("task") if isinstance(input_data, dict) else None
    if not task:
        return error("task is required")
    tools = input_data.get("tools", [])
    model = input_data.get("model", "default")
    system_prompt = input_data.get("system_prompt", None)
    engine = AgentEngine()
    result = engine.plan(task, tools, model, system_prompt, context)
    execution_id = result.get("execution_id", "")
    set_engine(execution_id, engine)
    return ok(result)
