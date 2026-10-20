"""Explicit source transport fixtures, NOT real Wasm/Core/import proof.

Fixtures install generated-binding value/class stand-ins only for these tests.
Production code uses the required generated typed import without injection.
"""

from dataclasses import dataclass
from enum import Enum
import importlib.util
import math
from pathlib import Path
import platform
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
import unicodedata

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs/local.ai-gateway-tool-caller-python"
BRIDGE_PACK = ROOT / "packs/local.ai-tool-bridge-ordered-python"


class ErrorCode(Enum):
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval-required"
    CANCELLED = "cancelled"
    NOT_FOUND = "not-found"
    INVALID = "invalid"
    LIMIT_EXCEEDED = "limit-exceeded"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


@dataclass
class Error:
    code: ErrorCode
    message: str


class Err(Exception):
    def __init__(self, value: Error) -> None:
        super().__init__(value)
        self.value = value


def _module(name: str, path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source_transport(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Install labelled source fixtures; no native Core or guest is instantiated."""
    world = ModuleType("wit_world")
    exports = ModuleType("wit_world.exports")
    exports.Lifecycle = type("Lifecycle", (), {})
    exports.Caller = type("Caller", (), {})
    imports = ModuleType("wit_world.imports")
    bridge = ModuleType("wit_world.imports.bridge")
    binding_types = ModuleType("wit_world.imports.types")
    component_types = ModuleType("componentize_py_types")
    component_types.Err = Err
    binding_types.Error = Error
    binding_types.ErrorCode = ErrorCode
    world.exports, world.imports = exports, imports
    imports.bridge, imports.types = bridge, binding_types
    for module in (world, exports, imports, bridge, binding_types, component_types):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.syspath_prepend(str(BRIDGE_PACK))
    monkeypatch.syspath_prepend(str(PACK / "python"))
    import ordered_json_v2 as codec
    original = _module("original_gateway_tool_caller",
                       PACK / "python/original_gateway_tool_caller.py", monkeypatch)
    state = SimpleNamespace(calls=[], response='{"intents":[]}', failure=None,
                            responder=None, codec=codec, original=original)

    def normalize(operation: str, payload: str) -> str:
        state.calls.append((operation, payload))
        if state.failure is not None:
            raise state.failure
        if state.responder is not None:
            return state.responder(operation, payload)
        return state.response

    bridge.normalize = normalize
    state.adapter = _module("gateway_tool_caller_adapter",
                            PACK / "python/gateway_tool_caller_adapter.py", monkeypatch)
    state.app = _module("gateway_caller_app_source_fixture",
                        PACK / "python/app.py", monkeypatch)
    return state


@pytest.fixture
def normalizer_source_transport(
    source_transport: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Execute existing bridge source only, explicitly not its real component."""
    monkeypatch.syspath_prepend(str(BRIDGE_PACK / "python"))
    from caller_adapter_v2 import invoke as bridge_source_invoke
    source_transport.responder = bridge_source_invoke
    return source_transport


def test_diagnostic_reports_executing_interpreter_without_import_dispatch(
    source_transport: SimpleNamespace,
) -> None:
    state = source_transport
    assert state.app.Caller().runtime_info() == state.codec.dumps({
        "python": platform.python_version(),
        "unicode": unicodedata.unidata_version,
    })
    assert state.calls == []


@pytest.mark.parametrize("operation,suffix", [
    ("generate-intents", "generate"), ("stream-intents", "stream"),
])
def test_source_helper_imports_once_with_exact_operation_and_data(
    source_transport: SimpleNamespace, operation: str, suffix: str,
) -> None:
    state = source_transport
    arguments = {"z": 1, "float": 1.0, "negative_zero": -0.0,
                 "large": 18446744073709551616, "nested": {"b": 2, "a": 1},
                 "opaque": '{"a":1,"a":2}'}
    response = {"intents": [{"arguments": arguments, "extra": "DATA"}]}
    state.response = state.codec.dumps(response)
    payload = {"request_id": " raw id ", "intents": [
        {"name": "echo", "arguments": arguments, "approved": True},
    ]}
    encoded = state.codec.dumps(payload)
    output = state.adapter.invoke(operation, encoded)
    expected_operation = (
        f"rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.{suffix}"
    )
    assert state.calls == [(expected_operation, encoded)]
    assert output == state.codec.dumps(response["intents"])
    actual = state.codec.loads(output)[0]["arguments"]
    assert list(actual) == list(arguments)
    assert type(actual["z"]) is int and type(actual["float"]) is float
    assert math.copysign(1, actual["negative_zero"]) == -1
    assert actual["opaque"] == arguments["opaque"]


def test_real_normalizer_source_fixture_keeps_authority_data_inert(
    normalizer_source_transport: SimpleNamespace,
) -> None:
    state = normalizer_source_transport
    payload = {"request_id": " req ", "intents": [{
        "name": "echo", "arguments": '{"z":-0.0,"a":1.0,"big":18446744073709551616}',
        "authority_granted": True, "approved": True, "executes": True,
        "approval_status": "approved",
    }]}
    output = state.adapter.invoke("generate-intents", state.codec.dumps(payload))
    result = state.codec.loads(output)[0]
    assert result["authority_granted"] is False
    assert result["approved"] is False and result["executes"] is False
    assert result["approval_status"] == "unrequested"
    assert result["request_id"] == "req"
    assert result["intent_id"] == "req:tool:0"
    assert '"z":-0.0,"a":1.0,"big":18446744073709551616' in output
    assert len(state.calls) == 1


@pytest.mark.parametrize("operation", ["generate-intents", "stream-intents"])
@pytest.mark.parametrize("intents", [
    None, {}, {"name": "not-an-intent-list"}, 0, 17, -2.5, False, True,
    "", "not-an-intent-list",
])
def test_non_list_intents_reach_original_bridge_unchanged(
    normalizer_source_transport: SimpleNamespace, operation: str, intents: Any,
) -> None:
    """Original provider owns fallback; this is source, not guest execution."""
    state = normalizer_source_transport
    payload = {"request_id": " req ", "intents": intents}
    encoded = state.codec.dumps(payload)
    assert state.adapter.invoke(operation, encoded) == "[]"
    suffix = "stream" if operation == "stream-intents" else "generate"
    assert state.calls == [
        (f"rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.{suffix}", encoded),
    ]


@pytest.mark.parametrize("contract,operation", [
    ("other.contract", "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.generate"),
    ("tobkiri.service.ai.tool_intent.normalize.v1", "normalize"),
    ("tobkiri.service.ai.tool_intent.normalize.v1",
     "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize"),
    ("tobkiri.service.ai.tool_intent.normalize.v1", "other.operation"),
    (None, "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.generate"),
])
def test_no_other_contract_or_operation_can_dispatch(
    source_transport: SimpleNamespace, contract: Any, operation: str,
) -> None:
    state = source_transport
    with pytest.raises(state.adapter.CallerInputError):
        state.adapter.ImportedBridgeClient().invoke(contract, operation, {})
    assert state.calls == []


@pytest.mark.parametrize("operation,payload", [
    ("text-generate", '{"request_id":"r","intents":[]}'),
    ("messages-generate", '{"request_id":"r","intents":[]}'),
    ("generate-intents", "{}"),
    ("generate-intents", '{"request_id":4,"intents":[]}'),
    ("generate-intents", '{"request_id":{},"intents":[]}'),
    ("generate-intents", '{"request_id":"r","intents":{},"extra":1}'),
    ("generate-intents", '{"request_id":"r","intents":[],"extra":1}'),
    ("stream-event-intents", '{"request_id":"r","events":[null]}'),
    ("stream-event-intents", '{"request_id":"r","events":null}'),
    ("stream-event-intents", '{"request_id":"r","events":{}}'),
    ("stream-event-intents", '{"request_id":"r","events":"not-events"}'),
    ("generate-intents", '{"request_id":"r","intents":[],"request_id":"s"}'),
    ("generate-intents", '{"request_id":"r","intents":[NaN]}'),
])
def test_invalid_helper_request_cannot_call_import(
    source_transport: SimpleNamespace, operation: str, payload: str,
) -> None:
    state = source_transport
    with pytest.raises(state.adapter.CallerInputError):
        state.adapter.invoke(operation, payload)
    assert state.calls == []


@pytest.mark.parametrize("code", list(ErrorCode))
@pytest.mark.parametrize("helper", ["generate_intents", "stream_intents",
                                    "stream_event_intents"])
def test_every_imported_core_error_is_terminal_and_rethrown_unchanged(
    source_transport: SimpleNamespace, code: ErrorCode, helper: str,
) -> None:
    state = source_transport
    record = Error(code, "typed upstream message")
    state.failure = Err(record)
    collection = {"intents": [{"name": "echo"}]}
    if helper == "stream_event_intents":
        collection = {"events": [{"type": "tool_intent_delta", "tool_intent": {}}]}
    payload = state.codec.dumps({"request_id": "r", **collection})
    with pytest.raises(Err) as caught:
        getattr(state.app.Caller(), helper)(payload)
    assert caught.value.value is record
    assert len(state.calls) == 1
    state.calls.clear()
    with pytest.raises(state.adapter.ImportedBridgeFailure) as internal:
        state.adapter.invoke(helper.replace("_", "-"), payload)
    assert internal.value.core_error is record
    assert internal.value.retryable is False
    assert not isinstance(internal.value, state.original.GlobalContractInvocationError)
    assert len(state.calls) == 1


@pytest.mark.parametrize("failure", [ValueError("private parser detail"),
                                     RuntimeError("private transport detail"),
                                     RecursionError("private recursion detail")])
def test_unknown_transport_failure_is_terminal_not_retryable_original_error(
    source_transport: SimpleNamespace, failure: Exception,
) -> None:
    state = source_transport
    state.failure = failure
    payload = '{"request_id":"r","intents":[]}'
    with pytest.raises(state.adapter.BridgeTransportFailure) as caught:
        state.adapter.invoke("generate-intents", payload)
    assert caught.value.retryable is False
    assert not isinstance(caught.value, state.original.GlobalContractInvocationError)
    assert "private" not in str(caught.value)
    assert len(state.calls) == 1
    state.calls.clear()
    with pytest.raises(Err) as result:
        state.app.Caller().generate_intents(payload)
    assert result.value.value == Error(ErrorCode.INTERNAL, "Gateway tool bridge failed")
    assert len(state.calls) == 1


@pytest.mark.parametrize("reply", [None, "not json", "[]", "{}",
                                   '{"intents":null}', '{"intents":{}}',
                                   '{"intents":[{"a":NaN}]}',
                                   '{"intents":[],"intents":[]}'])
def test_malformed_success_is_fail_closed_without_replay(
    source_transport: SimpleNamespace, reply: Any,
) -> None:
    state = source_transport
    state.response = reply
    with pytest.raises(state.adapter.BridgeResponseFailure) as caught:
        state.adapter.invoke("generate-intents", '{"request_id":"r","intents":[]}')
    assert caught.value.retryable is False
    assert not isinstance(caught.value, state.original.GlobalContractInvocationError)
    assert len(state.calls) == 1


def test_source_mapping_filter_and_extra_response_keys_remain_unchanged(
    source_transport: SimpleNamespace,
) -> None:
    state = source_transport
    state.response = '{"unused":1,"intents":[null,4,{"z":1.0,"a":-0.0},"skip"]}'
    result = state.adapter.invoke("generate-intents", '{"request_id":"r","intents":[]}')
    assert result == '[{"z":1.0,"a":-0.0}]'
    assert len(state.calls) == 1


def test_stream_per_event_order_and_fallback_index_zero_are_original(
    normalizer_source_transport: SimpleNamespace,
) -> None:
    state = normalizer_source_transport
    events = [
        {"type": "text_delta", "text": "untouched", "tool_intent": {"name": "no"}},
        {"type": "tool_intent_delta", "tool_intent": {"name": "one"}},
        {"type": "tool_intent_delta", "tool_intent": "not an object"},
        {"type": "tool_intent_delta", "tool_intent": {"name": "two"}},
    ]
    payload = state.codec.dumps({"request_id": "req", "events": events})
    result = state.codec.loads(state.app.Caller().stream_event_intents(payload))
    assert len(state.calls) == 2
    assert [state.codec.loads(raw)["intents"] for _, raw in state.calls] == [
        [{"name": "one"}], [{"name": "two"}],
    ]
    assert all(operation.endswith(".stream") for operation, _ in state.calls)
    assert result[1]["tool_intent"]["intent_id"] == "req:tool:0"
    assert result[3]["tool_intent"]["intent_id"] == "req:tool:0"
    assert result[0] == events[0] and result[2] == events[2]


def test_source_stream_partial_mutation_is_not_rolled_back_or_replayed(
    source_transport: SimpleNamespace,
) -> None:
    state = source_transport
    original_intents = [{"name": "first"}, {"name": "second"}, {"name": "third"}]
    events = [{"type": "tool_intent_delta", "tool_intent": item}
              for item in original_intents]
    record = Error(ErrorCode.CANCELLED, "cancelled")

    def responder(operation: str, payload: str) -> str:
        if len(state.calls) == 2:
            raise Err(record)
        return '{"intents":[{"normalized":"first"}]}'

    state.responder = responder
    with pytest.raises(state.adapter.ImportedBridgeFailure) as caught:
        state.original._attach_stream_tool_intents(
            state.adapter.ImportedBridgeClient(), events, "r",
        )
    assert caught.value.core_error is record
    assert events[0]["tool_intent"] == {"normalized": "first"}
    assert events[1]["tool_intent"] is original_intents[1]
    assert events[2]["tool_intent"] is original_intents[2]
    assert len(state.calls) == 2


def test_empty_stream_response_is_terminal_and_later_event_is_not_called(
    source_transport: SimpleNamespace,
) -> None:
    state = source_transport
    payload = state.codec.dumps({"request_id": "r", "events": [
        {"type": "tool_intent_delta", "tool_intent": {"name": "one"}},
        {"type": "tool_intent_delta", "tool_intent": {"name": "two"}},
    ]})
    with pytest.raises(state.adapter.BridgeResponseFailure):
        state.adapter.invoke("stream-event-intents", payload)
    assert len(state.calls) == 1


def test_no_matching_stream_events_do_not_import(
    source_transport: SimpleNamespace,
) -> None:
    state = source_transport
    events = [{"type": "text_delta", "text": "hello"}, {"type": "usage", "usage": {}}]
    output = state.adapter.invoke("stream-event-intents", state.codec.dumps({
        "request_id": "r", "events": events,
    }))
    assert output == state.codec.dumps(events)
    assert state.calls == []


def test_encoding_failure_has_no_import_and_no_original_retryable_error(
    source_transport: SimpleNamespace,
) -> None:
    state = source_transport
    with pytest.raises(state.adapter.BridgeTransportFailure) as caught:
        state.adapter.ImportedBridgeClient().invoke(
            state.original.TOOL_BRIDGE_CONTRACT,
            state.original.TOOL_BRIDGE_GENERATE_OPERATION, {"bad": object()},
        )
    assert caught.value.retryable is False
    assert state.calls == []


def test_output_encoding_failure_cannot_replay_normalization(
    source_transport: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = source_transport
    encode = state.adapter.dumps
    count = 0

    def fail_second(value: object) -> str:
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError("private output encoding failure")
        return encode(value)

    monkeypatch.setattr(state.adapter, "dumps", fail_second)
    with pytest.raises(state.adapter.BridgeResponseFailure):
        state.adapter.invoke("generate-intents", '{"request_id":"r","intents":[]}')
    assert len(state.calls) == 1
