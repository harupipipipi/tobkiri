"""Retained production sessions keep their captured Host owner and freshness."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import copy
import shutil
from typing import Any, Iterator

import pytest

from core_runtime import external_pack_catalog_v4 as external
from core_runtime.authority.v4 import AuthorityDenied, AuthorityStore
from core_runtime.bootstrap import production_v4, profile_capture
from core_runtime.bundled_artifact_retention_v4 import (
    BundledArtifactRetentionDenied,
    RetainedBundledArtifact,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from ecosystem.defaultspack.domain.runtime_v4 import ProfileResolutionDenied
from scripts import migrate_pack_artifacts_v4 as generator
from tests.test_bundled_artifact_retention_v4 import (
    PACK_ID,
    RetentionFixture,
    make_retention_fixture,
    retain_fixture,
)
from tests.test_live_production_v4_dispatch import (
    _deny_production_test_internet,  # noqa: F401 - explicitly reused pytest fixture
)
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.credential_store import host_credential_store_factory
from tobkiri_host.runtime import V4DispatchSession
from tobkiri_protocol.validation import validate_file

SessionFixture = tuple[V4DispatchSession, RetentionFixture, RetainedBundledArtifact]
RUNTIME_ROOT = Path(profile_capture.__file__).resolve().parents[2]
pytestmark = pytest.mark.usefixtures("_deny_production_test_internet")


@pytest.fixture
def retained_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SessionFixture]:
    """Capture a real old active session against a valid same-Pack successor."""
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    retained = retain_fixture(fixture)
    active = profile_capture.capture_active_profile()
    successor = tmp_path / "current-ecosystem" / PACK_ID
    shutil.copytree(fixture.source, successor)
    record = copy.deepcopy(fixture.record)
    record["version"] = "9.9.9"
    with monkeypatch.context() as scoped:
        scoped.setattr(generator, "ECOSYSTEM", successor.parent)
        rendered = generator._render_record(record)
    generator.verify_rendered_artifacts(rendered)
    for relative, contents in rendered.items():
        (successor / relative).write_text(contents, encoding="utf-8")
    current = compile_pack_root(successor).artifact
    assert current.pack_id == PACK_ID
    assert current.digest != fixture.pin
    manifest = validate_file(successor / "pack.v4.json", "pack")
    executable = validate_file(successor / "executables.v4.json", "executable_catalog")
    load_catalog = production_v4._host_profile_catalog
    resolve_root = external.resolve_pack_root

    def current_catalog(bundle_root: Path, *, authority_user_data: Path) -> Any:
        catalog = load_catalog(bundle_root, authority_user_data=authority_user_data)
        # The current catalog port exposes a newer, genuinely compiled revision;
        # the active Profile, origin and old bytes use normal authentication.
        return replace(
            catalog,
            packs={**catalog.packs, PACK_ID: manifest},
            executable_catalogs={**catalog.executable_catalogs, PACK_ID: executable},
        )

    def current_source(pack_id: str, bundled_root: Path | None = None) -> Path:
        if pack_id == PACK_ID:
            return successor
        return resolve_root(pack_id, bundled_root)

    monkeypatch.setattr(production_v4, "_host_profile_catalog", current_catalog)
    monkeypatch.setattr(external, "resolve_pack_root", current_source)
    authority = AuthorityStore(fixture.owner / "authority/v4.sqlite3")
    session = None
    try:
        session = production_v4.capture_production_dispatch(
            active,
            bundle_root=fixture.bundle,
            ecosystem_root=RUNTIME_ROOT / "ecosystem",
            authority_store=authority,
            activation_snapshot_loader=defaultspack_activation_snapshot_loader,
            runtime_surface_factory=create_runtime_surface_services,
            credential_store_factory=host_credential_store_factory,
            # No authenticated VM backend is provisioned or invoked.
            backends=BackendRegistry(()),
        )
        assert session.plan_digest == fixture.origin.plan_digest
        assert session.activation_id == fixture.origin.activation_id
        assert session.owned_authority_store is authority
        session.assert_current()
        yield session, fixture, retained
    finally:
        if session is not None:
            session.close()
        authority.close()


def test_retained_production_check_uses_captured_owner_after_ambient_owner_change(
    retained_session: SessionFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, fixture, _retained = retained_session
    other = tmp_path / "ambient-owner"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(other))
    capture = profile_capture.capture_active_profile
    owners = []

    def observed_capture(*args: Any, **kwargs: Any) -> Any:
        owners.append(profile_capture.runtime_user_data_root())
        return capture(*args, **kwargs)

    monkeypatch.setattr(profile_capture, "capture_active_profile", observed_capture)
    session.assert_current()
    first_checks = len(owners)
    assert first_checks > 0
    session.assert_current()
    assert len(owners) > first_checks
    assert all(owner == fixture.owner.resolve() for owner in owners)
    assert not other.exists()


@pytest.mark.parametrize("change", ["content", "epoch", "root"])
def test_retained_production_check_rejects_drift_under_captured_owner(
    retained_session: SessionFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    session, fixture, retained = retained_session
    other = tmp_path / "ambient-owner"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(other))
    # A passing check under the changed ambient owner separates the intended
    # fence below from an unrelated missing-active-owner failure.
    session.assert_current()
    if change == "content":
        target = retained.root / "runtime/streaming.py"
        target.chmod(0o600)
        target.write_bytes(target.read_bytes() + b"\n# isolated byte drift\n")
        target.chmod(0o400)
    elif change == "epoch":
        with AuthorityStore(fixture.owner / "authority/v4.sqlite3") as store:
            store.advance_security_epoch("isolated retained production fence")
    else:
        retained.root.parent.chmod(0o700)
        retained.root.chmod(0o700)
        moved = retained.root.with_name("previous")
        retained.root.rename(moved)
        moved.chmod(0o500)
        shutil.copytree(moved, retained.root)
        retained.root.parent.chmod(0o500)
    with pytest.raises(
        (AuthorityDenied, ProfileResolutionDenied, BundledArtifactRetentionDenied),
        match="stale|bytes differ|identity|unsafe",
    ):
        session.assert_current()
    assert not other.exists()
