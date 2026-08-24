from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from contextvars import Token
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.templates import (  # noqa: E402
    TemplateRoot,
    TemplateTrustLevel,
    build_template_catalog,
    default_template_roots,
    discover_templates,
)
from domain.templates.catalog_runtime import get_template_catalog_snapshot  # noqa: E402
from core_runtime.resolved_profile_scope import (  # noqa: E402
    V4PackView,
    V4ResolvedProfileView,
    activate_resolved_profile,
    restore_resolved_profile,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    runtime_root = tmp_path / "tobkiri_runtime"
    ecosystem_root = runtime_root / "ecosystem"
    defaultspack_root = ecosystem_root / "defaultspack"
    _write_json(
        defaultspack_root / "pack.v4.json",
        {"pack_api_version": "io.tobkiri.pack.v4", "pack": {"id": "defaultspack"}},
    )
    return runtime_root, ecosystem_root, defaultspack_root


@pytest.fixture
def select_packs() -> Iterator[Callable[[list[str], Path], None]]:
    tokens: list[Token[Any | None]] = []

    def select(pack_ids: list[str], ecosystem_root: Path) -> None:
        packs = tuple(
            V4PackView(
                pack_id=pack_id,
                version="1.0.0",
                manifest_hash=_selected_artifact_digest(ecosystem_root, pack_id),
                content_hash=_selected_artifact_digest(ecosystem_root, pack_id),
            )
            for pack_id in pack_ids
        )
        tokens.append(
            activate_resolved_profile(
                V4ResolvedProfileView(
                    profile_id="test.defaults",
                    profile_revision="1",
                    plan_hash="plan:" + ",".join(pack_ids),
                    effective_pack_set=tuple(pack_ids),
                    packs=packs,
                    providers=(),
                )
            )
        )

    yield select
    for token in reversed(tokens):
        restore_resolved_profile(token)


def _template(template_id: str, *, version: str = "1.0.0") -> dict:
    return {
        "schema_version": 1,
        "id": template_id,
        "kind": "pack",
        "version": version,
        "status": "active",
        "trust_level": "builtin",
        "metadata": {
            "source_pack_id": "spoofed-pack",
            "source_kind": "spoofed-kind",
        },
        "pieces": [
            {
                "id": "command",
                "kind": "composer_command",
                "command": {
                    "id": f"{template_id}.command",
                    "label": "Feature command",
                    "execution": {
                        "type": "pack_block",
                        "qualified_name": "defaultspack:context.token_estimate",
                    },
                },
            },
            {
                "id": "settings",
                "kind": "settings_section",
                "section": {"id": f"{template_id}.settings", "fields": []},
            },
            {
                "id": "sidebar",
                "kind": "sidebar_item",
                "label": "Feature sidebar",
            },
            {
                "id": "action",
                "kind": "function",
                "role": "action",
                "action_id": f"{template_id}.action",
                "block_module": "blocks.context.token_estimate",
            },
            {
                "id": "route",
                "kind": "api_route",
                "method": "POST",
                "route_path": f"/api/{template_id}/run",
                "action_id": f"{template_id}.action",
            },
        ],
    }


def _write_pack(
    ecosystem_root: Path,
    pack_id: str,
    template: dict | None = None,
) -> Path:
    pack_root = ecosystem_root / pack_id
    _write_json(pack_root / "ecosystem.json", {"pack_id": pack_id})
    if template is not None:
        _write_json(pack_root / "templates" / template["id"] / "template.json", template)
    _refresh_pack_manifest(pack_root, pack_id=pack_id)
    return pack_root


def _refresh_pack_manifest(
    pack_root: Path,
    *,
    pack_id: str,
    version: str = "1.0.0",
) -> str:
    artifacts = [
        {
            "path": path.relative_to(pack_root).as_posix(),
            "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            "kind": "template",
        }
        for path in sorted((pack_root / "templates").rglob("template.json"))
        if path.is_file()
    ]
    artifact_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(artifacts, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    _write_json(
        pack_root / "pack.v4.json",
        {
            "pack_api_version": "io.tobkiri.pack.v4",
            "pack": {
                "id": pack_id,
                "version": version,
                "artifact_digest": artifact_digest,
            },
            "artifacts": artifacts,
        },
    )
    return artifact_digest


def _selected_artifact_digest(ecosystem_root: Path, pack_id: str) -> str:
    manifest_path = ecosystem_root / pack_id / "pack.v4.json"
    if manifest_path.is_file():
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        return str(raw.get("pack", {}).get("artifact_digest") or "")
    return "sha256:" + hashlib.sha256(pack_id.encode()).hexdigest()


def test_selected_sibling_projects_catalog_with_loader_owned_local_trust(
    tmp_path: Path, select_packs
):
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    _write_pack(ecosystem_root, "feature_pack", _template("feature.template"))
    _write_pack(ecosystem_root, "unselected_pack", _template("hidden.template"))
    select_packs(["feature_pack"], ecosystem_root)

    catalog = build_template_catalog(defaultspack_root=defaultspack_root)

    summaries = {item["id"]: item for item in catalog["templates"]}
    assert "feature.template" in summaries
    assert "hidden.template" not in summaries
    feature = summaries["feature.template"]
    assert feature["trust_level"] == "local"
    assert feature["metadata"]["declared_trust_level"] == "builtin"
    assert feature["metadata"]["source_pack_id"] == "feature_pack"
    assert feature["metadata"]["source_kind"] == "selected_sibling_pack"
    assert feature["metadata"]["source_pack_artifact_digest"] == (
        _selected_artifact_digest(ecosystem_root, "feature_pack")
    )
    assert [item["id"] for item in catalog["commands"]] == ["feature.template.command"]
    assert [item["id"] for item in catalog["settings_sections"]] == ["feature.template.settings"]
    assert [item["id"] for item in catalog["sidebar_items"]] == ["sidebar"]
    assert [item["id"] for item in catalog["actions"]] == ["action"]
    assert [item["path"] for item in catalog["api_routes"]] == ["/api/feature.template/run"]
    assert all(
        item["trust_level"] == "local"
        for key in ("commands", "settings_sections", "sidebar_items", "actions", "api_routes")
        for item in catalog[key]
    )
    assert all(
        item["source_pack_id"] == "feature_pack"
        for key in ("commands", "settings_sections", "sidebar_items", "actions", "api_routes")
        for item in catalog[key]
    )


def test_missing_v4_activation_never_scans_all_installed_sibling_packs(tmp_path: Path):
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    _write_pack(ecosystem_root, "installed_but_unselected", _template("hidden.template"))

    result = discover_templates(defaultspack_root=defaultspack_root)

    assert result.templates == []
    assert not any(
        "installed_but_unselected" in str(diagnostic.details) for diagnostic in result.diagnostics
    )


def test_legacy_selection_file_cannot_activate_sibling_templates(tmp_path: Path):
    runtime_root, ecosystem_root, defaultspack_root = _layout(tmp_path)
    _write_pack(ecosystem_root, "installed_pack", _template("hidden.template"))
    selection_path = runtime_root / "user_data" / "settings" / "setup_pack_selection.json"
    _write_json(selection_path, {"target_pack_ids": ["installed_pack"]})

    result = discover_templates(defaultspack_root=defaultspack_root)

    assert result.templates == []
    assert result.diagnostics == []


def test_root_order_is_builtin_selected_pack_id_user_then_configured_extra(
    monkeypatch, tmp_path: Path, select_packs
):
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    pack_b = _write_pack(ecosystem_root, "pack_b")
    pack_a = _write_pack(ecosystem_root, "pack_a")
    select_packs(["pack_b", "pack_a"], ecosystem_root)
    extra_root = tmp_path / "extra_templates"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_TEMPLATE_ROOTS", str(extra_root))

    roots = default_template_roots(defaultspack_root)

    assert roots == [
        TemplateRoot(defaultspack_root.resolve() / "templates", TemplateTrustLevel.BUILTIN),
        TemplateRoot(
            pack_a.resolve() / "templates",
            TemplateTrustLevel.LOCAL,
            source_pack_id="pack_a",
            source_kind="selected_sibling_pack",
            source_pack_artifact_digest=_selected_artifact_digest(ecosystem_root, "pack_a"),
        ),
        TemplateRoot(
            pack_b.resolve() / "templates",
            TemplateTrustLevel.LOCAL,
            source_pack_id="pack_b",
            source_kind="selected_sibling_pack",
            source_pack_artifact_digest=_selected_artifact_digest(ecosystem_root, "pack_b"),
        ),
        TemplateRoot(
            defaultspack_root.resolve() / "user_data" / "shared" / "templates",
            TemplateTrustLevel.USER,
        ),
        TemplateRoot(
            extra_root.resolve(),
            TemplateTrustLevel.USER,
            source_kind="configured_extra_root",
        ),
    ]


def test_deselect_upgrade_and_uninstall_invalidate_snapshot_without_stale_projection(
    tmp_path: Path, select_packs
):
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    pack_root = _write_pack(
        ecosystem_root, "feature_pack", _template("feature.template", version="1.0.0")
    )
    select_packs(["feature_pack"], ecosystem_root)
    initial = get_template_catalog_snapshot(defaultspack_root=defaultspack_root)
    assert [item["id"] for item in initial.catalog["templates"]] == ["feature.template"]

    select_packs([], ecosystem_root)
    deselected = get_template_catalog_snapshot(defaultspack_root=defaultspack_root)
    assert deselected.generation != initial.generation
    assert deselected.catalog["templates"] == []

    select_packs(["feature_pack"], ecosystem_root)
    _write_json(
        pack_root / "templates" / "feature.template" / "template.json",
        _template("feature.template", version="2.0.0"),
    )
    stale = get_template_catalog_snapshot(defaultspack_root=defaultspack_root)
    assert stale.generation != initial.generation
    assert stale.catalog["templates"] == []

    _refresh_pack_manifest(pack_root, pack_id="feature_pack", version="2.0.0")
    select_packs(["feature_pack"], ecosystem_root)
    upgraded = get_template_catalog_snapshot(defaultspack_root=defaultspack_root)
    assert upgraded.generation != initial.generation
    assert upgraded.catalog["templates"][0]["version"] == "2.0.0"

    _refresh_pack_manifest(pack_root, pack_id="feature_pack", version="2.1.0")
    select_packs(["feature_pack"], ecosystem_root)
    manifest_upgraded = get_template_catalog_snapshot(defaultspack_root=defaultspack_root)
    assert manifest_upgraded.generation != upgraded.generation

    shutil.rmtree(pack_root)
    removed = get_template_catalog_snapshot(defaultspack_root=defaultspack_root)
    assert removed.generation != manifest_upgraded.generation
    assert removed.catalog["templates"] == []
    assert removed.catalog["templates"] == []


def test_sibling_collision_fails_closed_with_both_pack_ids(tmp_path: Path, select_packs):
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    _write_pack(ecosystem_root, "pack_a", _template("collision.template"))
    _write_pack(ecosystem_root, "pack_b", _template("collision.template"))
    select_packs(["pack_b", "pack_a"], ecosystem_root)

    catalog = build_template_catalog(defaultspack_root=defaultspack_root)

    duplicate = next(
        item
        for item in catalog["template_diagnostics"]
        if item["code"] == "template.registry.duplicate_template"
    )
    assert duplicate["details"]["existing_source_pack_id"] == "pack_a"
    assert duplicate["details"]["duplicate_source_pack_id"] == "pack_b"
    assert len(catalog["templates"]) == 1


def test_dependency_template_only_resolves_when_dependency_pack_is_selected(
    tmp_path: Path, select_packs
):
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    feature = _template("feature.template")
    feature["dependencies"] = ["dependency.template"]
    _write_pack(ecosystem_root, "feature_pack", feature)
    _write_pack(ecosystem_root, "dependency_pack", _template("dependency.template"))
    select_packs(["feature_pack"], ecosystem_root)

    missing = build_template_catalog(defaultspack_root=defaultspack_root)
    feature_summary = next(
        item for item in missing["templates"] if item["id"] == "feature.template"
    )
    assert feature_summary["projectable"] is False
    assert "dependency.template" in feature_summary["blocked_by"]
    missing_diagnostic = next(
        item
        for item in missing["template_diagnostics"]
        if item["code"] == "template.dependency.missing"
    )
    assert missing_diagnostic["template_id"] == "feature.template"
    assert missing_diagnostic["details"]["source_pack_id"] == "feature_pack"

    select_packs(["feature_pack", "dependency_pack"], ecosystem_root)
    resolved = build_template_catalog(defaultspack_root=defaultspack_root)
    summaries = {item["id"]: item for item in resolved["templates"]}
    assert summaries["feature.template"]["projectable"] is True
    assert summaries["dependency.template"]["projectable"] is True


def test_configured_extra_root_remains_user_trust(tmp_path: Path, monkeypatch):
    _, _, defaultspack_root = _layout(tmp_path)
    extra_root = tmp_path / "extra"
    _write_json(extra_root / "extra.template" / "template.json", _template("extra.template"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_TEMPLATE_ROOTS", str(extra_root))

    result = discover_templates(defaultspack_root=defaultspack_root)

    template = next(item for item in result.templates if item.id == "extra.template")
    assert template.trust_level == TemplateTrustLevel.USER
    assert template.metadata["declared_trust_level"] == "builtin"
    assert template.metadata["source_kind"] == "configured_extra_root"


def test_selected_pack_manifest_id_mismatch_fails_closed(tmp_path: Path, select_packs):
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    pack_root = ecosystem_root / "feature_pack"
    _write_json(
        pack_root / "pack.v4.json",
        {
            "pack_api_version": "io.tobkiri.pack.v4",
            "pack": {
                "id": "different_pack",
                "artifact_digest": "sha256:" + "1" * 64,
            },
        },
    )
    _write_json(pack_root / "ecosystem.json", {"pack_id": "feature_pack"})
    _write_json(
        pack_root / "templates" / "feature.template" / "template.json",
        _template("feature.template"),
    )
    select_packs(["feature_pack"], ecosystem_root)

    result = discover_templates(defaultspack_root=defaultspack_root)

    assert result.templates == []
    assert any(
        diagnostic.code == "template.discovery.selected_pack_manifest_mismatch"
        and diagnostic.details["source_pack_id"] == "feature_pack"
        for diagnostic in result.diagnostics
    )


def test_unsafe_selected_pack_id_cannot_escape_ecosystem_root(tmp_path: Path, select_packs):
    runtime_root, ecosystem_root, defaultspack_root = _layout(tmp_path)
    outside_pack = runtime_root / "outside"
    _write_json(
        outside_pack / "pack.v4.json",
        {"pack_api_version": "io.tobkiri.pack.v4", "pack": {"id": "../outside"}},
    )
    _write_json(
        outside_pack / "templates" / "outside.template" / "template.json",
        _template("outside.template"),
    )
    select_packs(["../outside"], ecosystem_root)

    result = discover_templates(defaultspack_root=defaultspack_root)

    assert result.templates == []
    assert any(
        diagnostic.code == "template.discovery.selected_pack_invalid_id"
        for diagnostic in result.diagnostics
    )


def test_multiple_configured_extra_roots_follow_environment_order(monkeypatch, tmp_path: Path):
    _, _, defaultspack_root = _layout(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_TEMPLATE_ROOTS", os.pathsep.join((str(first), str(second)))
    )

    roots = default_template_roots(defaultspack_root)

    assert [root.path for root in roots[-2:]] == [first.resolve(), second.resolve()]


def test_selected_sibling_requires_v4_manifest(tmp_path: Path, select_packs) -> None:
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    pack_root = ecosystem_root / "legacy_pack"
    _write_json(pack_root / "ecosystem.json", {"pack_id": "legacy_pack"})
    _write_json(
        pack_root / "templates" / "legacy.template" / "template.json",
        _template("legacy.template"),
    )
    select_packs(["legacy_pack"], ecosystem_root)

    result = discover_templates(defaultspack_root=defaultspack_root)

    assert result.templates == []
    diagnostic = next(
        item
        for item in result.diagnostics
        if item.code == "template.discovery.selected_pack_manifest_mismatch"
    )
    assert diagnostic.details["source_pack_id"] == "legacy_pack"


def test_selected_sibling_is_bound_to_resolved_pack_artifact(tmp_path: Path, select_packs) -> None:
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    pack_root = _write_pack(
        ecosystem_root,
        "feature_pack",
        _template("feature.template"),
    )
    select_packs(["feature_pack"], ecosystem_root)
    manifest_path = pack_root / "pack.v4.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pack"]["artifact_digest"] = "sha256:" + "f" * 64
    _write_json(manifest_path, manifest)

    result = discover_templates(defaultspack_root=defaultspack_root)

    assert result.templates == []
    assert any(
        item.code == "template.discovery.selected_pack_manifest_mismatch"
        and item.details["source_pack_id"] == "feature_pack"
        for item in result.diagnostics
    )


def test_undeclared_or_modified_sibling_template_artifact_fails_closed(
    tmp_path: Path, select_packs
) -> None:
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    pack_root = _write_pack(
        ecosystem_root,
        "feature_pack",
        _template("feature.template"),
    )
    select_packs(["feature_pack"], ecosystem_root)
    _write_json(
        pack_root / "templates" / "injected.template" / "template.json",
        _template("injected.template"),
    )

    result = discover_templates(defaultspack_root=defaultspack_root)

    assert result.templates == []
    diagnostic = next(
        item
        for item in result.diagnostics
        if item.code == "template.discovery.selected_pack_template_artifact_mismatch"
    )
    assert diagnostic.details == {
        "source_pack_id": "feature_pack",
        "artifact_path": "templates/injected.template/template.json",
    }


def test_builtin_and_shared_templates_cannot_spoof_loader_provenance(
    tmp_path: Path,
) -> None:
    _, _, defaultspack_root = _layout(tmp_path)
    builtin = _template("builtin.template")
    shared = _template("shared.template")
    _write_json(
        defaultspack_root / "templates" / "builtin.template" / "template.json",
        builtin,
    )
    shared_root = defaultspack_root / "user_data" / "shared" / "templates"
    _write_json(
        shared_root / "shared.template" / "template.json",
        shared,
    )

    result = discover_templates(defaultspack_root=defaultspack_root)

    by_id = {template.id: template for template in result.templates}
    for template_id, expected_root in (
        ("builtin.template", defaultspack_root / "templates"),
        ("shared.template", shared_root),
    ):
        metadata = by_id[template_id].metadata
        assert metadata["source_root"] == str(expected_root.resolve())
        assert "source_pack_id" not in metadata
        assert "source_kind" not in metadata
        assert "source_pack_artifact_digest" not in metadata


def test_piece_public_id_collision_reports_both_sibling_pack_ids(
    tmp_path: Path, select_packs
) -> None:
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    template_a = _template("template.a")
    template_b = _template("template.b")
    template_a["pieces"][0]["command"]["id"] = "shared.command"
    template_b["pieces"][0]["command"]["id"] = "shared.command"
    _write_pack(ecosystem_root, "pack_a", template_a)
    _write_pack(ecosystem_root, "pack_b", template_b)
    select_packs(["pack_a", "pack_b"], ecosystem_root)

    catalog = build_template_catalog(defaultspack_root=defaultspack_root)

    collision = next(
        item
        for item in catalog["template_diagnostics"]
        if item["code"] == "template.catalog.public_id_collision"
        and item["details"]["bucket"] == "commands"
    )
    assert collision["details"]["source_pack_ids"] == ["pack_a", "pack_b"]
    assert not any(item["id"] == "shared.command" for item in catalog["commands"])


def test_selected_sibling_template_symlink_cannot_escape_pack(tmp_path: Path, select_packs) -> None:
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    pack_root = _write_pack(ecosystem_root, "feature_pack")
    outside_root = tmp_path / "outside_templates"
    _write_json(
        outside_root / "escaped.template" / "template.json",
        _template("escaped.template"),
    )
    template_root = pack_root / "templates"
    try:
        template_root.symlink_to(outside_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")
    select_packs(["feature_pack"], ecosystem_root)

    result = discover_templates(defaultspack_root=defaultspack_root)

    assert result.templates == []
    assert any(
        item.code == "template.discovery.selected_pack_template_root_escape"
        and item.details["source_pack_id"] == "feature_pack"
        for item in result.diagnostics
    )


def test_trust_diagnostic_includes_loader_owned_pack_provenance(
    tmp_path: Path, select_packs
) -> None:
    _, ecosystem_root, defaultspack_root = _layout(tmp_path)
    template = _template("feature.template")
    template["pieces"][0]["entrypoint"] = "external.runtime:run"
    _write_pack(ecosystem_root, "feature_pack", template)
    select_packs(["feature_pack"], ecosystem_root)

    catalog = build_template_catalog(defaultspack_root=defaultspack_root)

    diagnostic = next(
        item
        for item in catalog["template_diagnostics"]
        if item["code"] == "template.reference.non_builtin_handler_not_executable"
    )
    assert diagnostic["template_id"] == "feature.template"
    assert diagnostic["details"]["source_pack_id"] == "feature_pack"
    assert diagnostic["details"]["source_kind"] == "selected_sibling_pack"
