"""handler: defaults.agent.add_instruction

Add a runtime instruction to a running agent execution.
The instruction will be injected into the agent's context
at the next AI completion step.

input_data:
    execution_id: str (required)
    instruction: str (required)
    priority: str (optional, default "normal") — "normal" or "urgent"
"""


from blocks._common import ok, error
from blocks.agent._state import get_engine, get_instruction_queue

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

    # エンジンの状態を確認し、active でなければ拒否する
    engine_status = engine.status(execution_id)
    current_status = engine_status.get("status", "unknown")
    if current_status not in _ACTIVE_STATUSES:
        return error(
            "execution is not active (current status: "
            + current_status
            + "); instructions can only be added to running or waiting_approval executions"
        )

    priority = input_data.get("priority", "normal") if isinstance(input_data, dict) else "normal"
    if priority not in ("normal", "urgent"):
        priority = "normal"

    queue = get_instruction_queue()
    entry = queue.add_instruction(execution_id, instruction, priority)

    return ok({
        "instruction_id": entry["id"],
        "execution_id": execution_id,
        "priority": priority,
        "status": "queued",
    })
