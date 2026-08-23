from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.templates import ResolvedTemplate, parse_template, project_resolved_templates  # noqa: E402


def _resolved(template_id: str, surface: dict, *, trust_level: str = "local") -> ResolvedTemplate:
    surface = dict(surface)
    data_source_id = str(surface.setdefault("data_source", f"{template_id}.active"))
    action_ids = sorted(
        {
            str(control["action_id"])
            for control in surface.get("controls", [])
            if isinstance(control, dict) and control.get("action_id")
        }
    )
    pieces = [
        {
            "id": data_source_id,
            "kind": "function",
            "role": "data_source",
            "data_source": data_source_id,
            "snapshot": {},
        },
        *[
            {
                "id": action_id,
                "kind": "composer_command",
                "command": {
                    "id": action_id,
                    "name": action_id,
                    "label": action_id,
                    "execution": {
                        "type": "pack_block",
                        "qualified_name": f"{template_id}:{action_id}",
                    },
                },
            }
            for action_id in action_ids
        ],
        {
            "id": surface["id"],
            "kind": "status_surface",
            "slot": surface.get("slot", "above_composer"),
            "surface": surface,
        },
    ]
    result = parse_template(
        {
            "schema_version": 1,
            "id": template_id,
            "kind": "pack",
            "version": "1.0.0",
            "status": "active",
            "pieces": pieces,
        },
        trust_level=trust_level,
    )
    assert result.template is not None, result.diagnostics
    assert result.ok, result.diagnostics
    return ResolvedTemplate(result.template, result.diagnostics)


def test_status_surface_projects_versioned_generic_contract_with_provenance():
    catalog = project_resolved_templates(
        [
            _resolved(
                "fixture.review",
                {
                    "id": "active_review",
                    "data_source": "review.active",
                    "title": "Review",
                    "summary_path": "instruction",
                    "controls": [
                        {"type": "button", "label": "Cancel", "action_id": "review.cancel"}
                    ],
                },
            ),
            _resolved(
                "fixture.upload",
                {
                    "id": "active_upload",
                    "slot": "chat_header",
                    "data_source": "upload.active",
                    "title_path": "filename",
                    "progress": {"current_path": "sent", "total_path": "total"},
                },
            ),
        ]
    )

    assert [item["surface_id"] for item in catalog["status_surfaces"]] == [
        "active_review",
        "active_upload",
    ]
    review, upload = catalog["status_surfaces"]
    assert review["api_version"] == "rumi.status_surface.v1"
    assert review["slot"] == "above_composer"
    assert review["template_id"] == "fixture.review"
    assert review["projected_id"] == "fixture.review:active_review"
    assert review["trust_level"] == "local"
    assert review["controls"][0]["action_id"] == "review.cancel"
    assert upload["slot"] == "chat_header"
    assert upload["progress"] == {"current_path": "sent", "total_path": "total"}


def test_status_surface_collision_fails_closed_between_packs():
    catalog = project_resolved_templates(
        [
            _resolved("fixture.one", {"id": "shared", "title": "One"}),
            _resolved("fixture.two", {"id": "shared", "title": "Two"}),
        ]
    )

    assert catalog["status_surfaces"] == []
    assert any(
        item.get("code") == "template.catalog.public_id_collision"
        and item.get("details", {}).get("bucket") == "status_surfaces"
        for item in catalog["template_diagnostics"]
    )


def test_frontend_catalog_exposes_projected_status_surfaces_to_the_webapp():
    registry_path = DEFAULTSPACK_ROOT / "domain" / "frontend" / "registry.py"
    tree = ast.parse(registry_path.read_text(encoding="utf-8"))
    build_catalog = next(
        node
        for class_node in tree.body
        if isinstance(class_node, ast.ClassDef) and class_node.name == "FrontendRegistry"
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_catalog"
    )
    returned_keys = {
        key.value
        for node in ast.walk(build_catalog)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
        for key in node.value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }

    assert "status_surfaces" in returned_keys


def test_two_unrelated_sibling_pack_fixtures_validate_and_project_from_disk():
    fixture_root = ROOT / "tests" / "fixtures" / "status_surfaces"
    resolved = []
    for fixture_name in ("review", "upload"):
        path = fixture_root / fixture_name / "template.json"
        result = parse_template(
            json.loads(path.read_text(encoding="utf-8")),
            source_path=str(path),
            trust_level="local",
        )
        assert result.ok, result.diagnostics
        assert result.template is not None
        resolved.append(ResolvedTemplate(result.template, result.diagnostics))

    catalog = project_resolved_templates(resolved)

    assert [surface["surface_id"] for surface in catalog["status_surfaces"]] == [
        "active-review",
        "active-upload",
    ]
    assert {source["data_source"] for source in catalog["data_sources"]} == {
        "review.active",
        "upload.active",
    }
    assert any(command["id"] == "review.pause" for command in catalog["commands"])


def test_status_surface_schema_rejects_unregistered_and_unsafe_contract_fields():
    result = parse_template(
        {
            "schema_version": 1,
            "id": "fixture.status.invalid",
            "kind": "pack",
            "version": "1.0.0",
            "status": "active",
            "pieces": [
                {
                    "id": "invalid-surface",
                    "kind": "status_surface",
                    "slot": "floating_script",
                    "surface": {
                        "id": "invalid-surface",
                        "api_version": "rumi.status_surface.v99",
                        "data_source": "missing.source",
                        "title_path": "constructor.prototype.title",
                        "progress": {"current_path": "done"},
                        "visible_when": {"__proto__.visible": True},
                        "controls": [
                            {
                                "id": "invalid control id",
                                "type": "button",
                                "action_id": "missing.action",
                            }
                        ],
                    },
                }
            ],
        },
        trust_level="local",
    )

    assert result.template is not None
    assert not result.ok
    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert {
        "template.reference.status_surface_unsupported_version",
        "template.reference.status_surface_invalid_slot",
        "template.reference.status_surface_unknown_data_source",
        "template.reference.status_surface_invalid_path",
        "template.reference.status_surface_invalid_control_id",
        "template.reference.status_surface_unknown_action",
    } <= codes
