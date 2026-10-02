"""Versioned work-plan and review types shared only through public contracts."""

PLAN_VERSION = "tobkiri.work-plan.v1"
RESOURCE_CONTRACT = "tobkiri.resource.work-plan.v1"
ACTION_CONTRACT = "tobkiri.action.work-plan.v1"
REPLACE_CONTRACT = "tobkiri.action.context.replace.v1"
REVIEW_STATES = frozenset(
    {"on_track", "drift", "blocked", "unverifiable", "review_failed"}
)
TODO_STATES = frozenset(
    {"pending", "in_progress", "blocked", "needs_review", "done", "cancelled"}
)
