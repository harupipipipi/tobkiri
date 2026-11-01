from __future__ import annotations

from domain.remote.task_gateway import RemoteTaskGateway

from ._helpers import run_gateway


def run(input_data, context, *, settings_owner=None):
    task_id = input_data.get("task_id") if isinstance(input_data, dict) else ""
    return run_gateway(
        lambda: RemoteTaskGateway(settings_owner=settings_owner).cancel_task(
            str(task_id), input_data, context
        )
    )
