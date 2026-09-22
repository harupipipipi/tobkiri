

from blocks._common import error, ok
from blocks.agent.interrupt.add import run as add_interrupt


def run(input_data, context=None):
    payload = input_data if isinstance(input_data, dict) else {}
    instruction = str(payload.get("instruction") or payload.get("guidance") or payload.get("message") or "").strip()
    execution_id = str(payload.get("execution_id") or payload.get("agent_run_id") or "").strip()
    if not execution_id:
        return error("execution_id is required", "INVALID_INPUT")
    if not instruction:
        return error("instruction is required", "INVALID_INPUT")
    result = add_interrupt(
        {
            "execution_id": execution_id,
            "instruction": instruction,
            "priority": str(payload.get("priority") or "high"),
        },
        context or {},
    )
    if isinstance(result, dict) and result.get("status") == "ok":
        data = dict(result.get("data") or {})
        data["guidance"] = True
        return ok(data)
    return result
