"""Renderer selection comes from verified v4 artifacts, never executable metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core_runtime.pack_sdk import refresh_scaffold_artifacts, scaffold_pack
from ecosystem.defaultspack.defaultspack import v4_frontend_contributions as frontend
from ecosystem.defaultspack.defaultspack.v4_view_contract import validate_catalog_view
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.canonical import canonical_digest

_PACK = "qa.surface.renderer"
_PATH = "frontend/contributions/renderer.json"
_ROOT = Path(__file__).parents[1]


def descriptor() -> dict[str, Any]:
    """Use the shipped data-only optional Pack's real inert descriptor."""
    return json.loads(
        (
            _ROOT
            / "ecosystem/tobkiri_surface_renderer_pack/frontend/contributions/standard.json"
        ).read_text("utf-8")
    )


def signed_pack(root: Path, value: dict[str, Any]) -> str:
    """Bind sidecar bytes into an SDK artifact/index fixture with no Functions."""
    scaffold_pack(root, pack_id=_PACK, display_name="QA Surface renderer")
    path = root / _PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value) + "\n")
    refresh_scaffold_artifacts(root)
    manifest_path = root / "pack.v4.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["functions"] == []
    manifest["pack"]["kind"] = "normal_sandbox"
    manifest["requirements"]["execution_boundary"] = "declarative_only"
    next(item for item in manifest["artifacts"] if item["path"] == _PATH)["kind"] = (
        "ui.contribution"
    )
    digest = canonical_digest(manifest["artifacts"])
    manifest["pack"]["artifact_digest"] = digest
    manifest["integrity"]["artifact_set_digest"] = digest
    manifest_path.write_text(json.dumps(manifest) + "\n")
    index_path = root / "artifact-index.v4.json"
    index = json.loads(index_path.read_text())
    index["artifact_set_digest"] = digest
    import hashlib

    next(item for item in index["artifacts"] if item["path"] == "pack.v4.json")[
        "digest"
    ] = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    unsigned = {key: value for key, value in index.items() if key != "integrity_seal"}
    index["integrity_seal"]["signed_digest"] = canonical_digest(unsigned)
    index_path.write_text(json.dumps(index) + "\n")
    compiled = compile_pack_root(root)
    assert compiled.artifact.functions == ()
    return compiled.artifact.digest


def test_signed_selected_renderer_projects_exact_capture_and_host_expiry(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The manifest selects only after compilation and artifact/index checks."""
    root = tmp_path / "renderer"
    digest = signed_pack(root, descriptor())
    monkeypatch.setattr(frontend, "resolve_admitted_pack_root", lambda pack_id: root)
    monkeypatch.setattr(
        frontend,
        "load_admitted_pack_catalog",
        lambda: {
            _PACK: {"runtime_artifacts": [{"path": _PATH, "kind": "ui.contribution"}]},
        },
    )
    monkeypatch.setattr(frontend.time, "time", lambda: 1700000000)
    selected = [{"role": "pack", "identity": _PACK, "artifact_digest": digest}]
    items, diagnostics, quarantined = frontend.project_selected_declarative_routes(
        selected,
        [],
        profile_id="profile",
        profile_revision="revision",
        activation_id="activation",
        plan_digest="plan",
    )
    assert diagnostics == [] and quarantined == []
    assert items[0]["kind"] == "renderer"
    assert items[0]["owner_pack_hash"] == digest
    assert items[0]["resolved_expires_at_ms"] == 1700000300000
    assert items[0]["resolved_profile_id"] == "profile"
    assert items[0]["resolved_activation_id"] == "activation"
    assert items[0]["resolved_plan_hash"] == "plan"
    assert "module" not in items[0]
    assert "action_contract" not in items[0]
    # Tampering cannot refresh a verified renderer or invent a fallback.
    path = root / _PATH
    path.write_text(path.read_text().replace("semantic_standard", "semantic_compact"))
    items, _, quarantined = frontend.project_selected_declarative_routes(
        selected,
        [],
        profile_id="profile",
        profile_revision="revision",
        activation_id="activation",
        plan_digest="plan",
    )
    assert items == [] and quarantined == [_PACK]


def test_shipped_surface_template_passes_both_public_schema_layers() -> None:
    from jsonschema import Draft202012Validator
    from tobkiri_protocol.surface_templates_v1 import validate_surface_template

    template = json.loads(
        (_ROOT / "tests/fixtures/surface_templates_v1/ten_patterns.json").read_text()
    )
    view = {
        "version": "tobkiri.ui.view.v1",
        "slot": "workspace_tab",
        "renderer": "surface_template",
        "surface_template": template,
    }
    validate_catalog_view(view)
    outer = {
        "version": "rumi.ui.contribution.v1",
        "id": "fixture.surface",
        "kind": "view",
        "mode": "declarative",
        "label": "Surface",
        "priority": 0,
        "accessibility": {"name": "Surface", "keyboard": True},
        "view": view,
    }
    Draft202012Validator(
        json.loads((_ROOT / "schemas/frontend_contribution.schema.json").read_text())
    ).validate(outer)
    assert validate_surface_template(template) == template


@pytest.mark.parametrize(
    "change",
    [
        {"renderer": "https://evil.test/module.js"},
        {"module": {"path": "evil.js"}},
        {"view": {"version": "tobkiri.ui.surface-renderer.v2"}},
    ],
)
def test_unknown_or_executable_renderer_descriptor_is_never_admitted(
    tmp_path: Path, monkeypatch: Any, change: dict[str, Any]
) -> None:
    """Even a selected exact artifact cannot turn inert metadata into code."""
    root = tmp_path / "renderer"
    digest = signed_pack(root, {**descriptor(), **change})
    monkeypatch.setattr(frontend, "resolve_admitted_pack_root", lambda pack_id: root)
    monkeypatch.setattr(
        frontend,
        "load_admitted_pack_catalog",
        lambda: {
            _PACK: {"runtime_artifacts": [{"path": _PATH, "kind": "ui.contribution"}]},
        },
    )
    items, diagnostics, quarantined = frontend.project_selected_declarative_routes(
        [{"role": "pack", "identity": _PACK, "artifact_digest": digest}],
        [],
        profile_id="profile",
        profile_revision="revision",
        activation_id="activation",
        plan_digest="plan",
    )
    assert items == [] and quarantined == [_PACK]
    assert diagnostics[0]["code"] == "v4_frontend_pack_quarantined"
