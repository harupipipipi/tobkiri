from __future__ import annotations



import json
import re
from pathlib import Path

import yaml

from backend_core.ecosystem.spec.schema.validator import validate_ecosystem
from ecosystem.setup_pack.pack_selector import PackSelector
import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parent.parent
PACK_ID = "rumi_runtime_benchmark_pack"
PACK_DIR = ROOT / "ecosystem" / PACK_ID
V4_AUTHORITY_ARTIFACTS = {"pack.v4.json", "contracts.v4.json", "artifact-index.v4.json"}
SETUP_PACK_JSON = ROOT / "ecosystem" / "setup_pack" / PACK_ID / "pack.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_pack_required_assets_and_metadata() -> None:
    required = [
        "README.md",
        "docs/README.md",
        "docs/architecture.md",
        "docs/interfaces.md",
        "docs/operations.md",
        "ecosystem.json",
        "catalog/runtime_benchmark_matrix.yaml",
        "catalog/environment_capture_schema.json",
        "catalog/latency_cost_sampling_matrix.yaml",
        "catalog/reproducibility_ledger.yaml",
        "catalog/release_gate_thresholds.yaml",
        "schemas/metric_record.schema.json",
        "specs/environment_descriptor.yaml",
        "policies/benchmark_reproducibility.policy.yaml",
        "policies/environment_capture.policy.yaml",
        "policies/warm_cold_start.policy.yaml",
        "policies/sample_size_confidence.policy.yaml",
        "coordination/subagent_benchmark_review_roster.yaml",
        "profiles/runtime_benchmark_reviewer.profile.yaml",
        "prompts/runtime_benchmark_reviewer.system.md",
        "prompts/reproducible_latency_cost_reviewer.system.md",
        "presets/safe_default.preset.yaml",
        "presets/handoff_review.preset.yaml",
        "presets/quality_gate.preset.yaml",
        "examples/latency_cost_snapshot.example.yaml",
        "examples/environment_capture_review.example.yaml",
        "examples/sampling_latency_cost_review.example.yaml",
    ]
    assert [path for path in required if not (PACK_DIR / path).is_file()] == []
    ecosystem = read_json(PACK_DIR / "ecosystem.json")
    assert ecosystem["pack_identity"] == f"rumi:ecosystem/{PACK_ID}"
    assert validate_ecosystem(ecosystem, raise_on_error=False) == []
    assert ecosystem["vocabulary"]["types"]
    assert ecosystem["dependencies"] == {}
    assert all((PACK_DIR / name).is_file() for name in V4_AUTHORITY_ARTIFACTS)
    assert "depends_on" not in ecosystem
    assert "optional_integrations" not in ecosystem
    assert ecosystem["required_secrets"] == []
    assert ecosystem["required_network"] == []
    assert ecosystem["metadata"]["required_secrets"] == []
    assert ecosystem["metadata"]["network_policy"] == "none_by_default"
    assert ecosystem["metadata"]["executable_code"] is False
    assert set(ecosystem["metadata"]["owner_surfaces"]) >= {
        "cost_snapshot",
        "latency_measurement",
        "sampling_plan",
        "environment_capture",
        "multi_pass_specialist_review",
    }
    indexed = {
        item
        for values in ecosystem["metadata"]["asset_index"].values()
        for item in values
    }
    for path in required:
        assert path in indexed or path in {"ecosystem.json"}


def test_pack_yaml_json_assets_parse() -> None:
    for path in PACK_DIR.rglob("*.yaml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path
    for path in PACK_DIR.rglob("*.json"):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), path


def test_pack_setup_discoverable_and_overlap_scoped() -> None:
    setup = read_json(SETUP_PACK_JSON)
    candidate = {item.pack_id: item for item in PackSelector(ROOT / "ecosystem").scan_candidates()}[PACK_ID]
    assert setup["supports_all_ok"] is False
    assert setup["risk_level"] == "medium"
    assert candidate.depends_on == [{"pack_id": "defaultspack", "version": ">=2.0.0"}]
    assert candidate.overlap_policy["model_quality_metrics"] == "handoff_to_rumi_model_evals_pack"
    assert candidate.defaultspack_promotion["eligible"] is False
    assert "reason" in candidate.defaultspack_promotion
    assert "promotion_blockers" in candidate.defaultspack_promotion
    assert "promotion_evidence_required" in candidate.defaultspack_promotion
    assert candidate.marketplace["id"].startswith("rumi.")
    assert candidate.marketplace["registry"] == "bundled"
    assert candidate.marketplace["publisher"] == "rumi-ai"
    assert candidate.marketplace["status"] == "verified"
    assert candidate.marketplace["category"] == "runtime-benchmark"
    assert candidate.signing["verified"] is True


def test_pack_thickened_runtime_benchmark_contracts() -> None:
    environment_schema = read_json(PACK_DIR / "catalog" / "environment_capture_schema.json")
    sampling_matrix = yaml.safe_load((PACK_DIR / "catalog" / "latency_cost_sampling_matrix.yaml").read_text(encoding="utf-8"))
    environment_policy = yaml.safe_load((PACK_DIR / "policies" / "environment_capture.policy.yaml").read_text(encoding="utf-8"))
    roster = yaml.safe_load((PACK_DIR / "coordination" / "subagent_benchmark_review_roster.yaml").read_text(encoding="utf-8"))
    metric_schema = read_json(PACK_DIR / "schemas" / "metric_record.schema.json")
    environment_descriptor = yaml.safe_load((PACK_DIR / "specs" / "environment_descriptor.yaml").read_text(encoding="utf-8"))
    warm_cold = yaml.safe_load((PACK_DIR / "policies" / "warm_cold_start.policy.yaml").read_text(encoding="utf-8"))
    sample_confidence = yaml.safe_load((PACK_DIR / "policies" / "sample_size_confidence.policy.yaml").read_text(encoding="utf-8"))
    ledger = yaml.safe_load((PACK_DIR / "catalog" / "reproducibility_ledger.yaml").read_text(encoding="utf-8"))
    thresholds = yaml.safe_load((PACK_DIR / "catalog" / "release_gate_thresholds.yaml").read_text(encoding="utf-8"))

    assert "network_policy" in environment_schema["required_fields"]
    assert "cache_policy" in environment_schema["required_fields"]
    assert "sampling_plan_id" in environment_schema["required_fields"]
    assert {"latency", "cost", "reliability"} <= {
        item["id"] for item in sampling_matrix["metric_families"]
    }
    assert {"smoke_three_run", "comparison_ten_run", "release_gate_thirty_run"} <= {
        item["id"] for item in sampling_matrix["sampling_plans"]
    }
    assert "claiming p95 from smoke samples" in environment_policy["blocked_by_default"]
    assert "seed" in metric_schema["required"]
    assert "sample_count" in metric_schema["required"]
    assert "replay_evidence" in metric_schema["required"]
    assert metric_schema["properties"]["units"]["properties"]["latency"]["enum"] == ["milliseconds", "seconds"]
    assert "warm_cold_policy_id" in environment_descriptor["descriptor_fields"]["required"]
    assert {item["id"] for item in warm_cold["start_modes"]} == {"cold_start", "warm_path"}
    assert sample_confidence["sample_size_rules"]["release_gate_thirty_run"]["minimum_samples"] == 30
    assert "confidence_statement" in sample_confidence["variance_notes_required"]
    assert "seed" in ledger["replay_evidence_required"]
    assert "tool_trace_id" in ledger["replay_evidence_required"]
    assert thresholds["thresholds"]["latency"]["units"] == "milliseconds"
    assert thresholds["thresholds"]["cost"]["units"] == "estimated_usd"
    assert roster["governance"]["repeated_specialist_passes_required"] is True
    assert roster["governance"]["grant_effect"] == "none"
    assert {item["id"] for item in roster["review_passes"]} == {
        "reproducibility_auditor",
        "sampling_reviewer",
        "latency_cost_analyst",
        "final_benchmark_integrator",
    }


def test_pack_docs_no_secrets_and_explain_boundaries() -> None:
    docs = "\n".join((PACK_DIR / path).read_text(encoding="utf-8") for path in ["README.md", "docs/interfaces.md", "docs/operations.md"])
    for expected in ["Required Secrets", "None", "defaultspack", "handoff", "evidence", "sampling", "latency/cost", "environment-capture"]:
        assert expected in docs
    pattern = re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\'\"]?[A-Za-z0-9_\-]{12,}"
    )
    checked = [p for p in PACK_DIR.rglob("*") if p.is_file()] + [SETUP_PACK_JSON]
    assert [str(p.relative_to(ROOT)) for p in checked if pattern.search(p.read_text(encoding="utf-8"))] == []
    combined = "\n".join(p.read_text(encoding="utf-8") for p in checked)
    assert "sample user request" not in combined
    assert "reviewer_ready_plan" not in combined
