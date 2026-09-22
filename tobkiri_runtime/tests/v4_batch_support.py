"""Small fixtures for the residual Pack v4 contract migration batch."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
from typing import Any, Mapping

import pytest

from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope, LeaseState

from tests.test_authority_v4_lifecycle import _Harness, _digest


def harness(tmp_path: Path, **kwargs: Any) -> _Harness:
    """Build the canonical two-domain Authority Kernel fixture."""
    return _Harness(tmp_path, **kwargs)


def authority_bindings_for_profile(
    profile: Mapping[str, Any],
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Create one explicit, stable test reference for every requested edge."""

    bindings: dict[str, str] = {}
    for edge in profile["requested_edges"]:
        key = "|".join(
            str(edge[field])
            for field in (
                "caller_function_id",
                "target_provider_id",
                "contract_id",
                "operation_id",
            )
        )
        edge_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        bindings[key] = f"authority-ref:test.{edge_digest}"
    bindings.update(dict(overrides or {}))
    return bindings


def bounded_scope(*, path: str = "/safe", max_bytes: int = 1024) -> AuthorityScope:
    """Return a deliberately bounded request scope."""
    return AuthorityScope(
        capability="host.http",
        semantics_digest=_digest("e"),
        dimensions={"path": (path,), "method": ("GET",)},
        quotas={"max_bytes": max_bytes},
    )


def altered_context(h: _Harness, **changes: Any):
    """Return a context mutation used to prove payload binding is exact."""
    return h.context(**changes)


def assert_payload_mutations_denied(h: _Harness) -> None:
    """Assert caller, target, activation, session, and epoch are host-bound."""
    impostor = replace(h.target, operation_id="admin")
    mutations = (
        {"target": impostor},
        {"activation_id": "activation-attacker"},
        {"caller_session_id": "session-attacker"},
        {"security_epoch": 2},
        {"plan_digest": _digest("attacker-plan")},
    )
    for mutation in mutations:
        try:
            h.kernel.authorize(altered_context(h, **mutation), h.scope)
        except AuthorityDenied:
            continue
        raise AssertionError(f"payload mutation was accepted: {mutation}")
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)


def assert_lease_is_single_use(h: _Harness) -> None:
    """Assert reserve, dispatch, finish, and replay are durable and ordered."""
    result = h.kernel.authorize(h.context(), h.scope)
    lease = h.kernel.dispatch(
        result.lease_token,
        target_domain_id=h.target_domain.domain_id,
        target_boot_epoch=h.target_domain.boot_epoch,
        request_digest=h.context().request_digest,
    )
    h.kernel.finish(
        lease.lease_id,
        state=LeaseState.COMMITTED,
        outcome_digest=_digest("batch-outcome"),
    )
    try:
        h.kernel.dispatch(
            result.lease_token,
            target_domain_id=h.target_domain.domain_id,
            target_boot_epoch=h.target_domain.boot_epoch,
            request_digest=h.context().request_digest,
        )
    except AuthorityDenied:
        pass
    else:
        raise AssertionError("a committed InvocationLease was replayed")
    assert h.store.grant_usage(h.grant.grant_id) == (0, 1)


def assert_legacy_registry_fails_closed() -> None:
    """The retained offline registry shape cannot discover runtime Packs."""
    from backend_core.ecosystem.registry import (
        LegacyRegistryUnavailable,
        Registry,
        get_registry,
        reload_registry,
        resolve_load_order,
    )

    with pytest.raises(LegacyRegistryUnavailable):
        Registry().load_all_packs()
    with pytest.raises(LegacyRegistryUnavailable):
        get_registry()
    with pytest.raises(LegacyRegistryUnavailable):
        reload_registry()
    with pytest.raises(LegacyRegistryUnavailable):
        resolve_load_order(())


def assert_route_cutover(
    method: str,
    path: str,
    contract_id: str,
    operation_id: str,
) -> None:
    """Require a retired HTTP route to dispatch only as one captured operation.

    The route table is deliberately not used as an operation catalog.  This
    helper also proves that caller-supplied identity and approval fields do not
    replace the Host-captured context or effect scope.
    """
    from ecosystem.defaultspack.transport.http import DefaultsHttpServer
    from ecosystem.defaultspack.transport.registry import canonical_http_route_specs
    from tobkiri_host.runtime import V4DispatchSession

    assert (method, path) not in {
        (spec.method, spec.pattern) for spec in canonical_http_route_specs()
    }
    server = DefaultsHttpServer(facade=None)
    assert server._match_route(method, path) == (None, None, None, None, None)

    class Broker:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, Any, Mapping[str, Any]]] = []

        def invoke(
            self,
            frame: Any,
            context: Any,
            *,
            effect_scope: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            if (frame.contract_id, frame.operation_id) != (
                contract_id,
                operation_id,
            ):
                raise RuntimeError("operation is not pinned by the captured plan")
            self.calls.append((frame, context, effect_scope))
            return {"status": "ok"}

    broker = Broker()
    captured_context = {"profile_id": "profile:captured", "session_id": "session:1"}
    captured_scope = {"effect": operation_id, "approval": "host-bound"}
    session = V4DispatchSession(
        broker=broker,  # type: ignore[arg-type]
        context_for=lambda _contract, _operation: captured_context,
        effect_scope_for=lambda _contract, _operation, _payload: captured_scope,
        providers={},
        profile_id="profile:captured",
        plan_digest="sha256:" + "7" * 64,
    )
    payload = {
        "approved": True,
        "profile_id": "profile:forged",
        "session_id": "session:forged",
    }
    assert session.invoke(contract_id, operation_id, payload) == {"status": "ok"}
    frame, context, effect_scope = broker.calls[0]
    assert frame.payload == payload
    assert context is captured_context
    assert effect_scope is captured_scope
    with pytest.raises(RuntimeError, match="not pinned"):
        session.invoke(contract_id, f"{operation_id}.forged", {})


__all__ = [
    "authority_bindings_for_profile",
    "assert_lease_is_single_use",
    "assert_payload_mutations_denied",
    "altered_context",
    "bounded_scope",
    "harness",
    "assert_legacy_registry_fails_closed",
    "assert_route_cutover",
]
