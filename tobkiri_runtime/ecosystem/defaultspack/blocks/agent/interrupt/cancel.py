"""block: blocks.agent.interrupt.cancel

Cancel a pending instruction by instruction_id.

input_data:
    execution_id   : str  (required)
    instruction_id : str  (required)  — the ID returned by interrupt/add
"""



from blocks._common import ok, error
from blocks.agent._state import get_engine
from domain.agent.interrupt_manager import get_priority_queue


def run(input_data, context):
    execution_id = input_data.get("execution_id") if isinstance(input_data, dict) else None
    if not execution_id:
        return error("execution_id is required")

    instruction_id = input_data.get("instruction_id") if isinstance(input_data, dict) else None
    if not instruction_id:
        return error("instruction_id is required")

    engine = get_engine(execution_id)
    if not engine:
        return error("execution not found")

    queue = get_priority_queue()
    cancelled = queue.cancel(instruction_id)

    if not cancelled:
        return error(
            "instruction not found or already consumed/cancelled",
            code="NOT_FOUND",
        )

    return ok({
        "instruction_id": instruction_id,
        "execution_id": execution_id,
        "status": "cancelled",
    })
