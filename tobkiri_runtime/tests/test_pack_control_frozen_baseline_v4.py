"""Runtime catalog projections use the Native's authenticated frozen baseline."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any

import pytest

from core_runtime import bundled_host_baseline_v4 as baselines
from core_runtime import external_pack_catalog_v4 as external
from core_runtime.bootstrap import profile_capture
from core_runtime.pack_control_v4 import (
    PACK_CONTROL_CONTRACT,
    _read_control_state,
    capture_pack_control_session,
)
from core_runtime.profile_pack_versions import (
    profile_pack_versions,
    select_profile_pack_version,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from tests.test_bundled_artifact_retention_v4 import PACK_ID, make_retention_fixture
from tests.test_bundled_host_baseline_v4 import _publish, _signed_successor
from tests.test_host_extension_dispatch_update_v4 import (
    _approve_gateway_profile_revision,
)
from tests.test_host_extension_update_v4 import _refresh
from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.validation import validate_file

UNCHANGED_NORMAL_PACK = "rumi_model_evals_pack"


@pytest.fixture
def frozen_successor_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Use real signed CAS admission, selection and normally issued Origins."""
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    _publish(fixture)
    source, trust = _signed_successor(fixture, tmp_path)
    admitted = external.admit_signed_pack_version(
        source,
        trust_store_path=trust,
        expected_predecessor_digest=fixture.pin,
        expected_catalog_revision=external.control_catalog_revision(),
    )
    view = profile_pack_versions("defaults")
    selected = next(row for row in view["packs"] if row["pack_id"] == PACK_ID)
    select_profile_pack_version({
        "profile_id": "defaults",
        "pack_id": PACK_ID,
        "artifact_digest": admitted["artifact_digest"],
        "expected_selected_digest": selected["selected_digest"],
        "expected_profile_revision": view["profile_definition_revision"],
        "expected_store_generation": view["store_generation"],
        "expected_catalog_revision": view["catalog_revision"],
    })
    _approve_gateway_profile_revision(fixture.owner, "defaults")
    active = profile_capture.capture_active_profile()
    session = capture_pack_control_session(
        active=active, bundle_root=fixture.bundle,
        runtime_surface_factory=create_runtime_surface_services,
    )
    session.invoke(PACK_CONTROL_CONTRACT, "pack.install", {
        "pack_id": UNCHANGED_NORMAL_PACK, "_session_id": "frozen-read-setup",
    })
    assert UNCHANGED_NORMAL_PACK in _read_control_state(
        session.profile_id, read_only=True,
    )
    drift = tmp_path / "new-source" / PACK_ID
    shutil.copytree(source, drift, ignore=shutil.ignore_patterns(".tobkiri"))
    manifest = validate_file(drift / "pack.v4.json", "pack")
    manifest["pack"]["version"] = "100.0.0"
    (drift / "pack.v4.json").write_bytes(canonical_json(manifest))
    _refresh(drift)
    original_root = external.resolve_pack_root
    monkeypatch.setattr(
        external, "resolve_pack_root",
        lambda pack_id, *args: (
            drift if pack_id == PACK_ID else original_root(pack_id, *args)
        ),
    )
    monkeypatch.setattr(
        baselines, "resolve_pack_root",
        lambda pack_id: drift if pack_id == PACK_ID else original_root(pack_id),
    )
    with pytest.raises(external.ExternalPackCatalogDenied, match="compatibility"):
        external.load_external_pack_catalog({PACK_ID: admitted["artifact_digest"]})
    yield session, fixture, admitted
    session.close()


@pytest.mark.parametrize("operation", ["catalog.read", "dashboard.read", "pack.status"])
def test_read_projection_keeps_frozen_baseline_until_payload_is_verified(
    frozen_successor_session: Any, operation: str,
) -> None:
    """All three real read callbacks survive Source drift without changing pins."""
    session, fixture, admitted = frozen_successor_session
    # Use the normal store path rather than assuming a storage-layout filename.
    from core_runtime.active_profile_store_v4 import ActiveProfileStore

    pointer = ActiveProfileStore(fixture.owner).path
    before = pointer.read_bytes()
    payload = {"_session_id": "frozen-read-session"}
    if operation == "pack.status":
        payload["pack_id"] = PACK_ID
    result = session.invoke(PACK_CONTROL_CONTRACT, operation, payload)
    if operation == "dashboard.read":
        assert result["kernel"]["status"] == "running"
        assert result["packs"]["enabled"] > 0
    else:
        row = (
            next(item for item in result["packs"] if item["pack_id"] == PACK_ID)
            if operation == "catalog.read" else result
        )
        assert row["pack_artifact_digest"] == admitted["artifact_digest"]
        assert row["installed"] is True
        if operation == "catalog.read":
            sentinel = next(
                item for item in result["packs"]
                if item["pack_id"] == UNCHANGED_NORMAL_PACK
            )
            assert sentinel["installed"] is True
    assert pointer.read_bytes() == before
    assert baselines.current_bundled_host_baseline_read() is None
