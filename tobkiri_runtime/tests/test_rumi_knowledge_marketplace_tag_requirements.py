from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parent.parent
PACK_DIR = ROOT / "ecosystem" / "rumi_knowledge_marketplace_pack"
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_tag_requirement_schema_is_human_readable_and_extensible() -> None:
    schema = read_json(PACK_DIR / "schemas" / "marketplace_requirement.schema.json")
    requirement = schema["$defs"]["requirement"]
    selector = schema["$defs"]["tag_selector"]
    tag = schema["$defs"]["tag"]

    assert selector["properties"]["match"]["enum"] == ["any", "all"]
    assert selector["properties"]["match"]["default"] == "any"
    assert requirement["properties"]["prefer_pack"]["default"] is True
    assert requirement["properties"]["marketplace_fallback"]["enum"] == [
        "search",
        "prompt_install",
        "disabled",
    ]
    assert "enum" not in requirement["properties"]["kind"]
    assert {"rough", "standard", "strong", "frontier"} == set(
        requirement["properties"]["model_tier"]["enum"]
    )

    uuid_pattern = re.compile(tag["not"]["pattern"])
    assert uuid_pattern.fullmatch("123e4567-e89b-12d3-a456-426614174000")
    assert not uuid_pattern.fullmatch("coding.python.refactor")


def test_marketplace_card_supports_tools_mcp_tags_and_listing_preview() -> None:
    card = read_json(PACK_DIR / "schemas" / "marketplace_card.schema.json")
    capability_types = set(card["properties"]["capability_type"]["enum"])

    assert {"skill", "tool", "mcp_tool", "mcp_server", "extension", "pack_bundle"} <= capability_types
    assert card["properties"]["requirements"]["items"]["$ref"].endswith(
        "marketplace_requirement.schema.json#/$defs/requirement"
    )
    assert set(card["properties"]["model_policy"]["properties"]["tier"]["enum"]) == {
        "rough",
        "standard",
        "strong",
        "frontier",
    }
    assert {"preview", "coming_soon", "listed"} <= set(
        card["properties"]["listing"]["properties"]["status"]["enum"]
    )
    assert "requirement selector" in card["properties"]["card_id"]["description"]


def test_model_tiers_define_low_risk_rough_usage_and_stronger_routes() -> None:
    catalog = read_yaml(PACK_DIR / "catalog" / "model_tiers.yaml")
    tiers = {item["id"]: item for item in catalog["tiers"]}

    assert catalog["provider_independent"] is True
    assert set(tiers) == {"rough", "standard", "strong", "frontier"}
    assert tiers["rough"]["label"] == "雑に使ってよい"
    assert tiers["rough"]["rough_use_allowed"] is True
    assert "security or trust decisions" in tiers["rough"]["avoid_for"]
    assert tiers["frontier"]["routing"]["min_quality_tier"] == "frontier"


def test_tagged_example_prefers_pack_and_supports_any_all_matching() -> None:
    example = read_yaml(PACK_DIR / "examples" / "tagged_requirement.example.yaml")
    card = example["marketplace_card"]
    requirements = {item["kind"]: item for item in card["requirements"]}

    assert requirements["pack"]["prefer_pack"] is True
    assert requirements["pack"]["selector"]["match"] == "any"
    assert requirements["mcp_tool"]["selector"]["match"] == "any"
    assert requirements["extension"]["selector"]["match"] == "all"
    assert requirements["extension"]["required"] is False
    assert card["model_policy"]["tier"] == "rough"
    assert card["listing"] == {
        "status": "preview",
        "searchable": True,
        "search_label": "探す",
    }
    assert example["expected_resolution"]["auto_install_allowed"] is False


def test_frontend_registry_exposes_marketplace_coming_soon_search(monkeypatch) -> None:
    from domain.frontend import registry as frontend_registry
    from domain.frontend.registry import FrontendRegistry

    monkeypatch.setattr(
        frontend_registry,
        "selected_extension_pack_ids",
        lambda _pack_root: {"rumi_knowledge_marketplace_pack"},
    )
    with patch("domain.frontend.registry.AIClient") as mock_client:
        mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
        catalog = FrontendRegistry(pack_root=DEFAULTSPACK_ROOT).build_catalog()

    item = next(
        entry for entry in catalog["sidebar"]["items"] if entry["id"] == "marketplace-preview"
    )
    assert item["label"] == "探す"
    assert item["description"] == "Marketplace — Coming soon"
    assert item["badge"] == "Coming soon"
    assert {"skill", "tool", "mcp", "pack"} <= set(item["tags"])
    assert "Coming soon" in item["panel"]["notes"]


def test_manifest_indexes_new_contract_and_preview_assets() -> None:
    manifest = read_json(PACK_DIR / "ecosystem.json")
    index = manifest["metadata"]["asset_index"]

    assert manifest["version"] == "0.2.0"
    assert "schemas/marketplace_requirement.schema.json" in index["schemas"]
    assert "catalog/model_tiers.yaml" in index["catalog"]
    assert "docs/tag-requirements.md" in index["docs"]
    assert "frontend_extensions/marketplace_preview.ui.json" in index["frontend_extensions"]
    assert "examples/tagged_requirement.example.yaml" in index["examples"]
