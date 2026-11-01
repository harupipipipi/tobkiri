"""Managed execution boundaries are captured by Host Composition in v4."""

from __future__ import annotations

from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from tests.legacy_authority_contracts import assert_retired_module_absent
from tests.test_tobkiri_host_v4_composition import _capture
from tests.v4_batch_support import harness
from tobkiri_host.composition import HostV4Composition
from tobkiri_host.errors import ResolutionError


def test_managed_sandbox_executor_is_not_a_legacy_authority() -> None:
    assert_retired_module_absent("core_runtime.capability_executor")


def test_host_flag_cannot_promote_an_unverified_profile(tmp_path: Path) -> None:
    h = harness(tmp_path)
    with pytest.raises(AuthorityDenied):
        h.kernel.authorize(h.context(activation_id="unverified"), h.scope)


def test_untrusted_artifact_and_context_cannot_bypass_plan(tmp_path: Path) -> None:
    _composition, resolved, activation, artifacts, routes, ceilings = _capture(tmp_path)
    with pytest.raises(ResolutionError):
        HostV4Composition.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=activation,
            artifacts=artifacts[:-1],
            routes=routes,
            authority_ceilings=ceilings,
        )


def test_v4_plan_rejects_unknown_authority_edge(tmp_path: Path) -> None:
    _composition, resolved, activation, artifacts, routes, ceilings = _capture(tmp_path)
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
