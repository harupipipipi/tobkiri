"""Bounded status projection; durable inputs and request payloads stay private."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
import re

from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.errors import CanonicalizationError

_MAX_ATTEMPTS = 256
_PREVIEW_BUDGET = 2 * 1024 * 1024
_AUDIO_KEYS = ("content_id", "media_type", "data_base64", "byte_size")


def observe_workflow_run(run: Mapping[str, Any], attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Expose latest step statuses and size-budgeted previews without secrets.

    This does not authorize or fetch content. Audio bytes must already be in a
    committed outcome; the browser separately validates their MIME and digest.
    No input/request dictionaries, credential handles or arbitrary output keys
    cross this read-only projection.
    """
    latest: dict[str, Mapping[str, Any]] = {}
    for attempt in attempts:
        if attempt.get("run_id") != run.get("run_id"):
            continue
        step = str(attempt["step_id"])
        previous = latest.get(step)
        if previous is None or int(attempt["attempt_number"]) > int(previous["attempt_number"]):
            latest[step] = attempt
    terminal_states = {"succeeded", "failed", "cancelled", "timed_out"}
    # A bounded preview must not hide the pending approval needed to make
    # progress. Show active attempts first, then fill with recent terminal rows.
    active = [item for item in latest.values() if item["state"] not in terminal_states]
    completed = [item for item in latest.values() if item["state"] in terminal_states]
    selected = active[:_MAX_ATTEMPTS]
    remaining = _MAX_ATTEMPTS - len(selected)
    if remaining:
        selected.extend(completed[-remaining:])
    rows: list[dict[str, Any]] = []
    budget = _PREVIEW_BUDGET
    omitted = len(latest) > len(selected)
    for attempt in selected:
        row = {key: attempt[key] for key in ("run_id", "step_id", "attempt_number", "state")}
        approval_id = attempt.get("approval_request_id")
        # This is a public request locator, never a Grant or dispatch token.
        # Opening it still requires the Host's exact owner/session checks.
        if (
            attempt["state"] == "waiting_approval"
            and isinstance(approval_id, str)
            and re.fullmatch(r"interactive-effect-[0-9a-f]{36}", approval_id)
        ):
            row["approval_request_id"] = approval_id
        outcome = attempt.get("outcome")
        if attempt["state"] == "succeeded" and isinstance(outcome, Mapping):
            preview: dict[str, Any] = {}
            text = outcome.get("text")
            if isinstance(text, str):
                preview["text"] = text[:65536]
                omitted = omitted or len(text) > 65536
            audio = outcome.get("content")
            if (
                isinstance(audio, Mapping)
                and all(isinstance(audio.get(key), str) for key in _AUDIO_KEYS[:3])
                and type(audio.get("byte_size")) is int
                and 0 < audio["byte_size"] <= 1024 * 1024
                and len(audio["data_base64"]) <= 2 * 1024 * 1024
                and len(audio["content_id"]) == 71
                and len(audio["media_type"]) <= 64
            ):
                preview["content"] = {key: audio[key] for key in _AUDIO_KEYS}
            if outcome and not preview:
                omitted = True
            if preview:
                try:
                    size = len(canonical_json(preview))
                except CanonicalizationError:
                    size = budget + 1
                if size <= budget:
                    row["outcome"] = preview
                    budget -= size
                else:
                    omitted = True
        rows.append(row)
    return {
        "run": {key: run[key] for key in ("run_id", "definition_id", "revision_digest", "state")},
        "attempts": rows,
        "details_omitted": omitted,
        "step_count": len(latest),
    }
