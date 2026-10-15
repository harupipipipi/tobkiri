"""Exhaustive fail-closed tests for the AuthorityScope lattice."""

from __future__ import annotations

from dataclasses import replace
from itertools import product
from pathlib import Path
import sqlite3

import pytest

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityKernel,
    AuthorityScope,
    AuthorityStore,
    AuthorityValidationError,
    LeaseState,
    intersect_scopes,
)

from tests.test_authority_v4_lifecycle import _Harness, _Resolver, _digest


_DIMENSIONS = (None, ("*",), ("a",), ("b",), ("a", "b"))
_QUOTAS = (None, 0, 1, 2)


def _lattice_scope(
    dimension: tuple[str, ...] | None,
    quota: int | None,
) -> AuthorityScope:
    return AuthorityScope(
        capability="host.scope-test",
        semantics_digest=_digest("scope-lattice"),
        dimensions={} if dimension is None else {"resource": dimension},
        quotas={} if quota is None else {"units": quota},
    )


def _http_scope(
    *,
    paths: tuple[str, ...] | None = ("/safe",),
    methods: tuple[str, ...] | None = ("GET",),
    max_bytes: int | None = 1024,
) -> AuthorityScope:
    dimensions = {}
    if paths is not None:
        dimensions["path"] = paths
    if methods is not None:
        dimensions["method"] = methods
    quotas = {} if max_bytes is None else {"max_bytes": max_bytes}
    return AuthorityScope(
        capability="host.http",
        semantics_digest=_digest("e"),
        dimensions=dimensions,
        quotas=quotas,
    )


@pytest.mark.parametrize(
    ("requested", "allowed", "expected"),
    [
        (None, None, True),
        (None, ("*",), True),
        (("*",), None, True),
        (("*",), ("*",), True),
        (None, ("a",), False),
        (("*",), ("a",), False),
        (("a",), None, True),
        (("a",), ("*",), True),
        (("a",), ("a",), True),
        (("a",), ("a", "b"), True),
        (("a", "b"), ("a",), False),
        (("a",), ("b",), False),
    ],
)
def test_dimension_subset_table(
    requested: tuple[str, ...] | None,
    allowed: tuple[str, ...] | None,
    expected: bool,
) -> None:
    assert _lattice_scope(requested, 1).is_subset_of(_lattice_scope(allowed, 1)) is expected


@pytest.mark.parametrize(
    ("requested", "allowed", "expected"),
    [
        (None, None, True),
        (None, 0, False),
        (None, 2, False),
        (0, None, True),
        (2, None, True),
        (0, 0, True),
        (0, 1, True),
        (1, 1, True),
        (1, 2, True),
        (2, 1, False),
    ],
)
def test_quota_subset_table(
    requested: int | None,
    allowed: int | None,
    expected: bool,
) -> None:
    assert (
        _lattice_scope(("a",), requested).is_subset_of(_lattice_scope(("a",), allowed)) is expected
    )


def test_scope_subset_is_reflexive_and_transitive_exhaustively() -> None:
    scopes = [
        _lattice_scope(dimension, quota)
        for dimension, quota in product(
            _DIMENSIONS,
            _QUOTAS,
        )
    ]
    assert all(scope.is_subset_of(scope) for scope in scopes)

    for lower, middle, upper in product(scopes, repeat=3):
        if lower.is_subset_of(middle) and middle.is_subset_of(upper):
            assert lower.is_subset_of(upper)


def test_intersection_is_commutative_and_greatest_lower_bound_exhaustively() -> None:
    scopes = [
        _lattice_scope(dimension, quota)
        for dimension, quota in product(
            _DIMENSIONS,
            _QUOTAS,
        )
    ]

    for left, right in product(scopes, repeat=2):
        try:
            effective = intersect_scopes(left, right)
        except AuthorityValidationError as exc:
            assert "empty" in str(exc)
            assert not any(
                candidate.is_subset_of(left) and candidate.is_subset_of(right)
                for candidate in scopes
            )
            continue
        assert effective.to_dict() == intersect_scopes(right, left).to_dict()
        assert effective.is_subset_of(left)
        assert effective.is_subset_of(right)
        for candidate in scopes:
            if candidate.is_subset_of(left) and candidate.is_subset_of(right):
                assert candidate.is_subset_of(effective)


def test_intersection_preserves_restrictions_named_by_only_one_ceiling() -> None:
    unbounded = _http_scope(paths=None, methods=None, max_bytes=None)
    bounded = _http_scope()

    effective = intersect_scopes(unbounded, bounded)

    assert effective.to_dict() == bounded.to_dict()
    assert not unbounded.is_subset_of(effective)
    assert bounded.is_subset_of(unbounded)


@pytest.mark.parametrize(
    "request_scope",
    [
        _http_scope(paths=None),
        _http_scope(methods=None),
        _http_scope(max_bytes=None),
        _http_scope(paths=("*",)),
        _http_scope(methods=("*",)),
    ],
    ids=(
        "omitted-path",
        "omitted-method",
        "omitted-quota",
        "wildcard-path",
        "wildcard-method",
    ),
)
def test_unbounded_request_never_reaches_lease_against_bounded_authority(
    tmp_path: Path,
    request_scope: AuthorityScope,
) -> None:
    harness = _Harness(tmp_path)
    baseline_events = len(harness.store.audit_events())

    with pytest.raises(AuthorityDenied):
        harness.kernel.check_static_path(harness.context(), request_scope)
    with pytest.raises(AuthorityDenied):
        harness.kernel.authorize(harness.context(), request_scope)

    assert harness.store.grant_usage(harness.grant.grant_id) == (0, 0)
    assert len(harness.store.audit_events()) == baseline_events
    with sqlite3.connect(harness.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM invocation_leases").fetchone() == (0,)


def test_narrow_request_flows_from_approval_and_grant_to_persisted_lease(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    narrow = _http_scope(max_bytes=128)

    result = harness.kernel.authorize(harness.context(), narrow)
    dispatched = harness.kernel.dispatch(
        result.lease_token,
        target_domain_id=harness.target_domain.domain_id,
        target_boot_epoch=harness.target_domain.boot_epoch,
        request_digest=harness.context().request_digest,
    )
    assert dispatched.authorized_scope == narrow

    reloaded = AuthorityStore(
        harness.store.path,
        key_path=harness.store.key_path,
        clock=harness.clock,
    )
    stored_grant = reloaded.get_grant(harness.grant.grant_id)
    stored_provider = reloaded.get_provider_authority(harness.provider.record_id)
    stored_lease = reloaded.get_lease(result.lease_id)
    assert stored_grant is not None and stored_grant.scope == harness.scope
    assert stored_provider is not None and stored_provider.scope == harness.scope
    assert stored_lease is not None
    assert stored_lease[0].authorized_scope == narrow
    assert stored_lease[1] is LeaseState.DISPATCHED


def test_reloaded_bounded_authority_still_denies_omitted_request_fields(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    reloaded = AuthorityStore(
        harness.store.path,
        key_path=harness.store.key_path,
        clock=harness.clock,
    )
    restarted = AuthorityKernel(
        reloaded,
        _Resolver(harness.scope),
        clock=harness.clock,
    )

    with pytest.raises(AuthorityDenied):
        restarted.authorize(harness.context(), _http_scope(max_bytes=None))
    assert reloaded.grant_usage(harness.grant.grant_id) == (0, 0)


def test_approval_bundle_rejects_grant_broader_than_provider_ceiling(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    approval = replace(harness.approval, approval_id="approval-broad")
    grant = replace(
        harness.grant,
        grant_id="grant-broad",
        approval_id=approval.approval_id,
        scope=_http_scope(max_bytes=None),
    )

    with pytest.raises(AuthorityValidationError, match="exceeds Provider"):
        harness.kernel.commit_approval_bundle(
            approval,
            provider_authorities=(harness.provider,),
            grants=(grant,),
        )
    assert harness.store.get_approval(approval.approval_id) is None
    assert harness.store.get_grant(grant.grant_id) is None
