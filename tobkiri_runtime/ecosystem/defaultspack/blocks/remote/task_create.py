from __future__ import annotations

from domain.remote.task_gateway import RemoteTaskGateway
from domain.subagent_team.availability import (
    settings_owner_from_context,
    subagent_delegation_enabled,
    subagents_disabled_result,
)
from blocks._common import error

from ._helpers import run_gateway


def run(input_data, context, *, settings_owner=None):
    settings_owner = settings_owner_from_context(settings_owner, context)
    if not subagent_delegation_enabled(settings_owner=settings_owner):
        disabled = subagents_disabled_result()
        return error(disabled["message"], disabled["code"])
    return run_gateway(
        lambda: RemoteTaskGateway(settings_owner=settings_owner).create_task(
            input_data, context
        )
    )
