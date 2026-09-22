from __future__ import annotations

from domain.remote.task_gateway import RemoteTaskGateway

from ._helpers import run_gateway


def run(input_data, context, *, settings_owner=None):
    return run_gateway(
        lambda: RemoteTaskGateway(settings_owner=settings_owner).create_task(
            input_data, context
        )
    )
