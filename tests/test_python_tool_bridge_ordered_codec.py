"""Pure ordered finite JSON v1 codec tests; no interpreter build is needed."""

import importlib.util
import json
from pathlib import Path
import struct
from typing import Any

import pytest


_PATH = (
    Path(__file__).resolve().parents[1]
    / "packs/local.ai-tool-bridge-ordered-python/ordered_json.py"
)
_SPEC = importlib.util.spec_from_file_location("ordered_json_codec_test", _PATH)
codec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(codec)


def _nested(value: Any, levels: int) -> Any:
    for _ in range(levels):
        value = [value]
    return value


def test_round_trip_preserves_order_types_unicode_and_opaque_strings() -> None:
    value = {
        "z": [None, True, False, 1, 1.0, -0.0],
        "é😀": {"b": "e\u0301", "a": "é"},
        "a": '{"z":1,"z":2,"unsafe":9007199254740992}',
        "escapes": '\\"\b\f\n\r\t\x00/[]{}',
    }
    text = codec.dumps(value)
    result = codec.loads(text)
    assert result == value
    assert list(result) == list(value)
    assert list(result["é😀"]) == ["b", "a"]
    assert type(result["z"][3]) is int
    assert type(result["z"][4]) is float
    assert struct.pack(">d", result["z"][5]) == struct.pack(">d", -0.0)
    assert "é😀" in text
    assert "e\u0301" in text


@pytest.mark.parametrize(
    "value",
    [0.0, -0.0, 1.0, 1.1, 5e-324, -5e-324, 1.7976931348623157e308],
)
def test_float_binary64_round_trip(value: float) -> None:
    result = codec.loads(codec.dumps(value))
    assert type(result) is float
    assert struct.pack(">d", result) == struct.pack(">d", value)


@pytest.mark.parametrize("value", [-(2**53 - 1), -1, 0, 1, 2**53 - 1])
def test_safe_integer_boundaries(value: int) -> None:
    assert codec.loads(codec.dumps(value)) == value


@pytest.mark.parametrize("value", [2**53, -(2**53), 10**100])
def test_unsafe_integers_are_rejected_both_directions(value: int) -> None:
    with pytest.raises(ValueError, match="safe integer"):
        codec.dumps(value)
    with pytest.raises(ValueError, match="safe integer"):
        codec.loads(str(value))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_floats_are_rejected_on_encode(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        codec.dumps(value)


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "1e309"])
def test_nonfinite_numbers_are_rejected_on_decode(text: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        codec.loads(text)


@pytest.mark.parametrize(
    "text",
    ['{"a":1,"a":2}', '{"outer":{"a":1,"\\u0061":2}}', '{"😀":1,"\\ud83d\\ude00":2}'],
)
def test_duplicate_decoded_object_keys_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        codec.loads(text)


@pytest.mark.parametrize("value", ["\ud800", "\udfff", {"\ud800": 1}])
def test_surrogates_are_rejected_on_encode(value: Any) -> None:
    with pytest.raises(ValueError, match="surrogate"):
        codec.dumps(value)


@pytest.mark.parametrize(
    "text", ['"\\ud800"', '"\\udfff"', '{"\\ud800":1}', '"\ud800"']
)
def test_raw_and_escaped_lone_surrogates_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="surrogate"):
        codec.loads(text)


def test_escaped_surrogate_pair_is_valid_unicode() -> None:
    assert codec.loads('"\\ud83d\\ude00"') == "😀"


@pytest.mark.parametrize("base", [int, float, str, list, dict])
def test_native_type_subclasses_are_not_serialized(base: type) -> None:
    subclass = type("Subclass", (base,), {})
    with pytest.raises(ValueError, match="exact native"):
        codec.dumps(subclass())


@pytest.mark.parametrize("value", [(1, 2), {1, 2}, b"text", object(), {1: "x"}])
def test_non_json_values_are_rejected(value: Any) -> None:
    with pytest.raises(ValueError):
        codec.dumps(value)


def test_key_subclasses_and_input_subclasses_are_rejected() -> None:
    class StringSubclass(str):
        pass

    with pytest.raises(ValueError, match="exact strings"):
        codec.dumps({StringSubclass("x"): 1})
    with pytest.raises(ValueError, match="exact string"):
        codec.loads(StringSubclass("null"))
    with pytest.raises(ValueError, match="exact string"):
        codec.loads(b"null")


def test_cycles_are_rejected_but_repeated_shared_children_are_valid() -> None:
    cycle: list[Any] = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="cycles"):
        codec.dumps(cycle)
    mapping: dict[str, Any] = {}
    mapping["self"] = mapping
    with pytest.raises(ValueError, match="cycles"):
        codec.dumps(mapping)
    child = {"x": [1.0]}
    assert codec.loads(codec.dumps([child, child])) == [child, child]


@pytest.mark.parametrize("leaf", [0, []])
def test_depth_boundary_is_root_zero(leaf: Any) -> None:
    allowed = _nested(leaf, codec.MAX_DEPTH)
    assert codec.loads(codec.dumps(allowed)) == allowed
    rejected = _nested(leaf, codec.MAX_DEPTH + 1)
    with pytest.raises(ValueError, match="depth"):
        codec.dumps(rejected)
    with pytest.raises(ValueError, match="depth"):
        codec.loads(json.dumps(rejected))


def test_dict_keys_count_as_child_depth_nodes() -> None:
    allowed = _nested({"a": 1}, codec.MAX_DEPTH - 1)
    assert codec.loads(codec.dumps(allowed)) == allowed
    with pytest.raises(ValueError, match="depth"):
        codec.dumps(_nested({"a": 1}, codec.MAX_DEPTH))


def test_excessive_depth_is_rejected_before_parser(monkeypatch: Any) -> None:
    def fail_parser(*args: Any, **kwargs: Any) -> None:
        pytest.fail("native parser must not receive excessively deep input")

    monkeypatch.setattr(codec.json, "loads", fail_parser)
    with pytest.raises(ValueError, match="depth"):
        codec.loads("[" * 10000 + "0" + "]" * 10000)


def test_list_node_budget_includes_root_and_exact_boundary() -> None:
    allowed = [None] * (codec.MAX_NODES - 1)
    assert codec.loads(codec.dumps(allowed)) == allowed
    with pytest.raises(ValueError, match="node count"):
        codec.dumps(allowed + [None])
    with pytest.raises(ValueError, match="node count"):
        codec.loads("[" + ",".join(["null"] * codec.MAX_NODES) + "]")


def test_dict_key_nodes_count_toward_budget(monkeypatch: Any) -> None:
    monkeypatch.setattr(codec, "MAX_NODES", 5)
    assert codec.loads(codec.dumps({"a": 1, "b": 2})) == {"a": 1, "b": 2}
    for operation, value in [
        (codec.dumps, {"a": 1, "b": 2, "c": 3}),
        (codec.loads, '{"a":1,"b":2,"c":3}'),
    ]:
        with pytest.raises(ValueError, match="node count"):
            operation(value)


def test_exact_utf8_document_size_limit() -> None:
    allowed = "x" * (codec.MAX_BYTES - 2)
    assert len(codec.dumps(allowed).encode("utf-8")) == codec.MAX_BYTES
    assert codec.loads('"' + allowed + '"') == allowed
    with pytest.raises(ValueError, match="4 MiB"):
        codec.dumps(allowed + "x")
    with pytest.raises(ValueError, match="4 MiB"):
        codec.loads('"' + allowed + 'x"')


def test_multibyte_and_escaping_limits_are_bytes_not_characters() -> None:
    with pytest.raises(ValueError, match="4 MiB"):
        codec.dumps("😀" * (codec.MAX_BYTES // 4))
    with pytest.raises(ValueError, match="4 MiB"):
        codec.loads('"' + "😀" * (codec.MAX_BYTES // 4) + '"')
    with pytest.raises(ValueError, match="4 MiB"):
        codec.dumps("\x00" * (codec.MAX_BYTES // 6 + 1))


@pytest.mark.parametrize("padding", range(-3, 4))
def test_chunk_boundaries_handle_escapes_quotes_and_unicode(padding: int) -> None:
    value = "x" * (codec.STRING_CHUNK_SIZE + padding) + '\\"😀[]{}' * 2000
    assert codec.loads(codec.dumps(value)) == value
    assert codec.loads(json.dumps(value, ensure_ascii=True)) == value


def test_long_opaque_json_like_string_is_not_structurally_counted() -> None:
    value = ('{"x":[[[[[[[[[["\\\\' * 20000) + "😀"
    assert codec.loads(codec.dumps(value)) == value


@pytest.mark.parametrize("text", ["", "01", "[1,]", "{}{}", "[}", '"\\x"'])
def test_malformed_json_is_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        codec.loads(text)
