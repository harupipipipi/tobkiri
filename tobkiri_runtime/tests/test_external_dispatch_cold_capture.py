"""Cold dispatch must recompose signed external executable and inert Packs."""

def test_current_signed_external_cold_dispatch(tmp_path, monkeypatch):
    from core_runtime.authority.v4 import AuthorityStore
    from core_runtime.bootstrap.production_v4 import capture_production_dispatch
    from core_runtime.bootstrap.profile_capture import capture_active_profile, host_profile_catalog
    from ecosystem.defaultspack.defaultspack.http_contract_composition import (
        defaultspack_capability_binding,
        defaultspack_capability_snapshot_mapping,
    )
    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        defaultspack_activation_snapshot_loader,
        defaultspack_runtime_capture_inputs,
    )
    from ecosystem.defaultspack.domain.runtime_surface_v4 import create_runtime_surface_services
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
    from tests.test_external_pack_catalog_v4 import PACK_ID, RUNTIME_ROOT
    from tests.test_profile_external_review_boundary import _enable_signed_external_pack

    user_data = _enable_signed_external_pack(tmp_path, monkeypatch)
    active = capture_active_profile()
    catalog = host_profile_catalog()
    print(
        "SIGNED_EXTERNAL_SELECTED",
        PACK_ID in {row["pack_id"] for row in active.resolved.profile["packs"]},
    )
    print("HOST_CATALOG_HAS_EXTERNAL", PACK_ID in catalog.packs)
    inputs = defaultspack_runtime_capture_inputs(active)
    authority = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    try:
        session = capture_production_dispatch(
            active,
            bundle_root=packaged_profile_bundle_root(),
            ecosystem_root=RUNTIME_ROOT / "ecosystem",
            authority_store=authority,
            http_contract_bindings=inputs.contract_bindings,
            activation_snapshot_loader=defaultspack_activation_snapshot_loader,
            runtime_surface_factory=create_runtime_surface_services,
            capability_binding_snapshot_factory=defaultspack_capability_snapshot_mapping,
            capability_binding_selector=defaultspack_capability_binding,
        )
        session.close()
    finally:
        authority.close()


def test_current_qa_inert_external_cold_dispatch(tmp_path, monkeypatch):
    from core_runtime.authority.v4 import AuthorityStore
    from core_runtime.bootstrap.production_v4 import capture_production_dispatch
    from core_runtime.bootstrap.profile_capture import (
        capture_active_profile,
        capture_default_profile,
        host_profile_catalog,
        prepare_default_profile_confirmation,
    )
    from core_runtime.native_pack_onboarding import commit_signed_pack, preview_signed_pack
    from ecosystem.defaultspack.defaultspack.http_contract_composition import (
        defaultspack_capability_binding,
        defaultspack_capability_snapshot_mapping,
    )
    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        defaultspack_activation_snapshot_loader,
        defaultspack_runtime_capture_inputs,
    )
    from ecosystem.defaultspack.domain.runtime_surface_v4 import create_runtime_surface_services
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
    from tests.qa_frontend_input_pack import PACK_ID, build_signed_qa_pack
    from tests.test_external_pack_catalog_v4 import RUNTIME_ROOT, _capture_control_session, _invoke

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    source = tmp_path / PACK_ID
    public_key = tmp_path / f"{PACK_ID}.public.pem"
    build_signed_qa_pack(source, public_key)
    trust_dir = user_data / "pack_control"
    trust_dir.mkdir(parents=True, mode=0o700)
    trust_store = trust_dir / "publisher-trust.json"
    preview = preview_signed_pack(source, public_key)
    admitted = commit_signed_pack(
        source,
        public_key,
        expected_preview_digest=preview["preview_digest"],
        trust_store_path=trust_store,
    )
    assert admitted["state"] == "committed"
    capture_default_profile(confirmation=prepare_default_profile_confirmation())
    control = _capture_control_session()
    assert _invoke(control, "pack.install", {"pack_id": PACK_ID})["installed"]
    candidate = _invoke(control, "approval.candidate", {"pack_id": PACK_ID})
    assert _invoke(
        control, "approval.approve", {"pack_id": PACK_ID, "candidate_id": candidate["candidate_id"]}
    )["approved"]
    assert _invoke(control, "pack.enable", {"pack_id": PACK_ID})["enabled"]
    active = capture_active_profile()
    assert PACK_ID in {row["pack_id"] for row in active.resolved.profile["packs"]}
    assert PACK_ID not in {row["pack_id"] for row in active.resolved.plan["bindings"]}
    print("QA_INERT_SIGNED_EXTERNAL_SELECTED", PACK_ID)
    print("HOST_CATALOG_HAS_EXTERNAL", PACK_ID in host_profile_catalog().packs)
    inputs = defaultspack_runtime_capture_inputs(active)
    authority = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    try:
        session = capture_production_dispatch(
            active,
            bundle_root=packaged_profile_bundle_root(),
            ecosystem_root=RUNTIME_ROOT / "ecosystem",
            authority_store=authority,
            http_contract_bindings=inputs.contract_bindings,
            activation_snapshot_loader=defaultspack_activation_snapshot_loader,
            runtime_surface_factory=create_runtime_surface_services,
            capability_binding_snapshot_factory=defaultspack_capability_snapshot_mapping,
            capability_binding_selector=defaultspack_capability_binding,
        )
        session.close()
    finally:
        authority.close()
