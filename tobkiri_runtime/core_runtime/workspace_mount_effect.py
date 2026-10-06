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


MAX_PROJECT_ROOTS = 32


def project_selection_request(request: Mapping[str, Any]) -> tuple[list[str], str]:
    """Normalize only opaque selections and an explicitly selected primary."""
    if set(request) == {"selection_id"}:
        tokens, primary = [request["selection_id"]], request["selection_id"]
    elif set(request) == {"selection_ids", "primary_selection_id"}:
        tokens, primary = request["selection_ids"], request["primary_selection_id"]
    else:
        raise PermissionError("project workspace selection is invalid")
    if (
        not isinstance(tokens, list)
        or not 1 <= len(tokens) <= MAX_PROJECT_ROOTS
        or any(not isinstance(t, str) or not t for t in tokens)
        or len(set(tokens)) != len(tokens)
        or primary not in tokens
    ):
        raise PermissionError("project workspace selection is invalid")
    return list(tokens), primary


def project_plan_roots(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return separately scoped approved roots, preserving single-plan support."""
    return plan.get(
        "roots",
        [
            {
                key: plan[key]
                for key in (
                    "workspace_id",
                    "root_path",
                    "root_identity",
                    "display_name",
                )
            }
        ],
    )


def validate_project_plan(request: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    """Reject authority flags, substituted roots and malformed prepared plans."""
    tokens, primary_token = project_selection_request(request)
    allowed_keys = _PROJECT_PLAN_KEYS | {"roots", "primary_workspace_id"}
    if (
        set(plan) not in (_PROJECT_PLAN_KEYS, allowed_keys)
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

    roots = project_plan_roots(plan)
    if (
        not isinstance(roots, list)
        or len(roots) != len(tokens)
        or not 1 <= len(roots) <= MAX_PROJECT_ROOTS
    ):
        raise PermissionError("project workspace roots are invalid")
    ids, paths, identities = set(), set(), set()
    for root in roots:
        if (
            not isinstance(root, Mapping)
            or set(root)
            != {"workspace_id", "root_path", "root_identity", "display_name"}
            or any(
                not isinstance(root.get(k), str) or not root[k]
                for k in ("workspace_id", "root_path", "display_name")
            )
            or re.fullmatch(r"project-[0-9a-f]{32}", root["workspace_id"]) is None
            or not Path(root["root_path"]).is_absolute()
            or not isinstance(root["root_identity"], list)
            or len(root["root_identity"]) != 2
            or any(type(v) is not int or v < 0 for v in root["root_identity"])
        ):
            raise PermissionError("project workspace root is invalid")
        identity = tuple(root["root_identity"])
        if (
            root["workspace_id"] in ids
            or root["root_path"] in paths
            or identity in identities
        ):
            raise PermissionError("duplicate project workspace root")
        ids.add(root["workspace_id"])
        paths.add(root["root_path"])
        identities.add(identity)
    if roots[tokens.index(primary_token)]["workspace_id"] != plan["workspace_id"]:
        raise PermissionError("project primary selection changed")
    primary = next(
        (r for r in roots if r["workspace_id"] == plan["workspace_id"]), None
    )
    if primary is None or any(primary[k] != plan[k] for k in primary):
        raise PermissionError("project primary workspace is invalid")
    if "roots" in plan and plan["primary_workspace_id"] != plan["workspace_id"]:
        raise PermissionError("project primary workspace is invalid")


class ProjectMountPersistenceUncertain(RuntimeError):
    """A canonical mount persistence attempt may have completed before failure."""
