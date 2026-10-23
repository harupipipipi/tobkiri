"""Contract tests independent of any claimed Wasm/interpreter evidence."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs/local.ai-tool-bridge-ordered-python"
sys.path[:0] = [str(PACK), str(PACK / "python"), str(ROOT / "scripts/wasm")]


def test_exact_ordered_result_roundtrip():
    from caller_adapter import OPERATIONS, invoke, request, response
    from ordered_json import dumps
    payload = {"request_id": " req ", "intents": [
        {"name": "echo", "arguments": {"z": 1.0, "a": -0.0}}]}
    for operation in OPERATIONS:
        encoded = invoke(operation, request(payload))
        result = response(encoded)
        assert '"z":1.0,"a":-0.0' in dumps(result)
        assert result["intents"][0]["authority_granted"] is False
        assert result["intents"][0]["approved"] is False
        assert result["intents"][0]["executes"] is False


def test_original_error_is_not_success_envelope():
    from caller_adapter import OPERATIONS, invoke, request
    from original_bridge import GlobalContractInvocationError
    with pytest.raises(GlobalContractInvocationError) as caught:
        invoke(OPERATIONS[0], request({"intents": [1]}))
    assert caught.value.code == "invalid_response"
    assert str(caught.value) == "tool intent must be an object"


def test_inner_json_duplicates_preserve_original_semantics():
    from caller_adapter import OPERATIONS, invoke, request, response
    result = response(invoke(OPERATIONS[0], request({"intents": [
        {"name": "echo", "arguments": '{"a":1,"a":2}'}]})))
    assert result["intents"][0]["arguments"] == {"a": 2}


@pytest.mark.parametrize("arguments", ['{"n":NaN}', '{"n":9007199254740992}'])
def test_original_output_outside_domain_is_excluded(arguments):
    from caller_adapter import OPERATIONS, invoke, request
    with pytest.raises(ValueError):
        invoke(OPERATIONS[0], request({"intents": [
            {"name": "echo", "arguments": arguments}]}))


def test_canonical_core_is_unchanged(tmp_path):
    from stage_ordered_tool_bridge import stage
    output = tmp_path / "stage"
    stage(output)
    assert (output / "wit/deps/core.wit").read_bytes() == (
        ROOT / "crates/tobkiri-wit/wit/core/core.wit").read_bytes()


def test_stage_hashes_all_inputs_and_rejects_existing_directory(tmp_path):
    import hashlib
    from stage_ordered_tool_bridge import stage
    output = tmp_path / "stage"
    plan = stage(output)
    for entry in plan["sources"]:
        assert hashlib.sha256((output / entry["source"]).read_bytes()).hexdigest() == entry["sha256"]
    with pytest.raises(ValueError):
        stage(output)


def test_checked_in_oracle_preserves_original_unicode_expectation():
    import hashlib
    import json
    oracle = json.loads((PACK / "oracle.json").read_text())
    assert oracle["python"] == "3.13.5"
    assert oracle["unicode"] == "15.1.0"
    assert oracle["source_provenance_sha256"] == hashlib.sha256(
        (PACK / "python/provenance.json").read_bytes()).hexdigest()
    assert oracle["freeze_script_sha256"] == hashlib.sha256(
        (ROOT / "scripts/wasm/freeze_ordered_tool_bridge.py").read_bytes()).hexdigest()
    cases = oracle["cases"]
    assert len(cases) == 33
    assert sum(case["transport"]["status"] == "excluded" for case in cases) == 9
    unicode_case = next(case for case in cases if case["id"] == "base:unicode-repr-15-1")
    assert "\\\\u1c89" in unicode_case["transport"]["output_json"]


def test_staged_plan_is_accepted_by_existing_sdk(tmp_path):
    from stage_ordered_tool_bridge import stage
    sys.path.insert(0, str(ROOT / "sdk/python"))
    from build_component import _verify_plan
    output = tmp_path / "stage"
    stage(output)
    plan, _, _, captures = _verify_plan(output / "plan.json")
    assert len(captures) == 9
    assert "local:ai-tool-bridge-ordered-python/bridge@1.0.0" in plan["outer_exports"]
