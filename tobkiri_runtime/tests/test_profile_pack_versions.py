"""Same-ID revisions need definition CAS and fresh, exact Profile activation."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from core_runtime.active_profile_store_v4 import ActiveProfileStore
from core_runtime.bootstrap.profile_capture import (
    capture_active_profile,
    capture_default_profile,
    capture_profile,
    host_profile_catalog,
    prepare_default_profile_confirmation,
    prepare_profile_confirmation,
)
from core_runtime.external_pack_catalog_v4 import (
    admit_signed_external_pack,
    admit_signed_pack_version,
    control_catalog_revision,
    resolve_admitted_pack_root,
)
from core_runtime.pack_control_v4 import catalog_with_admitted_pack_closure
from core_runtime.profile_catalog_v4 import bundle_lock_digest, profile_catalog_digest
from core_runtime.profile_definition_store_v4 import (
    ProfileDefinitionStore,
    ProfileDefinitionStoreConflict,
)
from core_runtime.profile_pack_versions import profile_pack_versions, select_profile_pack_version
from core_runtime.profile_runtime_port import require_profile_runtime
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    RuntimeProfileChangeService,
    RuntimeSurfaceService,
    RuntimeSurfaceError,
)
from tests.test_external_pack_catalog_v4 import PACK_ID, _signed_external_pack
from tobkiri_protocol.canonical import canonical_digest

PROFILE_ID = "revision-profile"


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    optional: bool = False,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    source, trust = _signed_external_pack(tmp_path)
    first = dict(admit_signed_external_pack(source, trust_store_path=trust))
    capture_default_profile(confirmation=prepare_default_profile_confirmation())
    catalog, _ = catalog_with_admitted_pack_closure(host_profile_catalog(), [PACK_ID])
    definition = deepcopy(catalog.profiles["defaults"])
    definition["profile_id"] = PROFILE_ID
    definition["display_name"] = "Pack revision fixture"
    if not optional:
        definition["packs"].append(
            {"pack_id": PACK_ID, "artifact_digest": first["artifact_digest"], "role": "provider"}
        )
        definition["requested_edges"].extend(
            require_profile_runtime().dynamic_profile_edges(catalog, "defaults", (PACK_ID,))
        )
    ProfileDefinitionStore(user_data).create_profile(definition)
    capture_profile(PROFILE_ID, confirmation=prepare_profile_confirmation(PROFILE_ID))
    next_release = tmp_path / "next-release"
    next_release.mkdir()
    source, trust = _signed_external_pack(
        next_release, version="1.1.0", runtime_suffix="\n# revision two\n"
    )
    second = dict(
        admit_signed_pack_version(
            source,
            trust_store_path=trust,
            expected_predecessor_digest=first["artifact_digest"],
            expected_catalog_revision=control_catalog_revision(),
        )
    )
    return user_data, first, second


def _payload(view: dict[str, Any], digest: str) -> dict[str, Any]:
    selected = next(row for row in view["packs"] if row["pack_id"] == PACK_ID)
    return {
        "profile_id": PROFILE_ID,
        "pack_id": PACK_ID,
        "artifact_digest": digest,
        "expected_selected_digest": selected["selected_digest"],
        "expected_profile_revision": view["profile_definition_revision"],
        "expected_store_generation": view["store_generation"],
        "expected_catalog_revision": view["catalog_revision"],
    }


def _activate(user_data: Path) -> None:
    catalog = host_profile_catalog()
    predecessor = ActiveProfileStore(user_data).load(verify_snapshot=True)
    assert predecessor is not None
    definition = catalog.profiles[PROFILE_ID]
    surface = RuntimeSurfaceService(catalog_loader=host_profile_catalog)
    service = RuntimeProfileChangeService(surface_service=surface, user_data_root=user_data)
    result = service.resolve(
        {
            "profile_id": PROFILE_ID,
            "expected_profile_revision": predecessor.profile_revision,
            "expected_plan_digest": predecessor.plan_digest,
            "desired_pack_ids": [
                row["pack_id"] for row in definition["packs"] if row.get("role") != "application"
            ],
            "profile_definition_digest": canonical_digest(definition),
            "profile_catalog_digest": profile_catalog_digest(catalog),
            "bundle_lock_digest": bundle_lock_digest(catalog),
        },
        session_id="revision-session",
    )
    view = surface.read_profile_catalog(session_id="revision-session")["data"]
    assert next(row for row in view["profiles"] if row["profile_id"] == PROFILE_ID)["available"]
    assert ActiveProfileStore(user_data).load(verify_snapshot=True) == predecessor
    with pytest.raises(RuntimeSurfaceError):
        service.activate(
            {"approval_id": "unapproved", "approval_digest": "sha256:" + "f" * 64},
            session_id="revision-session",
        )
    review = service.review(
        {"candidate_id": result["candidate_id"], "candidate_digest": result["candidate_digest"]},
        session_id="revision-session",
    )
    approved = service.approve(
        {"candidate_id": review["candidate_id"], "candidate_digest": review["candidate_digest"]},
        session_id="revision-session",
    )
    service.activate(
        {"approval_id": approved["approval_id"], "approval_digest": approved["approval_digest"]},
        session_id="revision-session",
    )
    surface.close()


def test_select_activate_and_rollback_retains_old_active_and_cas(tmp_path, monkeypatch):
    user_data, first, second = _setup(tmp_path, monkeypatch)
    old_root = resolve_admitted_pack_root(PACK_ID, artifact_digest=first["artifact_digest"])
    old_bytes = (old_root / "runtime/echo.py").read_bytes()
    old = capture_active_profile()
    pointer_path = user_data / "profiles/active.json"
    before = pointer_path.read_bytes()
    view = profile_pack_versions(PROFILE_ID)
    result = select_profile_pack_version(_payload(view, second["artifact_digest"]))
    assert result["activation_required"]
    assert pointer_path.read_bytes() == before
    assert capture_active_profile().resolved.plan == old.resolved.plan
    store = ProfileDefinitionStore(user_data)
    current = store.get_profile(PROFILE_ID)
    assert current is not None and current.parent_revision == view["profile_definition_revision"]
    projection = RuntimeSurfaceService(catalog_loader=host_profile_catalog)
    entry = next(
        row
        for row in projection.read_profile_catalog()["data"]["profiles"]
        if row["profile_id"] == PROFILE_ID
    )
    assert entry["available"]
    # The active closure still projects the predecessor while its definition selects the successor.
    assert (
        next(row for row in entry["pack_closure"] if row["pack_id"] == PACK_ID)["artifact_digest"]
        == first["artifact_digest"]
    )
    projection.close()
    with pytest.raises(ProfileDefinitionStoreConflict):
        select_profile_pack_version(_payload(view, first["artifact_digest"]))
    _activate(user_data)
    assert (
        next(
            row
            for row in capture_active_profile().resolved.lock["effective_set"]
            if row["identity"] == PACK_ID
        )["artifact_digest"]
        == second["artifact_digest"]
    )
    select_profile_pack_version(
        _payload(profile_pack_versions(PROFILE_ID), first["artifact_digest"])
    )
    _activate(user_data)
    assert (
        next(
            row
            for row in capture_active_profile().resolved.lock["effective_set"]
            if row["identity"] == PACK_ID
        )["artifact_digest"]
        == first["artifact_digest"]
    )
    assert (old_root / "runtime/echo.py").read_bytes() == old_bytes


@pytest.mark.parametrize("change", ["unknown", "catalog", "generation", "selected", "authority"])
def test_selection_rejects_unknown_stale_or_authority_fields_without_writes(
    tmp_path, monkeypatch, change
):
    user_data, first, second = _setup(tmp_path, monkeypatch)
    payload = _payload(profile_pack_versions(PROFILE_ID), second["artifact_digest"])
    if change == "unknown":
        payload["artifact_digest"] = "sha256:" + "f" * 64
    elif change == "catalog":
        payload["expected_catalog_revision"] = "sha256:" + "f" * 64
    elif change == "generation":
        payload["expected_store_generation"] += 1
    elif change == "selected":
        payload["expected_selected_digest"] = second["artifact_digest"]
    else:
        payload["approved"] = True
    before = ProfileDefinitionStore(user_data).snapshot()
    active = ActiveProfileStore(user_data).load(verify_snapshot=True)
    with pytest.raises((ValueError, ProfileDefinitionStoreConflict)):
        select_profile_pack_version(payload)
    assert ProfileDefinitionStore(user_data).snapshot() == before
    assert ActiveProfileStore(user_data).load(verify_snapshot=True) == active


def test_same_revision_noop_checks_cas_without_appending_history(tmp_path, monkeypatch):
    user_data, first, _second = _setup(tmp_path, monkeypatch)
    before = ProfileDefinitionStore(user_data).snapshot()
    result = select_profile_pack_version(
        _payload(profile_pack_versions(PROFILE_ID), first["artifact_digest"])
    )
    assert result["activation_required"] is False
    assert ProfileDefinitionStore(user_data).snapshot() == before


@pytest.mark.parametrize("initially_enabled", [False, True])
def test_optional_intent_preserves_old_active_and_requires_fresh_install_approval(
    tmp_path,
    monkeypatch,
    initially_enabled,
):
    from core_runtime.pack_control_v4 import PackControlDenied, _required_profile_pack_ids
    from tests.test_pack_control_v4 import _capture_control_session, _invoke

    user_data, first, second = _setup(tmp_path, monkeypatch, optional=True)
    store = ProfileDefinitionStore(user_data)
    original = deepcopy(dict(store.get_profile(PROFILE_ID).profile))

    def approve(session):
        _invoke(session, "pack.install", {"pack_id": PACK_ID})
        candidate = _invoke(session, "approval.candidate", {"pack_id": PACK_ID})
        _invoke(
            session,
            "approval.approve",
            {"pack_id": PACK_ID, "candidate_id": candidate["candidate_id"]},
        )

    if initially_enabled:
        session = _capture_control_session()
        approve(session)
        _invoke(session, "pack.enable", {"pack_id": PACK_ID})
        session.close()
    predecessor = capture_active_profile()
    pointer_path = user_data / "profiles/active.json"
    pointer_bytes = pointer_path.read_bytes()
    for digest in (second["artifact_digest"], first["artifact_digest"], second["artifact_digest"]):
        view = profile_pack_versions(PROFILE_ID)
        assert next(row for row in view["packs"] if row["pack_id"] == PACK_ID)["role"] == "optional"
        select_profile_pack_version(_payload(view, digest))
        assert pointer_path.read_bytes() == pointer_bytes
        assert capture_active_profile().resolved.plan == predecessor.resolved.plan
    current = store.get_profile(PROFILE_ID)
    assert current.profile["packs"] == original["packs"]
    assert current.profile["requested_edges"] == original["requested_edges"]
    assert current.profile["authority_references"] == original["authority_references"]
    assert current.profile["optional_pack_revisions"] == [
        {"pack_id": PACK_ID, "artifact_digest": second["artifact_digest"]}
    ]
    assert PACK_ID not in _required_profile_pack_ids(PROFILE_ID)
    old_session = _capture_control_session()
    old_row = next(
        row for row in _invoke(old_session, "catalog.read")["packs"] if row["pack_id"] == PACK_ID
    )
    if initially_enabled:
        assert old_row["enabled"]
        assert old_row["pack_artifact_digest"] == first["artifact_digest"]
    old_session.close()

    _activate(user_data)
    active = capture_active_profile()
    assert PACK_ID not in {row["identity"] for row in active.resolved.lock["effective_set"]}
    assert PACK_ID not in _required_profile_pack_ids(PROFILE_ID)
    session = _capture_control_session()
    row = next(
        row for row in _invoke(session, "catalog.read")["packs"] if row["pack_id"] == PACK_ID
    )
    assert not row["enabled"] and not row["approved"]
    with pytest.raises(PackControlDenied):
        _invoke(session, "pack.enable", {"pack_id": PACK_ID})
    approve(session)
    _invoke(session, "pack.enable", {"pack_id": PACK_ID})
    active = capture_active_profile()
    assert (
        next(row for row in active.resolved.lock["effective_set"] if row["identity"] == PACK_ID)[
            "artifact_digest"
        ]
        == second["artifact_digest"]
    )
    _invoke(session, "pack.disable", {"pack_id": PACK_ID})
    assert PACK_ID not in {
        row["identity"] for row in capture_active_profile().resolved.lock["effective_set"]
    }
    assert (
        store.get_profile(PROFILE_ID).profile["optional_pack_revisions"]
        == current.profile["optional_pack_revisions"]
    )
    session.close()


def test_optional_intent_schema_rejects_authority_duplicates_and_required_overlap(
    tmp_path, monkeypatch
):
    from tobkiri_protocol.validation import validate_document

    user_data, first, _ = _setup(tmp_path, monkeypatch, optional=True)
    definition = deepcopy(dict(ProfileDefinitionStore(user_data).get_profile(PROFILE_ID).profile))
    intent = {"pack_id": PACK_ID, "artifact_digest": first["artifact_digest"]}
    for pins in (
        [{**intent, "enabled": True}],
        [intent, intent],
        [
            {
                "pack_id": definition["packs"][0]["pack_id"],
                "artifact_digest": first["artifact_digest"],
            }
        ],
    ):
        with pytest.raises(Exception):
            validate_document({**definition, "optional_pack_revisions": pins}, "profile")
