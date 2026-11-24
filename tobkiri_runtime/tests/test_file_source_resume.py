"""Retained Source file scheduling never substitutes native effect authority."""

from copy import copy
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4 as Scope
from core_runtime import owned_file_approval_v4 as owned
from tests.test_operation_cancellation import _envelope


@pytest.fixture
def source_file():
    guards = []
    live = [True]

    def guard():
        if not live[0]:
            raise PermissionError("source revoked")
        guards.append("current")

    def scope(pair, parent=None, payload=None):
        envelope = replace(
            _envelope(),
            contract_id=pair[0],
            operation_id=pair[1],
            contract_version="1.0.0",
            payload=payload or {},
        )
        return Scope(envelope, guard, parent)

    saved = scope(
        owned.SAVED,
        payload={
            "request": {
                "conversation_id": "source",
                "turn_id": "turn",
                "action_approval_mode": "ask",
            }
        },
    )
    broker = scope(owned.BROKER, saved)
    executor = scope(owned.EXECUTOR, broker)
    local = scope(
        owned.LOCAL,
        executor,
        {
            "tool_id": "coding_file_create",
            "tool_call_id": "call",
            "arguments": {"path": "file.txt", "content": "exact"},
        },
    )
    root = saved.envelope.context
    invocation = NS(
        envelope=local.envelope,
        parent_invocation=executor,
        assert_current=guard,
        presentation_owner_principal_id=root.caller_principal.value,
        presentation_owner_session_id=root.caller_session_id,
    )
    request = owned.register_file_tool_request(
        {"path": "file.txt", "content": "exact", "workspace_id": "workspace"}, invocation
    )
    owned.bind_file_effect("effect", request, local.envelope.context, lambda: None)
    resume_context = replace(root, caller_principal=local.envelope.target_principal)
    resume = scope(
        ("tobkiri.service.interactive-effect.v1", "interactive_effect.manage"),
        local,
        {"phase": "resume", "effect_id": "effect"},
    )
    resume = replace(resume, envelope=replace(resume.envelope, context=resume_context))
    yield request, invocation, saved, resume, live, guards
    owned.close_file_tool_request(request["invocation_key"])


def test_exact_live_source_ask_selects_retained_resume(source_file):
    _, _, _, resume, _, guards = source_file
    assert owned.file_effect_source_resume("effect", resume.envelope.context, resume)
    assert guards


@pytest.mark.parametrize(
    "change",
    [
        "copied_parent",
        "foreign_effect",
        "phase",
        "foreign_capture",
        "context_copy",
        "foreign_caller",
        "source_session",
        "source_owner",
        "source_revoked",
        "agent",
        "full",
        "ambiguous_saved",
        "copied_ancestry",
        "closed",
    ],
)
def test_source_resume_requires_original_live_native_context(source_file, change):
    request, invocation, saved, resume, live, _ = source_file
    context = resume.envelope.context
    if change == "foreign_effect":
        assert not owned.file_effect_source_resume("foreign", context, resume)
        return
    if change == "copied_parent":
        resume = replace(
            resume, parent=replace(resume.parent, envelope=copy(resume.parent.envelope))
        )
    elif change == "copied_ancestry":
        resume = replace(
            resume, parent=replace(resume.parent, parent=replace(resume.parent.parent))
        )
    elif change == "phase":
        resume = replace(
            resume,
            envelope=replace(resume.envelope, payload={"phase": "status", "effect_id": "effect"}),
        )
    elif change == "foreign_capture":
        context = replace(context, profile_id="foreign")
        resume = replace(resume, envelope=replace(resume.envelope, context=context))
    elif change == "context_copy":
        context = replace(context)
    elif change == "foreign_caller":
        context = replace(context, caller_principal=saved.envelope.context.caller_principal)
        resume = replace(resume, envelope=replace(resume.envelope, context=context))
    elif change == "source_session":
        invocation.presentation_owner_session_id = "foreign-session"
    elif change == "source_owner":
        invocation.presentation_owner_principal_id = "foreign-owner"
    elif change == "source_revoked":
        live[0] = False
    elif change in {"agent", "full"}:
        saved.envelope.payload["request"]["action_approval_mode"] = change
    elif change == "ambiguous_saved":
        object.__setattr__(saved, "parent", replace(saved))
    elif change == "closed":
        owned.close_file_tool_request(request["invocation_key"])
        assert not owned.file_effect_source_resume("effect", context, resume)
        return
    with pytest.raises(PermissionError):
        owned.file_effect_source_resume("effect", context, resume)
