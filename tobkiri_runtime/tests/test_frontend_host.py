from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import core_runtime.frontend_host as frontend_host_module
from core_runtime.frontend_host import FrontendHostRegistry
from core_runtime.resolved_profile import ResolutionInput, resolve_profile


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


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


def test_declarative_catalog_is_profile_scoped_and_provenance_bound(
    tmp_path: Path,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    _write_ui_pack(ecosystem, "pack-a", [_route("pack-a.home", "/home")])
    _write_ui_pack(ecosystem, "pack-b", [_route("pack-b.hidden", "/hidden")])
    plan = _plan(ecosystem, "pack-a")

    catalog = FrontendHostRegistry(
        plan, ecosystem_dir=ecosystem
    ).build_catalog()

    assert [item.contribution_id for item in catalog.contributions] == [
        "pack-a.home"
    ]
    contribution = catalog.contributions[0]
    assert contribution.owner_pack_id == "pack-a"
    assert contribution.resolved_plan_hash == plan.plan_hash
    assert contribution.build_identity == "fixture:pack-a"


def test_self_declared_system_same_origin_module_is_quarantined(
    tmp_path: Path,
) -> None:
    ecosystem = tmp_path / "ecosystem"
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

    catalog = FrontendHostRegistry(
        plan, ecosystem_dir=ecosystem
    ).build_catalog()

    assert catalog.contributions == ()
    assert any(
        item.code == "frontend_same_origin_not_system"
        for item in catalog.diagnostics
    )


def test_host_verified_system_same_origin_module_is_accepted(
    tmp_path: Path,
) -> None:
    ecosystem = tmp_path / "ecosystem"
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

    catalog = FrontendHostRegistry(
        plan, ecosystem_dir=ecosystem
    ).build_catalog()

    assert [item.contribution_id for item in catalog.contributions] == [
        "pack-a.executable"
    ]
    assert plan.packs[0].trust_class == "system"


def test_priority_tie_rejects_both_routes_without_crashing_host(
    tmp_path: Path,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    _write_ui_pack(ecosystem, "pack-a", [_route("pack-a.route", "/same")])
    _write_ui_pack(ecosystem, "pack-b", [_route("pack-b.route", "/same")])
    _write_ui_pack(ecosystem, "pack-c", [_route("pack-c.route", "/safe")])
    plan = _plan(ecosystem, "pack-a", "pack-b", "pack-c")

    catalog = FrontendHostRegistry(
        plan, ecosystem_dir=ecosystem
    ).build_catalog()

    assert [item.route for item in catalog.contributions] == ["/safe"]
    assert any(item.code == "frontend_priority_tie" for item in catalog.diagnostics)


def test_removing_pack_removes_route_without_host_rebuild(tmp_path: Path) -> None:
    ecosystem = tmp_path / "ecosystem"
    _write_ui_pack(ecosystem, "pack-a", [_route("pack-a.route", "/feature")])

    with_pack = FrontendHostRegistry(
        _plan(ecosystem, "pack-a"), ecosystem_dir=ecosystem
    ).build_catalog()
    without_pack = FrontendHostRegistry(
        _plan(ecosystem), ecosystem_dir=ecosystem
    ).build_catalog()

    assert [item.route for item in with_pack.contributions] == ["/feature"]
    assert without_pack.contributions == ()


def test_missing_jsonschema_quarantines_frontend_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    _write_ui_pack(ecosystem, "pack-a", [_route("pack-a.route", "/feature")])
    monkeypatch.setattr(frontend_host_module, "Draft202012Validator", None)

    catalog = FrontendHostRegistry(
        _plan(ecosystem, "pack-a"), ecosystem_dir=ecosystem
    ).build_catalog()

    assert catalog.contributions == ()
    assert any(
        item.code == "frontend_descriptor_invalid"
        and "unavailable" in item.message
        for item in catalog.diagnostics
    )
