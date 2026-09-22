from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import ecosystem.defaultspack.domain.frontend.host as frontend_host_module
from ecosystem.defaultspack.domain.frontend.host import FrontendHostRegistry
from core_runtime.pack_artifact_integrity import write_host_install_record
from core_runtime.resolved_profile import ResolutionInput, resolve_profile


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _authorize_developer_packs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *pack_ids: str,
) -> None:
    trust_store = tmp_path / "host-policy" / "publisher-trust.json"
    for pack_id in pack_ids:
        install_path = tmp_path / "ecosystem" / pack_id
        install_path.mkdir(parents=True, exist_ok=True)
        write_host_install_record(
            trust_store,
            pack_id=pack_id,
            install_path=install_path,
            record={
                "signature_required": False,
                "developer_mode": True,
                "publisher_id": "",
                "key_id": "",
                "installed_version": "1.0.0",
                "signed_manifest_path": "",
                "contract_versions": {},
                "requested_capabilities": [],
            },
        )
    monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(trust_store))
    monkeypatch.setenv("RUMI_PACK_DEVELOPER_MODE", "1")


def _write_ui_pack(
    ecosystem: Path,
    pack_id: str,
    descriptors: list[dict[str, object]],
    *,
    trust_class: str = "untrusted",
) -> Path:
    pack = ecosystem / pack_id
    contribution_root = pack / "frontend" / "contributions"
    contribution_root.mkdir(parents=True)
    resources = []
    for index, descriptor in enumerate(descriptors):
        relative = f"frontend/contributions/{index}.json"
        raw = json.dumps(descriptor, sort_keys=True).encode("utf-8")
        (pack / relative).write_bytes(raw)
        resources.append(
            {
                "id": relative,
                "kind": "ui.contribution",
                "content_hash": _sha256(raw),
            }
        )
    manifest = {
        "pack_id": pack_id,
        "version": "1.0.0",
        "resources": resources,
        "provenance": {
            "content_hash": "sha256:" + "0" * 64,
            "build_identity": f"fixture:{pack_id}",
            "trust_class": trust_class,
        },
    }
    (pack / "ecosystem.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return pack


def _route(
    contribution_id: str,
    route: str,
    *,
    priority: int = 0,
) -> dict[str, object]:
    return {
        "version": "rumi.ui.contribution.v1",
        "id": contribution_id,
        "kind": "route",
        "mode": "declarative",
        "label": contribution_id,
        "priority": priority,
        "route": route,
        "view": {"type": "status_card", "title": contribution_id},
        "accessibility": {"name": contribution_id, "keyboard": True},
    }


def _plan(
    ecosystem: Path,
    *pack_ids: str,
    verified_pack_trust: tuple[tuple[str, str], ...] = (),
):
    return resolve_profile(
        ResolutionInput(
            profile_id="frontend-fixture",
            profile_revision="r1",
            platform="fixture",
            policy_revision="p1",
            lockfile_revision=None,
            requested_pack_ids=tuple(pack_ids),
            authorized_pack_ids=tuple(pack_ids),
            verified_pack_trust=verified_pack_trust,
        ),
        ecosystem_dir=ecosystem,
    )


def _assert_legacy_frontend_fails_closed(
    plan,
    *pack_ids: str,
) -> None:
    """Require old filesystem frontend projections to have no runtime effect."""
    from core_runtime.manifest_authority import load_manifest_authority_catalog

    authority = load_manifest_authority_catalog()
    assert set(authority.values()) == {"v4-authoritative"}
    assert authority["defaultspack"] == "v4-authoritative"
    defaultspack_root = (
        Path(__file__).resolve().parent.parent / "ecosystem" / "defaultspack"
    )
    assert (defaultspack_root / "pack.v4.json").is_file()
    assert not (defaultspack_root / "ecosystem.json").exists()
    assert plan.effective_pack_set == ()
    assert {
        item.subject
        for item in plan.diagnostics
        if item.code == "offline_projection_not_authority"
    } >= set(pack_ids)


def test_declarative_catalog_is_profile_scoped_and_provenance_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    _authorize_developer_packs(monkeypatch, tmp_path, "pack-a", "pack-b")
    _write_ui_pack(ecosystem, "pack-a", [_route("pack-a.home", "/home")])
    _write_ui_pack(ecosystem, "pack-b", [_route("pack-b.hidden", "/hidden")])
    plan = _plan(ecosystem, "pack-a")

    catalog = FrontendHostRegistry(plan, ecosystem_dir=ecosystem).build_catalog()

    _assert_legacy_frontend_fails_closed(plan, "pack-a")
    assert catalog.contributions == ()
    assert catalog.quarantined_pack_ids == ()


def test_self_declared_system_same_origin_module_is_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    _authorize_developer_packs(monkeypatch, tmp_path, "pack-a")
    module_raw = b"export const Screen = () => null;\n"
    descriptor = {
        **_route("pack-a.executable", "/executable"),
        "mode": "same_origin_builtin",
        "module": {
            "path": "/static/packs/pack-a/frontend/screen.js",
            "export": "Screen",
            "content_hash": _sha256(module_raw),
        },
    }
    pack = _write_ui_pack(
        ecosystem,
        "pack-a",
        [descriptor],
        trust_class="system",
    )
    (pack / "frontend" / "screen.js").write_bytes(module_raw)
    plan = _plan(ecosystem, "pack-a")

    catalog = FrontendHostRegistry(plan, ecosystem_dir=ecosystem).build_catalog()

    _assert_legacy_frontend_fails_closed(plan, "pack-a")
    assert catalog.contributions == ()
    assert catalog.diagnostics == ()


def test_host_verified_system_same_origin_module_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    _authorize_developer_packs(monkeypatch, tmp_path, "pack-a")
    module_raw = b"export const Screen = () => null;\n"
    descriptor = {
        **_route("pack-a.executable", "/executable"),
        "mode": "same_origin_builtin",
        "module": {
            "path": "/static/packs/pack-a/frontend/screen.js",
            "export": "Screen",
            "content_hash": _sha256(module_raw),
        },
    }
    pack = _write_ui_pack(ecosystem, "pack-a", [descriptor])
    (pack / "frontend" / "screen.js").write_bytes(module_raw)
    plan = _plan(
        ecosystem,
        "pack-a",
        verified_pack_trust=(("pack-a", "system"),),
    )

    catalog = FrontendHostRegistry(plan, ecosystem_dir=ecosystem).build_catalog()

    _assert_legacy_frontend_fails_closed(plan, "pack-a")
    assert catalog.contributions == ()
    assert catalog.quarantined_pack_ids == ()


def test_priority_tie_rejects_both_routes_without_crashing_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    _authorize_developer_packs(
        monkeypatch,
        tmp_path,
        "pack-a",
        "pack-b",
        "pack-c",
    )
    _write_ui_pack(ecosystem, "pack-a", [_route("pack-a.route", "/same")])
    _write_ui_pack(ecosystem, "pack-b", [_route("pack-b.route", "/same")])
    _write_ui_pack(ecosystem, "pack-c", [_route("pack-c.route", "/safe")])
    plan = _plan(ecosystem, "pack-a", "pack-b", "pack-c")

    catalog = FrontendHostRegistry(plan, ecosystem_dir=ecosystem).build_catalog()

    _assert_legacy_frontend_fails_closed(plan, "pack-a", "pack-b", "pack-c")
    assert catalog.contributions == ()
    assert not any(item.code == "frontend_priority_tie" for item in catalog.diagnostics)


def test_removing_pack_removes_route_without_host_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    _authorize_developer_packs(monkeypatch, tmp_path, "pack-a")
    _write_ui_pack(ecosystem, "pack-a", [_route("pack-a.route", "/feature")])

    with_pack = FrontendHostRegistry(
        _plan(ecosystem, "pack-a"), ecosystem_dir=ecosystem
    ).build_catalog()
    without_pack = FrontendHostRegistry(
        _plan(ecosystem), ecosystem_dir=ecosystem
    ).build_catalog()

    _assert_legacy_frontend_fails_closed(_plan(ecosystem, "pack-a"), "pack-a")
    assert with_pack.contributions == ()
    assert without_pack.contributions == ()


def test_missing_jsonschema_quarantines_frontend_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    _authorize_developer_packs(monkeypatch, tmp_path, "pack-a")
    _write_ui_pack(ecosystem, "pack-a", [_route("pack-a.route", "/feature")])
    monkeypatch.setattr(frontend_host_module, "Draft202012Validator", None)

    catalog = FrontendHostRegistry(
        _plan(ecosystem, "pack-a"), ecosystem_dir=ecosystem
    ).build_catalog()

    _assert_legacy_frontend_fails_closed(_plan(ecosystem, "pack-a"), "pack-a")
    assert catalog.contributions == ()
    assert catalog.diagnostics == ()


def test_modified_pack_artifact_quarantines_the_entire_frontend_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    _authorize_developer_packs(monkeypatch, tmp_path, "pack-a")
    pack = _write_ui_pack(
        ecosystem,
        "pack-a",
        [_route("pack-a.route", "/feature")],
    )
    plan = _plan(ecosystem, "pack-a")
    artifact_manifest = pack / "artifact-manifest.json"
    artifact_manifest.write_text(
        json.dumps({"artifacts": []}),
        encoding="utf-8",
    )
    manifest_path = pack / "ecosystem.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["metadata"] = {
        "integrity": {"artifact_manifest": "artifact-manifest.json"}
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    catalog = FrontendHostRegistry(
        plan, ecosystem_dir=ecosystem
    ).build_catalog()

    _assert_legacy_frontend_fails_closed(plan, "pack-a")
    assert catalog.contributions == ()
    assert catalog.quarantined_pack_ids == ()
