"""block: blocks.agent.interrupt.resume

Resume a paused agent execution.

input_data:
    execution_id : str  (required)
"""



from blocks._common import ok, error
from blocks.agent._state import get_engine
from domain.agent.interrupt_manager import get_interrupt_manager


def run(input_data, context):
    execution_id = input_data.get("execution_id") if isinstance(input_data, dict) else None
    if not execution_id:
        return error("execution_id is required")

    engine = get_engine(execution_id)
    if not engine:
        return error("execution not found")

    mgr = get_interrupt_manager()
    state = mgr.resume(execution_id)

    if state.get("already_running"):
        return ok({
            "execution_id": execution_id,
            "status": "already_running",
            "message": "execution is not paused",
        })

    return ok({
        "execution_id": execution_id,
        "status": "resumed",
        "resumed_at": state.get("resumed_at"),
    })
