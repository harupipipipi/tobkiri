"""Additive source-profile tests; no component build or guest execution."""

import hashlib
import json
from pathlib import Path
import platform
import struct
import sys
from typing import Any
import unicodedata

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs/local.ai-tool-bridge-ordered-python"
sys.path[:0] = [str(PACK), str(PACK / "python"), str(ROOT / "scripts/wasm")]
import caller_adapter as v1_adapter
import caller_adapter_v2 as adapter
import ordered_json as v1
import ordered_json_v2 as codec
from original_bridge import GlobalContractInvocationError

OBSERVATIONS = PACK / "integer-v2.observations.json"


@pytest.mark.parametrize("value", [
    0, 1, -1, 2**53 - 1, 2**53, -(2**53), 2**64, -(2**64),
    10**100, -(10**100), 10**4300 - 1, -(10**4300 - 1),
], ids=[
    "zero", "one", "negative-one", "safe-limit", "beyond-safe", "negative-beyond-safe",
    "uint64", "negative-uint64", "positive-101", "negative-101",
    "positive-4300", "negative-4300",
])
def test_bounded_integers_preserve_exact_native_identity(value: int) -> None:
    result = codec.loads(codec.dumps({"n": value}))
    assert type(result["n"]) is int
    assert result["n"] == value


@pytest.mark.parametrize(
    "value", [10**4300, -(10**4300), 10**10000],
    ids=["positive-4301", "negative-4301", "positive-10001"],
)
def test_oversized_native_integers_rejected_before_str(
    value: int, monkeypatch: Any,
) -> None:
    def forbidden(_value: object) -> str:
        pytest.fail("oversized integer must not reach decimal conversion")

    monkeypatch.setattr(codec, "str", forbidden, raising=False)
    with pytest.raises(ValueError, match="4300 decimal digits"):
        codec._integer_token(value)


@pytest.mark.parametrize("sign", ["", "-"])
def test_oversized_integer_tokens_rejected_before_int(
    sign: str, monkeypatch: Any,
) -> None:
    def forbidden(_value: object) -> int:
        pytest.fail("oversized integer token must not reach int conversion")

    monkeypatch.setattr(codec, "int", forbidden, raising=False)
    with pytest.raises(ValueError, match="4300 decimal digits"):
        codec._parse_integer(sign + "9" * 4301)


def test_mixed_scalar_identity_order_and_inner_json_are_unchanged() -> None:
    value = {
        "z": [True, False, 1, 1.0, -0.0, 5e-324, 2**64],
        "e\u0301": {"b": "é😀", "a": '{"dup":1,"dup":2}'},
    }
    actual = codec.loads(codec.dumps(value))
    assert list(actual) == list(value)
    assert list(actual["e\u0301"]) == ["b", "a"]
    for index, item in enumerate(value["z"]):
        assert type(actual["z"][index]) is type(item)
    assert struct.pack(">d", actual["z"][4]) == struct.pack(">d", -0.0)
    assert actual == value


def test_integer_digit_bound_does_not_restrict_finite_float_tokens() -> None:
    assert codec.loads("9" * 4301 + "e-4300") == 10.0
    assert type(codec.loads("9007199254740992.0")) is float
    assert type(codec.loads("9007199254740992")) is int


@pytest.mark.parametrize("text", [
    "NaN", "Infinity", "-Infinity", "1e309", "01", "-01", "+1", "1_0",
    "--1", "1.", ".1", "1e", "[1,]", "{}{}", '{"a":1,"a":2}', '"\\ud800"',
])
def test_nonfinite_malformed_duplicate_and_surrogate_inputs_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        codec.loads(text)


@pytest.mark.parametrize("value", [
    float("nan"), float("inf"), -float("inf"), "\ud800", {1: "x"}, (1,), b"1",
])
def test_unchanged_non_json_output_exclusions(value: object) -> None:
    with pytest.raises(ValueError):
        codec.dumps(value)


@pytest.mark.parametrize("base", [int, float, str, list, dict])
def test_exact_type_rule_is_not_weakened(base: type) -> None:
    subclass = type("Subclass", (base,), {})
    with pytest.raises(ValueError):
        codec.dumps(subclass())


def test_depth_is_iterative_but_nodes_cycles_and_shared_children_are_bounded() -> None:
    nested: object = 2**64
    for _ in range(256):
        nested = [nested]
    restored = codec.loads(codec.dumps(nested))
    for _ in range(256):
        assert type(restored) is list and len(restored) == 1
        restored = restored[0]
    assert restored == 2**64
    for operation, value in (
        (codec.dumps, [0] * codec.MAX_NODES),
        (codec.loads, "[" + ",".join(["0"] * codec.MAX_NODES) + "]"),
    ):
        with pytest.raises(ValueError):
            operation(value)
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="cycles"):
        codec.dumps(cycle)
    child = {"n": 2**64}
    assert codec.loads(codec.dumps([child, child])) == [child, child]


def test_object_keys_and_nested_containers_count_toward_node_budget(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(codec, "MAX_NODES", 5)
    assert codec.loads(codec.dumps({"a": 2**64, "b": 0})) == {"a": 2**64, "b": 0}
    with pytest.raises(ValueError, match="node count"):
        codec.dumps({"a": 2**64, "b": 0, "c": 0})
    with pytest.raises(ValueError, match="node count"):
        codec.loads('{"a":9007199254740992,"b":0,"c":0}')
    assert codec.dumps([[[[0]]]]) == "[[[[0]]]]"
    with pytest.raises(ValueError, match="node count"):
        codec.dumps([[[[[0]]]]])
    with pytest.raises(ValueError, match="node count"):
        codec.loads("[[[[[0]]]]]")


def test_utf8_budget_is_unchanged_and_outer_quoting_is_separate() -> None:
    value = "x" * (codec.MAX_BYTES - 2)
    assert len(codec.dumps(value).encode()) == codec.MAX_BYTES
    assert codec.loads('"' + value + '"') == value
    with pytest.raises(ValueError, match="4 MiB"):
        codec.dumps(value + "x")
    with pytest.raises(ValueError, match="4 MiB"):
        codec.loads('"' + value + 'x"')
    with pytest.raises(ValueError, match="4 MiB"):
        codec.dumps("😀" * (codec.MAX_BYTES // 4))
    from materialize_ordered_tool_bridge import envelope_bounds
    inner = codec.dumps('"' * (codec.MAX_BYTES // 3))
    assert len(inner.encode()) < codec.MAX_BYTES
    with pytest.raises(ValueError, match="outer-envelope"):
        envelope_bounds({"cases": [{
            "operation": adapter.OPERATIONS[0], "input_json": inner,
            "transport": {"status": "invalid"},
        }]})


@pytest.mark.parametrize("operation", adapter.OPERATIONS)
@pytest.mark.parametrize("as_inner_json", [True, False])
def test_all_operations_preserve_bigint_results_and_no_authority(
    operation: str, as_inner_json: bool,
) -> None:
    arguments: object = {"z": 2**64, "negative": -(2**64), "f": -0.0}
    if as_inner_json:
        arguments = (
            '{"z":18446744073709551616,"negative":-18446744073709551616,"f":-0.0}'
        )
    result = adapter.response(adapter.invoke(operation, adapter.request({
        "intents": [{"name": "echo", "arguments": arguments}],
    })))
    intent = result["intents"][0]
    assert intent["arguments"]["z"] == 2**64
    assert type(intent["arguments"]["z"]) is int
    assert intent["arguments"]["negative"] == -(2**64)
    assert list(intent["arguments"]) == ["z", "negative", "f"]
    assert intent["authority_granted"] is False
    assert intent["approved"] is False
    assert intent["executes"] is False
    assert intent["approval_status"] == "unrequested"


def test_inner_duplicates_and_original_error_boundary_are_preserved() -> None:
    payload = adapter.request({"intents": [{
        "name": "echo", "arguments": '{"n":1,"n":9007199254740992}',
    }]})
    result = adapter.response(adapter.invoke(adapter.OPERATIONS[0], payload))
    assert result["intents"][0]["arguments"]["n"] == 2**53
    payload = adapter.request({"intents": [{
        "name": "echo", "arguments": '{"n":' + "9" * 4301 + "}",
    }]})
    with pytest.raises(ValueError) as caught:
        adapter.invoke(adapter.OPERATIONS[0], payload)
    assert type(caught.value) is ValueError
    with pytest.raises(GlobalContractInvocationError) as caught:
        adapter.invoke(adapter.OPERATIONS[0], adapter.request({"intents": [1]}))
    assert caught.value.code == "invalid_response"
    assert str(caught.value) == "tool intent must be an object"


def test_adapter_fails_closed_on_unreviewed_integer_limit(monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "get_int_max_str_digits", lambda: 640)
    with pytest.raises(ValueError, match="unchanged 4300-digit"):
        adapter.invoke(adapter.OPERATIONS[0], "{}")


def test_v1_behavior_is_unchanged_and_fresh_fixture_binds_current_source_plan(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="safe integer"):
        v1.dumps(2**53)
    with pytest.raises(ValueError, match="safe integer"):
        v1_adapter.invoke(v1_adapter.OPERATIONS[0], v1_adapter.request({
            "intents": [{"name": "echo", "arguments": '{"n":9007199254740992}'}],
        }))
    from stage_ordered_tool_bridge import stage
    output = tmp_path / "stage"
    plan = stage(output)
    fixture = ROOT / "crates/tobkiri-e2e/fixtures/ordered-python"
    receipt_bytes = (fixture / "component.build.json").read_bytes()
    assert hashlib.sha256(receipt_bytes).hexdigest() == (
        "496d94b8a3ed9b53e97a58c8f58a17f42ea3d5d1837bb1466b039a82ae6d867a")
    assert hashlib.sha256((fixture / "component.wasm.xz").read_bytes()).hexdigest() == (
        "c34ba42e584ab8b6e2fdbfc94e102cef385bc4edc13083cd0d1fe54f7823ce04")
    receipt = json.loads(receipt_bytes)
    assert receipt["artifact_sha256"] == (
        "9beee26b1ad8229c04571eca1f22d47f9d23e1a8475324919f8c1fc4c7ea1e94")
    manifest = [{**item, "bytes": len((output / item["source"]).read_bytes())}
                for item in plan["sources"]]
    assert manifest == receipt["source_manifest"]
    assert hashlib.sha256((output / "plan.json").read_bytes()).hexdigest() == (
        receipt["build_plan_sha256"])
    assert not any("v2" in entry["source"] for entry in manifest)
    assert hashlib.sha256((PACK / "oracle.json").read_bytes()).hexdigest() == (
        "9aae37387a6b0758451abe3cb6070d8a3d8106b744de053a5bc16e8715f3481d")


def test_supplemental_observations_bind_sources_without_claiming_guest_parity() -> None:
    report = json.loads(OBSERVATIONS.read_bytes())
    from observe_ordered_integer_v2 import canonical, digest
    for item in report["source_bindings"]:
        data = (ROOT / item["path"]).read_bytes()
        assert len(data) == item["bytes"]
        assert digest(data) == item["sha256"]
    assert digest(canonical(report["source_bindings"])) == (
        report["source_bindings_sha256"])
    assert {item["path"] for item in report["source_bindings"]} == {
        "packs/local.ai-tool-bridge-ordered-python/oracle.json",
        "packs/local.ai-tool-bridge-ordered-python/oracle.freeze.json",
        "packs/local.ai-tool-bridge-ordered-python/ordered_json.py",
        "packs/local.ai-tool-bridge-ordered-python/ordered_json_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/python/caller_adapter.py",
        "packs/local.ai-tool-bridge-ordered-python/python/caller_adapter_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/python/original_bridge.py",
        "packs/local.ai-tool-bridge-ordered-python/python/provenance.json",
        "packs/local.ai-tool-bridge-ordered-python/python/inventory.json",
        "scripts/wasm/freeze_ordered_tool_bridge.py",
        "scripts/wasm/prepare_ordered_tool_bridge.py",
        "scripts/wasm/stage_ordered_tool_bridge.py",
        "scripts/wasm/observe_ordered_integer_v2.py",
        "crates/tobkiri-wit/wit/core/core.wit",
        "crates/tobkiri-canon/src/codec.rs",
        "tobkiri_runtime/tobkiri_host/broker.py",
        "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/executables.v4.json",
        "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/runtime/bridge.py",
        "tobkiri_runtime/core_runtime/global_contract_dispatch.py",
    }
    assert report["frozen_case_counts"] == {
        "success": 18, "original-invalid": 12, "excluded-not-parity": 3,
    }
    assert report["admitted_source_parity_count"] == 30
    assert report["wasm_execution_performed"] is False
    assert report["native_core_acceptance"] is False
    assert report["full_host_replay_performed"] is False
    assert report["production_enabled"] is False
    assert len(report["cases"]) == 33
    bigint = [case for case in report["cases"] if "inner-bigint" in case["id"]]
    assert len(bigint) == 3
    for case in bigint:
        assert case["v1_classification"] == "excluded"
        assert case["classification"] == "success"
        assert case["output_sha256"] == (
            "c1ef462a0094ddeec95bc5167821c0545f2ca14fc5b1c301c6475565194b80c5")
        assert case["outer_response_bytes"] == 229
    nonfinite = [case for case in report["cases"]
                 if "inner-nonfinite" in case["id"]]
    assert len(nonfinite) == 3
    for case in nonfinite:
        assert case["classification"] == "excluded-not-parity"
        assert case["admitted_source_parity"] is False
        assert case["source_error_type"] == "NonFiniteProviderOutputError"
        assert case["source_provider_code"] == "provider_failed"
        assert case["source_retryable"] is False
        assert case["intended_core_error"] == "internal"
        assert case["legacy_http_error_parity_claimed"] is False
    boundaries = report["integer_boundaries"]
    assert len(boundaries) == 4
    statuses = {(item["digits"], item["negative"], item["status"])
                for item in boundaries}
    assert statuses == {
        (4300, False, "success"), (4300, True, "success"),
        (4301, False, "original-ValueError"), (4301, True, "original-ValueError"),
    }
    for item in boundaries:
        if item["digits"] == 4300:
            assert digest(item["output_json"].encode()) == item["output_sha256"]
        else:
            assert item["candidate_matches_original_error"] is True


@pytest.mark.skipif(
    platform.python_version() != "3.13.5" or unicodedata.unidata_version != "15.1.0",
    reason="exact original interpreter required for source observations",
)
def test_exact_reference_reproduces_all_supplemental_observations() -> None:
    from observe_ordered_integer_v2 import observe
    assert observe() == json.loads(OBSERVATIONS.read_bytes())
