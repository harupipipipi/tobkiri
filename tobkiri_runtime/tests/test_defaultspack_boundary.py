from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK = ROOT / "ecosystem" / "defaultspack"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_defaultspack_has_no_legacy_concrete_tool_collection() -> None:
    extension_tools = DEFAULTSPACK / "extensions" / "tools"
    allowed_settings_tools = {"settings_inspect", "settings_update"}
    if extension_tools.exists():
        assert {
            item.name for item in extension_tools.iterdir() if item.is_dir()
        } <= allowed_settings_tools

    registry_source = _read(DEFAULTSPACK / "domain" / "tool" / "registry.py")
    assert "def _register_defaults" not in registry_source
    assert "web_search" not in registry_source
    assert "calculator" not in registry_source
    assert "file_reader" not in registry_source


def test_defaultspack_has_no_operations_company_product_content() -> None:
    forbidden_paths = [
        DEFAULTSPACK / "domain" / "agent" / "operations_company.py",
        DEFAULTSPACK / "profiles" / "operations_company.profile.yaml",
        DEFAULTSPACK / "graphs" / "operations_company.graph.yaml",
        DEFAULTSPACK / "prompts" / "operations_company.system.md",
    ]
    for path in forbidden_paths:
        assert not path.exists(), str(path)

    frontend_registry = _read(DEFAULTSPACK / "domain" / "frontend" / "registry.py")
    assert "operations-company" not in frontend_registry
    assert "operations_company" not in frontend_registry


def test_defaultspack_has_no_concrete_agent_profiles_or_prompts() -> None:
    forbidden_profiles = {
        "local_agent.profile.yaml",
        "coding.profile.yaml",
        "artifact_agent.profile.yaml",
        "compact_coding.profile.yaml",
        "local_research_writer.profile.yaml",
        "research_agent.profile.yaml",
    }
    forbidden_prompts = {
        "local_agent.system.md",
        "planner.system.md",
        "coding_agent.system.md",
        "artifact_writer.system.md",
        "compact.system.md",
        "file_authoring.system.md",
        "memory_curator.system.md",
        "research_agent.system.md",
        "reviewer.system.md",
    }
    for name in forbidden_profiles:
        assert not (DEFAULTSPACK / "profiles" / name).exists()
    for name in forbidden_prompts:
        assert not (DEFAULTSPACK / "prompts" / name).exists()


def test_frontend_registry_does_not_hardcode_concrete_sidebar_items() -> None:
    source = _read(DEFAULTSPACK / "domain" / "frontend" / "registry.py")
    forbidden = [
        "provider-catalog",
        "collaboration",
        "share-export",
        "browser-computer",
        "research-providers",
    ]
    for token in forbidden:
        assert token not in source


def test_provider_catalog_concrete_data_lives_outside_defaultspack_loader() -> None:
    provider_catalog = _read(DEFAULTSPACK / "backend" / "ai_client" / "provider_catalog.py")
    assert "_PROVIDER_CATALOG" not in provider_catalog
    assert "_CATALOG_MODELS" not in provider_catalog


def test_required_starter_packs_exist() -> None:
    for pack_id in [
        "rumi_default_tools_pack",
        "rumi_local_agent_pack",
        "rumi_operations_team_pack",
        "rumi_reference_ui_pack",
        "rumi_model_catalog_pack",
    ]:
        pack_root = ROOT / "ecosystem" / pack_id
        assert (pack_root / "ecosystem.json").is_file(), pack_id
