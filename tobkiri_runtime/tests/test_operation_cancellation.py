"""Live cancellation is owner-scoped and never a durable completion proof."""

from dataclasses import replace
import time

import pytest

from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.operation_cancellation import OwnedCancellationHandles
from tobkiri_host.ports import OpaqueInvocationLease
from tests.test_tobkiri_host_execution_integration import context, digest


def _envelope() -> RequestEnvelope:
    return RequestEnvelope(
        context=context(), target_principal=OpaqueAuthorityRef("provider"),
        target_domain=OpaqueAuthorityRef("domain"), contract_id="contract",
        contract_version="1.0.0", operation_id="operation", payload={},
        request_digest=digest("request"), deadline_monotonic=time.monotonic() + 30,
        lease=OpaqueInvocationLease(b"lease"), idempotency_key=None,
    )


def _binding(registry, envelope, role, **overrides):
    values = {
        "group": ("pack", "saved"), "owner_principal": "owner",
        "owner_session": "session", "guard": lambda: None,
    }
    values.update(overrides)
    return registry.bind(envelope=envelope, role=role, **values)


def test_request_signals_only_live_execution_without_discarding_handle() -> None:
    registry = OwnedCancellationHandles()
    execution, stop = _envelope(), _envelope()
    execute = _binding(registry, execution, "execute")
    cancel = _binding(registry, stop, "stop")
    with execute.track("turn"):
        first = cancel.request("turn")
        second = cancel.request("turn")
        assert first.completed is second.completed
        assert not first.completed.is_set()
        assert execution.cancellation_requested.is_set()
        assert not stop.cancellation_requested.is_set()
        with pytest.raises(PermissionError):
            with execute.track("turn"):
                pytest.fail("duplicate execution must not replace the handle")
    assert first.completed.is_set()
    with pytest.raises(PermissionError):
        cancel.request("turn")


@pytest.mark.parametrize("override", [
    {"owner_principal": "other"}, {"owner_session": "other"},
    {"group": ("other-pack", "saved")}, {"group": ("pack", "other-group")},
])
def test_foreign_owner_or_group_cannot_signal(override) -> None:
    registry, execution = OwnedCancellationHandles(), _envelope()
    with _binding(registry, execution, "execute").track("turn"):
        with pytest.raises(PermissionError):
            _binding(registry, _envelope(), "stop", **override).request("turn")
        assert not execution.cancellation_requested.is_set()


@pytest.mark.parametrize("field,value", [
    ("profile_id", "other"), ("profile_revision", digest("revision")),
    ("activation_id", "other"), ("activation_digest", digest("activation")),
    ("plan_digest", digest("plan")), ("profile_authority_digest", digest("authority")),
    ("security_epoch", 10),
])
def test_foreign_capture_cannot_signal(field, value) -> None:
    registry, execution = OwnedCancellationHandles(), _envelope()
    foreign = replace(_envelope(), context=replace(context(), **{field: value}))
    with _binding(registry, execution, "execute").track("turn"):
        with pytest.raises(PermissionError):
            _binding(registry, foreign, "stop").request("turn")
        assert not execution.cancellation_requested.is_set()


def test_roles_guards_and_close_preserve_live_ownership() -> None:
    registry, execution = OwnedCancellationHandles(), _envelope()
    producer = _binding(registry, execution, "execute")
    consumer = _binding(registry, _envelope(), "stop")
    with pytest.raises(PermissionError):
        with consumer.track("turn"):
            pytest.fail("stop role cannot register work")
    with producer.track("turn"):
        with pytest.raises(PermissionError):
            producer.request("turn")
        def stale():
            raise PermissionError("stale")
        with pytest.raises(PermissionError, match="stale"):
            _binding(registry, _envelope(), "stop", guard=stale).request("turn")
        assert not execution.cancellation_requested.is_set()
        registry.close()
        assert execution.cancellation_requested.is_set()
        assert len(registry._active) == 1
    assert not registry._active
    with pytest.raises(PermissionError):
        with producer.track("new-turn"):
            pytest.fail("closed capture cannot register work")
