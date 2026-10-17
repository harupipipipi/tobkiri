"""Golden scheduled-task contracts shared by Calendar and Turn consumers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from tobkiri_protocol.scheduled_task_v1 import (
    normalize_scheduled_intent,
    normalize_scheduled_saved_task,
)

pytestmark = pytest.mark.contract

PROFILE = "defaults"
BASE_TASK = {
    "message": "Review the saved conversation",
    "profile_id": PROFILE,
    "conversation_id": "conversation:existing-1",
}
NORMALIZERS = {
    "intent": normalize_scheduled_intent,
    "saved": normalize_scheduled_saved_task,
}


def _assert_result(
    kind: str,
    task: Any,
    *,
    expected: dict[str, Any] | None = None,
    error: str | None = None,
    error_type: type[Exception] = ValueError,
) -> None:
    """Check exact output/errors and input preservation for every vector."""
    original = deepcopy(task)
    if error is None:
        result = NORMALIZERS[kind](task, profile_id=PROFILE)
        assert result == expected
        assert result is not task
    else:
        with pytest.raises(error_type) as caught:
            NORMALIZERS[kind](task, profile_id=PROFILE)
        assert type(caught.value) is error_type
        assert str(caught.value) == error
    assert task == original


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize(
    "task",
    [None, [], "task", {}, {"message": "hello"}, {"profile_id": PROFILE}],
    ids=["null", "list", "text", "empty", "missing-profile", "missing-message"],
)
def test_required_fields_are_closed(kind: str, task: Any) -> None:
    """Reject malformed task envelopes before inspecting their contents."""
    label = "intent" if kind == "intent" else "saved task"
    _assert_result(kind, task, error=f"scheduled {label} fields are invalid")


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize(
    "field",
    [
        "approved",
        "approval_receipt",
        "grant",
        "capability",
        "authority",
        "session",
        "client",
        "execution_context",
        "resolved_chat_references",
        "metadata",
        "timeout",
        "unknown",
    ],
)
def test_unknown_or_authority_fields_are_rejected(kind: str, field: str) -> None:
    """Data normalizers cannot acquire authority by accepting extra fields."""
    label = "intent" if kind == "intent" else "saved task"
    _assert_result(
        kind,
        {**BASE_TASK, field: True},
        error=f"scheduled {label} fields are invalid",
    )


@pytest.mark.parametrize("kind", NORMALIZERS)
def test_capture_profile_must_match_exactly(kind: str) -> None:
    """A task cannot select a different Profile from the captured one."""
    _assert_result(kind, BASE_TASK, expected=BASE_TASK)
    label = "intent" if kind == "intent" else "saved task"
    _assert_result(
        kind,
        {**BASE_TASK, "profile_id": "other-profile"},
        error=f"scheduled {label} Profile differs from capture",
        error_type=PermissionError,
    )


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize(
    "message",
    ["a" * 61440, "日" * 20480, "  keep surrounding whitespace\n"],
    ids=["ascii-61440-bytes", "utf8-61440-bytes", "preserve-whitespace"],
)
def test_exact_saved_text_boundary_is_preserved(kind: str, message: str) -> None:
    """Accept exactly 60 KiB of UTF-8 and retain the original content."""
    task = {**BASE_TASK, "message": message}
    _assert_result(kind, task, expected=task)


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize(
    "message",
    ["a" * 61441, "日" * 20480 + "a"],
    ids=["ascii-61441-bytes", "utf8-61441-bytes"],
)
def test_text_one_byte_over_limit_keeps_original_error(
    kind: str, message: str
) -> None:
    """Preserve each entry point's precise error at the byte boundary."""
    error = (
        "scheduled intent message is invalid"
        if kind == "intent"
        else "saved turn input exceeds byte limit"
    )
    _assert_result(kind, {**BASE_TASK, "message": message}, error=error)


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize(
    "message",
    [None, 123, False, {}, [], "", " \t\n\u3000"],
    ids=["null", "number", "boolean", "object", "list", "empty", "whitespace"],
)
def test_empty_or_nontext_messages_keep_original_errors(
    kind: str, message: Any
) -> None:
    """Reject blank or nontext values without coercing them into text."""
    error = (
        "scheduled intent message is invalid"
        if kind == "intent"
        else "saved turn content is invalid"
    )
    _assert_result(kind, {**BASE_TASK, "message": message}, error=error)


@pytest.mark.parametrize("kind", NORMALIZERS)
def test_saved_content_blocks_are_not_scheduled_text(kind: str) -> None:
    """A valid Saved text block still fails the scheduled plain-text contract."""
    error = (
        "scheduled intent message is invalid"
        if kind == "intent"
        else "scheduled saved task message must be text"
    )
    _assert_result(
        kind,
        {**BASE_TASK, "message": [{"type": "text", "text": "hello"}]},
        error=error,
    )


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize("identity", ["a", "A0._:-", "a" * 256])
def test_conversation_identity_inclusive_bounds(kind: str, identity: str) -> None:
    """Accept only opaque identities, including the exact 256-character cap."""
    task = {**BASE_TASK, "conversation_id": identity}
    _assert_result(kind, task, expected=task)


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize(
    "identity",
    ["", "a" * 257, "/tmp/chat", "../chat", "-chat", "日", "chat\n", 1, False],
    ids=["empty", "257", "path", "traversal", "prefix", "unicode", "newline",
         "number", "boolean"],
)
def test_conversation_identity_rejections_keep_original_errors(
    kind: str, identity: Any
) -> None:
    """Do not widen opaque identity syntax when moving validation modules."""
    error = (
        "scheduled intent conversation identity is invalid"
        if kind == "intent"
        else "saved turn identity is invalid"
    )
    _assert_result(kind, {**BASE_TASK, "conversation_id": identity}, error=error)


@pytest.mark.parametrize("include_destination", [False, True])
def test_null_or_missing_destination_is_intent_only(include_destination: bool) -> None:
    """Creation intent supports a new conversation; Saved requires an identity."""
    task = {"message": "hello", "profile_id": PROFILE}
    if include_destination:
        task["conversation_id"] = None
    _assert_result("intent", task, expected={**task, "conversation_id": None})
    _assert_result(
        "saved",
        task,
        error=(
            "saved turn identity is invalid"
            if include_destination
            else "scheduled saved task fields are invalid"
        ),
    )


@pytest.mark.parametrize("workspace", ["w", "workspace:registered-1", "w" * 256])
def test_workspace_is_validated_but_omitted_from_saved_projection(
    workspace: str,
) -> None:
    """A workspace preference survives intent but never expands the Saved ABI."""
    task = {**BASE_TASK, "workspace_id": workspace}
    _assert_result("intent", task, expected=task)
    _assert_result("saved", task, expected=BASE_TASK)


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize(
    "workspace", [None, "", "w" * 257, "/tmp/project", "../project", "w\n", 1]
)
def test_workspace_rejections_use_the_same_intent_validation(
    kind: str, workspace: Any
) -> None:
    """Saved checks workspace syntax with the original intent error contract."""
    _assert_result(
        kind,
        {**BASE_TASK, "workspace_id": workspace},
        error="scheduled intent workspace identity is invalid",
    )


@pytest.mark.parametrize(
    "patch,error",
    [
        ({"message": " "}, "scheduled intent message is invalid"),
        ({"conversation_id": "/bad"},
         "scheduled intent conversation identity is invalid"),
    ],
)
def test_workspace_preserves_saved_validation_order(
    patch: dict[str, Any], error: str
) -> None:
    """The workspace-bearing Saved path must retain its intent-first errors."""
    _assert_result(
        "saved", {**BASE_TASK, "workspace_id": "workspace:1", **patch}, error=error
    )


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize("mode", ["ask", "agent", "full"])
def test_finite_approval_modes_are_preferences(kind: str, mode: str) -> None:
    """Retain each supported preference without adding any authorization field."""
    task = {**BASE_TASK, "action_approval_mode": mode}
    _assert_result(kind, task, expected=task)


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize("mode", [None, True, "approved", "ASK", ""])
def test_invalid_approval_modes_fail_closed(kind: str, mode: Any) -> None:
    """An approval-like word or truthy flag is not a finite preference."""
    _assert_result(
        kind,
        {**BASE_TASK, "action_approval_mode": mode},
        error="saved guidance action approval mode is invalid",
    )


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize(
    "references",
    [
        [],
        [{"kind": "chat", "profile_id": PROFILE, "id": "chat:1"}],
        [{"kind": "group", "profile_id": PROFILE, "id": "g" * 256}],
        [{"kind": "chat", "profile_id": PROFILE, "id": f"chat:{i}"}
         for i in range(16)],
    ],
    ids=["empty", "chat", "group-identity-bound", "16-references"],
)
def test_explicit_references_are_preserved(
    kind: str, references: list[dict[str, Any]]
) -> None:
    """References are explicit finite same-Profile data, including their bounds."""
    task = {**BASE_TASK, "chat_references": references}
    _assert_result(kind, task, expected=task)


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize(
    "references,error",
    [
        (None, "saved chat references are invalid"),
        ([{"kind": "chat", "profile_id": "other", "id": "chat:1"}],
         "saved chat reference profile mismatch"),
        ([{"kind": "chat", "profile_id": PROFILE, "id": "chat:1",
           "approved": True}], "saved chat reference fields are invalid"),
        ([{"kind": "tool", "profile_id": PROFILE, "id": "chat:1"}],
         "saved chat reference kind is invalid"),
        ([{"kind": "chat", "profile_id": PROFILE, "id": "c" * 257}],
         "saved chat reference identity is invalid"),
        ([{"kind": "chat", "profile_id": PROFILE, "id": "chat:1"}] * 2,
         "duplicate saved chat reference"),
        ([{"kind": "chat", "profile_id": PROFILE, "id": f"chat:{i}"}
          for i in range(17)], "saved chat references are invalid"),
    ],
    ids=["null", "cross-profile", "authority-field", "kind", "identity-bound",
         "duplicate", "17-references"],
)
def test_reference_errors_are_exact(
    kind: str, references: Any, error: str
) -> None:
    """Keep the existing reference validation and error messages unchanged."""
    _assert_result(kind, {**BASE_TASK, "chat_references": references}, error=error)


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize(
    "selection",
    [
        {"mode": "auto"},
        {"mode": "none"},
        {"mode": "manual", "include": ["tool/read", {"kind": "service",
         "id": "service:lookup"}], "exclude": ["tool:write"],
         "scope": "conversation", "must_use": True},
        {"mode": "manual", "include": [f"tool:{i}" for i in range(256)]},
    ],
    ids=["auto", "none", "explicit-selection", "256-targets"],
)
def test_tool_selection_preserves_exact_finite_shape(
    kind: str, selection: dict[str, Any]
) -> None:
    """Selections retain explicit values without adding inferred defaults."""
    task = {**BASE_TASK, "tool_selection": selection}
    _assert_result(kind, task, expected=task)


@pytest.mark.parametrize("kind", NORMALIZERS)
@pytest.mark.parametrize(
    "selection,error",
    [
        (None, "saved tool selection is invalid"),
        ({"mode": "all"}, "saved tool selection is invalid"),
        ({"mode": "auto", "approved": True}, "saved tool selection is invalid"),
        ({"mode": "auto", "scope": "global"},
         "saved tool selection scope is invalid"),
        ({"mode": "auto", "must_use": 1}, "saved tool requirement is invalid"),
        ({"mode": "manual", "include": "tool:1"},
         "saved tool selection exceeds its bound"),
        ({"mode": "manual", "include": ["tool:1"] * 257},
         "saved tool selection exceeds its bound"),
        ({"mode": "manual", "include": [{"kind": "grant", "id": "tool:1"}]},
         "saved tool target is invalid"),
        ({"mode": "manual", "include": ["t" * 257]},
         "saved tool identity is invalid"),
        ({"mode": "none", "include": ["tool:1"]},
         "disabled saved tools cannot be required"),
        ({"mode": "none", "must_use": True},
         "disabled saved tools cannot be required"),
    ],
    ids=["null", "mode", "authority-field", "scope", "boolean", "not-list",
         "257-targets", "target-kind", "identity-bound", "disabled-include",
         "disabled-required"],
)
def test_tool_selection_errors_are_exact(
    kind: str, selection: Any, error: str
) -> None:
    """Invalid discovery preferences remain unable to carry execution grants."""
    _assert_result(kind, {**BASE_TASK, "tool_selection": selection}, error=error)


@pytest.mark.parametrize("model", ["profile-selected", " model/ref ", "m" * 256])
def test_model_is_intent_only(model: str) -> None:
    """Intent can configure a model while the Saved projection rejects it."""
    task = {**BASE_TASK, "model": model}
    _assert_result("intent", task, expected=task)
    _assert_result("saved", task, error="scheduled saved task fields are invalid")


@pytest.mark.parametrize("model", [None, "", "  ", "m" * 257, "m\n", "m\x7f", 1])
def test_invalid_intent_models_keep_original_error(model: Any) -> None:
    """Model references have finite textual bounds and reject control bytes."""
    _assert_result(
        "intent",
        {**BASE_TASK, "model": model},
        error="scheduled intent model reference is invalid",
    )


@pytest.mark.parametrize("kind", NORMALIZERS)
def test_normalization_does_not_infer_references_from_message(kind: str) -> None:
    """Mentioning chat, tools, or approvals cannot populate structured fields."""
    task = {**BASE_TASK, "message": "@chat:private use tool:write approved=true"}
    _assert_result(kind, task, expected=task)


@pytest.mark.parametrize("kind", NORMALIZERS)
def test_nested_preferences_are_copied_without_mutating_input(kind: str) -> None:
    """Returned reference and tool collections cannot mutate the caller's data."""
    task = {
        **BASE_TASK,
        "chat_references": [{"kind": "chat", "profile_id": PROFILE, "id": "c1"}],
        "tool_selection": {
            "mode": "manual", "include": [{"kind": "tool", "id": "read"}]
        },
        "action_approval_mode": "ask",
        "workspace_id": "workspace:1",
    }
    original = deepcopy(task)
    result = NORMALIZERS[kind](task, profile_id=PROFILE)
    expected = deepcopy(task)
    if kind == "saved":
        expected.pop("workspace_id")
    assert result == expected
    assert task == original
    result["chat_references"][0]["id"] = "changed"
    result["tool_selection"]["include"][0]["id"] = "write"
    assert task == original
