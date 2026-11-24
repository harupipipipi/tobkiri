"""Selected Workflow timing data behind the captured saved-turn Broker callbacks."""

from __future__ import annotations

import hashlib
import os
import stat
from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.profile_content_projection import _inventory, selected_projection_roots
from core_runtime.resolved_profile_scope import effective_profile_projections
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.saved_internal_context import validate_timing_output

WORKFLOW_TARGET = ("tobkiri.workflow.v4", "run.selected")
_KEYS = {"context_binding_api_version", "kind", "workflow_id", "output_step_id"}


def selected_timing_binding() -> dict[str, str] | None:
    """Capture at most one finite binding from verified selected projection bytes."""
    selections = effective_profile_projections()
    found = []
    for _projection, root in selected_projection_roots(selections, kind="profile_content"):
        for path in sorted((root / "contexts").glob("*.conversation-context.v1.json")):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > 4096:
                    raise AuthorityDenied("selected context binding exceeds its byte limit")
                raw = stream.read(4097)
                after = os.fstat(stream.fileno())
            if len(raw) != before.st_size or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise AuthorityDenied("selected context binding changed during read")
            binding = strict_loads(raw)
            if (
                not isinstance(binding, dict)
                or set(binding) != _KEYS
                or binding["context_binding_api_version"]
                != "io.tobkiri.conversation-context-binding.v1"
                or binding["kind"] != "timing.task_gap"
                or any(
                    not isinstance(binding[key], str) or not binding[key]
                    for key in ("workflow_id", "output_step_id")
                )
            ):
                raise AuthorityDenied("selected context binding is invalid")
            if _inventory(root).get(path.relative_to(root).as_posix()) != (
                "sha256:" + hashlib.sha256(raw).hexdigest()
            ):
                raise AuthorityDenied("selected context binding changed during read")
            found.append(binding)
    selected_projection_roots(selections, kind="profile_content")
    if len(found) > 1:
        raise AuthorityDenied("selected context bindings are conflicting")
    return found[0] if found else None


def selected_timing_context(
    outer: object,
    conversation: Mapping[str, Any],
    *,
    dispatch: Callable[[object, tuple[str, str], Mapping[str, Any]], Mapping[str, Any]],
    read_owner: Callable[[], Mapping[str, Any]],
) -> tuple[bool, dict[str, Any] | None]:
    """Execute selected source normally and bind its output to a fresh owner read."""
    binding = selected_timing_binding()
    if binding is None:
        return False, None
    context = getattr(outer, "context", None)
    profile_id, request_id = getattr(context, "profile_id", ""), getattr(context, "request_id", "")
    if not profile_id or not request_id:
        raise AuthorityDenied("selected context Host identity is unavailable")
    owner_pin = {
        key: conversation.get(key)
        for key in (
            "id",
            "conversation_revision",
            "current_node_id",
            "lifecycle",
        )
    }
    result = dispatch(
        outer,
        WORKFLOW_TARGET,
        {
            "definition_id": binding["workflow_id"],
            "inputs": {"profile_id": profile_id, "conversation_id": conversation["id"]},
            "occurrence_id": canonical_digest(
                {
                    "request_id": request_id,
                    "profile_id": profile_id,
                    "owner": owner_pin,
                    "binding": binding,
                }
            ),
        },
    )
    value = result.get("value")
    if (
        result.get("status") != "ok"
        or not isinstance(value, Mapping)
        or not isinstance(value.get("run"), Mapping)
        or value["run"].get("state") != "succeeded"
        or not isinstance(value.get("attempts"), list)
    ):
        raise AuthorityDenied("selected context Workflow is not successfully settled")
    candidates = [
        attempt
        for attempt in value["attempts"]
        if isinstance(attempt, Mapping)
        and attempt.get("step_id") == binding["output_step_id"]
        and attempt.get("state") == "succeeded"
        and not attempt.get("skipped")
    ]
    if len(candidates) != 1:
        raise AuthorityDenied("selected context Workflow output is unavailable")
    outcome = candidates[0].get("outcome")
    if not isinstance(outcome, Mapping) or outcome.get("status") != "ok":
        raise AuthorityDenied("selected context Pack outcome is unavailable")
    output = outcome.get("value")
    fresh = read_owner()
    if canonical_json({key: fresh.get(key) for key in owner_pin}) != canonical_json(owner_pin):
        raise AuthorityDenied("selected context conversation changed during execution")
    if not isinstance(output, Mapping):
        raise AuthorityDenied("selected context output is invalid")
    try:
        timing = validate_timing_output(output, profile_id=profile_id, conversation=fresh)
    except ValueError as error:
        raise AuthorityDenied(str(error)) from error
    if selected_timing_binding() != binding:
        raise AuthorityDenied("selected context binding changed during execution")
    return True, timing
