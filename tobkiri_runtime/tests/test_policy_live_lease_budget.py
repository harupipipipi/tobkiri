"""Live-clock long policy lease retains short invocation and finished fences."""

from types import SimpleNamespace
from threading import Event
import time

import pytest

from core_runtime.authority.policy_exact_grant import PolicyExactGrantKernel
from core_runtime.authority.v4_models import LeaseState
from core_runtime.invocation_scope_v4 import assert_dispatched_invocation
from tests.test_authority_v4_lifecycle import _Harness, _MutableClock, _Resolver, _digest
from tests.test_tobkiri_host_authority_v4_adapter import _context


@pytest.mark.parametrize("stop", ["deadline", "finished"])
def test_live_policy_300_second_lease_keeps_actual_invocation_fences(tmp_path, monkeypatch, stop):
    """A longer sealed parent lifetime cannot extend the actual source invocation."""
    monkeypatch.setattr(_MutableClock, "__call__", lambda self: time.time())
    harness = _Harness(tmp_path)
    kernel = PolicyExactGrantKernel(
        harness.store,
        _Resolver(harness.scope),
        clock=time.time,
        lease_ttl_seconds=300,
    )
    kernel.policy_roots = {}
    authorization = kernel.authorize(harness.context(), harness.scope)
    lease = kernel.dispatch(
        authorization.lease_token,
        target_domain_id=harness.target_domain.domain_id,
        target_boot_epoch=harness.target_domain.boot_epoch,
        request_digest=_digest("5"),
    )
    envelope = SimpleNamespace(
        context=_context(harness),
        target_principal=SimpleNamespace(value=harness.target.principal_id),
        operation_id=harness.target.operation_id,
        request_digest=_digest("5"),
        lease=SimpleNamespace(token=authorization.lease_token.encode("ascii")),
        cancellation_requested=Event(),
        deadline_monotonic=time.monotonic() + (0.02 if stop == "deadline" else 60),
    )
    assert_dispatched_invocation(envelope, harness.store)
    assert lease.expires_at > time.time() + 250
    if stop == "deadline":
        time.sleep(0.03)
    else:
        kernel.finish(lease.lease_id, state=LeaseState.COMMITTED, outcome_digest=_digest("7"))
    effects = []
    with pytest.raises(PermissionError):
        assert_dispatched_invocation(envelope, harness.store)
        effects.append("effect")
    assert effects == []
