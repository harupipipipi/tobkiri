"""Finite prefix guards use actual captured scope objects; no capture claim."""
from dataclasses import replace
import pytest
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tests.test_operation_cancellation import _envelope

from ecosystem.rumi_tool_broker_pack.runtime import chat_delivery as helper


def test_calendar_payload_does_not_normalize_unregistered_long_ancestry():
    envelope = replace(_envelope(), payload={'origin': 'calendar', 'approved': True})
    scope = None
    for _ in range(12):
        scope = CapturedInvocationScopeV4(envelope, lambda: None, scope)
    with pytest.raises(PermissionError, match='ancestry'):
        helper.captured_delivery_ancestors(scope, limit=10)


def test_each_scope_guard_is_rechecked_and_foreign_capture_denied():
    envelope = _envelope()
    calls = []
    parent = CapturedInvocationScopeV4(envelope, lambda: calls.append('parent'))
    child = CapturedInvocationScopeV4(envelope, lambda: calls.append('child'), parent)
    assert helper.captured_delivery_ancestors(child, limit=10) == [child, parent]
    assert calls == ['child', 'parent']
    capture = tuple(getattr(envelope.context, k) for k in helper.CAPTURE_FIELDS)
    foreign = replace(envelope, context=replace(envelope.context, security_epoch=envelope.context.security_epoch + 1))
    with pytest.raises(PermissionError, match='capture'):
        helper.captured_delivery_ancestors(CapturedInvocationScopeV4(foreign, lambda: None), limit=10, capture=capture)


def test_stale_parent_guard_stops_normalized_prefix():
    def stale():
        raise PermissionError('stale source')
    scope = CapturedInvocationScopeV4(_envelope(), stale)
    with pytest.raises(PermissionError, match='stale source'):
        helper.captured_delivery_ancestors(scope, limit=10)
