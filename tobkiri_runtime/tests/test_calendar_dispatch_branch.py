"""Scheduling units with actual envelopes/registry; no capture claim."""
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace

import pytest
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tests.test_operation_cancellation import _envelope, _child_envelope

from tobkiri_host.operation_cancellation import OwnedCancellationHandles
from tobkiri_host.calendar_dispatch_branch import CalendarDispatchBranch, is_calendar_effect_dispatch


def setup():
    payload = dict(profile_id="profile-1", operation="dispatch", action_id="chat.saved",
                   payload=dict(profile_id="profile-1", message="hello", conversation_id=None),
                   idempotency_key="key", schedule_id="schedule", lease_id="lease")
    source = replace(_envelope(), contract_id="tobkiri.action.job.v1",
                     operation_id="rumi_job_action_broker_pack.job-action-broker", payload=payload)
    scope = CapturedInvocationScopeV4(source, lambda: None)
    target = replace(_child_envelope(source), contract_id="tobkiri.action.job.adapter.v2",
                     contract_version="2.0.0", operation_id="rumi_turn_runtime_pack.chat-saved-job-adapter",
                     payload=dict(payload), idempotency_key="key")
    binding = SimpleNamespace(principal_ref=target.target_principal,
                              operation=SimpleNamespace(contract_version="2.0.0"))
    registry = OwnedCancellationHandles()
    branch = CalendarDispatchBranch(scope, registry, ("host-owner", "host-session"), binding)
    target = replace(target, cancellation_requested=branch.cancellation)
    return branch, source, target, binding, registry


def test_calendar_real_future_before_entry_and_child_stop_does_not_stop_clock():
    branch, source, target, binding, registry = setup()
    with branch.parent_scope(target, binding, None) as proof:
        identifier = proof.reserve_independent_calendar_child(target)
        with pytest.raises(PermissionError):
            with proof.independent_calendar_execution_scope(target):
                pass
        future = Future()
        future.set_running_or_notify_cancel()
        proof.bind_child(identifier, future)
        with proof.independent_calendar_execution_scope(target):
            target.cancellation_requested.set()
            assert not source.cancellation_requested.is_set()
        future.set_result(None)
        proof.record_resource_drain(future)
    assert not registry._records


@pytest.mark.parametrize("change", ["binding", "scope", "event", "deadline", "principal"])
def test_calendar_selected_identity_and_parent_ceiling_cannot_change(change):
    branch, source, target, binding, registry = setup()
    if change == "binding":
        binding = SimpleNamespace(**vars(binding))
    elif change == "scope":
        with pytest.raises(PermissionError):
            branch.assert_selected(replace(branch.scope), target)
        return
    elif change == "event":
        import threading
        target = replace(target, cancellation_requested=threading.Event())
    elif change == "deadline":
        target = replace(target, deadline_monotonic=source.deadline_monotonic + 1)
    elif change == "principal":
        target = replace(target, target_principal=source.context.caller_principal)
    with pytest.raises(PermissionError):
        with branch.parent_scope(target, binding, None):
            pass
    assert not registry._records


@pytest.mark.parametrize("operation", ["describe", "status", "cancel"])
def test_non_dispatch_retains_ordinary_adapter_route(operation):
    branch, source, target, binding, registry = setup()
    assert not is_calendar_effect_dispatch(source, target.contract_id,
        target.operation_id, {"profile_id": source.context.profile_id,
                              "operation": operation})


def test_dispatch_selects_only_exact_calendar_route_before_selected_guards():
    branch, source, target, binding, registry = setup()
    assert is_calendar_effect_dispatch(source, target.contract_id,
                                       target.operation_id, target.payload)
    assert not is_calendar_effect_dispatch(replace(source, operation_id="status"),
        target.contract_id, target.operation_id, target.payload)
    assert not is_calendar_effect_dispatch(source, target.contract_id,
        "unselected-adapter", target.payload)
    with pytest.raises(PermissionError):
        with branch.parent_scope(replace(target, target_principal=type(target.target_principal)("foreign-adapter")),
                                 binding, None):
            pass
