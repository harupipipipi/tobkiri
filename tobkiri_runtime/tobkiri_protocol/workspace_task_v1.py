"""Finite portable task plans; authority and private host paths are excluded."""

from __future__ import annotations

from typing import Any, Mapping

from tobkiri_protocol.workspace_capsule_v1 import (
    canonical,
    content_digest,
    digest,
    identifier,
    integer,
)

TASK_VERSION = "tobkiri.workspace-task-plan.v1"
TASK_CONTRACT = "tobkiri.action.workspace.container.v1"
TASK_RESOURCE = "tobkiri.resource.workspace.container-task.v1"
SANDBOX_PACK = "rumi_coding_sandbox_service_pack"
PREPARE = SANDBOX_PACK + ".task-prepare"
EXECUTE = SANDBOX_PACK + ".task-execute"
RESOURCE = SANDBOX_PACK + ".task-resource"
MAX_TIMEOUT = 120
REQUEST_FIELDS = {
    "task_request_id",
    "profile_id",
    "workspace_id",
    "expected_revision",
    "expected_writer_epoch",
    "argv",
    "timeout_seconds",
}
PLAN_FIELDS = REQUEST_FIELDS | {
    "version",
    "task_id",
    "plan_digest",
    "security_epoch",
    "checkpoint_digest",
    "recipe_digest",
    "image_reference",
    "expires_at_ms",
    "request_digest",
}


def task_argv(value: Any) -> list[str]:
    """Require explicit bounded container argv, with no host shell interpretation."""
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 64
        or any(
            not isinstance(item, str)
            or "\x00" in item
            or len(item.encode("utf-8")) > 8192
            for item in value
        )
        or not value[0]
        or value[0].startswith("-")
        or sum(len(item.encode("utf-8")) for item in value) > 16384
    ):
        raise ValueError("workspace task argv is invalid")
    return list(value)


def validate_task_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize exactly one finite request without client authority flags."""
    if not isinstance(value, Mapping) or set(value) != REQUEST_FIELDS:
        raise ValueError("workspace task request fields are invalid")
    request = dict(value)
    for key in ("task_request_id", "profile_id", "workspace_id"):
        identifier(request[key])
    integer(request["expected_revision"], 1)
    integer(request["expected_writer_epoch"], 1)
    request["argv"] = task_argv(request["argv"])
    if integer(request["timeout_seconds"], 1) > MAX_TIMEOUT:
        raise ValueError("workspace task timeout is invalid")
    return request


def task_identity(request: Mapping[str, Any], private_owner: str) -> str:
    """Derive an opaque shared task reference without disclosing owner context."""
    request = validate_task_request(request)
    return (
        "task-"
        + digest(canonical([request["task_request_id"], private_owner, request]))[7:39]
    )


def validate_task_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate immutable public execution metadata with a pinned image identity."""
    if not isinstance(value, Mapping) or set(value) != PLAN_FIELDS:
        raise ValueError("workspace task plan fields are invalid")
    plan = dict(value)
    validate_task_request({key: plan[key] for key in REQUEST_FIELDS})
    if plan["version"] != TASK_VERSION:
        raise ValueError("workspace task plan version is invalid")
    identifier(plan["task_id"])
    integer(plan["security_epoch"])
    integer(plan["expires_at_ms"], 1)
    for key in ("plan_digest", "checkpoint_digest", "recipe_digest", "request_digest"):
        content_digest(plan[key])
    image = plan["image_reference"]
    if not isinstance(image, str) or "@sha256:" not in image:
        raise ValueError("workspace task image is not pinned")
    name, image_digest = image.split("@", 1)
    if (
        not 1 <= len(name) <= 200
        or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789/._:-" for c in name)
        or name.startswith(("-", "/"))
    ):
        raise ValueError("workspace task image is invalid")
    content_digest(image_digest)
    return plan


def execute_payload(
    request: Mapping[str, Any],
    prepared_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal only a provider-produced plan matched to the original request."""
    request = validate_task_request(request)
    if set(prepared_result) != {"executed", "task_plan", "task_plan_digest"}:
        raise ValueError("workspace task prepare result is invalid")
    if prepared_result["executed"] is not False:
        raise ValueError("workspace task prepare must not execute a workload")
    plan = validate_task_plan(prepared_result["task_plan"])
    if (
        plan["request_digest"] != digest(canonical(request))
        or any(plan[key] != request[key] for key in REQUEST_FIELDS)
        or prepared_result["task_plan_digest"] != digest(canonical(plan))
    ):
        raise ValueError("workspace task prepared plan differs")
    return {"task_plan": plan, "task_plan_digest": digest(canonical(plan))}
