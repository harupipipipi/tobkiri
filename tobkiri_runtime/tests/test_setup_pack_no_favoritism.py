from __future__ import annotations

import json
from pathlib import Path

import yaml

from core_runtime.setup_pack import SetupPackManager
from core_runtime.setup_pack_metadata import validate_setup_pack_schema
from ecosystem.setup_pack.pack_selector import PackSelector


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
ECOSYSTEM_ROOT = RUNTIME_ROOT / "ecosystem"
LEGACY_NAMED_PROMOTION_IDENTIFIERS = {
    "defaultspack_promotion",
    "defaultspack_promotion_false",
}
LEGACY_NAMED_PROMOTION_ASSETS = (
    "rumi_agent_workroom_pack/checklists/review.checklist.yaml",
    "rumi_agentic_qa_pack/policies/agentic_qa_gate.policy.yaml",
    "rumi_api_toolsmith_pack/policies/api_tool_safety.policy.yaml",
    "rumi_artifact_app_runtime_pack/checklists/review.checklist.yaml",
    "rumi_browser_form_operator_pack/policies/form_action_safety.policy.yaml",
    "rumi_computer_control_pack/metadata/overlap_promotion.yaml",
    "rumi_devops_release_pack/catalog/devops_operations_catalog.json",
    "rumi_devops_release_pack/metadata/overlap_promotion.yaml",
    "rumi_document_intelligence_pack/policies/document_privacy.policy.yaml",
    "rumi_evidence_dossier_pack/checklists/review.checklist.yaml",
    "rumi_omnichannel_agent_inbox_pack/checklists/review.checklist.yaml",
    "rumi_runtime_benchmark_pack/policies/benchmark_reproducibility.policy.yaml",
    "rumi_subagent_pr_manager_pack/catalog/subagent_routing_matrix.yaml",
    "rumi_subagent_pr_manager_pack/policies/subagent_pr_governance.policy.yaml",
)


def _structured_strings(value: object) -> set[str]:
    """Return every key and scalar string in a structured Pack asset."""

    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {
            *(
                item
                for key, nested in value.items()
                for item in {str(key), *_structured_strings(nested)}
            ),
        }
    if isinstance(value, list):
        return {item for nested in value for item in _structured_strings(nested)}
    return set()


def test_setup_pack_uses_generic_base_pack_promotion_metadata(tmp_path) -> None:
    root = tmp_path / "setup_pack"
    definition_path = root / "contribution_setup" / "pack.json"
    definition_path.parent.mkdir(parents=True)
    definition_path.write_text(
        json.dumps(
            {
                "pack_id": "contribution_setup",
                "target_pack_id": "contribution_pack",
                "base_pack_promotion": {"eligible": False},
            }
        ),
        encoding="utf-8",
    )

    manager = SetupPackManager(
        root=root,
        selection_file=tmp_path / "selection.json",
        ecosystem_dir=tmp_path / "ecosystem",
    )
    definition = manager.list_packs()["packs"][0]

    assert definition["base_pack_promotion"] == {"eligible": False}
    assert not validate_setup_pack_schema(
        json.loads(definition_path.read_text(encoding="utf-8"))
    )


def test_selector_reads_only_generic_base_pack_promotion_metadata(tmp_path) -> None:
    """Legacy named promotion metadata cannot restore a core fallback."""
    setup_root = tmp_path / "setup_pack"
    manifest = setup_root / "contribution_setup" / "pack.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "pack_id": "contribution_setup",
                "target_pack_id": "contribution_pack",
                "base_pack_promotion": {"eligible": False},
                "defaultspack_promotion": {"eligible": True},
            }
        ),
        encoding="utf-8",
    )

    candidate = PackSelector(setup_root).scan_candidates()[0]

    assert candidate.base_pack_promotion == {"eligible": False}
    assert candidate.to_dict()["base_pack_promotion"] == {"eligible": False}
    assert "defaultspack_promotion" not in candidate.to_dict()


def test_pack_metadata_has_no_legacy_named_promotion_identifiers() -> None:
    """Keep the generic promotion terminology consistent across Pack metadata."""

    offenders: list[str] = []
    for relative_path in LEGACY_NAMED_PROMOTION_ASSETS:
        path = ECOSYSTEM_ROOT / relative_path
        assert path.is_file(), relative_path
        if path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
        else:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        found = LEGACY_NAMED_PROMOTION_IDENTIFIERS & _structured_strings(value)
        if found:
            offenders.append(f"{path.relative_to(RUNTIME_ROOT)}: {sorted(found)}")

    assert offenders == []
