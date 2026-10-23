"""Source-only nonfinite failure semantics; no guest or native Core execution."""

import ast
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs/local.ai-tool-bridge-ordered-python"
sys.path[:0] = [str(PACK), str(PACK / "python")]
import caller_adapter as v1_adapter
import caller_adapter_v2 as adapter
import ordered_json_v2 as codec
from original_bridge import GlobalContractInvocationError, create_tool_intent_operation


def _payload(token: str) -> dict[str, object]:
    return {"intents": [{"name": "echo", "arguments": '{"n":' + token + "}"}]}


def _original_audit_digest(value: object) -> str:
    """Execute only the exact original digest function, not the Host path."""
    path = ROOT / "tobkiri_runtime/tobkiri_host/broker.py"
    parsed = ast.parse(path.read_text())
    function = next(
        item for item in parsed.body
        if isinstance(item, ast.FunctionDef) and item.name == "_digest"
    )
    namespace = {"json": json, "hashlib": hashlib}
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_digest"](value)


@pytest.mark.parametrize("operation", adapter.OPERATIONS)
@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
def test_nonfinite_original_output_is_terminal_source_provider_failure(
    operation: str, token: str,
) -> None:
    payload = _payload(token)
    original = create_tool_intent_operation(None)("normalize", payload)
    assert not math.isfinite(original["intents"][0]["arguments"]["n"])
    with pytest.raises(ValueError, match="not JSON compliant"):
        _original_audit_digest(original)
    with pytest.raises(adapter.NonFiniteProviderOutputError) as caught:
        adapter.invoke(operation, adapter.request(payload))
    error = caught.value
    assert type(error) is adapter.NonFiniteProviderOutputError
    assert error.code == "provider_failed"
    assert error.retryable is False
    assert str(error) == "provider output contains a non-finite number"
    assert error.__cause__ is None
    assert not isinstance(error, (ValueError, GlobalContractInvocationError))
    # V1's frozen source behavior is deliberately not retroactively relabeled.
    with pytest.raises(ValueError, match="must be finite"):
        v1_adapter.invoke(operation, v1_adapter.request(payload))


@pytest.mark.parametrize("operation", adapter.OPERATIONS)
def test_nonfinite_failure_never_reflects_payload_or_normalizes_twice(
    operation: str, monkeypatch: Any,
) -> None:
    original_factory = adapter.create_tool_intent_operation
    calls = 0

    def factory(client: object) -> Any:
        original = original_factory(client)

        def normalize(name: str, payload: object) -> object:
            nonlocal calls
            calls += 1
            return original(name, payload)

        return normalize

    monkeypatch.setattr(adapter, "create_tool_intent_operation", factory)
    payload = _payload("NaN")
    payload["request_id"] = "private-request-marker"
    payload["intents"][0]["arguments"] = (
        '{"private-argument-marker":"private-value-marker","n":NaN}'
    )
    with pytest.raises(adapter.NonFiniteProviderOutputError) as caught:
        adapter.invoke(operation, adapter.request(payload))
    assert calls == 1
    assert "private-" not in str(caught.value)
    assert "private-" not in repr(caught.value)
    assert caught.value.args == ("provider output contains a non-finite number",)


@pytest.mark.parametrize("operation", adapter.OPERATIONS)
def test_finite_values_and_nonfinite_looking_strings_are_unchanged(
    operation: str,
) -> None:
    payload = _payload(
        '{"z":-0.0,"a":1.0,"big":9007199254740992,'
        '"tiny":5e-324,"text":"NaN Infinity -Infinity 1e400"}'
    )
    original = create_tool_intent_operation(None)("normalize", payload)
    actual = adapter.invoke(operation, adapter.request(payload))
    assert actual == json.dumps(
        original, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    values = adapter.response(actual)["intents"][0]["arguments"]["n"]
    assert list(values) == ["z", "a", "big", "tiny", "text"]
    assert struct.pack(">d", values["z"]) == struct.pack(">d", -0.0)
    assert type(values["a"]) is float
    assert type(values["big"]) is int
    assert values["big"] == 9007199254740992
    assert values["tiny"] == 5e-324


@pytest.mark.parametrize("operation", adapter.OPERATIONS)
def test_original_duplicate_key_resolution_precedes_output_classification(
    operation: str,
) -> None:
    payload = {"intents": [{
        "name": "echo", "arguments": '{"n":NaN,"n":1}',
    }]}
    actual = adapter.response(adapter.invoke(operation, adapter.request(payload)))
    assert actual["intents"][0]["arguments"] == {"n": 1}
    payload["intents"][0]["arguments"] = '{"n":1,"n":NaN}'
    with pytest.raises(adapter.NonFiniteProviderOutputError):
        adapter.invoke(operation, adapter.request(payload))


@pytest.mark.parametrize("operation", adapter.OPERATIONS)
@pytest.mark.parametrize("payload", [
    {"intents": [1]},
    {"intents": [{"name": "a b"}]},
    {"intents": [{"name": "echo", "arguments": "[1]"}]},
    {"intents": [{"name": "echo", "arguments": "{"}]},
])
def test_original_normalization_errors_retain_class_code_and_message(
    operation: str, payload: dict[str, object],
) -> None:
    with pytest.raises(GlobalContractInvocationError) as original:
        create_tool_intent_operation(None)("normalize", payload)
    with pytest.raises(GlobalContractInvocationError) as actual:
        adapter.invoke(operation, adapter.request(payload))
    assert type(actual.value) is type(original.value)
    assert (actual.value.code, str(actual.value)) == (
        original.value.code, str(original.value)
    )


@pytest.mark.parametrize("payload", [
    '{"intents":[],"n":NaN}',
    '{"intents":[],"n":Infinity}',
    '{"intents":[],"n":-1e400}',
    '{"intents":[],"intents":[]}',
    '[]',
    '{',
])
def test_invalid_outer_requests_do_not_become_provider_output_failures(
    payload: str, monkeypatch: Any,
) -> None:
    def forbidden(_client: object) -> object:
        pytest.fail("invalid outer request must not enter the original normalizer")

    monkeypatch.setattr(adapter, "create_tool_intent_operation", forbidden)
    with pytest.raises(ValueError):
        adapter.invoke(adapter.OPERATIONS[0], payload)


def _invoke_output(value: object, monkeypatch: Any) -> str:
    """Inject a source-only output to isolate encoder error classification."""
    monkeypatch.setattr(
        adapter, "create_tool_intent_operation",
        lambda _client: lambda _name, _payload: value,
    )
    return adapter.invoke(adapter.OPERATIONS[0], "{}")


def test_actual_encoder_node_budget_precedes_later_nonfinite_output(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(codec, "MAX_NODES", 3)
    with pytest.raises(adapter.NonFiniteProviderOutputError):
        _invoke_output([float("nan"), 0], monkeypatch)
    with pytest.raises(codec.OrderedJSONError, match="node count"):
        _invoke_output([0, [float("nan")]], monkeypatch)


def test_actual_encoder_byte_budget_precedes_later_nonfinite_output(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(codec, "MAX_BYTES", 16)
    with pytest.raises(adapter.NonFiniteProviderOutputError):
        _invoke_output([float("nan"), "x" * 32], monkeypatch)
    with pytest.raises(codec.OrderedJSONError, match="4 MiB"):
        _invoke_output(["x" * 32, float("nan")], monkeypatch)


@pytest.mark.parametrize("operation", adapter.OPERATIONS)
def test_deep_original_nonfinite_output_never_becomes_data(operation: str) -> None:
    payload = _payload("[" * 70 + "NaN" + "]" * 70)
    with pytest.raises(adapter.NonFiniteProviderOutputError):
        adapter.invoke(operation, adapter.request(payload))


def test_cycles_reject_and_shared_children_succeed_in_the_actual_encoder(
    monkeypatch: Any,
) -> None:
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(codec.OrderedJSONError, match="cycles"):
        _invoke_output(cycle, monkeypatch)
    child = {"n": 0}
    assert _invoke_output([child, child], monkeypatch) == '[{"n":0},{"n":0}]'


def test_other_output_constraints_remain_codec_errors(monkeypatch: Any) -> None:
    for value in ["\ud800", (1,), {1: 0}, 10**4300]:
        with pytest.raises(codec.OrderedJSONError):
            _invoke_output(value, monkeypatch)
