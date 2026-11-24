"""Materialization structure tests; synthetic bytes are not runnable evidence."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import materialize_ordered_tool_bridge as materializer
import stage_ordered_tool_bridge as staging


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def inputs(tmp_path):
    stage = tmp_path / "stage"
    plan = staging.stage(stage)
    artifact = tmp_path / "synthetic.wasm"
    # Component header is a structural fixture, not a functional guest.
    artifact.write_bytes(b"\0asm\x0d\0\x01\0")
    manifest = [{**item, "bytes": len((stage / item["source"]).read_bytes())}
                for item in plan["sources"]]
    receipt = {"format": "tobkiri.component-build/2", "language": "python",
               "artifact_sha256": digest(artifact.read_bytes()),
               "artifact_bytes": artifact.stat().st_size,
               "build_plan_sha256": digest((stage / "plan.json").read_bytes()),
               "source_manifest": manifest,
               "source_manifest_sha256": digest(materializer.canonical(manifest)),
               "world": plan["world"], "imports": sorted(plan["allowed_imports"]),
               "exports": sorted(plan["outer_exports"])}
    path = artifact.with_suffix(".build.json")
    path.write_bytes(materializer.canonical(receipt))
    return artifact, path, {"artifact_sha256": digest(artifact.read_bytes()),
                            "receipt_sha256": digest(path.read_bytes())}


def run(inputs, output):
    artifact, _, pins = inputs
    return materializer.materialize(artifact, staging.PACK / "oracle.json", output,
                                    **pins)


def test_exact_generated_mapping_and_all_file_hashes(inputs, tmp_path):
    output = tmp_path / "pack"
    receipt = run(inputs, output)
    manifest = json.loads((output / "pack.json").read_bytes())
    assert receipt["case_count"] == 33
    assert receipt["core_runtime_acceptance"] is False
    assert receipt["production_enabled"] is False
    assert manifest["shapes"][0]["name"] == materializer.SHAPE
    assert manifest["ports"]["provides"][0]["shape"] == materializer.SHAPE
    assert manifest["permissions"]["ports"] == []
    assert manifest["runtime"]["toolchain"] == "python/build-receipt-pinned"
    assert not (output / "evidence/oracle.json").exists()
    for file in manifest["files"]:
        data = (output / file["path"]).read_bytes()
        assert file["size"] == len(data)
        assert file["sha256"] == "sha256:" + digest(data)
    assert (output / "component.wasm").read_bytes() == inputs[0].read_bytes()


@pytest.mark.parametrize("target", [0, 1])
def test_missing_component_or_receipt_fails_before_publish(inputs, tmp_path, target):
    inputs[target].rename(tmp_path / "retained-input")
    with pytest.raises(ValueError):
        run(inputs, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()


@pytest.mark.parametrize("target", [0, 1])
def test_replaced_artifact_or_receipt_fails(inputs, tmp_path, target):
    inputs[target].write_bytes(inputs[target].read_bytes() + b" ")
    with pytest.raises(ValueError):
        run(inputs, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()


def test_duplicate_evidence_key_rejected(inputs, tmp_path):
    path = inputs[1]
    path.write_bytes(b'{"format":"x",' + path.read_bytes()[1:])
    inputs[2]["receipt_sha256"] = digest(path.read_bytes())
    with pytest.raises(ValueError, match="duplicate"):
        run(inputs, tmp_path / "pack")


def test_duplicate_source_identity_rejected_even_with_rehashed_receipt(inputs, tmp_path):
    path = inputs[1]
    receipt = json.loads(path.read_bytes())
    receipt["source_manifest"].append(receipt["source_manifest"][0])
    receipt["source_manifest_sha256"] = digest(materializer.canonical(receipt["source_manifest"]))
    path.write_bytes(materializer.canonical(receipt))
    inputs[2]["receipt_sha256"] = digest(path.read_bytes())
    with pytest.raises(ValueError):
        run(inputs, tmp_path / "pack")


def test_parent_symlink_rejected(inputs, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    artifact, receipt, pins = inputs
    with pytest.raises(ValueError, match="symlink"):
        run((link / artifact.name, receipt, pins), tmp_path / "pack")


def test_output_symlink_rejected(inputs, tmp_path):
    output = tmp_path / "pack"
    output.symlink_to(tmp_path / "absent", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        run(inputs, output)


def test_existing_output_never_overwritten(inputs, tmp_path):
    output = tmp_path / "pack"
    output.mkdir()
    (output / "keep").write_text("unchanged")
    with pytest.raises(ValueError, match="fresh"):
        run(inputs, output)
    assert (output / "keep").read_text() == "unchanged"


def test_outer_escaping_limit_is_not_inner_string_limit():
    frozen = {"cases": [{"operation": "normalize", "input_json": '"' * (3 * 1024 * 1024),
                         "transport": {"status": "invalid"}}]}
    with pytest.raises(ValueError, match="outer-envelope"):
        materializer.envelope_bounds(frozen)


def test_opaque_ordered_string_preserved_in_budget_measurement():
    value = '{"z":1,"a":2,"argument_json":"{\\"x\\":1,\\"x\\":2}"}'
    frozen = {"cases": [{"operation": "x", "input_json": value,
                         "transport": {"status": "ok", "output_json": value}}]}
    measured = materializer.envelope_bounds(frozen)
    assert measured["max_success_response_bytes"] == len(materializer.canonical(value))
    assert frozen["cases"][0]["input_json"] == value


@pytest.mark.parametrize("key", ["artifact_sha256", "receipt_sha256"])
def test_wrong_explicit_pin_fails(inputs, tmp_path, key):
    inputs[2][key] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        run(inputs, tmp_path / "pack")
    assert not (tmp_path / "pack").exists()


def test_native_member_limit_applies_before_capture(inputs, tmp_path, monkeypatch):
    import replay_ordered_tool_bridge as replay
    original = replay.bounded_read
    observed = []

    def bounded(path, limit):
        if path == inputs[0]:
            observed.append(limit)
            # Model a stat result one byte above the permitted member size.
            # No giant fixture or large allocation is necessary.
            if 128 * 1024 * 1024 + 1 > limit:
                raise ValueError("oversized evidence file")
        return original(path, limit)

    monkeypatch.setattr(replay, "bounded_read", bounded)
    with pytest.raises(ValueError, match="oversized"):
        run(inputs, tmp_path / "pack")
    assert observed == [128 * 1024 * 1024]
    assert not (tmp_path / "pack").exists()


def test_bounded_reader_caps_stream_at_limit_plus_one(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    path = tmp_path / "bounded.wasm"
    path.write_bytes(b"x")
    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.read.return_value = b"x"
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: stream)
    assert staging.bounded_read(path, materializer.MAX_NATIVE_MEMBER) == b"x"
    stream.read.assert_called_once_with(materializer.MAX_NATIVE_MEMBER + 1)
