"""Invocation leases cannot extend their backing records' validity."""

from dataclasses import replace

import pytest

from core_runtime.authority.v4_models import AuthorityDenied
from tests.test_authority_v4_lifecycle import _digest, _Harness
from tests.test_tobkiri_host_authority_v4_adapter import _adapter, _Backend, _broker, _context
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.models import InvocationFrame


@pytest.mark.parametrize("kind", ["grant", "provider", "extension_trust"])
def test_lease_is_capped_by_backing_expiry(tmp_path, kind):
    harness = _Harness(tmp_path)
    record = replace(getattr(harness, kind), expires_at=1001.0)
    harness.store.put_record(record, replace=True)
    result = harness.kernel.authorize(harness.context(), harness.scope)
    assert result.expires_at == 1001.0
    harness.clock.value = 1001.0
    with pytest.raises(AuthorityDenied):
        harness.kernel.dispatch(
            result.lease_token,
            target_domain_id=harness.target_domain.domain_id,
            target_boot_epoch=harness.target_domain.boot_epoch,
            request_digest=_digest("5"),
        )
    assert all(event["event_state"] != "dispatched" for event in harness.store.audit_events())


@pytest.mark.parametrize("kind", ["grant", "provider", "extension_trust"])
def test_existing_longer_lease_rechecks_backing_expiry(tmp_path, kind):
    harness = _Harness(tmp_path)
    result = harness.kernel.authorize(harness.context(), harness.scope)
    # Represent an already-issued old-version lease whose envelope has the
    # original TTL. Backing validity must be checked independently of that TTL.
    harness.store.put_record(
        replace(getattr(harness, kind), expires_at=1001.0), replace=True
    )
    harness.clock.value = 1002.0
    with pytest.raises(AuthorityDenied, match="backing authority"):
        harness.kernel.dispatch(
            result.lease_token,
            target_domain_id=harness.target_domain.domain_id,
            target_boot_epoch=harness.target_domain.boot_epoch,
            request_digest=_digest("5"),
        )
    assert all(event["event_state"] != "dispatched" for event in harness.store.audit_events())


def test_earliest_deadline_still_permits_dispatch_before_expiry(tmp_path):
    harness = _Harness(tmp_path)
    for kind, deadline in (("grant", 1005.0), ("provider", 1004.0), ("extension_trust", 1003.0)):
        harness.store.put_record(replace(getattr(harness, kind), expires_at=deadline), replace=True)
    result = harness.kernel.authorize(harness.context(), harness.scope)
    assert result.expires_at == 1003.0
    harness.clock.value = 1002.999
    lease = harness.kernel.dispatch(
        result.lease_token,
        target_domain_id=harness.target_domain.domain_id,
        target_boot_epoch=harness.target_domain.boot_epoch,
        request_digest=_digest("5"),
    )
    assert lease.lease_id == result.lease_id


@pytest.mark.parametrize("deadline", [None, 1100.0])
def test_shorter_lease_ttl_is_preserved(tmp_path, deadline):
    harness = _Harness(tmp_path)
    for kind in ("grant", "provider", "extension_trust"):
        harness.store.put_record(replace(getattr(harness, kind), expires_at=deadline), replace=True)
    result = harness.kernel.authorize(harness.context(), harness.scope)
    assert result.expires_at == 1010.0


@pytest.mark.parametrize("kind", ["grant", "provider", "extension_trust"])
def test_broker_never_invokes_provider_after_backing_expiry(tmp_path, monkeypatch, kind):
    harness = _Harness(tmp_path)
    harness.store.put_record(replace(getattr(harness, kind), expires_at=1001.0), replace=True)
    adapter = _adapter(harness)
    reserve = adapter.reserve_effect
    calls = []

    def delayed_reservation(*args, **kwargs):
        reservation = reserve(*args, **kwargs)
        harness.clock.value = 1002.0
        return reservation

    def invoke(_self, _request):
        calls.append(True)
        return ProviderOutcome({"ok": True})

    monkeypatch.setattr(adapter, "reserve_effect", delayed_reservation)
    monkeypatch.setattr(_Backend, "invoke", invoke)
    broker = _broker(harness, adapter, ProviderOutcome({"ok": True}))
    try:
        with pytest.raises(AuthorityDenied):
            broker.invoke(
                InvocationFrame(
                    contract_id="host.http", version_range=">=1,<2",
                    operation_id="invoke", payload={"message": "hello"},
                    idempotency_key="expired-request",
                ),
                _context(harness), effect_scope=harness.scope.to_dict(),
            )
    finally:
        broker.close()
    assert not calls
    assert all(event["event_state"] != "dispatched" for event in harness.store.audit_events())
