from __future__ import annotations

from ecosystem.rumi_default_tools_pack import run_host_contract_action


def run(context, args):
    """Request one PID-scoped event through the captured Host contract."""

    del context
    payload = dict(args or {})
    return run_host_contract_action(
        "computer.pid_event",
        payload,
        source_function_id="computer_pid_event",
    )
