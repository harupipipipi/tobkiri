"""SessionStore and AgentSession — in-memory session management for agents."""


from blocks._common import timestamp, gen_id


_sessions = {}


class AgentSession:
    """Represents a single agent conversation session."""

    def __init__(self, conversation_id, agent_id):
        self.conversation_id = conversation_id
        self.agent_id = agent_id
        self.status = "idle"
        self.current_step = 0
        self.active_tool = None
        self.total_tokens = 0
        self.messages = []
        self.plan = None
        self.created_at = timestamp()
        self.updated_at = timestamp()
        self.pending_approvals = {}

    def approve_step(self, step_id):
        """Approve a pending step. Returns True if found and approved, False otherwise."""
        if step_id not in self.pending_approvals:
            return False
        if self.pending_approvals[step_id].get("status") != "pending":
            return False
        self.pending_approvals[step_id]["status"] = "approved"
        self.updated_at = timestamp()
        return True

    def reject_step(self, step_id, reason=""):
        """Reject a pending step. Returns True if found and rejected, False otherwise."""
        if step_id not in self.pending_approvals:
            return False
        if self.pending_approvals[step_id].get("status") != "pending":
            return False
        self.pending_approvals[step_id]["status"] = "rejected"
        self.pending_approvals[step_id]["reason"] = reason
        self.updated_at = timestamp()
        return True

    def cancel(self):
        """Cancel the session. Returns True if was running/waiting, False otherwise."""
        if self.status not in ("running", "waiting_for_approval"):
            return False
        self.status = "cancelled"
        self.updated_at = timestamp()
        return True

    def get_status(self):
        """Return current session status as a dict."""
        return {
            "status": self.status,
            "current_step": self.current_step,
            "active_tool": self.active_tool,
            "total_tokens": self.total_tokens,
        }

    def get_plan(self):
        """Return the current plan or None."""
        return self.plan

    def add_message(self, role, content):
        """Add a RumiMessage-format message to the session and return it."""
        msg = {
            "id": gen_id(),
            "role": role,
            "content": content,
            "created_at": timestamp(),
        }
        self.messages.append(msg)
        self.updated_at = timestamp()
        return msg

    def set_plan(self, plan_dict):
        """Set the plan for this session."""
        self.plan = plan_dict
        self.updated_at = timestamp()


class SessionStore:
    """Module-level singleton session store backed by _sessions dict."""

    @staticmethod
    def get(conversation_id):
        """Return the AgentSession for conversation_id, or None."""
        return _sessions.get(conversation_id)

    @staticmethod
    def get_or_create(conversation_id, agent_id):
        """Return existing session or create a new one."""
        if conversation_id not in _sessions:
            _sessions[conversation_id] = AgentSession(conversation_id, agent_id)
        return _sessions[conversation_id]

    @staticmethod
    def remove(conversation_id):
        """Remove a session. No-op if not found."""
        _sessions.pop(conversation_id, None)
