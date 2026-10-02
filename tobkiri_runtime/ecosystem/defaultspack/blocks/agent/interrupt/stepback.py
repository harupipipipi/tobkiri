"""block: blocks.agent.interrupt.stepback

Request to roll back one step and redo.

The agent will discard the last step's result and re-process from the
previous state.  This works by:
  1. Removing the last step from the execution's step list.
  2. Removing the corresponding messages from the conversation.
  3. Injecting a high-priority instruction telling the AI to try a
     different approach.

input_data:
    execution_id : str  (required)
    reason       : str  (optional) — why the user wants to redo
"""



from blocks._common import ok, error, timestamp
from blocks.agent._state import get_engine
from domain.agent.interrupt_manager import (
    get_interrupt_manager,
    get_priority_queue,
)

_ACTIVE_STATUSES = ("running", "waiting_approval")


def _try_rollback_step(engine, execution_id):
    """Attempt to remove the last step from the execution.

    Returns (success: bool, removed_step_dict: dict | None, error_msg: str | None).
    We access the engine's internal _executions dict (read-only pattern used by
    engine.status already).
    """
    execution = engine._executions.get(execution_id)
    if execution is None:
        return False, None, "execution not found in engine"

    if len(execution.steps) == 0:
        return False, None, "no steps to roll back"

    removed = execution.steps.pop()
    removed_dict = removed.to_dict()

    # Also try to remove matching messages from the conversation tail.
    # Heuristic: remove messages added after the rolled-back step's creation time.
    # A simpler approach: remove the last 1-2 messages if they look like they
    # came from the rolled-back step.
    step_type = removed_dict.get("step_type", "")
    messages_removed = 0
    if step_type == "tool_call" and execution.pending_tool_call is not None:
        # Remove pending tool call — it hasn't been executed yet
        execution.pending_tool_call = None
        execution.status = "running"
        messages_removed = 0  # no messages were added for unapproved tool calls
    elif step_type in ("response", "tool_result", "instruction_injected"):
        # Remove the last message(s) that correspond to this step
        if execution.messages and len(execution.messages) > 1:
            execution.messages.pop()
            messages_removed = 1
    elif step_type == "error":
        execution.status = "running"
        execution.error = None

    # Update current_step counter
    execution.current_step = len(execution.steps)
    execution.updated_at = timestamp()

    return True, removed_dict, None


def run(input_data, context):
    execution_id = input_data.get("execution_id") if isinstance(input_data, dict) else None
    if not execution_id:
        return error("execution_id is required")

    reason = input_data.get("reason", "") if isinstance(input_data, dict) else ""

    engine = get_engine(execution_id)
    if not engine:
        return error("execution not found")

    engine_status = engine.status(execution_id)
    current_status = engine_status.get("status", "unknown")

    mgr = get_interrupt_manager()
    is_paused = mgr.is_paused(execution_id)

    if current_status not in _ACTIVE_STATUSES and not is_paused and current_status != "error":
        return error(
            "cannot stepback execution with status: " + current_status
        )

    # Perform rollback
    success, removed_step, err_msg = _try_rollback_step(engine, execution_id)
    if not success:
        return error(err_msg if err_msg else "stepback failed")

    # Record the stepback in interrupt manager
    mgr.request_stepback(execution_id)

    # Inject a high-priority instruction to try differently
    queue = get_priority_queue()
    redo_instruction = "[STEPBACK] The user rolled back the last step"
    if reason:
        redo_instruction += " because: " + reason
    redo_instruction += ". Please try a different approach for this step."
    entry = queue.add(execution_id, redo_instruction, "high")

    return ok({
        "execution_id": execution_id,
        "status": "stepback_applied",
        "removed_step": removed_step,
        "redo_instruction_id": entry["id"],
        "stepback_count": mgr.get_state(execution_id).get("stepback_count", 0),
    })
