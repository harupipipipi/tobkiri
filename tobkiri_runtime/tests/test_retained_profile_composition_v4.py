"""Verified active origins reach catalog and lifecycle reads without admission."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core_runtime import admitted_pack_read_v4 as reads
from core_runtime import bundled_artifact_retention_v4 as retention
from core_runtime import external_pack_catalog_v4 as external
from core_runtime import pack_control_v4 as control
from core_runtime import retained_bundled_context_v4 as retained_context
from core_runtime.bootstrap import profile_capture
from core_runtime.bootstrap.external_profile_catalog import (
    catalog_for_verified_active_profile,
)
from core_runtime.pack_boundary import PackBoundaryError, resolve_pack_root
from tests.conformance_support.packaged_profile import load_packaged_profile_catalog
from tests.test_bundled_artifact_retention_v4 import (
    PACK_ID,
    make_retention_fixture,
    retain_fixture,
)
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.validation import validate_file


@pytest.fixture
def retained_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Retain real bound bytes and supply a distinct compiled current root."""
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    artifact = retain_fixture(fixture)
    active = profile_capture.capture_default_profile()
    catalog = load_packaged_profile_catalog()
    changed = deepcopy(catalog.packs[PACK_ID])
    changed["pack"]["artifact_digest"] = "sha256:" + "f" * 64
    current_catalog = replace(catalog, packs={**catalog.packs, PACK_ID: changed})
    current = resolve_pack_root("rumi_ai_gateway_pack")
    assert compile_pack_root(current).artifact.digest != artifact.artifact_digest
    external_resolve = external.resolve_pack_root
    read_resolve = reads.resolve_pack_root

    def current_external_root(pack_id: str, *args: Any) -> Path:
        return current if pack_id == PACK_ID else external_resolve(pack_id, *args)

    def current_read_root(pack_id: str, *args: Any) -> Path:
        return current if pack_id == PACK_ID else read_resolve(pack_id, *args)

    monkeypatch.setattr(external, "resolve_pack_root", current_external_root)
    monkeypatch.setattr(reads, "resolve_pack_root", current_read_root)
    return SimpleNamespace(
        fixture=fixture, artifact=artifact, active=active, catalog=current_catalog,
    )


def test_verified_active_origin_reaches_retained_manifest_and_executable_catalog(
    retained_active: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog helpers only forward a caller's origin after normal capture."""
    selected = retained_active
    origin = selected.fixture.origin
    before_profile = deepcopy(selected.active.resolved.profile)
    before_lock = deepcopy(selected.active.resolved.lock)
    before_plan = deepcopy(selected.active.resolved.plan)

    def forbidden_capture(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("generic catalog composition must not capture or mint an origin")

    monkeypatch.setattr(retention, "capture_retention_origin", forbidden_capture)
    monkeypatch.setattr(profile_capture, "capture_active_profile", forbidden_capture)
    merged = catalog_for_verified_active_profile(
        selected.catalog, selected.active, user_data=selected.fixture.owner,
        retained_bundled_origin=origin,
    )
    manifest = validate_file(selected.artifact.root / "pack.v4.json", "pack")
    executable = validate_file(
        selected.artifact.root / "executables.v4.json", "executable_catalog",
    )
    assert merged.packs[PACK_ID] == manifest
    assert merged.executable_catalogs[PACK_ID] == executable
    assert merged.packs[PACK_ID] != selected.catalog.packs[PACK_ID]
    assert merged.bases is selected.catalog.bases
    assert merged.shells is selected.catalog.shells
    assert merged.profiles is selected.catalog.profiles
    assert selected.active.resolved.profile == before_profile
    assert selected.active.resolved.lock == before_lock
    assert selected.active.resolved.plan == before_plan


def test_catalog_without_explicit_origin_cannot_use_retention_or_mint_authority(
    retained_active: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate/admission callers retain the current-Source default path."""
    selected = retained_active

    def forbidden_capture(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("no-origin catalog path must not capture retained authority")

    monkeypatch.setattr(retention, "capture_retention_origin", forbidden_capture)
    with pytest.raises(PackBoundaryError, match="revision is unavailable"):
        catalog_for_verified_active_profile(
            selected.catalog, selected.active, user_data=selected.fixture.owner,
        )


def test_lifecycle_capture_passes_only_its_verified_active_origin_to_root_read(
    retained_active: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifecycle entry point mints before entering the verified read scope."""
    selected = retained_active
    capture_origin = retained_context.retained_origin_for_verified_active_profile
    verified_read = reads.verified_pack_root_read
    observed: list[tuple[str, Any]] = []

    def origin_for_active(active: Any, *, user_data_root: Path) -> Any:
        assert active is selected.active
        assert user_data_root == selected.fixture.owner
        origin = capture_origin(active, user_data_root=user_data_root)
        observed.append(("capture", origin))
        return origin

    def root_read(pins: Any, *, origin: Any = None) -> Any:
        assert observed and observed[-1] == ("capture", origin)
        assert pins[PACK_ID] == selected.artifact.artifact_digest
        observed.append(("read", origin))
        return verified_read(pins, origin=origin)

    monkeypatch.setattr(
        retained_context,
        "retained_origin_for_verified_active_profile",
        origin_for_active,
    )
    monkeypatch.setattr(reads, "verified_pack_root_read", root_read)
    binding = control._capture_binding(selected.active)
    assert binding.profile_id == selected.fixture.origin.profile_id
    assert binding.plan_digest == selected.fixture.origin.plan_digest
    assert [stage for stage, _origin in observed] == ["capture", "read"]
    assert observed[0][1] is not None


@pytest.mark.parametrize("change", ["new_slot", "changed_kind"])
def test_retained_origin_cannot_create_or_change_a_host_extension_slot(
    retained_active: SimpleNamespace, change: str,
) -> None:
    """Exact old bytes do not turn an absent or Normal slot into Host authority."""
    selected = retained_active
    packs = dict(selected.catalog.packs)
    if change == "new_slot":
        del packs[PACK_ID]
    else:
        packs[PACK_ID] = deepcopy(packs[PACK_ID])
        packs[PACK_ID]["pack"]["kind"] = "normal_sandbox"
        packs[PACK_ID]["requirements"]["execution_boundary"] = "sandbox"
    with pytest.raises(
        control.PackControlDigestMismatch, match="identity is inconsistent",
    ):
        control.catalog_with_admitted_pack_closure(
            replace(selected.catalog, packs=packs), [PACK_ID],
            artifact_pins={PACK_ID: selected.artifact.artifact_digest},
            retained_bundled_origin=selected.fixture.origin,
        )


def test_retained_origin_cannot_supply_an_unbound_new_pin(
    retained_active: SimpleNamespace,
) -> None:
    """A new requested digest is never satisfied by the old active retention."""
    selected = retained_active
    with pytest.raises(external.ExternalPackCatalogDenied):
        control.catalog_with_admitted_pack_closure(
            selected.catalog, [PACK_ID],
            artifact_pins={PACK_ID: "sha256:" + "e" * 64},
            retained_bundled_origin=selected.fixture.origin,
        )
