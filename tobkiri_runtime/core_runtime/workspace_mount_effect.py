"""Validate immutable Host-prepared project workspace effects."""

from pathlib import Path
import re
from typing import Any, Mapping
from tobkiri_protocol.canonical import canonical_digest

_PROJECT_PLAN_KEYS = frozenset(
    {
        "version",
        "profile_id",
        "activation_id",
        "plan_digest",
        "security_epoch",
        "workspace_id",
        "root_path",
        "root_identity",
        "display_name",
        "expected_revision",
        "request_digest",
    }
)


def validate_project_plan(request: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    """Reject authority flags, substituted roots and malformed prepared plans."""
    if (
        set(request) != {"selection_id"}
        or not isinstance(request["selection_id"], str)
        or not request["selection_id"]
        or set(plan) != _PROJECT_PLAN_KEYS
        or plan.get("version") != "tobkiri.workspace.project-plan.v1"
        or plan.get("request_digest") != canonical_digest(dict(request))
        or type(plan.get("expected_revision")) is not int
        or plan["expected_revision"] < 0
        or type(plan.get("security_epoch")) is not int
        or plan["security_epoch"] <= 0
        or not isinstance(plan.get("root_identity"), list)
        or len(plan["root_identity"]) != 2
        or any(type(value) is not int or value < 0 for value in plan["root_identity"])
        or any(
            not isinstance(plan.get(key), str) or not plan[key]
            for key in (
                "profile_id",
                "activation_id",
                "plan_digest",
                "workspace_id",
                "root_path",
                "display_name",
            )
        )
        or not Path(plan["root_path"]).is_absolute()
    ):
        raise PermissionError("project workspace plan is invalid")
    if re.fullmatch(r"project-[0-9a-f]{32}", plan["workspace_id"]) is None:
        raise PermissionError("project workspace identity is invalid")


class ProjectMountPersistenceUncertain(RuntimeError):
    """A canonical mount persistence attempt may have completed before failure."""
