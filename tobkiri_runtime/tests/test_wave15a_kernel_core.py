"""Pack v4 replacements for the retired ``kernel_core`` test surface.

The old module owned flow loading, handler lookup, and mutable authority.  It
is intentionally gone; these tests keep the security invariants at the v4
composition boundary instead of asserting implementation details of a tombstone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope, GrantLifetime
from ecosystem.defaultspack.domain.runtime_v4 import (
    ProfileResolutionDenied,
    resolve_default_profile,
)
from tests.legacy_authority_contracts import (
    assert_legacy_service_fails_closed,
    assert_profile_resolver_requires_authority_snapshot,
    assert_retired_module_absent,
)
from tests.conformance_support.packaged_profile import load_packaged_profile_catalog
from tests.test_authority_v4_lifecycle import _digest
from tests.v4_batch_support import (
    assert_lease_is_single_use,
    assert_payload_mutations_denied,
    bounded_scope,
    harness,
)


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"


def test_kernel_core_is_physically_retired_and_not_importable() -> None:
    """No import path may resurrect the mutable pre-v4 kernel."""
    assert_retired_module_absent("core_runtime.kernel_core")


def test_kernel_and_system_handler_modules_are_physically_retired() -> None:
    """The split v3 handler authorities are absent as well as ``kernel_core``."""
    assert_retired_module_absent("core_runtime.kernel")
    assert_retired_module_absent("core_runtime.kernel_handlers_system")
    assert_retired_module_absent("core_runtime.kernel_context_builder")


def test_legacy_authority_entrypoint_fails_closed() -> None:
    """A caller cannot fall back to the deleted approval service."""
    assert_legacy_service_fails_closed()


def test_v4_kernel_binds_caller_target_activation_and_session(tmp_path: Path) -> None:
    """Payload fields cannot replace host-captured identity evidence."""
    assert_payload_mutations_denied(harness(tmp_path))


def test_v4_kernel_reserves_dispatches_and_finishes_one_lease(tmp_path: Path) -> None:
    """The replacement for mutable flow execution is a durable one-use lease."""
    assert_lease_is_single_use(harness(tmp_path))


def test_v4_kernel_rejects_scope_widening_without_reserving_use(tmp_path: Path) -> None:
    h = harness(tmp_path)
    unbounded = AuthorityScope(
        capability="host.http",
        semantics_digest=h.scope.semantics_digest,
        dimensions={"method": ("GET",)},
        quotas={},
    )
    with pytest.raises(AuthorityDenied, match="authority"):
        h.kernel.authorize(h.context(), unbounded)
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)


def test_v4_kernel_binds_opaque_request_to_exact_digest(tmp_path: Path) -> None:
    opaque = AuthorityScope(
        capability="host.http",
        semantics_digest=_digest("e"),
        dimensions={"path": ("/safe",), "method": ("GET",)},
        quotas={"max_bytes": 1024},
        exact_request_digest=_digest("another-request"),
        opaque=True,
    )
    h = harness(tmp_path, scope=opaque, grant_lifetime=GrantLifetime.ONE_SHOT, max_uses=1)
    with pytest.raises(AuthorityDenied, match="opaque scope"):
        h.kernel.authorize(h.context(), opaque)
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)


def test_v4_kernel_fences_outstanding_request_before_dispatch(tmp_path: Path) -> None:
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


def test_v4_kernel_rejects_stale_security_epoch(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied, match="stale SecurityEpoch"):
        h.kernel.authorize(h.context(security_epoch=2), h.scope)
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)


def test_profile_resolver_requires_authority_snapshot() -> None:
    assert_profile_resolver_requires_authority_snapshot()


def test_profile_resolver_uses_exact_v4_manifest_inventory() -> None:
    catalog = load_packaged_profile_catalog()
    approved = {str(manifest["pack"]["artifact_digest"]) for manifest in catalog.packs.values()}
    from tests.v4_batch_support import authority_bindings_for_profile

    bindings = authority_bindings_for_profile(catalog.profiles["defaults"])
    resolved = resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests=approved,
        authority_snapshot_digest="sha256:" + "9" * 64,
        authority_bindings=bindings,
        security_epoch=1,
    )
    assert resolved.profile["state"] == "resolved"
    assert resolved.lock["plan_digest"] == resolved.plan["plan_digest"]


def test_profile_resolver_rejects_artifact_not_in_approval_snapshot() -> None:
    catalog = load_packaged_profile_catalog()
    approved = {str(manifest["pack"]["artifact_digest"]) for manifest in catalog.packs.values()}
    approved.remove(catalog.packs["rumi_file_inspect_pack"]["pack"]["artifact_digest"])
    from tests.v4_batch_support import authority_bindings_for_profile

    bindings = authority_bindings_for_profile(catalog.profiles["defaults"])
    with pytest.raises(ProfileResolutionDenied, match="not approved"):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests=approved,
            authority_snapshot_digest="sha256:" + "9" * 64,
            authority_bindings=bindings,
            security_epoch=1,
        )


def test_v4_request_scope_is_narrower_than_the_captured_ceiling(tmp_path: Path) -> None:
    h = harness(tmp_path)
    narrow = bounded_scope(max_bytes=128)
    result = h.kernel.authorize(h.context(), narrow)
    assert result.lease_id
    assert h.store.grant_usage(h.grant.grant_id) == (1, 0)
