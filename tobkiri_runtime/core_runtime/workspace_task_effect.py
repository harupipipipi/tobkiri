"""Capture bindings for finite, approval-gated workspace task plans."""

from __future__ import annotations

import math
from typing import Any, Mapping

from tobkiri_host.models import RequestContext
from tobkiri_protocol.workspace_task_v1 import (
    REQUEST_FIELDS,
    execute_payload,
    validate_task_plan,
)


def workspace_task_payload(
    request: Mapping[str, Any],
    prepared_result: Mapping[str, Any],
    *,
    context: RequestContext | None,
    now: float,
) -> dict[str, Any]:
    """Bind the provider's public task plan to the current Host capture."""

    if not isinstance(context, RequestContext) or not math.isfinite(now):
        raise ValueError("workspace task capture is unavailable")
    payload = execute_payload(request, prepared_result)
    plan = payload["task_plan"]
    if (
        plan["profile_id"] != context.profile_id
        or plan["plan_digest"] != context.plan_digest
        or plan["security_epoch"] != context.security_epoch
        or plan["expires_at_ms"] <= now * 1000
    ):
        raise ValueError("workspace task capture differs or has expired")
    return payload


def workspace_task_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Revalidate the exact Broker-frozen execute shape before presentation."""

    if set(payload) != {"task_plan", "task_plan_digest"}:
        raise ValueError("workspace task snapshot fields are invalid")
    plan = validate_task_plan(payload["task_plan"])
    request = {key: plan[key] for key in REQUEST_FIELDS}
    return execute_payload(request, {"executed": False, **payload})["task_plan"]
