"""Public scoped instruction-inbox wire contract, independent of its owner."""

INBOX_VERSION = "tobkiri.agent-inbox.v1"
INBOX_CONTRACT = "tobkiri.action.agent.inbox.v1"
CONTEXT_CONTRACT = "tobkiri.resource.context.projection.v1"
INBOX_STATES = frozenset({"pending", "received", "applied", "cancelled", "expired"})
SAFE_BOUNDARIES = frozenset({"before_turn", "between_tools"})
