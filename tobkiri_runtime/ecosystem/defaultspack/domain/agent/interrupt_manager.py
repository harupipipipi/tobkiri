"""domain/agent/interrupt_manager.py — Interrupt management and priority instruction queue.

Provides two main classes:
  - PriorityInstructionQueue: Enhanced instruction queue with high/normal/low priorities,
    cancellation, reordering, and priority changes.
  - InterruptManager: Manages pause/resume/redirect/stepback state for running executions.

Does NOT modify any existing domain/agent files.  Blocks import from here.
"""

import threading


from blocks._common import gen_id, timestamp

# ---------------------------------------------------------------------------
# Priority ordering – lower number = higher priority
# ---------------------------------------------------------------------------
PRIORITY_LEVELS = {"high": 0, "normal": 1, "low": 2}
VALID_PRIORITIES = frozenset(PRIORITY_LEVELS.keys())

# ---------------------------------------------------------------------------
# Instruction statuses
# ---------------------------------------------------------------------------
STATUS_PENDING = "pending"
STATUS_CONSUMED = "consumed"
STATUS_CANCELLED = "cancelled"

# Housekeeping
_MAX_FINISHED = 200  # keep at most this many consumed/cancelled entries per execution


# ===========================================================================
# PriorityInstructionQueue
# ===========================================================================
class PriorityInstructionQueue:
    """Thread-safe instruction queue with three priority tiers.

    Entries are dicts with keys:
        id, execution_id, instruction, priority, status, created_at, sequence
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = []          # all entries (pending + consumed + cancelled)
        self._seq_counter = 0       # monotonic sequence for stable ordering

    # -- public API --------------------------------------------------------

    def add(self, execution_id, instruction_text, priority="normal"):
        """Add an instruction and return the created entry dict."""
        if priority not in VALID_PRIORITIES:
            priority = "normal"
        with self._lock:
            self._seq_counter += 1
            entry = {
                "id": gen_id(),
                "execution_id": execution_id,
                "instruction": instruction_text,
                "priority": priority,
                "status": STATUS_PENDING,
                "created_at": timestamp(),
                "sequence": self._seq_counter,
            }
            self._entries.append(entry)
            return dict(entry)

    def cancel(self, instruction_id):
        """Cancel a pending instruction.  Returns True if found and cancelled."""
        with self._lock:
            for entry in self._entries:
                if entry["id"] == instruction_id and entry["status"] == STATUS_PENDING:
                    entry["status"] = STATUS_CANCELLED
                    return True
            return False

    def change_priority(self, instruction_id, new_priority):
        """Change priority of a pending instruction.  Returns updated entry or None."""
        if new_priority not in VALID_PRIORITIES:
            return None
        with self._lock:
            for entry in self._entries:
                if entry["id"] == instruction_id and entry["status"] == STATUS_PENDING:
                    entry["priority"] = new_priority
                    return dict(entry)
            return None

    def reorder(self, execution_id, ordered_ids):
        """Reorder pending instructions for an execution to match *ordered_ids*.

        *ordered_ids* is a list of instruction IDs in the desired order.
        Instructions not in *ordered_ids* keep their original relative order
        and are placed after the explicitly ordered ones.
        Returns the new ordered list of pending entries.
        """
        with self._lock:
            pending = [
                e for e in self._entries
                if e["execution_id"] == execution_id and e["status"] == STATUS_PENDING
            ]
            id_to_entry = {e["id"]: e for e in pending}
            ordered = []
            seen = set()
            for iid in ordered_ids:
                if iid in id_to_entry and iid not in seen:
                    ordered.append(id_to_entry[iid])
                    seen.add(iid)
            for e in pending:
                if e["id"] not in seen:
                    ordered.append(e)
            # Reassign sequence numbers to reflect new order
            base_seq = self._seq_counter + 1
            for idx, e in enumerate(ordered):
                e["sequence"] = base_seq + idx
            self._seq_counter = base_seq + len(ordered)
            return [dict(e) for e in ordered]

    def get_pending(self, execution_id, consume=True):
        """Return pending instructions sorted by priority then sequence.

        If *consume* is True, mark them as consumed (for injection into the
        agent's message stream).
        """
        with self._lock:
            pending = [
                e for e in self._entries
                if e["execution_id"] == execution_id and e["status"] == STATUS_PENDING
            ]
            pending.sort(key=lambda e: (PRIORITY_LEVELS.get(e["priority"], 1), e["sequence"]))
            if consume:
                for e in pending:
                    e["status"] = STATUS_CONSUMED
                self._cleanup_finished_locked(execution_id)
            return [dict(e) for e in pending]

    def get_high_priority(self, execution_id, consume=True):
        """Return only *high* priority pending instructions (for immediate processing)."""
        with self._lock:
            high = [
                e for e in self._entries
                if e["execution_id"] == execution_id
                and e["status"] == STATUS_PENDING
                and e["priority"] == "high"
            ]
            high.sort(key=lambda e: e["sequence"])
            if consume:
                for e in high:
                    e["status"] = STATUS_CONSUMED
            return [dict(e) for e in high]

    def has_pending(self, execution_id):
        with self._lock:
            return any(
                e["execution_id"] == execution_id and e["status"] == STATUS_PENDING
                for e in self._entries
            )

    def has_high(self, execution_id):
        with self._lock:
            return any(
                e["execution_id"] == execution_id
                and e["status"] == STATUS_PENDING
                and e["priority"] == "high"
                for e in self._entries
            )

    def list_all(self, execution_id):
        """Return all entries (any status) for an execution."""
        with self._lock:
            return [
                dict(e) for e in self._entries
                if e["execution_id"] == execution_id
            ]

    def list_pending(self, execution_id):
        """Return only pending entries, sorted by priority then sequence."""
        with self._lock:
            pending = [
                e for e in self._entries
                if e["execution_id"] == execution_id and e["status"] == STATUS_PENDING
            ]
            pending.sort(key=lambda e: (PRIORITY_LEVELS.get(e["priority"], 1), e["sequence"]))
            return [dict(e) for e in pending]

    def clear(self, execution_id):
        with self._lock:
            self._entries = [
                e for e in self._entries if e["execution_id"] != execution_id
            ]

    # -- internal ----------------------------------------------------------

    def _cleanup_finished_locked(self, execution_id):
        """Remove excess consumed/cancelled entries.  Caller must hold _lock."""
        finished = [
            e for e in self._entries
            if e["execution_id"] == execution_id and e["status"] in (STATUS_CONSUMED, STATUS_CANCELLED)
        ]
        if len(finished) <= _MAX_FINISHED:
            return
        finished.sort(key=lambda e: e["created_at"])
        excess = len(finished) - _MAX_FINISHED
        ids_to_remove = {finished[i]["id"] for i in range(excess)}
        self._entries = [e for e in self._entries if e["id"] not in ids_to_remove]


# ===========================================================================
# InterruptManager
# ===========================================================================
class InterruptManager:
    """Manages pause / resume / redirect / stepback state per execution.

    State is kept in a per-execution dict:
        {
            "paused": bool,
            "paused_at": str | None,
            "redirect": {"new_goal": str, "applied": bool} | None,
            "stepback_requested": bool,
            "stepback_count": int,
        }
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._states = {}  # execution_id -> state dict

    def _ensure(self, execution_id):
        """Return (and lazily create) state for *execution_id*.
        Caller must hold _lock.
        """
        if execution_id not in self._states:
            self._states[execution_id] = {
                "paused": False,
                "paused_at": None,
                "resumed_at": None,
                "redirect": None,
                "stepback_requested": False,
                "stepback_count": 0,
            }
        return self._states[execution_id]

    # -- pause / resume ----------------------------------------------------

    def pause(self, execution_id):
        """Mark execution as paused.  Returns state dict."""
        with self._lock:
            state = self._ensure(execution_id)
            if state["paused"]:
                return {"already_paused": True, **state}
            state["paused"] = True
            state["paused_at"] = timestamp()
            state["resumed_at"] = None
            return dict(state)

    def resume(self, execution_id):
        """Mark execution as resumed.  Returns state dict."""
        with self._lock:
            state = self._ensure(execution_id)
            if not state["paused"]:
                return {"already_running": True, **state}
            state["paused"] = False
            state["resumed_at"] = timestamp()
            return dict(state)

    def is_paused(self, execution_id):
        with self._lock:
            state = self._states.get(execution_id)
            if state is None:
                return False
            return state["paused"]

    # -- redirect ----------------------------------------------------------

    def redirect(self, execution_id, new_goal):
        """Set a redirect (goal change) for the execution."""
        with self._lock:
            state = self._ensure(execution_id)
            state["redirect"] = {
                "new_goal": new_goal,
                "applied": False,
                "created_at": timestamp(),
            }
            return dict(state)

    def consume_redirect(self, execution_id):
        """Return the redirect if not yet applied, and mark it applied.
        Returns the redirect dict or None.
        """
        with self._lock:
            state = self._states.get(execution_id)
            if state is None or state["redirect"] is None:
                return None
            if state["redirect"]["applied"]:
                return None
            state["redirect"]["applied"] = True
            return dict(state["redirect"])

    def get_redirect(self, execution_id):
        with self._lock:
            state = self._states.get(execution_id)
            if state is None:
                return None
            return dict(state["redirect"]) if state["redirect"] else None

    # -- stepback ----------------------------------------------------------

    def request_stepback(self, execution_id):
        """Request to go back one step and redo."""
        with self._lock:
            state = self._ensure(execution_id)
            state["stepback_requested"] = True
            state["stepback_count"] += 1
            return dict(state)

    def consume_stepback(self, execution_id):
        """Check and consume a stepback request.  Returns True if one was pending."""
        with self._lock:
            state = self._states.get(execution_id)
            if state is None or not state["stepback_requested"]:
                return False
            state["stepback_requested"] = False
            return True

    # -- state access ------------------------------------------------------

    def get_state(self, execution_id):
        with self._lock:
            state = self._states.get(execution_id)
            if state is None:
                return None
            return dict(state)

    def clear(self, execution_id):
        with self._lock:
            self._states.pop(execution_id, None)

    def get_progress(self, execution_id, engine):
        """Build a progress snapshot from the engine and interrupt state.

        *engine* must be an AgentEngine instance with a status() method.
        Returns a dict with current step info, progress estimate, and
        upcoming action.
        """
        with self._lock:
            int_state = dict(self._ensure(execution_id))

        engine_status = engine.status(execution_id)
        if engine_status.get("status") == "error" and "execution not found" in str(engine_status.get("result", "")):
            return None

        steps = engine_status.get("steps", [])
        current_step_num = engine_status.get("current_step", 0)
        total_steps = len(steps)
        exec_status = engine_status.get("status", "unknown")

        # Determine current step info
        current_step_info = None
        if steps and current_step_num > 0 and current_step_num <= total_steps:
            current_step_info = steps[current_step_num - 1]

        # Estimate progress (heuristic: based on status and steps)
        progress_pct = None
        if exec_status == "completed":
            progress_pct = 100
        elif exec_status == "error":
            progress_pct = None
        elif exec_status == "cancelled":
            progress_pct = None
        elif total_steps > 0:
            # Count completed steps vs total; this is a rough estimate since
            # we don't know the total in advance
            completed_steps = sum(1 for s in steps if s.get("status") == "completed")
            if completed_steps > 0:
                # Heuristic: assume we're roughly proportional to completed steps
                # Cap at 95% since we don't know the real total
                progress_pct = min(95, int((completed_steps / max(total_steps, 1)) * 100))

        # Determine next action
        next_action = None
        if int_state["paused"]:
            next_action = {"type": "paused", "detail": "execution paused by user"}
        elif exec_status == "waiting_approval":
            last_tool = None
            for s in reversed(steps):
                if s.get("step_type") == "tool_call":
                    last_tool = s.get("content", {})
                    break
            next_action = {
                "type": "awaiting_approval",
                "detail": last_tool if last_tool else "tool call pending user approval",
            }
        elif exec_status == "running":
            next_action = {"type": "processing", "detail": "AI is processing"}

        # Pending redirect info
        pending_redirect = None
        if int_state.get("redirect") and not int_state["redirect"].get("applied", True):
            pending_redirect = int_state["redirect"].get("new_goal")

        return {
            "execution_id": execution_id,
            "status": exec_status,
            "paused": int_state["paused"],
            "current_step": current_step_info,
            "current_step_number": current_step_num,
            "total_steps_so_far": total_steps,
            "progress_percent": progress_pct,
            "next_action": next_action,
            "pending_redirect": pending_redirect,
            "stepback_count": int_state["stepback_count"],
        }


# ===========================================================================
# Module-level singletons  (blocks import these via get_*() functions)
# ===========================================================================
_manager = InterruptManager()
_priority_queue = PriorityInstructionQueue()


def get_interrupt_manager():
    return _manager


def get_priority_queue():
    return _priority_queue
