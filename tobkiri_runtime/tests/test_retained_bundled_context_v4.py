"""Keep active-only retained provenance explicit in runtime and UI reads."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core_runtime import admitted_pack_read_v4 as reads
from core_runtime import external_pack_catalog_v4 as external
from core_runtime import profile_catalog_v4
from core_runtime import retained_bundled_context_v4 as context
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import production_v4, profile_capture
from core_runtime.bundled_artifact_retention_v4 import (
    BundledArtifactRetentionDenied,
)
from core_runtime.pack_boundary import resolve_pack_root
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    RuntimeSurfaceError,
    RuntimeSurfaceErrorCode,
    RuntimeSurfaceService,
)
from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
from tests.test_bundled_artifact_retention_v4 import (
    PACK_ID,
    RUNTIME_ROOT,
    make_retention_fixture,
    retain_fixture,
)


def test_absent_store_does_not_capture_or_adopt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordinary reads cannot gain a new Authority lifecycle or recovery path."""

    def forbidden(*_args: Any) -> None:
        pytest.fail("no retained store means no additional capture")

    monkeypatch.setattr(context, "capture_retention_origin", forbidden)
    assert (
        context.retained_origin_for_verified_active_profile(
            object(), user_data_root=tmp_path
        )
        is None
    )
    assert not (tmp_path / "retained-bundled-artifacts-v4").exists()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_store_presence_cannot_authorize_unsafe_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    store = tmp_path / "retained-bundled-artifacts-v4"
    if kind == "file":
        store.write_text("not a store")
    else:
        other = tmp_path / "other"
        other.mkdir()
        store.symlink_to(other, target_is_directory=True)

    def forbidden(*_args: Any) -> None:
        pytest.fail("unsafe store must fail before Authority capture")

    monkeypatch.setattr(context, "capture_retention_origin", forbidden)
    with pytest.raises(BundledArtifactRetentionDenied, match="store is unsafe"):
        context.retained_origin_for_verified_active_profile(
            object(), user_data_root=tmp_path
        )


def test_real_active_snapshot_gets_a_fresh_matching_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    retain_fixture(fixture)
    active = profile_capture.capture_active_profile()
    captured = context.retained_origin_for_verified_active_profile(
        active, user_data_root=fixture.owner
    )
    assert captured is not None and captured is not fixture.origin
    assert captured.profile_binding == fixture.origin.profile_binding
    assert captured.bundle_digest == fixture.origin.bundle_digest
    assert captured.pack_pins == fixture.origin.pack_pins


@pytest.mark.parametrize("part", ["profile", "lock", "plan", "activation"])
def test_origin_cannot_be_attached_to_a_different_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, part: str
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    retain_fixture(fixture)
    active = profile_capture.capture_active_profile()
    altered = SimpleNamespace(
        resolved=SimpleNamespace(
            profile=deepcopy(active.resolved.profile),
            lock=deepcopy(active.resolved.lock),
            plan=deepcopy(active.resolved.plan),
        ),
        activation=deepcopy(active.activation),
    )
    if part == "profile":
        altered.resolved.profile["profile_id"] = "other"
    elif part == "lock":
        altered.resolved.lock["lock_digest"] = "sha256:" + "1" * 64
    elif part == "plan":
        altered.resolved.plan["bundle_digest"] = "sha256:" + "2" * 64
    else:
        altered.activation["fencing_token"] += 1
    with pytest.raises(BundledArtifactRetentionDenied, match="differs from"):
        context.retained_origin_for_verified_active_profile(
            altered, user_data_root=fixture.owner
        )


def test_profile_schema_read_uses_and_revalidates_exact_retained_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    retained = retain_fixture(fixture)
    active = profile_capture.capture_active_profile()
    current_other_revision = resolve_pack_root("rumi_ai_gateway_pack")
    original = external.resolve_pack_root

    def changed_source(pack_id: str, bundled_root: Path | None = None) -> Path:
        if pack_id == PACK_ID:
            return current_other_revision
        return original(pack_id, bundled_root)

    monkeypatch.setattr(external, "resolve_pack_root", changed_source)
    monkeypatch.setattr(reads, "resolve_pack_root", changed_source)
    service = RuntimeSurfaceService(
        snapshot_loader=lambda: active,
        catalog_loader=lambda: BundledCatalog.load(fixture.bundle),
    )
    try:
        result = service.read_profile()
        assert result["state"] == "ready"
        assert result["plan_digest"] == fixture.origin.plan_digest
        runtime = retained.root / "runtime/streaming.py"
        runtime.chmod(0o600)
        runtime.write_bytes(runtime.read_bytes() + b"\n# tampered retained bytes\n")
        with pytest.raises(RuntimeSurfaceError):
            service.read_profile()
    finally:
        service.close()


def test_production_origin_follows_authority_owner_and_exact_active_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    retain_fixture(fixture)
    active = profile_capture.capture_active_profile()
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "ambient-other-owner"))

    class RootsObserved(Exception):
        """Stop at the real production root-composition boundary."""

    def roots(
        pack_ids: tuple[str, ...],
        ecosystem_root: Path,
        *,
        artifact_pins: dict[str, str],
        origin: Any,
    ) -> None:
        assert profile_capture.runtime_user_data_root() == fixture.owner
        assert origin.user_data_root == fixture.owner
        assert origin.profile_binding == fixture.origin.profile_binding
        assert PACK_ID in pack_ids
        assert ecosystem_root == RUNTIME_ROOT / "ecosystem"
        assert artifact_pins == {
            row["identity"]: row["artifact_digest"]
            for row in active.resolved.lock["effective_set"]
        }
        raise RootsObserved

    monkeypatch.setattr(production_v4, "resolve_admitted_pack_roots", roots)
    with AuthorityStore(fixture.owner / "authority/v4.sqlite3") as authority:
        with pytest.raises(RootsObserved):
            production_v4.capture_production_dispatch(
                active,
                bundle_root=fixture.bundle,
                ecosystem_root=RUNTIME_ROOT / "ecosystem",
                authority_store=authority,
                activation_snapshot_loader=defaultspack_activation_snapshot_loader,
            )


@pytest.mark.parametrize("surface", ["settings", "profiles"])
def test_late_epoch_change_cannot_publish_a_ready_retained_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    retain_fixture(fixture)
    active = profile_capture.capture_active_profile()

    def revoke() -> dict[str, str]:
        with AuthorityStore(fixture.owner / "authority/v4.sqlite3") as authority:
            authority.advance_security_epoch("isolated late read revocation")
        return {"theme": "system"}

    if surface == "profiles":
        project = profile_catalog_v4.project_profile_catalog

        def late_projection(*args: Any, **kwargs: Any) -> dict[str, object]:
            result = project(*args, **kwargs)
            revoke()
            return result

        monkeypatch.setattr(
            profile_catalog_v4, "project_profile_catalog", late_projection
        )
    service = RuntimeSurfaceService(
        snapshot_loader=lambda: active,
        catalog_loader=lambda: BundledCatalog.load(fixture.bundle),
        user_settings_reader=revoke if surface == "settings" else None,
    )
    try:
        with pytest.raises(RuntimeSurfaceError) as caught:
            if surface == "settings":
                service.read_settings()
            else:
                service.read_profile_catalog()
        assert caught.value.code == RuntimeSurfaceErrorCode.STALE_REVISION
    finally:
        service.close()
