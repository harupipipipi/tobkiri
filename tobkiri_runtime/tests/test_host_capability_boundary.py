"""Host security invariants expressed through the Pack v4 composition root."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityScope,
    GrantLifetime,
    LeaseState,
)
from tests.legacy_authority_contracts import assert_retired_module_absent
from tests.test_authority_v4_lifecycle import _digest
from tests.test_tobkiri_host_v4_composition import _capture
from tests.v4_batch_support import (
    assert_lease_is_single_use,
    assert_payload_mutations_denied,
    harness,
)
from tobkiri_host.composition import HostV4Composition
from tobkiri_host.errors import ResolutionError
from tobkiri_host.runtime import V4DispatchSession, install_dispatch_session


def test_removed_capability_executor_cannot_be_used_for_host_effects() -> None:
    assert_retired_module_absent("core_runtime.capability_executor")
    assert_retired_module_absent("core_runtime.function_registry")


def test_host_authority_binds_caller_target_and_activation(tmp_path: Path) -> None:
    assert_payload_mutations_denied(harness(tmp_path))


def test_host_effect_requires_a_bounded_scope(tmp_path: Path) -> None:
    h = harness(tmp_path)
    unbounded = AuthorityScope(
        capability="host.http",
        semantics_digest=_digest("batch-scope"),
        dimensions={"method": ("GET",)},
        quotas={},
    )
    with pytest.raises(AuthorityDenied):
        h.kernel.check_static_path(h.context(), unbounded)
    with pytest.raises(AuthorityDenied):
        h.kernel.authorize(h.context(), unbounded)
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)


def test_host_effect_binds_opaque_request_digest(tmp_path: Path) -> None:
    opaque = AuthorityScope(
        capability="host.http",
        semantics_digest=_digest("e"),
        dimensions={"path": ("/safe",), "method": ("GET",)},
        quotas={"max_bytes": 1024},
        exact_request_digest=_digest("different-request"),
        opaque=True,
    )
    h = harness(
        tmp_path,
        scope=opaque,
        grant_lifetime=GrantLifetime.ONE_SHOT,
        max_uses=1,
    )
    with pytest.raises(AuthorityDenied, match="opaque scope"):
        h.kernel.authorize(h.context(), opaque)


def test_host_effect_lease_is_consumed_once_before_provider_dispatch(
    tmp_path: Path,
) -> None:
    assert_lease_is_single_use(harness(tmp_path))


def test_host_effect_dispatch_rejects_payload_request_digest_substitution(
    tmp_path: Path,
) -> None:
    h = harness(tmp_path)
    result = h.kernel.authorize(h.context(), h.scope)
    with pytest.raises(AuthorityDenied):
        h.kernel.dispatch(
            result.lease_token,
            target_domain_id=h.target_domain.domain_id,
            target_boot_epoch=h.target_domain.boot_epoch,
            request_digest=_digest("attacker-request"),
        )
    assert h.store.grant_usage(h.grant.grant_id) == (1, 0)


def test_host_effect_stale_domain_boot_epoch_is_denied(tmp_path: Path) -> None:
    h = harness(tmp_path)
    result = h.kernel.authorize(h.context(), h.scope)
    with pytest.raises(AuthorityDenied, match="context"):
        h.kernel.dispatch(
            result.lease_token,
            target_domain_id=h.target_domain.domain_id,
            target_boot_epoch=h.target_domain.boot_epoch + 1,
            request_digest=h.context().request_digest,
        )


def test_host_effect_stale_security_epoch_is_denied_before_lease(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied, match="stale SecurityEpoch"):
        h.kernel.authorize(h.context(security_epoch=2), h.scope)
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)


def test_host_effect_revoke_blocks_future_authorization(tmp_path: Path) -> None:
    h = harness(tmp_path)
    h.kernel.revoke(
        target_kind="function_principal",
        target_id=h.target.principal_id,
        reason="test revoke",
    )
    with pytest.raises(AuthorityDenied, match="revoked"):
        h.kernel.authorize(h.context(), h.scope)


def test_host_effect_request_fence_blocks_an_unconsumed_lease(tmp_path: Path) -> None:
    h = harness(tmp_path)
    result = h.kernel.authorize(h.context(), h.scope)
    assert h.kernel.fence_request(h.context().request_id) == [result.lease_id]
    with pytest.raises(AuthorityDenied):
        h.kernel.dispatch(
            result.lease_token,
            target_domain_id=h.target_domain.domain_id,
            target_boot_epoch=h.target_domain.boot_epoch,
            request_digest=h.context().request_digest,
        )


class _RecordingBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any, Any]] = []

    def invoke(self, frame, context, *, effect_scope):
        self.calls.append((frame, context, effect_scope))
        return {"ok": True}


def test_v4_dispatch_session_never_derives_identity_from_payload() -> None:
    broker = _RecordingBroker()
    expected_context = object()
    expected_scope = {"path": "/safe"}
    session = V4DispatchSession(
        broker=broker,
        context_for=lambda contract, operation: expected_context,
        effect_scope_for=lambda contract, operation, payload: expected_scope,
        providers={"contract.v4": ({"principal": "host-bound"},)},
        profile_id="defaults",
        plan_digest="sha256:" + "1" * 64,
        profile_revision="sha256:" + "2" * 64,
        activation_id="activation:boundary-test",
    )
    assert session.invoke(
        "contract.v4",
        "run",
        {"caller_principal": "attacker", "approved": True},
    ) == {"ok": True}
    _frame, context, scope = broker.calls[0]
    assert context is expected_context
    assert scope == expected_scope
    assert session.provider_metadata("contract.v4") == ({"principal": "host-bound"},)


class _Container:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def set_instance(self, name: str, instance: object) -> None:
        self.values[name] = instance


def test_dispatch_session_installation_rejects_noncanonical_identity() -> None:
    container = _Container()
    session = V4DispatchSession(
        broker=_RecordingBroker(),
        context_for=lambda _contract, _operation: object(),
        effect_scope_for=lambda _contract, _operation, _payload: {},
        providers={},
        profile_id="defaults",
        plan_digest="sha256:" + "2" * 64,
        profile_revision="sha256:" + "3" * 64,
        activation_id="activation:boundary-install-test",
    )
    install_dispatch_session(container, session)
    assert container.values["v4_dispatch_session"] is session


def test_host_composition_requires_exact_artifact_inventory(tmp_path: Path) -> None:
    _composition, resolved, activation, artifacts, routes, ceilings = _capture(tmp_path)
    with pytest.raises(ResolutionError, match="exactly equal"):
        HostV4Composition.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=activation,
            artifacts=artifacts[:-1],
            routes=routes,
            authority_ceilings=ceilings,
        )


def test_host_composition_rejects_injected_route_and_authority_ceiling(
    tmp_path: Path,
) -> None:
    _composition, resolved, activation, artifacts, routes, ceilings = _capture(tmp_path)
    with pytest.raises(ResolutionError, match="OperationCatalog routes"):
        HostV4Composition.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=activation,
            artifacts=artifacts,
            routes=routes[:-1],
            authority_ceilings=ceilings,
        )
    injected = dict(ceilings)
    injected[("sha256:" + "1" * 64, next(iter(ceilings))[1])] = next(
        iter(ceilings.values())
    )
    with pytest.raises(ResolutionError, match="authority ceilings"):
        HostV4Composition.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=activation,
            artifacts=artifacts,
            routes=routes,
            authority_ceilings=injected,
        )


def test_host_composition_rejects_stale_activation_record(tmp_path: Path) -> None:
    _composition, resolved, activation, artifacts, routes, ceilings = _capture(tmp_path)
    stale = dict(activation)
    stale["plan_digest"] = _digest("stale-plan")
    with pytest.raises(ResolutionError, match="ActivationRecord"):
        HostV4Composition.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=stale,
            artifacts=artifacts,
            routes=routes,
            authority_ceilings=ceilings,
        )


def test_host_lease_commit_records_audit_state(tmp_path: Path) -> None:
    h = harness(tmp_path)
    result = h.kernel.authorize(h.context(), h.scope)
    lease = h.kernel.dispatch(
        result.lease_token,
        target_domain_id=h.target_domain.domain_id,
        target_boot_epoch=h.target_domain.boot_epoch,
        request_digest=h.context().request_digest,
    )
    h.kernel.finish(lease.lease_id, state=LeaseState.FAILED, outcome_digest=_digest("failure"))
    assert h.store.audit_events()[-1]["event_state"] == "failed"
