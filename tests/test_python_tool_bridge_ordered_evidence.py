"""Adversarial evidence validation; synthetic bytes are never replay evidence."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import stage_ordered_tool_bridge as staging
from replay_ordered_tool_bridge import validate_evidence


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def synthetic_receipt(tmp_path):
    """Create structural validator inputs, explicitly not a Wasm component."""
    staged = tmp_path / "staged"
    plan = staging.stage(staged)
    artifact = tmp_path / "not-a-component.wasm"
    artifact.write_bytes(b"synthetic validator test only")
    manifest = [{**entry, "bytes": (staged / entry["source"]).stat().st_size}
                for entry in plan["sources"]]
    receipt = {
        "format": "tobkiri.component-build/2", "language": "python",
        "artifact_sha256": digest(artifact.read_bytes()),
        "artifact_bytes": artifact.stat().st_size,
        "build_plan_sha256": digest((staged / "plan.json").read_bytes()),
        "source_manifest": manifest,
        "source_manifest_sha256": digest(json.dumps(manifest, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False).encode()),
        "world": plan["world"], "imports": sorted(plan["allowed_imports"]),
        "exports": sorted(plan["outer_exports"]),
    }
    artifact.with_suffix(".build.json").write_text(json.dumps(receipt))
    return artifact, receipt


def test_full_reviewed_structure_validates_without_claiming_component_execution(synthetic_receipt):
    artifact, _ = synthetic_receipt
    binary, _, _, oracle = validate_evidence(artifact, staging.PACK / "oracle.json")
    assert binary == b"synthetic validator test only"
    assert len(oracle["cases"]) == 33


@pytest.mark.parametrize("destination", ["original_bridge.py", "app.py",
    "caller_adapter.py", "ordered_json.py", "wit/pack.wit", "wit/deps/core.wit",
    "provenance.json", "inventory.json", "reviewed-inputs.json"])
def test_rejects_any_changed_source_even_with_recomputed_manifest_hash(synthetic_receipt, destination):
    artifact, receipt = synthetic_receipt
    entry = next(item for item in receipt["source_manifest"] if item["destination"] == destination)
    entry["sha256"] = "0" * 64
    receipt["source_manifest_sha256"] = digest(json.dumps(receipt["source_manifest"],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    artifact.with_suffix(".build.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="complete reviewed source plan"):
        validate_evidence(artifact, staging.PACK / "oracle.json")


@pytest.mark.parametrize("mutation", ["duplicate", "extra", "missing", "plan", "artifact"])
def test_rejects_manifest_and_plan_tampering(synthetic_receipt, mutation):
    artifact, receipt = synthetic_receipt
    if mutation == "duplicate":
        receipt["source_manifest"].append(copy.deepcopy(receipt["source_manifest"][0]))
    elif mutation == "extra":
        receipt["source_manifest"].append({"source": "x.py", "destination": "x.py", "sha256": "0"*64, "bytes": 0})
    elif mutation == "missing":
        receipt["source_manifest"].pop()
    elif mutation == "plan":
        receipt["build_plan_sha256"] = "0" * 64
    else:
        artifact.write_bytes(b"changed artifact")
    artifact.with_suffix(".build.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        validate_evidence(artifact, staging.PACK / "oracle.json")


@pytest.mark.parametrize("mutation", ["empty", "reduced", "rewritten", "protocol", "freeze"])
def test_rejects_changed_external_oracle_with_same_provenance(synthetic_receipt, tmp_path, mutation):
    artifact, _ = synthetic_receipt
    oracle = json.loads((staging.PACK / "oracle.json").read_bytes())
    if mutation == "empty":
        oracle["cases"] = []
    elif mutation == "reduced":
        oracle["cases"].pop()
    elif mutation == "rewritten":
        oracle["cases"][0]["transport"]["output_json"] = "{}"
    elif mutation == "protocol":
        oracle["protocol"] = "other"
    else:
        oracle["freeze_script_sha256"] = "0" * 64
    path = tmp_path / "changed-oracle.json"
    path.write_text(json.dumps(oracle))
    with pytest.raises(ValueError, match="exact reviewed frozen oracle"):
        validate_evidence(artifact, path)


def test_stage_rejects_reduced_current_oracle_even_if_its_byte_hash_is_updated(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    shutil.copytree(staging.PACK, pack)
    monkeypatch.setattr(staging, "PACK", pack)
    oracle = json.loads((pack / "oracle.json").read_bytes())
    oracle["cases"].pop()
    raw = json.dumps(oracle).encode()
    (pack / "oracle.json").write_bytes(raw)
    binding = json.loads((pack / "oracle.freeze.json").read_bytes())
    binding["oracle_sha256"] = digest(raw)
    (pack / "oracle.freeze.json").write_text(json.dumps(binding))
    with pytest.raises(ValueError, match="complete case set"):
        staging.stage(tmp_path / "stage")


def test_only_hashes_not_gold_are_staged(tmp_path):
    target = tmp_path / "stage"
    staging.stage(target)
    binding = json.loads((target / "reviewed-inputs.json").read_bytes())
    assert "oracle_sha256" in binding
    assert "cases" not in binding
    assert not (target / "oracle.json").exists()
    assert not (target / "freeze_ordered_tool_bridge.py").exists()


@pytest.mark.parametrize("flags", [{"admitted_parity": True},
    {"exclusion_enforced": True}, {"admitted_parity": True, "exclusion_enforced": True}])
def test_late_runner_failure_revokes_all_success_flags(flags):
    from replay_ordered_tool_bridge import record_runner_failure
    record = {**flags, "actual": {"status": "ok"}}
    record_runner_failure(record, RuntimeError("get_fuel failed after guest return"))
    assert record["admitted_parity"] is False
    assert record["exclusion_enforced"] is False
    assert record["actual"]["status"] == "runner-failure"


def test_bounded_evidence_read_rejects_links_and_oversized_files(tmp_path):
    data = tmp_path / "data.json"
    data.write_bytes(b"12345")
    link = tmp_path / "link.json"
    link.symlink_to(data)
    with pytest.raises(ValueError):
        staging.bounded_read(link, 10)
    with pytest.raises(ValueError):
        staging.bounded_read(data, 4)
    assert staging.bounded_read(data, 5) == b"12345"


def test_duplicate_evidence_keys_are_rejected():
    with pytest.raises(ValueError, match="duplicate evidence key"):
        staging.strict_json(b'{"source_manifest":[],"source_manifest":[]}')


def test_receipt_rejects_rewritten_current_oracle_even_with_reforged_freeze_digest(synthetic_receipt, tmp_path, monkeypatch):
    artifact, _ = synthetic_receipt
    pack = tmp_path / "changed-pack"
    shutil.copytree(staging.PACK, pack)
    monkeypatch.setattr(staging, "PACK", pack)
    oracle = json.loads((pack / "oracle.json").read_bytes())
    oracle["cases"][0]["transport"]["output_json"] = "{}"
    raw = json.dumps(oracle).encode()
    (pack / "oracle.json").write_bytes(raw)
    binding = json.loads((pack / "oracle.freeze.json").read_bytes())
    binding["oracle_sha256"] = digest(raw)
    (pack / "oracle.freeze.json").write_text(json.dumps(binding))
    with pytest.raises(ValueError, match="complete reviewed source plan"):
        validate_evidence(artifact, pack / "oracle.json")


def test_receipt_rejects_stale_adapter_against_current_reviewed_file(synthetic_receipt, tmp_path, monkeypatch):
    artifact, _ = synthetic_receipt
    pack = tmp_path / "changed-pack"
    shutil.copytree(staging.PACK, pack)
    monkeypatch.setattr(staging, "PACK", pack)
    path = pack / "python/app.py"
    path.write_bytes(path.read_bytes() + b"\n# changed source\n")
    with pytest.raises(ValueError, match="complete reviewed source plan"):
        validate_evidence(artifact, pack / "oracle.json")


def test_clean_checkout_without_ignored_wit_dependencies_stages_canonical_core(tmp_path, monkeypatch):
    pack = tmp_path / "clean-checkout-pack"
    shutil.copytree(staging.PACK, pack, ignore=shutil.ignore_patterns("deps", "__pycache__"))
    assert not (pack / "wit/deps/core.wit").exists()
    monkeypatch.setattr(staging, "PACK", pack)
    output = tmp_path / "stage"
    plan = staging.stage(output)
    canonical = ROOT / "crates/tobkiri-wit/wit/core/core.wit"
    assert (output / "wit/deps/core.wit").read_bytes() == canonical.read_bytes()
    entry = next(item for item in plan["sources"] if item["destination"] == "wit/deps/core.wit")
    assert entry["sha256"] == digest(canonical.read_bytes())


def test_extracted_original_has_no_generated_blank_lines_at_eof():
    original = (staging.PACK / "python/original_bridge.py").read_bytes()
    assert original.endswith(b"\n")
    assert not original.endswith(b"\n\n")
