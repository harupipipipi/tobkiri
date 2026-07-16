from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.templates import ResolvedTemplate, parse_template, project_resolved_templates  # noqa: E402


def _resolved(template_id: str, surface: dict, *, trust_level: str = "local") -> ResolvedTemplate:
    result = parse_template(
        {
            "schema_version": 1,
            "id": template_id,
            "kind": "pack",
            "version": "1.0.0",
            "status": "active",
            "pieces": [
                {
                    "id": surface["id"],
                    "kind": "status_surface",
                    "slot": surface.get("slot", "above_composer"),
                    "surface": surface,
                }
            ],
        },
        trust_level=trust_level,
    )
    assert result.template is not None, result.diagnostics
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
