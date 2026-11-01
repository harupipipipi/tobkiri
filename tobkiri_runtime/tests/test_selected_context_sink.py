"""Selected saved-turn sink checks with explicit isolated Workflow dispatch data."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.profile_context_workflow import (
    selected_timing_binding,
    selected_timing_context,
)
import core_runtime.profile_content_projection as projections
from core_runtime.resolved_profile_scope import activate_resolved_profile, restore_resolved_profile
from tests.test_profile_workflow_catalog import _view
from tobkiri_protocol.conversation_lifecycle import active_task_gap_context
from tobkiri_protocol.saved_internal_context import CONTEXT_VERSION, validate_timing_output


def _owner() -> dict:
    return {
        "id": "conversation",
        "conversation_revision": 2,
        "current_node_id": "user",
        "messages": [{"id": "user", "role": "user", "content": "unchanged"}],
        "lifecycle": {
            "version": "tobkiri.conversation-lifecycle.v1",
            "state": "running",
            "resumed_completed_at_ms": 0,
            "active_user_received_at_ms": 3600000,
            "active_user_message_id": "user",
            "completion_message_id": "assistant",
        },
    }


def _output(owner: dict) -> dict:
    return {
        "internal_context_api_version": CONTEXT_VERSION,
        "profile_id": "example",
        "conversation_id": owner["id"],
        "conversation_revision": owner["conversation_revision"],
        "active_user_message_id": "user",
        "context": active_task_gap_context(owner),
    }


@pytest.fixture
def binding(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(projections, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(projections, "PROJECTION_ROOT", tmp_path / "profile_projections")
    root = tmp_path / "profile_projections/example/contexts"
    root.mkdir(parents=True)
    path = root / "example.conversation-context.v1.json"
    path.write_text(
        json.dumps(
            {
                "context_binding_api_version": "io.tobkiri.conversation-context-binding.v1",
                "kind": "timing.task_gap",
                "workflow_id": "example",
                "output_step_id": "context",
            }
        )
    )
    selection, _ = projections.resolve_intent_projection(
        {
            "projection_id": "example",
            "kind": "profile_content",
            "artifact_root": "profile_projections/example",
            "content_digest": None,
        }
    )
    token = activate_resolved_profile(_view(selection))
    yield path
    restore_resolved_profile(token)


def _dispatch(output: dict, *, state="succeeded"):
    def dispatch(outer, target, payload):
        assert target == ("tobkiri.workflow.v4", "run.selected")
        assert payload["inputs"] == {"profile_id": "example", "conversation_id": "conversation"}
        return {
            "status": "ok",
            "value": {
                "run": {"state": state},
                "attempts": [
                    {
                        "step_id": "context",
                        "state": "succeeded",
                        "outcome": {"status": "ok", "value": output},
                    }
                ],
            },
        }

    return dispatch


def test_selected_sink_checks_owner_then_emits_only_existing_timing_data(binding):
    owner = _owner()
    outer = SimpleNamespace(context=SimpleNamespace(profile_id="example", request_id="request"))
    result = selected_timing_context(
        outer, owner, dispatch=_dispatch(_output(owner)), read_owner=lambda: owner
    )
    assert result == (True, active_task_gap_context(owner))
    assert (
        validate_timing_output(_output(owner), profile_id="example", conversation=owner)
        == result[1]
    )


@pytest.mark.parametrize(
    "change",
    [
        "profile_id",
        "conversation_id",
        "conversation_revision",
        "active_user_message_id",
        "system_messages",
        "approved",
    ],
)
def test_forged_namespace_receipt_role_and_approval_never_enter_sink(binding, change):
    owner = _owner()
    output = _output(owner)
    output[change] = "forged"
    outer = SimpleNamespace(context=SimpleNamespace(profile_id="example", request_id="request"))
    with pytest.raises(AuthorityDenied, match="differs"):
        selected_timing_context(outer, owner, dispatch=_dispatch(output), read_owner=lambda: owner)


def test_owner_revision_changes_during_workflow_are_rejected(binding):
    owner = _owner()
    fresh = deepcopy(owner)
    fresh["conversation_revision"] += 1
    outer = SimpleNamespace(context=SimpleNamespace(profile_id="example", request_id="request"))
    with pytest.raises(AuthorityDenied, match="changed"):
        selected_timing_context(
            outer, owner, dispatch=_dispatch(_output(owner)), read_owner=lambda: fresh
        )


def test_waiting_workflow_cannot_insert_context(binding):
    owner = _owner()
    outer = SimpleNamespace(context=SimpleNamespace(profile_id="example", request_id="request"))
    with pytest.raises(AuthorityDenied, match="settled"):
        selected_timing_context(
            outer,
            owner,
            dispatch=_dispatch(_output(owner), state="waiting_approval"),
            read_owner=lambda: owner,
        )


def test_mutated_selected_binding_fails_before_dispatch(binding):
    binding.write_text("{}")
    with pytest.raises(projections.ProfileContentProjectionError, match="stale"):
        selected_timing_binding()


def test_unselected_binding_keeps_existing_context_without_workflow(tmp_path, monkeypatch):
    token = activate_resolved_profile(_view())
    try:
        assert selected_timing_context(
            object(),
            _owner(),
            dispatch=lambda *_: pytest.fail("unselected invoke"),
            read_owner=lambda: pytest.fail("unselected read"),
        ) == (False, None)
    finally:
        restore_resolved_profile(token)
