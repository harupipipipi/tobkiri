"""The removed kernel HTTP handler has no v4 authority role."""

from __future__ import annotations

from pathlib import Path

from tests.legacy_authority_contracts import assert_retired_module_absent
from tests.v4_batch_support import harness


def test_kernel_handler_port_resolver_is_physically_absent() -> None:
    assert_retired_module_absent("core_runtime.kernel_handlers_system")


def test_v4_authority_uses_activation_not_environment_port(tmp_path: Path) -> None:
    h = harness(tmp_path)
    result = h.kernel.authorize(h.context(), h.scope)
    assert result.lease_id.startswith("lease-")
    assert h.target_domain.domain_id == "domain-target"


def test_v4_target_domain_identity_is_exact(tmp_path: Path) -> None:
    h = harness(tmp_path)
    result = h.kernel.authorize(h.context(), h.scope)
    try:
        h.kernel.dispatch(
            result.lease_token,
            target_domain_id="domain-attacker",
            target_boot_epoch=h.target_domain.boot_epoch,
            request_digest=h.context().request_digest,
        )
    except Exception as exc:
        assert type(exc).__name__ == "AuthorityDenied"
    else:
        raise AssertionError("unknown target domain was accepted")


def test_v4_session_identity_is_canonical(tmp_path: Path) -> None:
    h = harness(tmp_path)
    assert h.context().activation_id == "activation-1"
