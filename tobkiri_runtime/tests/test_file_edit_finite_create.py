"""Real Host writes release Timeline metadata only after exact effect success."""

import json
from types import SimpleNamespace as NS
from typing import Any

import pytest

from core_runtime import owned_file_approval_v4 as owner
from core_runtime.file_edit_receipts import file_edit_receipt_from_tool_result
from ecosystem.rumi_default_tools_pack.runtime import file_create as create
from tests.test_finite_file_create_v4 import ancestry
from tests.test_finite_file_create_v4 import real_create as real_create


def test_real_host_save_then_terminal_success_produces_content_free_receipt(
    monkeypatch: pytest.MonkeyPatch, real_create: Any
) -> None:
    root, _, _, capture, execute_invocation, _ = real_create
    capture.interactive_approval_port = object()
    capture.authority_approval_window_port = object()
    monkeypatch.setattr(create, "open_file_tool_approval", lambda *args, **kwargs: None)
    tool_invocation, _, _ = ancestry()
    request = None

    def invoke(_contract: str, _operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal request
        phase = payload["phase"]
        if phase == "prepare":
            request = payload["request"]
            owner.bind_file_effect(
                "actual-effect", request, tool_invocation.envelope.context, lambda: None
            )
            create._bind_prepare(capture)(request, execute_invocation)
            return {"effect_id": "actual-effect", "state": "approval_pending"}
        if phase == "status":
            return {"effect_id": "actual-effect", "state": "approved"}
        assert phase == "resume"
        plan = create.create_plan(request, create._binding(capture))
        create._bind_execute(capture)({"request": request, "plan": plan}, execute_invocation)
        return {"effect_id": "actual-effect", "state": "succeeded"}

    tool_invocation.contract_client = lambda **_: NS(invoke=invoke)
    content = "first\nsecond\nthird\n"
    result = create._bind_tool(capture)(
        {
            "tool_id": "coding_file_create",
            "tool_call_id": "real-call",
            "arguments": {"path": "actual.txt", "content": content},
        },
        tool_invocation,
    )
    assert (root / "actual.txt").read_text() == content
    receipt = file_edit_receipt_from_tool_result("coding_file_create", result)
    assert receipt["stats"] == {
        "status": "available",
        "lines_added": 3,
        "lines_deleted": 0,
    }
    assert receipt["path"] == "actual.txt"
    assert str(root) not in json.dumps(receipt)
    assert content not in json.dumps(receipt)
    assert request["invocation_key"] not in owner._live_requests
    assert "actual-effect" not in owner._file_effects


@pytest.mark.parametrize("terminal", ["failed", "ambiguous", "cancelled", "stale"])
def test_saved_file_with_late_broker_failure_never_releases_receipt(
    monkeypatch: pytest.MonkeyPatch, real_create: Any, terminal: str
) -> None:
    root, _, _, capture, execution, _ = real_create
    capture.interactive_approval_port = object()
    capture.authority_approval_window_port = object()
    monkeypatch.setattr(create, "open_file_tool_approval", lambda *args, **kwargs: None)
    tool, _, _ = ancestry()
    request = None

    def invoke(_contract: str, _operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal request
        phase = payload["phase"]
        if phase == "prepare":
            request = payload["request"]
            owner.bind_file_effect("late-audit", request, execution.envelope.context, lambda: None)
            return {"effect_id": "late-audit", "state": "approval_pending"}
        if phase == "status":
            return {"effect_id": "late-audit", "state": "approved"}
        if phase == "cancel":
            return {"effect_id": "late-audit", "state": "cancelled"}
        assert phase == "resume"
        create._bind_execute(capture)(
            {
                "request": request,
                "plan": create.create_plan(request, create._binding(capture)),
            },
            execution,
        )
        assert owner._live_requests[request["invocation_key"]].file_edit_receipt is not None
        return {"effect_id": "late-audit", "state": terminal}

    tool.contract_client = lambda **_: NS(invoke=invoke)
    with pytest.raises(PermissionError, match="not approved and completed"):
        create._bind_tool(capture)(
            {
                "tool_id": "coding_file_create",
                "tool_call_id": "late-call",
                "arguments": {"path": "late.txt", "content": "actual saved text"},
            },
            tool,
        )
    assert (root / "late.txt").read_text() == "actual saved text"
    assert request["invocation_key"] not in owner._live_requests
    assert "late-audit" not in owner._file_effects


def test_metadata_failure_does_not_turn_actual_host_create_into_failure(
    monkeypatch: pytest.MonkeyPatch, real_create: Any
) -> None:
    root, _, _, capture, invocation, request = real_create

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("metadata lifetime drained")

    monkeypatch.setattr(create, "record_file_tool_edit", unavailable)
    result = create._bind_execute(capture)(
        {
            "request": request,
            "plan": create.create_plan(request, create._binding(capture)),
        },
        invocation,
    )
    assert result["created"] is True
    assert (root / "index.html").read_text() == request["content"]
    assert owner._live_requests[request["invocation_key"]].file_edit_receipt is None


def test_effect_or_workspace_mismatch_cannot_release_another_save(
    real_create: Any,
) -> None:
    _, _, _, capture, invocation, request = real_create
    owner.bind_file_effect("actual-effect", request, invocation.envelope.context, lambda: None)
    create._bind_execute(capture)(
        {
            "request": request,
            "plan": create.create_plan(request, create._binding(capture)),
        },
        invocation,
    )
    assert (
        owner.file_tool_edit_after_success(request, invocation.envelope.context, "foreign-effect")
        is None
    )
    with pytest.raises(PermissionError):
        owner.file_tool_edit_after_success(
            {**request, "workspace_id": "foreign"},
            invocation.envelope.context,
            "actual-effect",
        )
