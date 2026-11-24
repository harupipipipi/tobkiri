"""Pack v4 replacements for the removed KernelCore proxy bootstrap tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityDenied, LeaseState
from tests.legacy_authority_contracts import assert_retired_module_absent
from tests.v4_batch_support import harness


def test_removed_proxy_authority_modules_are_absent() -> None:
    assert_retired_module_absent("core_runtime.kernel_core")
    assert_retired_module_absent("core_runtime.kernel_handlers_system")
    assert_retired_module_absent("core_runtime.ecosystem_nodes")


def test_v4_kernel_does_not_retry_after_a_denied_proxy_request(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied):
        h.kernel.authorize(h.context(activation_id="attacker"), h.scope)
    with pytest.raises(AuthorityDenied):
        h.kernel.authorize(h.context(activation_id="attacker"), h.scope)
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)


def test_v4_kernel_rejects_unknown_target_domain(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied, match="unavailable"):
        h.kernel.authorize(
            h.context(target_domain_id="domain-injected"), h.scope
        )


def test_v4_kernel_rejects_changed_target_boot_epoch(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied, match="binding"):
        h.kernel.authorize(
            h.context(target_boot_epoch=h.target_domain.boot_epoch + 1), h.scope
        )


def test_v4_kernel_rejects_changed_fencing_token(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied, match="binding"):
        h.kernel.authorize(h.context(fencing_token=99), h.scope)


def test_v4_kernel_dispatch_requires_the_original_request_digest(tmp_path: Path) -> None:
    h = harness(tmp_path)
    result = h.kernel.authorize(h.context(), h.scope)
    with pytest.raises(AuthorityDenied):
        h.kernel.dispatch(
            result.lease_token,
            target_domain_id=h.target_domain.domain_id,
            target_boot_epoch=h.target_domain.boot_epoch,
            request_digest="sha256:" + "0" * 64,
        )


def test_v4_kernel_fence_request_is_idempotent(tmp_path: Path) -> None:
    h = harness(tmp_path)
    result = h.kernel.authorize(h.context(), h.scope)
    assert h.kernel.fence_request(h.context().request_id) == [result.lease_id]
    assert h.kernel.fence_request(h.context().request_id) == []


def test_v4_kernel_rejects_replay_after_commit(tmp_path: Path) -> None:
    h = harness(tmp_path)
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
        outcome_digest="sha256:" + "1" * 64,
    )
    with pytest.raises(AuthorityDenied):
        h.kernel.dispatch(
            result.lease_token,
            target_domain_id=h.target_domain.domain_id,
            target_boot_epoch=h.target_domain.boot_epoch,
            request_digest=h.context().request_digest,
        )
