"""Shared assertions for test groups migrated to the Pack v4 boundary.

The pre-v4 runtime authority modules were deliberately removed.  These helpers
keep the replacement tests explicit: a retired module must be absent on disk
and an import attempt must fail in a clean interpreter, while the canonical
Profile Resolver must reject an incomplete Authority Kernel snapshot.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core_runtime.legacy_runtime_removed import removed_authority_service
from ecosystem.defaultspack.domain.runtime_v4 import (
    ProfileResolutionDenied,
    resolve_default_profile,
)
from tests.conformance_support.packaged_profile import load_packaged_profile_catalog


RUNTIME = Path(__file__).resolve().parents[1]
SNAPSHOT_DIGEST = "sha256:" + "9" * 64


def assert_retired_module_absent(module_name: str) -> None:
    """Require a deleted legacy module to be absent and non-importable."""
    module_root = RUNTIME
    if module_name == "domain" or module_name.startswith("domain."):
        module_root = RUNTIME / "ecosystem" / "defaultspack"
    module_path = module_root.joinpath(*module_name.split(".")).with_suffix(".py")
    assert not module_path.exists(), f"retired runtime module still exists: {module_path}"

    import_code = f"import {module_name}"
    if module_name == "domain" or module_name.startswith("domain."):
        import_code = f"import sys; sys.path.insert(0, {str(module_root)!r}); {import_code}"
    result = subprocess.run(
        [sys.executable, "-c", import_code],
        cwd=RUNTIME,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr


def assert_legacy_service_fails_closed() -> None:
    """Require the explicit compatibility boundary to reject old authority use."""
    with pytest.raises(
        RuntimeError,
        match="legacy authority workflow is unavailable in Pack v4 production runtime",
    ):
        removed_authority_service()


def assert_profile_resolver_requires_authority_snapshot() -> None:
    """Require v4 Profile resolution to reject missing Kernel references."""
    catalog = load_packaged_profile_catalog()
    approved = {str(manifest["pack"]["artifact_digest"]) for manifest in catalog.packs.values()}
    with pytest.raises(ProfileResolutionDenied, match="Authority Kernel reference is missing"):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests=approved,
            authority_snapshot_digest=SNAPSHOT_DIGEST,
            authority_bindings={},
            security_epoch=1,
        )


def assert_profile_resolver_rejects_unapproved_artifact() -> None:
    """Require v4 Profile resolution to reject an incomplete approval set."""
    catalog = load_packaged_profile_catalog()
    approved = {str(manifest["pack"]["artifact_digest"]) for manifest in catalog.packs.values()}
    approved.remove(catalog.packs["rumi_file_inspect_pack"]["pack"]["artifact_digest"])
    from tests.v4_batch_support import authority_bindings_for_profile

    bindings = authority_bindings_for_profile(catalog.profiles["defaults"])
    with pytest.raises(ProfileResolutionDenied, match="not approved: rumi_file_inspect_pack"):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests=approved,
            authority_snapshot_digest=SNAPSHOT_DIGEST,
            authority_bindings=bindings,
            security_epoch=1,
        )


def assert_authority_kernel_rejects_payload_substitution(tmp_path: Path) -> None:
    """Require the canonical Authority Kernel to bind requests to activation."""
    from core_runtime.authority.v4 import AuthorityDenied
    from tests.test_authority_v4_lifecycle import _Harness

    harness = _Harness(tmp_path)
    with pytest.raises(AuthorityDenied):
        harness.kernel.authorize(
            harness.context(activation_id="activation-attacker"),
            harness.scope,
        )
