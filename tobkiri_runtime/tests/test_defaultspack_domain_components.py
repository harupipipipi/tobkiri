from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.components import DomainComponentRegistry, discover_components  # noqa: E402
from domain.components.registry import build_domain_component_roots  # noqa: E402


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_component_discovery_reads_manifest_without_importing_entrypoint(tmp_path):
    pack = tmp_path / "defaultspack"
    _write_json(pack / "pack.v4.json", {"pack": {"id": "defaultspack"}})
    _write_json(
        pack / "domain" / "tools" / "demo" / "manifest.json",
        {
            "id": "demo_tool",
            "category": "tools",
            "kind": "tool",
            "version": "1",
            "status": "stable",
            "entrypoints": {"handler": "missing.module:handler"},
            "aliases": ["legacy_demo_tool"],
            "security": {"approval": "never"},
        },
    )

    registry = DomainComponentRegistry([pack / "domain"])

    component = registry.get("tools", "legacy_demo_tool")
    assert component is not None
    assert component.id == "demo_tool"
    assert component.manifest["entrypoints"]["handler"] == "missing.module:handler"
    assert registry.diagnostics() == []


def test_component_discovery_fails_soft_on_bad_manifest(tmp_path):
    root = tmp_path / "pack" / "domain"
    _write_json(root / "webhooks" / "bad" / "manifest.json", {"id": "missing_fields"})

    result = discover_components(root)

    assert result.components == []
    assert len(result.issues) == 1
    assert "manifest.category is required" in result.issues[0].message


def test_component_roots_include_selected_sibling_ecosystem_packs(tmp_path, monkeypatch):
    from domain.components import registry as component_registry

    monkeypatch.setattr(
        component_registry,
        "effective_pack_ids",
        lambda: frozenset({"rumi_model_catalog_pack"}),
    )
    ecosystem = tmp_path / "ecosystem"
    defaultspack = ecosystem / "defaultspack"
    catalog = ecosystem / "rumi_model_catalog_pack"
    _write_json(defaultspack / "pack.v4.json", {"pack": {"id": "defaultspack"}})
    _write_json(catalog / "pack.v4.json", {"pack": {"id": "rumi_model_catalog_pack"}})
    (catalog / "domain").mkdir(parents=True)
    monkeypatch.setattr(
        "domain.components.registry.effective_pack_ids",
        lambda: frozenset({"rumi_model_catalog_pack"}),
    )

    roots = build_domain_component_roots(defaultspack)

    assert defaultspack / "domain" in roots
    assert catalog / "domain" in roots


def test_component_roots_skip_unreadable_sibling_pack_candidates(tmp_path, monkeypatch):
    from domain.components import registry as component_registry

    monkeypatch.setattr(
        component_registry,
        "effective_pack_ids",
        lambda: frozenset({"rumi_model_catalog_pack", "restricted"}),
    )
    ecosystem = tmp_path / "ecosystem"
    defaultspack = ecosystem / "defaultspack"
    catalog = ecosystem / "rumi_model_catalog_pack"
    restricted = ecosystem / "restricted"
    _write_json(defaultspack / "pack.v4.json", {"pack": {"id": "defaultspack"}})
    _write_json(catalog / "pack.v4.json", {"pack": {"id": "rumi_model_catalog_pack"}})
    (catalog / "domain").mkdir(parents=True)
    restricted.mkdir(parents=True)
    monkeypatch.setattr(
        "domain.components.registry.effective_pack_ids",
        lambda: frozenset({"rumi_model_catalog_pack"}),
    )

    original_is_file = Path.is_file

    def fake_is_file(path: Path) -> bool:
        if path == restricted / "pack.v4.json":
            raise PermissionError("blocked test path")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", fake_is_file)

    roots = build_domain_component_roots(defaultspack)

    assert defaultspack / "domain" in roots
    assert catalog / "domain" in roots
    assert restricted / "domain" not in roots


def test_component_registry_supports_category_scoped_aliases(tmp_path):
    root = tmp_path / "pack" / "domain"
    _write_json(
        root / "webhooks" / "line" / "manifest.json",
        {
            "id": "line",
            "category": "webhooks",
            "kind": "connector_defaults",
            "version": "1",
            "status": "stable",
            "aliases": {"endpoint_ids": ["line-main"], "profile_ids": ["line.default"]},
        },
    )

    registry = DomainComponentRegistry(root)

    assert registry.get("webhooks", "line-main").id == "line"
    assert registry.get("webhooks", "line.default").id == "line"
    assert registry.get("tools", "line-main") is None


def test_component_discovery_uses_root_pack_id_over_manifest_spoof(tmp_path):
    ecosystem = tmp_path / "ecosystem"
    malicious = ecosystem / "malicious_pack"
    _write_json(malicious / "pack.v4.json", {"pack": {"id": "malicious_pack"}})
    _write_json(
        malicious / "domain" / "tools" / "spoof" / "manifest.json",
        {
            "id": "spoof",
            "category": "tools",
            "kind": "tool",
            "version": "1",
            "status": "stable",
            "source_pack_id": "defaultspack",
        },
    )

    result = discover_components(malicious / "domain")

    assert len(result.components) == 1
    component = result.components[0]
    assert component.source_pack_id == "malicious_pack"
    assert component.manifest["source_pack_id"] == "malicious_pack"


def test_component_discovery_rejects_source_identity_on_v4_manifest_mismatch(tmp_path):
    ecosystem = tmp_path / "ecosystem"
    malicious = ecosystem / "malicious_pack"
    _write_json(malicious / "pack.v4.json", {"pack": {"id": "defaultspack"}})
    _write_json(
        malicious / "domain" / "tools" / "spoof" / "manifest.json",
        {
            "id": "spoof",
            "category": "tools",
            "kind": "tool",
            "version": "1",
            "status": "stable",
        },
    )

    result = discover_components(malicious / "domain")

    assert len(result.components) == 1
    component = result.components[0]
    assert component.source_pack_id == ""
    assert component.manifest["source_pack_id"] == ""
