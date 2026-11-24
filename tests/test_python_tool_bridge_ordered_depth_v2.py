"""Pure-Python v2 depth/resource tests; no component or native Core execution."""

import json
from pathlib import Path
import platform
import sys
from typing import Any
import unicodedata

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs/local.ai-tool-bridge-ordered-python"
sys.path[:0] = [str(PACK), str(PACK / "python")]
import caller_adapter_v2 as adapter
import ordered_json as v1
import ordered_json_v2 as codec
from original_bridge import create_tool_intent_operation


def nested(value: Any, levels: int) -> Any:
    """Build a nested list without recursively constructing its children."""
    for _ in range(levels):
        value = [value]
    return value


def leaf(value: Any, levels: int) -> Any:
    """Inspect a deep chain without recursively comparing entire trees."""
    for _ in range(levels):
        assert type(value) is list and len(value) == 1
        value = value[0]
    return value


@pytest.mark.parametrize("operation", adapter.OPERATIONS)
def test_frozen_depth_case_preserves_actual_original_bytes(operation: str) -> None:
    oracle = json.loads((PACK / "oracle.json").read_bytes())
    case = next(
        item for item in oracle["cases"]
        if item["operation"] == operation and "inner-depth-excluded" in item["id"]
    )
    payload = v1.loads(case["input_json"])
    output = create_tool_intent_operation(None)("normalize", payload)
    assert {"status": "ok", "repr": repr(output)} == case["original"]
    expected = json.dumps(
        output, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    actual = adapter.invoke(operation, case["input_json"])
    assert actual == expected
    decoded = adapter.response(actual)
    intent = decoded["intents"][0]
    assert leaf(intent["arguments"]["n"], 65) == 0
    assert intent["authority_granted"] is False
    assert intent["approved"] is False
    assert intent["executes"] is False
    assert intent["approval_status"] == "unrequested"


@pytest.mark.parametrize("levels", [256, 1024])
@pytest.mark.parametrize("value", [None, [], 2**64, -(10**4300 - 1)])
def test_encoder_and_validation_are_iterative(levels: int, value: Any) -> None:
    tree = nested(value, levels)
    token = codec.dumps(value)
    expected = "[" * levels + token + "]" * levels
    assert codec.dumps(tree) == expected
    budget = codec._Budget()
    assert "".join(codec._iterencode(tree, budget, 0)) == expected
    assert budget.nodes == levels + 1
    assert budget.active == set()


@pytest.mark.skipif(
    platform.python_version() != "3.13.5" or unicodedata.unidata_version != "15.1.0",
    reason="broad native-decoder depth probe requires the exact reference runtime",
)
@pytest.mark.parametrize("levels", [256, 1024])
@pytest.mark.parametrize("as_inner_json", [False, True])
def test_reference_runtime_handles_broad_nonfixture_depths(
    levels: int, as_inner_json: bool,
) -> None:
    arguments: Any = {"n": nested(2**64, levels)}
    if as_inner_json:
        arguments = codec.dumps(arguments)
    payload = {"intents": [{"name": "echo", "arguments": arguments}]}
    output = create_tool_intent_operation(None)("normalize", payload)
    actual = adapter.invoke(adapter.OPERATIONS[0], adapter.request(payload))
    assert actual == json.dumps(
        output, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    decoded = adapter.response(actual)
    value = leaf(decoded["intents"][0]["arguments"]["n"], levels)
    assert type(value) is int and value == 2**64


def test_recursion_and_integer_settings_are_not_changed(monkeypatch: Any) -> None:
    before = sys.getrecursionlimit(), sys.get_int_max_str_digits()

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the codec must not change interpreter safety settings")

    monkeypatch.setattr(sys, "setrecursionlimit", forbidden)
    monkeypatch.setattr(sys, "set_int_max_str_digits", forbidden)
    assert leaf(codec.loads(codec.dumps(nested(2**64, 256))), 256) == 2**64
    assert (sys.getrecursionlimit(), sys.get_int_max_str_digits()) == before


def test_decoder_resource_error_is_not_relabelled_invalid(monkeypatch: Any) -> None:
    failure = RecursionError("native decoder resource boundary")

    def exhausted(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(codec.json, "loads", exhausted)
    with pytest.raises(RecursionError) as caught:
        codec.loads("[]")
    assert caught.value is failure


def test_original_inner_resource_error_survives_source_adapter(
    monkeypatch: Any,
) -> None:
    failure = RecursionError("original inner decoder resource boundary")
    original_loads = codec.json.loads
    inner = '{"n":[]}'
    request = adapter.request({"intents": [{"name": "echo", "arguments": inner}]})

    def exhausted_inner(text: str, *args: Any, **kwargs: Any) -> Any:
        if text == inner:
            raise failure
        return original_loads(text, *args, **kwargs)

    monkeypatch.setattr(codec.json, "loads", exhausted_inner)
    with pytest.raises(RecursionError) as caught:
        adapter.invoke(adapter.OPERATIONS[0], request)
    assert caught.value is failure


@pytest.mark.parametrize("terminal", [0, []])
def test_node_budget_bounds_path_length_without_another_depth_limit(
    terminal: Any, monkeypatch: Any,
) -> None:
    monkeypatch.setattr(codec, "MAX_NODES", 257)
    allowed = nested(terminal, 256)
    encoded = codec.dumps(allowed)
    assert leaf(codec.loads(encoded), 256) == terminal
    for operation, value in (
        (codec.dumps, [allowed]),
        (codec.loads, "[" + encoded + "]"),
    ):
        with pytest.raises(ValueError, match="node count"):
            operation(value)


def test_keys_and_shared_occurrences_consume_the_node_budget(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(codec, "MAX_NODES", 7)
    allowed = {"outer": {"a": 1, "b": 2}}
    assert codec.loads(codec.dumps(allowed)) == allowed
    with pytest.raises(ValueError, match="node count"):
        codec.dumps({"outer": {"a": 1, "b": 2, "c": 3}})
    shared = {"n": 2**64}
    assert codec.loads(codec.dumps([shared, shared])) == [shared, shared]
    with pytest.raises(ValueError, match="node count"):
        codec.dumps([shared, shared, shared])


def test_exact_full_node_budget_is_retained() -> None:
    encoded = "[" + ",".join(["0"] * (codec.MAX_NODES - 1)) + "]"
    assert codec.dumps(codec.loads(encoded)) == encoded
    with pytest.raises(ValueError, match="node count"):
        codec.loads(encoded[:-1] + ",0]")
    with pytest.raises(ValueError, match="node count"):
        codec.dumps([0] * codec.MAX_NODES)


def test_preflight_rejects_node_and_byte_overages_before_parser(
    monkeypatch: Any,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("an over-budget document reached the native parser")

    monkeypatch.setattr(codec.json, "loads", forbidden)
    monkeypatch.setattr(codec, "MAX_NODES", 257)
    with pytest.raises(ValueError, match="node count"):
        codec.loads("[" * 258 + "]" * 258)
    with pytest.raises(ValueError, match="4 MiB"):
        codec.loads(" " * (codec.MAX_BYTES + 1))


def test_cycle_failure_cleans_active_frames_and_shared_children_remain_valid() -> None:
    cycle: list[Any] = []
    cycle.append(cycle)
    budget = codec._Budget()
    with pytest.raises(ValueError, match="cycles"):
        list(codec._iterencode(nested(cycle, 256), budget, 0))
    assert budget.active == set()
    shared = nested({"n": 2**64}, 256)
    result = codec.loads(codec.dumps([shared, shared]))
    assert leaf(result[0], 256) == leaf(result[1], 256) == {"n": 2**64}


@pytest.mark.parametrize("text", [
    "", "[}", "{]", "[1,]", '{"a":}', '"\\x"', "{}{}", "01",
    '{"a":1,"\\u0061":2}', '"\\ud800"', "NaN", "1e309",
])
def test_iterative_preflight_does_not_replace_json_grammar(text: str) -> None:
    with pytest.raises(ValueError):
        codec.loads(text)


@pytest.mark.parametrize("padding", range(-3, 4))
def test_opaque_strings_and_chunk_crossing_escapes_remain_opaque(padding: int) -> None:
    value = (
        "x" * (v1.STRING_CHUNK_SIZE + padding)
        + '\\\\"😀[]{}'
        + "[" * (codec.MAX_NODES + 1)
    )
    assert codec.loads(codec.dumps(value)) == value
    assert codec.loads(json.dumps(value, ensure_ascii=True)) == value


def test_exact_utf8_and_outer_quoting_budgets_remain_separate() -> None:
    value = "x" * (codec.MAX_BYTES - 2)
    encoded = codec.dumps(value)
    assert len(encoded.encode()) == codec.MAX_BYTES
    assert codec.loads(encoded) == value
    assert len(json.dumps(encoded).encode()) > codec.MAX_BYTES
    with pytest.raises(ValueError, match="4 MiB"):
        codec.dumps(value + "x")
    with pytest.raises(ValueError, match="4 MiB"):
        codec.loads('"' + value + 'x"')
    with pytest.raises(ValueError, match="4 MiB"):
        codec.dumps("😀" * (codec.MAX_BYTES // 4))


def test_decode_byte_budget_counts_input_not_normalized_spelling(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(codec, "MAX_BYTES", 4)
    assert codec.loads("1e-4") == 0.0001
    with pytest.raises(ValueError, match="4 MiB"):
        codec.dumps(0.0001)


def test_v1_depth_behavior_is_not_modified() -> None:
    value = nested(0, 65)
    assert codec.loads(codec.dumps(value))
    assert v1.MAX_DEPTH == 64
    for operation, payload in (
        (v1.dumps, value), (v1.loads, "[" * 65 + "0" + "]" * 65),
    ):
        with pytest.raises(ValueError, match="depth"):
            operation(payload)
