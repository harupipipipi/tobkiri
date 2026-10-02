"""block: blocks.agent.interrupt.redirect

Redirect (change the goal of) a running agent execution.

The new goal will be injected as a high-priority system instruction at the
next AI completion step, overriding the original task direction.

input_data:
    execution_id : str  (required)
    new_goal     : str  (required)
"""



from blocks._common import ok, error
from blocks.agent._state import get_engine
from domain.agent.interrupt_manager import (
    get_interrupt_manager,
    get_priority_queue,
)

_ACTIVE_STATUSES = ("running", "waiting_approval")


def run(input_data, context):
    execution_id = input_data.get("execution_id") if isinstance(input_data, dict) else None
    if not execution_id:
        return error("execution_id is required")

    new_goal = input_data.get("new_goal") if isinstance(input_data, dict) else None
    if not new_goal:
        return error("new_goal is required")

    engine = get_engine(execution_id)
    if not engine:
        return error("execution not found")

    engine_status = engine.status(execution_id)
    current_status = engine_status.get("status", "unknown")

    mgr = get_interrupt_manager()
    is_paused = mgr.is_paused(execution_id)

    if current_status not in _ACTIVE_STATUSES and not is_paused:
        return error(
            "cannot redirect execution with status: " + current_status
        )

    # Store the redirect in InterruptManager
    state = mgr.redirect(execution_id, new_goal)

    # Also inject a high-priority instruction so the AI processes the goal
    # change at the next opportunity
    queue = get_priority_queue()
    redirect_instruction = (
        "[GOAL CHANGE] The user has changed the objective of this task. "
        "Abandon the current approach and pursue the following new goal instead: "
        + new_goal
    )
    entry = queue.add(execution_id, redirect_instruction, "high")

    return ok({
        "execution_id": execution_id,
        "status": "redirect_queued",
        "new_goal": new_goal,
        "instruction_id": entry["id"],
        "redirect_state": state.get("redirect"),
    })
