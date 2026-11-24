"""block: blocks.agent.interrupt.queue

Manage the instruction queue for an execution.

GET  → list pending instructions
PUT  → reorder or change priority of instructions

input_data for GET (list):
    execution_id    : str  (required)
    include_all     : bool (optional, default False) — include consumed/cancelled

input_data for PUT (modify):
    execution_id    : str  (required)

    One of (or both):
      reorder          : list[str]  (optional) — ordered list of instruction IDs
      change_priority  : dict       (optional) — {"instruction_id": "new_priority"}
"""



from blocks._common import ok, error
from blocks.agent._state import get_engine
from domain.agent.interrupt_manager import get_priority_queue, VALID_PRIORITIES


def run(input_data, context):
    execution_id = input_data.get("execution_id") if isinstance(input_data, dict) else None
    if not execution_id:
        return error("execution_id is required")

    engine = get_engine(execution_id)
    if not engine:
        return error("execution not found")

    queue = get_priority_queue()

    # Detect operation type via presence of mutation fields
    is_mutation = False
    if isinstance(input_data, dict):
        if "reorder" in input_data or "change_priority" in input_data:
            is_mutation = True

    if not is_mutation:
        # ----- LIST mode -----
        include_all = input_data.get("include_all", False) if isinstance(input_data, dict) else False
        if include_all:
            entries = queue.list_all(execution_id)
        else:
            entries = queue.list_pending(execution_id)
        return ok({
            "execution_id": execution_id,
            "count": len(entries),
            "instructions": entries,
        })

    # ----- MUTATION mode -----
    results = {}

    # Handle priority changes first (before reorder, so reorder uses new priorities)
    change_priority = input_data.get("change_priority")
    if isinstance(change_priority, dict) and change_priority:
        priority_results = {}
        for inst_id, new_pri in change_priority.items():
            if new_pri not in VALID_PRIORITIES:
                priority_results[inst_id] = {
                    "status": "error",
                    "message": "invalid priority: " + str(new_pri),
                }
                continue
            updated = queue.change_priority(inst_id, new_pri)
            if updated:
                priority_results[inst_id] = {
                    "status": "updated",
                    "new_priority": new_pri,
                }
            else:
                priority_results[inst_id] = {
                    "status": "error",
                    "message": "not found or not pending",
                }
        results["priority_changes"] = priority_results

    # Handle reorder
    reorder = input_data.get("reorder")
    if isinstance(reorder, list) and reorder:
        ordered = queue.reorder(execution_id, reorder)
        results["reordered"] = ordered

    # Return current state
    entries = queue.list_pending(execution_id)
    results["execution_id"] = execution_id
    results["count"] = len(entries)
    results["instructions"] = entries

    return ok(results)
