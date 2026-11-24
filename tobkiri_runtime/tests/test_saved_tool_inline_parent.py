"""Modeled retained scope / genuine registry scheduling; no native capture claim."""

from dataclasses import replace
from typing import Any

import pytest
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4 as Scope
from tests.test_operation_cancellation import _child_envelope
from tests.test_saved_tool_recipient_selection import recipient
from tobkiri_host.finite_chat_dispatch import select_inline_chat_dispatch
from tobkiri_host.saved_tool_context import NestedToolBinding


@pytest.mark.parametrize(
    "operation",
    [
        "rumi_default_tools_pack.calculator-evaluate",
        "rumi_default_tools_pack.files-read-operation",
    ],
)
def test_retained_original_executor_scope_selects_real_recipient_child(
    operation: str,
) -> None:
    """Original parent scope keeps supported nested reads in the finite tree."""
    with recipient(public_track=True) as case:
        retained = Scope(
            case.invocation.envelope,
            case.invocation.assert_current,
            case.invocation.parent_invocation,
        )
        binding = NestedToolBinding(
            case.invocation.envelope.context,
            {},
            "publisher",
            case.proof,
            lambda: None,
            retained,
        )
        child = replace(
            _child_envelope(retained.envelope),
            contract_id="tobkiri.service.tool.local.operation.v1",
            operation_id=operation,
        )
        assert binding.inline_parent_scope is retained
        assert select_inline_chat_dispatch(
            child, binding.cancellation_proof, binding.inline_parent_scope
        )


def test_five_positional_binding_keeps_original_none_control() -> None:
    """Existing five-argument callers retain the absence of an inline scope."""
    with recipient(public_track=True) as case:
        binding = NestedToolBinding(
            case.invocation.envelope.context, {}, "publisher", case.proof, lambda: None
        )
        assert binding.inline_parent_scope is None
        child = replace(
            _child_envelope(case.invocation.envelope),
            contract_id="tobkiri.service.tool.local.operation.v1",
            operation_id="rumi_default_tools_pack.calculator-evaluate",
        )
        with pytest.raises(PermissionError, match="parent is unavailable"):
            select_inline_chat_dispatch(
                child, binding.cancellation_proof, binding.inline_parent_scope
            )
        assert (
            select_inline_chat_dispatch(child, None, binding.inline_parent_scope)
            is False
        )


@pytest.mark.parametrize(
    "changed",
    [
        "missing",
        "wrong",
        "cloned-envelope",
        "cancelled",
        "revoked",
        "expired",
        "completed",
        "foreign-capture",
    ],
)
def test_finite_recipient_parent_does_not_admit_changed_identity_or_liveness(
    changed: str,
) -> None:
    """Real proof/current guard stays mandatory after scope propagation."""
    with recipient(public_track=True) as case:
        parent: Any = Scope(
            case.invocation.envelope,
            case.invocation.assert_current,
            case.invocation.parent_invocation,
        )
        child = replace(
            _child_envelope(parent.envelope),
            contract_id="tobkiri.service.tool.local.operation.v1",
            operation_id="rumi_default_tools_pack.calculator-evaluate",
        )
        if changed == "missing":
            parent = None
        elif changed == "wrong":
            parent = case.source
        elif changed == "cloned-envelope":
            parent = Scope(
                replace(parent.envelope), parent.assert_current, parent.parent
            )
        elif changed == "cancelled":
            case.proof.request()
        elif changed == "revoked":
            case.live[0] = False
        elif changed == "expired":
            # Expire an actual enrolled child; preserve its exact object identity.
            object.__setattr__(parent.envelope, "deadline_monotonic", 0.0)
        elif changed == "completed":
            case.futures[2].set_result(None)
        elif changed == "foreign-capture":
            child = replace(child, context=replace(child.context, profile_id="foreign"))
        with pytest.raises(PermissionError) as rejected:
            select_inline_chat_dispatch(child, case.proof, parent)
        expected = {
            "missing": "finite message scheduling parent is unavailable",
            "wrong": "finite message scheduling parent is unavailable",
            "cloned-envelope": "finite message scheduling parent is unavailable",
            "cancelled": "tracked Saved execution parent changed",
            "revoked": "original owner revoked",
            "expired": "finite message scheduling parent is unavailable",
            "completed": "finite message scheduling parent is unavailable",
            "foreign-capture": "finite message scheduling capture changed",
        }
        assert str(rejected.value) == expected[changed]
