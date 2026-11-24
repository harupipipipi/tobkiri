"""block: blocks.agent.interrupt.add

Add an interrupt instruction with priority (high / normal / low) to a running
agent execution.

input_data:
    execution_id : str   (required)
    instruction  : str   (required)
    priority     : str   (optional, default "normal") — "high" | "normal" | "low"

high  → processed immediately (injected before current AI call completes)
normal → processed after the current step finishes
low   → added to the back of the queue
"""



from blocks._common import ok, error
from blocks.agent._state import get_engine
from domain.agent.interrupt_manager import get_priority_queue, get_interrupt_manager, VALID_PRIORITIES

_ACTIVE_STATUSES = ("running", "waiting_approval")


def run(input_data, context):
    execution_id = input_data.get("execution_id") if isinstance(input_data, dict) else None
    if not execution_id:
        return error("execution_id is required")

    instruction = input_data.get("instruction") if isinstance(input_data, dict) else None
    if not instruction:
        return error("instruction is required")

    engine = get_engine(execution_id)
    if not engine:
        return error("execution not found")

    # Check that execution is active
    engine_status = engine.status(execution_id)
    current_status = engine_status.get("status", "unknown")

    # Also allow interrupts if the execution is paused (user may want to
    # queue instructions while paused)
    mgr = get_interrupt_manager()
    is_paused = mgr.is_paused(execution_id)

    if current_status not in _ACTIVE_STATUSES and not is_paused:
        return error(
            "execution is not active (current status: "
            + current_status
            + "); instructions can only be added to running, waiting_approval, or paused executions"
        )

    priority = input_data.get("priority", "normal") if isinstance(input_data, dict) else "normal"
    if priority not in VALID_PRIORITIES:
        priority = "normal"

    queue = get_priority_queue()
    entry = queue.add(execution_id, instruction, priority)

    return ok({
        "instruction_id": entry["id"],
        "execution_id": execution_id,
        "instruction": instruction,
        "priority": priority,
        "status": "queued",
        "created_at": entry["created_at"],
    })
