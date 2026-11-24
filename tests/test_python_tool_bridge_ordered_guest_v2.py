"""Source-boundary tests only: no Wasm guest or native Core execution proof.

The minimal generated-binding stand-ins below let Python execute the entrypoint's
mapping code. They provide no Core imports, authority, dispatch or audit behavior.
The actual v2 codec, adapter and unchanged original normalizer run in these tests.
"""

from dataclasses import dataclass
from enum import Enum
import importlib.util
import json
from pathlib import Path
import platform
import re
import sys
from types import ModuleType
from typing import Any
import unicodedata

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs/local.ai-tool-bridge-ordered-python"
sys.path[:0] = [str(PACK), str(PACK / "python")]
import caller_adapter_v2 as adapter
import ordered_json_v2 as codec
from original_bridge import GlobalContractInvocationError, create_tool_intent_operation


class _ErrorCode(Enum):
    INVALID = "invalid"
    LIMIT_EXCEEDED = "limit-exceeded"
    INTERNAL = "internal"


@dataclass
class _Error:
    code: _ErrorCode
    message: str


class _Err(Exception):
    def __init__(self, value: _Error) -> None:
        super().__init__(value)
        self.value = value


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load source with generated-value/class stand-ins, never host authority."""
    world = ModuleType("wit_world")
    exports = ModuleType("wit_world.exports")
    exports.Lifecycle = type("Lifecycle", (), {})
    exports.Bridge = type("Bridge", (), {})
    world.exports = exports
    imports = ModuleType("wit_world.imports")
    binding_types = ModuleType("wit_world.imports.types")
    binding_types.Error = _Error
    binding_types.ErrorCode = _ErrorCode
    imports.types = binding_types
    world.imports = imports
    component_types = ModuleType("componentize_py_types")
    component_types.Err = _Err
    for module in (world, exports, imports, binding_types, component_types):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    spec = importlib.util.spec_from_file_location(
        "_ordered_guest_v2_source", PACK / "python/app_v2.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_error(
    guest: ModuleType, operation: str, payload: str, code: _ErrorCode, message: str
) -> None:
    with pytest.raises(_Err) as caught:
        guest.Bridge().normalize(operation, payload)
    assert caught.value.value == _Error(code, message)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "private-" not in str(caught.value)


@pytest.mark.parametrize("operation", adapter.OPERATIONS)
@pytest.mark.parametrize(
    "arguments",
    [
        '{"z":-0.0,"a":1.0,"tiny":5e-324,"text":"NaN Infinity 😀"}',
        '{"n":' + "9" * 4300 + "}",
        '{"n":' + "[" * 70 + "18446744073709551616" + "]" * 70 + "}",
        '{"n":NaN,"n":1}',
    ],
)
def test_actual_normalizer_bytes_and_authority_custody_remain_unchanged(
    guest: ModuleType, operation: str, arguments: str
) -> None:
    """Run real finite normalization without accepting caller authority flags."""
    payload = {
        "request_id": " req ",
        "intents": [
            {
                "name": "echo",
                "arguments": arguments,
                "authority_granted": True,
                "approved": True,
                "approval_status": "approved",
                "executes": True,
            }
        ],
    }
    original = create_tool_intent_operation(None)("normalize", payload)
    expected = json.dumps(
        original, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    actual = guest.Bridge().normalize(operation, adapter.request(payload))
    assert actual == expected
    result = adapter.response(actual)["intents"][0]
    assert result["request_id"] == "req"
    assert result["authority_granted"] is False
    assert result["approved"] is False
    assert result["approval_status"] == "unrequested"
    assert result["executes"] is False


@pytest.mark.parametrize("operation", adapter.OPERATIONS)
@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
def test_actual_nonfinite_output_maps_only_to_fixed_internal(
    guest: ModuleType, operation: str, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normalize once and sanitize all admitted nonfinite provider failures."""
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
    payload = {
        "request_id": "private-request-marker",
        "intents": [
            {
                "name": "echo",
                "arguments": '{"private-value-marker":' + token + "}",
            }
        ],
    }
    _assert_error(
        guest,
        operation,
        adapter.request(payload),
        _ErrorCode.INTERNAL,
        "provider output contains a non-finite number",
    )
    assert calls == 1


@pytest.mark.parametrize("operation", adapter.OPERATIONS)
@pytest.mark.parametrize(
    "payload",
    [
        {"intents": [1]},
        {"intents": [{"name": "private-invalid name"}]},
        {"intents": [{"name": "echo", "arguments": "[1]"}]},
        {"intents": [{"name": "echo", "arguments": "{"}]},
    ],
)
def test_actual_original_normalization_errors_stay_invalid(
    guest: ModuleType, operation: str, payload: dict[str, object]
) -> None:
    """Keep original contract failures distinct from provider-output failures."""
    with pytest.raises(GlobalContractInvocationError):
        create_tool_intent_operation(None)("normalize", payload)
    _assert_error(
        guest,
        operation,
        adapter.request(payload),
        _ErrorCode.INVALID,
        "outside ordered-finite-json-v2 domain",
    )


@pytest.mark.parametrize(
    "payload",
    [
        '{"intents":[],"n":NaN}',
        '{"intents":[],"n":Infinity}',
        '{"intents":[],"n":-1e400}',
        '{"intents":[],"intents":[]}',
        "[]",
        "{",
    ],
)
def test_actual_invalid_transport_never_enters_normalizer(
    guest: ModuleType, payload: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject malformed transport before any original normalizer invocation."""
    def forbidden(_client: object) -> object:
        pytest.fail("invalid transport reached the original normalizer")

    monkeypatch.setattr(adapter, "create_tool_intent_operation", forbidden)
    _assert_error(
        guest,
        adapter.OPERATIONS[0],
        payload,
        _ErrorCode.INVALID,
        "outside ordered-finite-json-v2 domain",
    )


def test_actual_unknown_operation_and_oversize_inner_integer_stay_invalid(
    guest: ModuleType,
) -> None:
    """Keep operation and original integer-conversion exclusions invalid."""
    _assert_error(
        guest,
        "private-unknown-operation",
        "{}",
        _ErrorCode.INVALID,
        "outside ordered-finite-json-v2 domain",
    )
    payload = {
        "intents": [{"name": "echo", "arguments": '{"n":' + "9" * 4301 + "}"}]
    }
    with pytest.raises(ValueError) as caught:
        create_tool_intent_operation(None)("normalize", payload)
    assert not isinstance(caught.value, GlobalContractInvocationError)
    _assert_error(
        guest,
        adapter.OPERATIONS[0],
        adapter.request(payload),
        _ErrorCode.INVALID,
        "outside ordered-finite-json-v2 domain",
    )


@pytest.mark.parametrize("location", ["outer", "inner"])
def test_real_recursion_exception_at_either_decoder_maps_to_limit_exceeded(
    guest: ModuleType, location: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject real resource exceptions without probing or raising stack limits."""
    inner = '{"private-inner-marker":[]}'
    request = adapter.request({"intents": [{"name": "echo", "arguments": inner}]})
    original_loads = codec.json.loads
    failure = RecursionError("private-resource-detail")
    calls = 0

    def exhausted(text: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        if text == (request if location == "outer" else inner):
            calls += 1
            raise failure
        return original_loads(text, *args, **kwargs)

    monkeypatch.setattr(codec.json, "loads", exhausted)
    _assert_error(
        guest,
        adapter.OPERATIONS[0],
        request,
        _ErrorCode.LIMIT_EXCEEDED,
        "ordered tool bridge resource limit exceeded",
    )
    assert calls == 1


@pytest.mark.parametrize("exception_type", [ValueError, TypeError, OverflowError])
def test_ordinary_transport_failure_classes_map_to_invalid(
    guest: ModuleType, exception_type: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve the ordinary transport failure class mapping with fixed text."""
    def failed(_operation: str, _payload: str) -> str:
        raise exception_type("private-transport-detail")

    monkeypatch.setattr(guest, "invoke", failed)
    _assert_error(
        guest,
        adapter.OPERATIONS[0],
        "{}",
        _ErrorCode.INVALID,
        "outside ordered-finite-json-v2 domain",
    )


@pytest.mark.parametrize(
    "exception_type",
    [MemoryError, PermissionError, RuntimeError, AssertionError, KeyboardInterrupt],
)
def test_unrecognized_resource_security_and_programming_errors_are_not_masked(
    guest: ModuleType,
    exception_type: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never convert arbitrary exceptions or interruptions into invalid input."""
    failure = exception_type("private-unexpected-detail")

    def failed(_operation: str, _payload: str) -> str:
        raise failure

    monkeypatch.setattr(guest, "invoke", failed)
    with pytest.raises(exception_type) as caught:
        guest.Bridge().normalize(adapter.OPERATIONS[0], "{}")
    assert caught.value is failure


def test_runtime_info_uses_actual_interpreter_and_v2_codec(guest: ModuleType) -> None:
    """Report the executing interpreter rather than a promised guest profile."""
    assert guest.invoke is adapter.invoke
    assert guest.dumps is codec.dumps
    assert guest.Bridge().runtime_info() == codec.dumps(
        {
            "python": platform.python_version(),
            "unicode": unicodedata.unidata_version,
        }
    )


def test_lifecycle_does_not_consume_configuration(guest: ModuleType) -> None:
    """Retain no-op lifecycle behavior without configuration or resources."""
    lifecycle = guest.Lifecycle()
    assert lifecycle.init("private-unparsed-config") is None
    assert lifecycle.shutdown() is None
    assert vars(lifecycle) == {}


def test_versioned_wit_retains_exact_canonical_minimal_world() -> None:
    """Change only the Pack-owned version and transport documentation."""
    wit = (PACK / "wit/pack-v2.wit").read_text()
    v1 = (PACK / "wit/pack.wit").read_text()
    assert wit == v1.replace(
        "local:ai-tool-bridge-ordered-python@1.0.0",
        "local:ai-tool-bridge-ordered-python@2.0.0",
    ).replace("ordered-finite-json-v1", "ordered-finite-json-v2")
    core = (ROOT / "crates/tobkiri-wit/wit/core/core.wit").read_text()
    minimal = re.search(r"world pack-minimal \{([^}]+)\}", core)
    assert minimal is not None
    assert re.findall(r"import ([a-z-]+);", minimal.group(1)) == [
        "types",
        "flow",
        "clock",
        "random",
        "audit",
    ]
    assert re.findall(r"export ([a-z-]+);", minimal.group(1)) == ["lifecycle"]
