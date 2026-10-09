"""Pure finite file-create request and sealed plan protocol."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest

MAX_BYTES = 65_536
PLAN_VERSION = "tobkiri.file-create.plan.v1"


def file_arguments(value: Any) -> tuple[str, bytes]:
    """Validate a bounded UTF-8 create request without touching a filesystem."""
    if not isinstance(value, Mapping) or set(value) != {"path", "content"}:
        raise ValueError("file create arguments are invalid")
    path, content = value["path"], value["content"]
    if (
        not isinstance(path, str)
        or not 1 <= len(path) <= 1024
        or "\\" in path
        or any(ord(char) < 32 for char in path)
        or PurePosixPath(path).is_absolute()
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or not isinstance(content, str)
    ):
        raise ValueError("file create arguments are invalid")
    data = content.encode("utf-8")
    if not 1 <= len(data) <= MAX_BYTES:
        raise ValueError("file create content exceeds its finite bound")
    return path, data


def validate_execute_payload(request: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    """Check the fixed coordinator transform before freezing an execute request."""
    keys = {
        "version",
        "profile_id",
        "workspace_id",
        "mount_revision",
        "canonical_root",
        "root_st_dev",
        "root_st_ino",
        "path",
        "byte_count",
        "content_digest",
        "request_digest",
        "preimage",
        "plan_digest",
    }
    if not isinstance(plan, Mapping) or set(plan) != keys:
        raise PermissionError("file create plan is invalid")
    path, data = file_arguments({key: request[key] for key in ("path", "content")})
    frozen = {key: plan[key] for key in keys - {"plan_digest"}}
    if (
        plan["version"] != PLAN_VERSION
        or plan["preimage"] != "absent"
        or plan["path"] != path
        or plan["byte_count"] != len(data)
        or plan["content_digest"] != "sha256:" + hashlib.sha256(data).hexdigest()
        or plan["request_digest"] != canonical_digest(dict(request))
        or plan["plan_digest"] != canonical_digest(frozen)
        or plan["workspace_id"] != request.get("workspace_id")
        or plan["mount_revision"] != request.get("expected_mount_revision")
    ):
        raise PermissionError("file create plan changed")
    return {"request": dict(request), "plan": dict(plan)}
