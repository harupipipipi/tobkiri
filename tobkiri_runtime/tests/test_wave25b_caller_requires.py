"""Caller-requirement invariants on the Pack v4 Authority Kernel."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope, GrantLifetime
from tests.legacy_authority_contracts import assert_retired_module_absent
from tests.test_authority_v4_lifecycle import _digest
from tests.v4_batch_support import bounded_scope, harness


def test_permission_manager_is_not_a_runtime_authority() -> None:
    assert_retired_module_absent("core_runtime.permission_manager")


def test_caller_requires_exact_authenticated_session(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied):
        h.kernel.authorize(h.context(caller_session_id="caller-forged"), h.scope)
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)


def test_caller_requires_exact_principal(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied):
        h.kernel.authorize(h.context(target=replace(h.target, operation_id="admin")), h.scope)


def test_caller_requires_all_captured_identity_fields(tmp_path: Path) -> None:
    h = harness(tmp_path)
    for changes in (
        {"profile_id": "other"},
        {"activation_id": "other"},
        {"activation_digest": "sha256:" + "0" * 64},
        {"plan_digest": "sha256:" + "0" * 64},
        {"fencing_token": 999},
    ):
        with pytest.raises(AuthorityDenied):
            h.kernel.authorize(h.context(**changes), h.scope)


def test_caller_requires_scope_subset_of_every_ceiling(tmp_path: Path) -> None:
    h = harness(tmp_path)
    requested = bounded_scope(path="/outside")
    with pytest.raises(AuthorityDenied, match="authority"):
        h.kernel.authorize(h.context(), requested)
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)


def test_caller_requires_no_lease_for_missing_authority(tmp_path: Path) -> None:
    h = harness(tmp_path)
    h.kernel.revoke(
        target_kind="grant", target_id=h.grant.grant_id, reason="caller test"
    )
    with pytest.raises(AuthorityDenied):
        h.kernel.authorize(h.context(), h.scope)
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)


def test_caller_requires_opaque_scope_request_binding(tmp_path: Path) -> None:
    opaque = AuthorityScope(
        capability="host.http",
        semantics_digest=_digest("e"),
        dimensions={"path": ("/safe",), "method": ("GET",)},
        quotas={"max_bytes": 1024},
        exact_request_digest="sha256:" + "a" * 64,
        opaque=True,
    )
    h = harness(tmp_path, scope=opaque, grant_lifetime=GrantLifetime.ONE_SHOT, max_uses=1)
    with pytest.raises(AuthorityDenied, match="opaque"):
        h.kernel.authorize(h.context(), opaque)


def test_caller_requires_one_shot_grant_to_be_consumed_once(tmp_path: Path) -> None:
    h = harness(tmp_path, grant_lifetime=GrantLifetime.ONE_SHOT, max_uses=1)
    first = h.kernel.authorize(h.context(), h.scope)
    with pytest.raises(AuthorityDenied, match="limit"):
        h.kernel.authorize(h.context(request_id="request-2"), h.scope)
    assert first.lease_id
