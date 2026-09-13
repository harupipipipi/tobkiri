"""Security and projection tests for the Protocol v4 Profile catalog."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core_runtime.profile_catalog_v4 import (
    bundle_lock_digest,
    profile_catalog_digest,
    project_profile_catalog,
    require_profile_catalog_binding,
)
from core_runtime.bootstrap.profile_capture import (
    capture_active_profile,
    capture_default_profile,
    host_profile_catalog,
    prepare_default_profile_confirmation,
)
from core_runtime.active_profile_store_v4 import ActiveProfileStore
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    NO_ACTIVE_PLAN_DIGEST,
    NO_ACTIVE_PROFILE_REVISION,
    RuntimeProfileChangeService,
    RuntimeSurfaceError,
    RuntimeSurfaceErrorCode,
    RuntimeSurfaceService,
)
from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
from tobkiri_protocol.canonical import canonical_digest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = RUNTIME_ROOT / "ecosystem" / "defaultspack" / "v4"


def _bundle_root() -> Path:
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    return packaged_profile_bundle_root()


@pytest.fixture
def active_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    return capture_default_profile(confirmation=prepare_default_profile_confirmation())


def _catalog_with_second_profile(catalog: BundledCatalog) -> BundledCatalog:
    second = {
        **catalog.profiles["defaults"],
        "profile_id": "defaults.alternate",
        "display_name": "Tobkiri Alternate",
        "provenance": {
            **catalog.profiles["defaults"]["provenance"],
            "source_path": ("ecosystem/defaultspack/v4/defaults.alternate.profile.v4.json"),
        },
    }
    return replace(
        catalog,
        profiles={**catalog.profiles, "defaults.alternate": second},
    )


_MISSING = object()


def _catalog_with_profile_packs(
    catalog: BundledCatalog,
    packs: object = _MISSING,
) -> BundledCatalog:
    definition = dict(catalog.profiles["defaults"])
    if packs is _MISSING:
        definition.pop("packs", None)
    else:
        definition["packs"] = packs
    return replace(
        catalog,
        profiles={**catalog.profiles, "defaults": definition},
    )


def _resolved_with_added_pack(active_runtime, catalog: BundledCatalog, pack_id: str):
    manifest = catalog.packs[pack_id]
    artifact_digest = manifest["pack"]["artifact_digest"]
    profile = {
        **active_runtime.resolved.profile,
        "packs": [
            *active_runtime.resolved.profile["packs"],
            {
                "pack_id": pack_id,
                "artifact_digest": artifact_digest,
                "role": "provider",
            },
        ],
    }
    profile_lock = {
        **active_runtime.resolved.lock,
        "effective_set": [
            *active_runtime.resolved.lock["effective_set"],
            {
                "identity": pack_id,
                "artifact_digest": artifact_digest,
                "role": "pack",
            },
        ],
    }
    return replace(active_runtime.resolved, profile=profile, lock=profile_lock)


def test_multiple_profile_projection_has_exact_bindings_and_active_marker(
    active_runtime,
) -> None:
    catalog = _catalog_with_second_profile(BundledCatalog.load(_bundle_root()))

    projection = project_profile_catalog(catalog, active_runtime)

    assert projection["count"] == 2
    assert [item["profile_id"] for item in projection["profiles"]] == [
        "defaults",
        "defaults.alternate",
    ]
    active, candidate = projection["profiles"]
    assert active["active"] is True
    assert candidate["active"] is False
    assert candidate["available"] is True
    assert candidate["bindings"]["base"]["pack_id"] == "defaults-basepack"
    assert candidate["bindings"]["shell"]["provider_id"] == "shell.tauri.default"
    assert candidate["bindings"]["application"]["pack_id"] == ("runtime.tauri.application.default")
    assert {item["pack_id"] for item in candidate["pack_closure"]} >= {
        "defaults-basepack",
        "shell.tauri.default",
        "runtime.tauri.application.default",
    }
    assert candidate["authority_snapshot"]["state"] == "captured_on_resolve"
    assert candidate["candidate"]["state"] == "not_staged"


def test_catalog_refresh_exposes_new_profile_without_changing_active_pointer(
    active_runtime,
) -> None:
    base_catalog = BundledCatalog.load(_bundle_root())
    refreshed = _catalog_with_second_profile(base_catalog)
    service = RuntimeSurfaceService(
        snapshot_loader=lambda: active_runtime,
        catalog_loader=lambda: refreshed,
    )
    before_activation = dict(active_runtime.activation)

    result = service.read_profile_catalog()

    assert result["surface"] == "profiles"
    assert result["data"]["active_profile_id"] == "defaults"
    assert result["data"]["count"] == 2
    assert active_runtime.activation == before_activation


def test_active_catalog_projects_exact_profile_lock_closure_and_current_records(
    active_runtime,
) -> None:
    catalog = BundledCatalog.load(_bundle_root())
    pack_id = "dev.tauri.toolchain.default"
    resolved = _resolved_with_added_pack(active_runtime, catalog, pack_id)
    active = replace(active_runtime, resolved=resolved)

    projected = project_profile_catalog(catalog, active)["profiles"][0]
    closure = {item["pack_id"]: item for item in projected["pack_closure"]}

    assert pack_id not in {item["pack_id"] for item in catalog.profiles["defaults"]["packs"]}
    assert closure[pack_id]["role"] == "provider"
    assert (
        closure[pack_id]["artifact_digest"] == (catalog.packs[pack_id]["pack"]["artifact_digest"])
    )
    assert resolved.profile["profile_api_version"] == "io.tobkiri.profile.v5"
    assert resolved.lock["lock_api_version"] == "io.tobkiri.profile-lock.v5"
    assert resolved.plan["plan_api_version"] == "io.tobkiri.resolved-plan.v2"
    assert active.activation["activation_api_version"] == ("io.tobkiri.activation-record.v2")


@pytest.mark.parametrize(
    ("packs", "diagnostic_code", "diagnostic_subject"),
    [
        pytest.param(_MISSING, "PROFILE_PACKS_INVALID", "packs", id="missing"),
        pytest.param(None, "PROFILE_PACKS_INVALID", "packs", id="null"),
        pytest.param("not-an-array", "PROFILE_PACKS_INVALID", "packs", id="wrong-type"),
        pytest.param(
            ["not-a-pack-binding"],
            "PROFILE_PACK_ENTRY_INVALID",
            "packs[0]",
            id="wrong-entry-type",
        ),
        pytest.param(
            [{"pack_id": "runtime.tauri.application.default"}],
            "PROFILE_PACK_ENTRY_INVALID",
            "packs[0]",
            id="missing-entry-field",
        ),
        pytest.param(
            [{"pack_id": 42, "artifact_digest": None, "role": "application"}],
            "PROFILE_PACK_ENTRY_INVALID",
            "packs[0]",
            id="wrong-entry-field-type",
        ),
        pytest.param(
            [
                {
                    "pack_id": "runtime.tauri.application.default",
                    "artifact_digest": None,
                    "role": "unknown",
                }
            ],
            "PROFILE_PACK_ENTRY_INVALID",
            "packs[0]",
            id="invalid-entry-role",
        ),
    ],
)
def test_malformed_profile_packs_are_diagnosed_without_erasing_authoritative_closure(
    active_runtime,
    packs: object,
    diagnostic_code: str,
    diagnostic_subject: str,
) -> None:
    catalog = _catalog_with_profile_packs(BundledCatalog.load(_bundle_root()), packs)
    service = RuntimeSurfaceService(
        snapshot_loader=lambda: active_runtime,
        catalog_loader=lambda: catalog,
    )

    entry = service.read_profile_catalog()["data"]["profiles"][0]

    assert entry["available"] is False
    assert any(
        diagnostic["code"] == diagnostic_code and diagnostic["subject"] == diagnostic_subject
        for diagnostic in entry["diagnostics"]
    )
    assert {item["pack_id"] for item in entry["pack_closure"]} == {
        item["identity"] for item in active_runtime.resolved.lock["effective_set"]
    }


def test_null_pack_artifact_digest_remains_valid_for_unresolved_source_profile(
    active_runtime,
) -> None:
    catalog = BundledCatalog.load(_bundle_root())
    source = catalog.profiles["defaults"]
    unresolved_packs = [{**item, "artifact_digest": None} for item in source["packs"]]
    catalog = _catalog_with_profile_packs(catalog, unresolved_packs)

    entry = project_profile_catalog(catalog, active_runtime)["profiles"][0]

    assert entry["available"] is True
    assert entry["diagnostics"] == []


def test_catalog_restores_session_candidate_and_pack_closure_after_restart(
    active_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = BundledCatalog.load(_bundle_root())
    pack_id = "dev.tauri.toolchain.default"
    resolved_with_pack = _resolved_with_added_pack(active_runtime, catalog, pack_id)
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        lambda _pack_ids, **_bindings: resolved_with_pack,
    )
    first_surface = RuntimeSurfaceService(
        snapshot_loader=lambda: active_runtime,
        catalog_loader=lambda: catalog,
    )
    resolved = RuntimeProfileChangeService(surface_service=first_surface).resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": ["defaultspack", pack_id],
        },
        session_id="session-catalog-restart",
    )

    restarted_surface = RuntimeSurfaceService(
        snapshot_loader=lambda: active_runtime,
        catalog_loader=lambda: catalog,
    )
    candidate = restarted_surface.read_profile_catalog(session_id="session-catalog-restart")[
        "data"
    ]["profiles"][0]
    isolated = restarted_surface.read_profile_catalog(session_id="other-session")["data"][
        "profiles"
    ][0]

    assert candidate["candidate"] == {
        "state": "resolved",
        "candidate_id": resolved["candidate_id"],
        "candidate_digest": resolved["candidate_digest"],
        "expires_at": candidate["candidate"]["expires_at"],
    }
    assert candidate["candidate"]["expires_at"].endswith("Z")
    assert pack_id in {item["pack_id"] for item in candidate["pack_closure"]}
    assert isolated["candidate"]["state"] == "not_staged"
    assert pack_id not in {item["pack_id"] for item in isolated["pack_closure"]}

    RuntimeProfileChangeService(surface_service=restarted_surface).review(
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
        session_id="session-catalog-restart",
    )
    after_review = RuntimeSurfaceService(
        snapshot_loader=lambda: active_runtime,
        catalog_loader=lambda: catalog,
    ).read_profile_catalog(session_id="session-catalog-restart")["data"]["profiles"][0]
    assert after_review["candidate"]["state"] == "reviewed"


def test_catalog_binding_rejects_unknown_stale_and_tampered_profiles() -> None:
    catalog = BundledCatalog.load(_bundle_root())
    definition_digest = canonical_digest(catalog.profiles["defaults"])
    catalog_digest = profile_catalog_digest(catalog)
    lock_digest = bundle_lock_digest(catalog)

    assert (
        require_profile_catalog_binding(
            catalog,
            profile_id="defaults",
            expected_definition_digest=definition_digest,
            expected_catalog_digest=catalog_digest,
            expected_bundle_lock_digest=lock_digest,
        )["profile_id"]
        == "defaults"
    )

    invalid = (
        ("unknown", definition_digest, catalog_digest, lock_digest),
        ("defaults", "sha256:" + "0" * 64, catalog_digest, lock_digest),
        ("defaults", definition_digest, "sha256:" + "0" * 64, lock_digest),
        ("defaults", definition_digest, catalog_digest, "sha256:" + "0" * 64),
    )
    for profile_id, definition, catalog_value, lock_value in invalid:
        with pytest.raises(ValueError):
            require_profile_catalog_binding(
                catalog,
                profile_id=profile_id,
                expected_definition_digest=definition,
                expected_catalog_digest=catalog_value,
                expected_bundle_lock_digest=lock_value,
            )


def test_authoritative_resolve_binds_selected_catalog_profile(
    active_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog_with_second_profile(BundledCatalog.load(_bundle_root()))
    service = RuntimeSurfaceService(
        snapshot_loader=lambda: active_runtime,
        catalog_loader=lambda: catalog,
    )
    projected = service.read_profile_catalog()["data"]
    candidate = projected["profiles"][1]
    captured: dict[str, object] = {}

    def resolve(pack_ids, **bindings):
        captured["pack_ids"] = pack_ids
        captured.update(bindings)
        selected = {
            **active_runtime.resolved.profile,
            "profile_id": "defaults.alternate",
        }
        return replace(active_runtime.resolved, profile=selected)

    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        resolve,
    )
    ceremony = RuntimeProfileChangeService(surface_service=service)
    result = ceremony.resolve(
        {
            "profile_id": "defaults.alternate",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": [
                item["pack_id"]
                for item in candidate["pack_closure"]
                if item["role"] not in {"base", "shell", "application"}
            ],
            "profile_definition_digest": candidate["definition"]["digest"],
            "profile_catalog_digest": projected["catalog_digest"],
            "bundle_lock_digest": projected["bundle_lock_digest"],
        },
        session_id="session-a",
    )

    assert result["state"] == "resolved"
    assert captured["profile_id"] == "defaults.alternate"
    assert captured["expected_profile_definition_digest"] == (candidate["definition"]["digest"])
    assert (
        result["review"]["catalog_binding"]["profile_catalog_digest"]
        == (projected["catalog_digest"])
    )


def test_non_default_resolve_without_catalog_binding_fails_closed(
    active_runtime,
) -> None:
    ceremony = RuntimeProfileChangeService(
        surface_service=RuntimeSurfaceService(
            snapshot_loader=lambda: active_runtime,
            catalog_loader=lambda: _catalog_with_second_profile(
                BundledCatalog.load(_bundle_root())
            ),
        )
    )
    with pytest.raises(RuntimeSurfaceError) as rejected:
        ceremony.resolve(
            {
                "profile_id": "defaults.alternate",
                "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
                "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
                "desired_pack_ids": ["defaultspack"],
            },
            session_id="session-a",
        )
    assert rejected.value.code is RuntimeSurfaceErrorCode.INVALID_REQUEST


def test_fresh_active_none_named_profiles_activate_and_survive_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    bundled = BundledCatalog.load(_bundle_root())
    store = ProfileDefinitionStore(user_data)
    for profile_id in ("profile-a", "profile-b"):
        store.create_profile(
            {
                **bundled.profiles["defaults"],
                "profile_id": profile_id,
                "display_name": profile_id,
            }
        )

    def load_catalog() -> BundledCatalog:
        return host_profile_catalog(
            bundle_root=_bundle_root(),
            user_data_root=user_data,
        )

    def resolve(
        profile_id: str,
        session_id: str,
    ) -> tuple[RuntimeProfileChangeService, dict[str, object]]:
        catalog = load_catalog()
        definition = catalog.profiles[profile_id]
        predecessor = ActiveProfileStore(user_data).load(verify_snapshot=True)
        service = RuntimeProfileChangeService(
            surface_service=RuntimeSurfaceService(catalog_loader=load_catalog),
            bundle_root=_bundle_root(),
            user_data_root=user_data,
        )
        resolved = service.resolve(
            {
                "profile_id": profile_id,
                "expected_profile_revision": (
                    predecessor.profile_revision
                    if predecessor
                    else NO_ACTIVE_PROFILE_REVISION
                ),
                "expected_plan_digest": (
                    predecessor.plan_digest if predecessor else NO_ACTIVE_PLAN_DIGEST
                ),
                "desired_pack_ids": [
                    str(item["pack_id"])
                    for item in definition["packs"]
                    if item.get("role") != "application"
                ],
                "profile_definition_digest": canonical_digest(definition),
                "profile_catalog_digest": profile_catalog_digest(catalog),
                "bundle_lock_digest": bundle_lock_digest(catalog),
            },
            session_id=session_id,
        )
        assert ActiveProfileStore(user_data).load(verify_snapshot=True) == predecessor
        return service, resolved

    def activate(profile_id: str, session_id: str) -> dict[str, object]:
        service, resolved = resolve(profile_id, session_id)
        reviewed = service.review(
            {
                "candidate_id": resolved["candidate_id"],
                "candidate_digest": resolved["candidate_digest"],
            },
            session_id=session_id,
        )
        approved = service.approve(
            {
                "candidate_id": reviewed["candidate_id"],
                "candidate_digest": reviewed["candidate_digest"],
            },
            session_id=session_id,
        )
        return service.activate(
            {
                "approval_id": approved["approval_id"],
                "approval_digest": approved["approval_digest"],
            },
            session_id=session_id,
        )

    assert {item.profile_id for item in store.list_profiles()} == {
        "profile-a",
        "profile-b",
    }
    assert ActiveProfileStore(user_data).load(verify_snapshot=True) is None
    stale_catalog_service, stale_catalog_candidate = resolve(
        "profile-a",
        "session-stale-catalog",
    )
    store.create_profile(
        {
            **bundled.profiles["defaults"],
            "profile_id": "profile-c",
            "display_name": "profile-c",
        }
    )
    with pytest.raises(RuntimeSurfaceError) as catalog_stale:
        stale_catalog_service.review(
            {
                "candidate_id": stale_catalog_candidate["candidate_id"],
                "candidate_digest": stale_catalog_candidate["candidate_digest"],
            },
            session_id="session-stale-catalog",
        )
    assert catalog_stale.value.code is RuntimeSurfaceErrorCode.DIGEST_MISMATCH

    stale_pointer_service, stale_pointer_candidate = resolve(
        "profile-a",
        "session-stale-pointer",
    )
    assert activate("profile-a", "session-a")["profile_id"] == "profile-a"
    with pytest.raises(RuntimeSurfaceError) as predecessor_stale:
        stale_pointer_service.review(
            {
                "candidate_id": stale_pointer_candidate["candidate_id"],
                "candidate_digest": stale_pointer_candidate["candidate_digest"],
            },
            session_id="session-stale-pointer",
        )
    assert predecessor_stale.value.code is RuntimeSurfaceErrorCode.STALE_REVISION
    assert activate("profile-b", "session-b")["profile_id"] == "profile-b"
    assert capture_active_profile().resolved.profile["profile_id"] == "profile-b"
    third = activate("profile-a", "session-a-restart")
    pointer = ActiveProfileStore(user_data).require(verify_snapshot=True)
    assert third["profile_id"] == pointer.profile_id == "profile-a"
    assert third["activation_id"] == pointer.activation_id
    assert third["plan_digest"] == pointer.plan_digest
