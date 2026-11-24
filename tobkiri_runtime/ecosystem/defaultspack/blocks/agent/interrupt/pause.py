"""block: blocks.agent.interrupt.pause

Pause a running agent execution.  The agent will not proceed to the next step
until resumed.

input_data:
    execution_id : str  (required)
"""



from blocks._common import ok, error
from blocks.agent._state import get_engine
from domain.agent.interrupt_manager import get_interrupt_manager

_PAUSABLE_STATUSES = ("running", "waiting_approval")


def run(input_data, context):
    execution_id = input_data.get("execution_id") if isinstance(input_data, dict) else None
    if not execution_id:
        return error("execution_id is required")

    engine = get_engine(execution_id)
    if not engine:
        return error("execution not found")

    engine_status = engine.status(execution_id)
    current_status = engine_status.get("status", "unknown")

    if current_status not in _PAUSABLE_STATUSES:
        return error(
            "cannot pause execution with status: " + current_status
        )

    mgr = get_interrupt_manager()
    state = mgr.pause(execution_id)

    if state.get("already_paused"):
        return ok({
            "execution_id": execution_id,
            "status": "already_paused",
            "paused_at": state.get("paused_at"),
        })

    return ok({
        "execution_id": execution_id,
        "status": "paused",
        "paused_at": state.get("paused_at"),
    })
