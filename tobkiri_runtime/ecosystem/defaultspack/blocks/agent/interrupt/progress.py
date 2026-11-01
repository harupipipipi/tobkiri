"""block: blocks.agent.interrupt.progress

Return real-time progress information about a running agent execution.

input_data:
    execution_id : str  (required)

Response includes:
    - current step info
    - progress estimate (percent)
    - next scheduled action
    - pause / redirect / stepback state
    - pending instruction count
"""



from blocks._common import ok, error
from blocks.agent._state import get_engine
from domain.agent.interrupt_manager import (
    get_interrupt_manager,
    get_priority_queue,
)


def run(input_data, context):
    execution_id = input_data.get("execution_id") if isinstance(input_data, dict) else None
    if not execution_id:
        return error("execution_id is required")

    engine = get_engine(execution_id)
    if not engine:
        return error("execution not found")

    mgr = get_interrupt_manager()
    progress = mgr.get_progress(execution_id, engine)

    if progress is None:
        return error("could not retrieve progress for execution")

    # Augment with queue info
    queue = get_priority_queue()
    pending = queue.list_pending(execution_id)

    progress["pending_instructions"] = {
        "count": len(pending),
        "high": sum(1 for p in pending if p["priority"] == "high"),
        "normal": sum(1 for p in pending if p["priority"] == "normal"),
        "low": sum(1 for p in pending if p["priority"] == "low"),
        "items": pending,
    }

    return ok(progress)
