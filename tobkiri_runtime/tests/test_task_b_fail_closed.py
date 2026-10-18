"""Fail-closed Pack v4 authority tests replacing CapabilityExecutor calls."""

from __future__ import annotations

from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from tests.legacy_authority_contracts import assert_retired_module_absent
from tests.v4_batch_support import harness


def test_deleted_executor_and_registry_are_not_compatibility_shims() -> None:
    assert_retired_module_absent("core_runtime.capability_executor")
    assert_retired_module_absent("core_runtime.function_registry")


def test_untrusted_function_artifact_cannot_obtain_a_lease(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied):
        h.kernel.authorize(
            h.context(target=h.target, target_domain_id="domain-unknown"), h.scope
        )
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)


def test_client_approved_flag_cannot_bypass_v4_authority(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied):
        h.kernel.authorize(
            h.context(target_domain_id="domain-attacker"), h.scope
        )


def test_core_prefixed_payload_cannot_substitute_target_principal(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied):
        h.kernel.authorize(h.context(target=h.caller), h.scope)


def test_denied_authority_preserves_zero_usage(tmp_path: Path) -> None:
    h = harness(tmp_path)
    for activation_id in ("wrong-1", "wrong-2"):
        with pytest.raises(AuthorityDenied):
            h.kernel.authorize(h.context(activation_id=activation_id), h.scope)
    assert h.store.grant_usage(h.grant.grant_id) == (0, 0)
