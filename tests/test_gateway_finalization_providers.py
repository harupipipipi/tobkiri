"""Original-source differential tests, not guest/Core/producer acceptance proof.

The binding doubles below represent generated value/class bindings ONLY. The
behavioral oracle is independently compiled from the original repository source,
never from a handwritten expected provider implementation or a fake Host.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
import unicodedata

import pytest

ROOT = Path(__file__).resolve().parents[1]
CODEC_PACK = ROOT / "packs/local.ai-tool-bridge-ordered-python"
SCRIPT = ROOT / "scripts/wasm/prepare_gateway_finalization_providers.py"
SPECS = {
    "usage-cost": ("original_usage_cost", "usage_cost_adapter", "UsageCost",
                   "calculate", "create_cost_operation", "usage cost"),
    "stream-normalizer": ("original_stream_normalizer", "stream_normalizer_adapter",
                          "StreamNormalizer", "normalize",
                          "create_stream_normalize_operation", "stream normalizer"),
}


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = _load("finalization_provider_extraction", SCRIPT)


def _pack(provider: str) -> Path:
    return ROOT / f"packs/local.ai-{provider}-python"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _oracle(provider: str) -> ModuleType:
    """Compile independent original AST nodes, without importing legacy Host."""
    info = prepare.PROVIDERS[provider]
    path = ROOT / prepare.ECOSYSTEM / info["pack"] / info["source"]
    original = ast.parse(path.read_bytes(), filename=str(path))
    allowed = {"__future__", "collections.abc", "typing"}
    body = [node for node in original.body if (
        isinstance(node, ast.ImportFrom) and node.module in allowed
    ) or (
        isinstance(node, ast.Import)
        and [item.name for item in node.names] == ["math"]
    )]
    if provider == "stream-normalizer":
        dispatch = ast.parse((ROOT / prepare.DISPATCH).read_bytes())
        body.extend(node for node in dispatch.body if isinstance(node, ast.ClassDef)
                    and node.name == "GlobalContractInvocationError")
    names = ({"create_cost_operation", "_number"} if provider == "usage-cost"
             else {"create_stream_normalize_operation", "_ALLOWED_TYPES"})
    body.extend(node for node in original.body if (
        isinstance(node, ast.FunctionDef) and node.name in names
    ) or (
        isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                                           and target.id in names
                                           for target in node.targets)
    ))
    module = ModuleType(f"independent_original_{provider}")
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"),
         module.__dict__)
    return module


@pytest.mark.parametrize("provider", SPECS)
def test_verbatim_source_provenance_and_staged_dependencies(
    provider: str, tmp_path: Path,
) -> None:
    checked = prepare.verify(provider, _pack(provider) / "python")
    result = prepare.prepare(provider, tmp_path)
    assert result == checked
    assert not result["build_performed"] and not result["production_enabled"]
    assert result["protocol"] == "ordered-finite-json-v2"
    output = (tmp_path / result["output"]["path"]).read_bytes()
    assert _digest(output) == result["output"]["sha256"]
    assert len(output) == result["output"]["bytes"]
    for record in result["sources"] + result["transport_dependencies"]:
        content = (ROOT / record["path"]).read_bytes()
        assert _digest(content) == record["sha256"]
        assert len(content) == record["bytes"]
    for item in result["slices"]:
        source = (ROOT / item["source_path"]).read_bytes()
        selected = source[item["source_start_byte"]:item["source_end_byte"]]
        assert selected == b"".join(source.splitlines(keepends=True)[
            item["source_start_line"] - 1:item["source_end_line"]
        ])
        assert selected == output[item["output_start_byte"]:item["output_end_byte"]]
        assert selected == b"".join(output.splitlines(keepends=True)[
            item["output_start_line"] - 1:item["output_end_line"]
        ])
        assert _digest(selected) == item["sha256"]
    assert {item["path"] for item in result["transport_dependencies"]} == {
        *prepare.CODEC_PATHS, prepare.CORE_WIT,
    }
    assert _digest((tmp_path / "inventory.json").read_bytes()) == (
        result["inventory"]["sha256"]
    )
    assert {item["stage_path"] for item in result["staged_references"]} == {
        "python/ordered_json.py", "python/ordered_json_v2.py",
        "wit/deps/tobkiri-core/core.wit",
    }
    assert all(item["copy_unchanged"] for item in result["staged_references"])
    for name in ("ordered_json.py", "ordered_json_v2.py"):
        assert not (_pack(provider) / name).exists()
        assert not (_pack(provider) / "python" / name).exists()


@pytest.mark.parametrize("provider", SPECS)
def test_extracted_source_closure_and_no_host_authority(provider: str) -> None:
    spec = prepare.PROVIDERS[provider]
    path = _pack(provider) / "python" / spec["module"]
    source = path.read_text()
    tree = ast.parse(source)
    imports = [(node.module if isinstance(node, ast.ImportFrom) else None,
                tuple(item.name for item in node.names)) for node in tree.body
               if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert imports == list(spec["imports"])
    functions = [node.name for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
    assert functions == (["create_cost_operation", "_number"]
                         if provider == "usage-cost" else [
                             "GlobalContractInvocationError",
                             "create_stream_normalize_operation",
                         ])
    for forbidden in ("HostProvider", "HOST_PROVIDER", "tokenize", "credential",
                      "broker", "register", "exec(", "eval(", "__import__"):
        assert forbidden not in source
    # Actual execution of the complete extracted closure resolves every global
    # used by its operation with no Host module or authority stub installed.
    module = _load(f"closure_{provider}", path)
    payload = {} if provider == "usage-cost" else {
        "request_id": "r", "value": [{"type": "finish"}],
    }
    getattr(module, SPECS[provider][4])(None)(SPECS[provider][3], payload)


@pytest.mark.parametrize("provider,edge_count", [("usage-cost", 4),
                                                ("stream-normalizer", 1)])
def test_inventory_resolves_original_manifests_aliases_and_caller_edges(
    provider: str, edge_count: int,
) -> None:
    inventory = json.loads((_pack(provider) / "python/inventory.json").read_bytes())
    assert len(inventory["caller_edges"]) == edge_count
    assert inventory["accepted_operation_aliases"] == prepare.PROVIDERS[provider][
        "aliases"
    ]
    sources = {}
    for item in inventory["sources"]:
        content = (ROOT / item["path"]).read_bytes()
        assert _digest(content) == item["sha256"]
        assert len(content) == item["bytes"]
        sources[item["path"]] = content
    for item in inventory["operations"] + inventory["caller_edges"]:
        assert len(item["declarations"]) == (2 if "caller_function_id" in item else 3)
        for evidence in item["declarations"]:
            value = json.loads(sources[evidence["path"]])
            for key in evidence["json_pointer"].lstrip("/").split("/"):
                value = value[int(key)] if isinstance(value, list) else value[key]
            assert value["operation_id"] == item["operation_id"]
            if "caller_function_id" in item:
                assert value["caller_function_id"] == item["caller_function_id"]
                assert value["target_provider_id"] == item["target_provider_id"]
    evidence = (inventory["gateway_evidence"]
                + inventory["gateway_dependency_call_sites"])
    evidence.append(inventory["host_dispatch_evidence"])
    for wrapper in inventory["wrapper_evidence"]:
        assert not wrapper["migrated"]
        assert wrapper["uses_this_provider"] is (provider == "usage-cost")
        assert [item["symbol"] for item in wrapper["gateway_delegation_calls"]] == [
            "gateway.create_generate_operation", "generate",
        ]
        evidence.extend(wrapper["definitions"] + wrapper["gateway_delegation_calls"])
    for item in evidence:
        assert _digest(sources[item["source_path"]][
            item["source_start_byte"]:item["source_end_byte"]
        ]) == item["sha256"]


@pytest.mark.parametrize("provider", SPECS)
def test_wit_canonical_lifecycle_and_one_typed_provider_method(provider: str) -> None:
    wit = (_pack(provider) / "wit/pack.wit").read_text()
    assert f"package local:ai-{provider}-python@1.0.0;" in wit
    assert "include tobkiri:core/pack-minimal@1.0.0;" in wit
    assert "use tobkiri:core/types@1.0.0.{error};" in wit
    assert f"export {provider};" in wit
    assert "runtime-info: func() -> string;" in wit
    assert wit.count("-> result<string, error>") == 1
    assert f"{SPECS[provider][3]}: func(operation: string, payload: string)" in wit
    for path in (_pack(provider) / "python").glob("*.py"):
        tree = ast.parse(path.read_bytes())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = node.module if isinstance(node, ast.ImportFrom) else ""
                names = [module or "", *(alias.name for alias in node.names)]
                assert not any(fragment in name for fragment in (
                    "host_provider", "core_runtime", "requests", "socket",
                    "subprocess", "urllib",
                ) for name in names)
        assert "set_int_max_str_digits" not in path.read_text()
        assert "setrecursionlimit" not in path.read_text()


@pytest.mark.parametrize("provider", SPECS)
def test_preparation_deterministic_readonly_and_detects_source_drift(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    assert prepare.prepare(provider, one) == prepare.prepare(provider, two)
    assert prepare.verify(provider, one) == prepare.verify(provider, two)
    for path in one.iterdir():
        assert path.read_bytes() == (two / path.name).read_bytes()
    capture = prepare._capture
    monkeypatch.setattr(prepare, "_capture", lambda path: capture(path) + (
        b"\n# original whole-source drift\n" if path == prepare.GATEWAY else b""
    ))
    before = (one / "inventory.json").read_bytes()
    with pytest.raises(ValueError):
        prepare.verify(provider, one)
    assert (one / "inventory.json").read_bytes() == before


@pytest.mark.parametrize("provider", SPECS)
@pytest.mark.parametrize("kind", ["symlink", "ancestor", "hardlink", "fifo"])
def test_preparation_rejects_unsafe_output_without_touching_target(
    provider: str, kind: str, tmp_path: Path,
) -> None:
    output, outside = tmp_path / "output", tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected"
    protected.write_bytes(b"unchanged")
    if kind == "ancestor":
        output.symlink_to(outside, target_is_directory=True)
    else:
        output.mkdir()
        target = output / prepare.PROVIDERS[provider]["module"]
        if kind == "symlink":
            target.symlink_to(protected)
        elif kind == "hardlink":
            os.link(protected, target)
        else:
            os.mkfifo(target)
    with pytest.raises(ValueError):
        prepare.prepare(provider, output)
    assert protected.read_bytes() == b"unchanged"


@pytest.mark.parametrize("kind", ["symlink", "ancestor", "fifo", "oversize"])
def test_source_reader_rejects_unsafe_or_unbounded_file(
    kind: str, tmp_path: Path,
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    source = actual / "source"
    source.write_bytes(b"12345678")
    path = tmp_path / "target"
    if kind == "symlink":
        path.symlink_to(source)
    elif kind == "ancestor":
        path.symlink_to(actual, target_is_directory=True)
        path = path / "source"
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path = source
    with pytest.raises(ValueError):
        prepare.bounded_read(path, limit=4)


class ErrorCode(Enum):
    """Only the three Core arms actually emitted by these pure providers."""

    INVALID = "invalid"
    INTERNAL = "internal"
    LIMIT_EXCEEDED = "limit-exceeded"


@dataclass
class Error:
    """SOURCE BINDING DOUBLE, not a native Core record."""

    code: ErrorCode
    message: str


class Err(Exception):
    """SOURCE BINDING DOUBLE, not a component ABI exception."""

    def __init__(self, value: Error) -> None:
        super().__init__(value)
        self.value = value


@pytest.fixture(params=tuple(SPECS))
def source_binding_double(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Load source under value/class binding doubles; never build/run a guest."""
    provider = request.param
    original_name, adapter_name, class_name, method, factory, label = SPECS[provider]
    world = ModuleType("wit_world")
    exports = ModuleType("wit_world.exports")
    imports = ModuleType("wit_world.imports")
    binding_types = ModuleType("wit_world.imports.types")
    component_types = ModuleType("componentize_py_types")
    exports.Lifecycle = type("Lifecycle", (), {})
    setattr(exports, class_name, type(class_name, (), {}))
    component_types.Err = Err
    binding_types.Error, binding_types.ErrorCode = Error, ErrorCode
    world.exports, world.imports = exports, imports
    imports.types = binding_types
    for module in (world, exports, imports, binding_types, component_types):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.syspath_prepend(str(CODEC_PACK))
    import ordered_json_v2 as codec
    path = _pack(provider) / "python"
    original = _load(original_name, path / f"{original_name}.py")
    monkeypatch.setitem(sys.modules, original_name, original)
    adapter = _load(adapter_name, path / f"{adapter_name}.py")
    monkeypatch.setitem(sys.modules, adapter_name, adapter)
    app = _load(f"source_binding_app_{provider}", path / "app.py")
    instance = getattr(app, class_name)()
    return SimpleNamespace(provider=provider, original=original, adapter=adapter,
                           app=app, call=getattr(instance, method), instance=instance,
                           codec=codec, oracle=_oracle(provider), factory=factory,
                           operation=method, label=label)


def _only(state: SimpleNamespace, provider: str) -> None:
    assert state.provider == provider


def _outcome(operation: Any, name: str, payload: Any) -> tuple[Any, ...]:
    try:
        return ("ok", operation(name, payload))
    except Exception as error:
        return ("failure", type(error).__name__,
                getattr(error, "code", None), str(error))


def _differential(
    state: SimpleNamespace, payload: Any, operation: str | None = None,
) -> Any:
    name = operation or state.operation
    original = getattr(state.oracle, state.factory)(None)
    extracted = getattr(state.original, state.factory)(None)
    expected = _outcome(original, name, payload)
    actual = _outcome(extracted, name, payload)
    assert actual == expected
    return expected


def test_runtime_diagnostics_and_noop_lifecycle(
    source_binding_double: SimpleNamespace,
) -> None:
    state = source_binding_double
    assert state.instance.runtime_info() == state.codec.dumps({
        "python": platform.python_version(), "unicode": unicodedata.unidata_version,
    })
    assert state.app.Lifecycle().init("ignored") is None
    assert state.app.Lifecycle().shutdown() is None


def test_aliases_match_original_source_and_explicit_base_host_mapping(
    source_binding_double: SimpleNamespace,
) -> None:
    state = source_binding_double
    payload = ({"usage": {"input_tokens": 2, "output_tokens": 3},
                "pricing": {"input": 0.25, "output": 0.5}}
               if state.provider == "usage-cost" else
               {"request_id": "r", "value": [{"type": "finish"}]})
    for alias, original in state.adapter.OPERATIONS.items():
        expected = _differential(state, payload, original)
        assert expected[0] == "ok"
        actual = state.call(alias, state.codec.dumps(payload))
        assert actual == state.codec.dumps(expected[1])
    if state.provider == "usage-cost":
        outcome = _differential(state, payload, "rumi_ai_usage_pack.ai-usage-cost")
        assert outcome[0:2] == ("failure", "ValueError")


@pytest.mark.parametrize("usage,pricing", [
    ({"input_tokens": 2, "output_tokens": 3}, {"input": 0.25, "output": 0.5}),
    ({"prompt_tokens": "2", "completion_tokens": "3.5"},
     {"input": "0.25", "output": "0.5", "currency": "EUR"}),
    ({"input_tokens": True, "output_tokens": False}, {"input": True, "output": False}),
    ({"input_tokens": -1, "output_tokens": 0}, {"input": 1, "output": 1}),
    ({"input_tokens": "-1", "output_tokens": "bad"}, {"input": 1, "output": 1}),
    ({"input_tokens": None, "prompt_tokens": 2, "output_tokens": 3},
     {"input": 1, "output": 1}),
    ({"input_tokens": "NaN", "output_tokens": "Infinity"}, {"input": 1, "output": 1}),
    ({"input_tokens": "1e309", "output_tokens": "-Infinity"},
     {"input": "NaN", "output": "Infinity"}),
    ({"input_tokens": -0.0, "output_tokens": 0.0}, {"input": 1, "output": 1}),
    ({}, {}), (None, []), ([], None),
])
@pytest.mark.parametrize("source_binding_double", ["usage-cost"], indirect=True)
def test_usage_known_unknown_bool_string_negative_finite_data_differential(
    source_binding_double: SimpleNamespace, usage: Any, pricing: Any,
) -> None:
    state = source_binding_double
    _only(state, "usage-cost")
    payload = {"usage": usage, "pricing": pricing, "usage_provenance": "source",
               "pricing_revision": {"z": 1, "a": 2**80, "float": 1.0, "zero": -0.0}}
    result = _differential(state, payload)
    assert result[0] == "ok"
    actual = state.call("calculate", state.codec.dumps(payload))
    assert actual == state.codec.dumps(result[1])


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("source_binding_double", ["usage-cost"], indirect=True)
def test_original_nonfinite_numbers_are_unknown_but_not_transportable_data(
    source_binding_double: SimpleNamespace, number: float,
) -> None:
    state = source_binding_double
    _only(state, "usage-cost")
    payload = {"usage": {"input_tokens": number, "output_tokens": 1},
               "pricing": {"input": 1, "output": 1}}
    outcome = _differential(state, payload)
    assert outcome[1]["input_tokens"] is None and not outcome[1]["known"]
    with pytest.raises(state.codec.NonFiniteJSONError):
        state.codec.dumps(payload)


@pytest.mark.parametrize("usage,pricing,original_failure", [
    ({"input_tokens": 10**400, "output_tokens": 1}, {"input": 1, "output": 1}, True),
    ({"input_tokens": 1, "output_tokens": 1}, {"input": 10**400, "output": 1}, True),
    ({"input_tokens": 1e308, "output_tokens": 1}, {"input": 1e308, "output": 1}, False),
    ({"input_tokens": 1e308, "output_tokens": 1e308}, {}, False),
])
@pytest.mark.parametrize("source_binding_double", ["usage-cost"], indirect=True)
def test_usage_overflow_never_becomes_zero_or_unknown_success(
    source_binding_double: SimpleNamespace, usage: Any, pricing: Any,
    original_failure: bool,
) -> None:
    state = source_binding_double
    _only(state, "usage-cost")
    payload = {"usage": usage, "pricing": pricing}
    outcome = _differential(state, payload)
    if original_failure:
        assert outcome[0:2] == ("failure", "OverflowError")
    else:
        assert outcome[0] == "ok"
        assert any(type(value) is float and not math.isfinite(value)
                   for value in outcome[1].values())
    with pytest.raises(Err) as error:
        state.call("calculate", state.codec.dumps(payload))
    assert error.value.value == Error(ErrorCode.INTERNAL,
                                      "usage cost provider arithmetic failed")


@pytest.mark.parametrize("terminal", ["finish", "error"])
@pytest.mark.parametrize("source_binding_double", ["stream-normalizer"], indirect=True)
def test_stream_all_allowed_types_order_projection_and_bindings(
    source_binding_double: SimpleNamespace, terminal: str,
) -> None:
    state = source_binding_double
    _only(state, "stream-normalizer")
    opaque = {"z": 1, "a": 1.0, "negative_zero": -0.0, "big": 2**100,
              "opaque": '{"duplicate":1,"duplicate":2}'}
    events = [{"type": name, "delta": opaque, "tool_intent": opaque,
               "usage": opaque, "finish_reason": "stop", "error_code": None,
               "request_id": "untrusted", "provider_attempt": 999,
               "sequence": 999, "extra": "discard"}
              for name in ("text_delta", "thinking_delta", "tool_intent_delta",
                           "usage", terminal)]
    payload = {"request_id": "  actual request  ", "provider_attempt": "3",
               "value": {"events": events}}
    outcome = _differential(state, payload)
    actual = state.call("normalize", state.codec.dumps(payload))
    assert actual == state.codec.dumps(outcome[1])
    normalized = state.codec.loads(actual)["events"]
    assert len(normalized) == len(events)
    for sequence, event in enumerate(normalized):
        assert list(event) == [
            "request_id", "sequence", "type", "delta", "tool_intent", "usage",
            "finish_reason", "error_code", "provider_attempt",
        ]
        assert event["sequence"] == sequence
        assert event["request_id"] == "actual request"
        assert event["provider_attempt"] == 3
        assert "extra" not in event
        assert list(event["delta"]) == list(opaque)
        assert type(event["delta"]["z"]) is int
        assert type(event["delta"]["a"]) is float
        assert math.copysign(1, event["delta"]["negative_zero"]) == -1


@pytest.mark.parametrize("attempt", [None, False, True, 0, -2, 2.9, "4", 10**400])
@pytest.mark.parametrize("request_id", ["  req  ", 42, ["data"], {"z": 1, "a": 2}])
@pytest.mark.parametrize("source_binding_double", ["stream-normalizer"], indirect=True)
def test_stream_original_attempt_and_request_data_coercions_preserved(
    source_binding_double: SimpleNamespace, attempt: Any, request_id: Any,
) -> None:
    state = source_binding_double
    _only(state, "stream-normalizer")
    payload = {"request_id": request_id, "provider_attempt": attempt,
               "value": [{"type": "finish"}]}
    outcome = _differential(state, payload)
    assert outcome[0] == "ok"
    actual = state.call("normalize", state.codec.dumps(payload))
    assert actual == state.codec.dumps(outcome[1])


@pytest.mark.parametrize("payload", [
    {}, {"request_id": "r"}, {"request_id": " ", "value": [{"type": "finish"}]},
    {"request_id": "r", "value": []},
    {"request_id": "r", "value": [{"type": "text_delta"}]},
    {"request_id": "r", "value": [{"type": "finish"}, {"type": "text_delta"}]},
    {"request_id": "r", "value": [{"type": "error"}, {"type": "finish"}]},
    {"request_id": "r", "value": [None]},
    {"request_id": "r", "value": [{"type": "unknown"}]},
    {"request_id": "r", "value": [{"type": True}]},
    {"request_id": "r", "value": {"type": "finish"}},
    {"request_id": "r", "value": "finish"},
    {"request_id": "r", "value": {"events": {"type": "finish"}}},
    {"request_id": "r", "provider_attempt": "bad", "value": [{"type": "finish"}]},
    {"request_id": "r", "provider_attempt": [1], "value": [{"type": "finish"}]},
])
@pytest.mark.parametrize("source_binding_double", ["stream-normalizer"], indirect=True)
def test_stream_provider_validation_is_terminal_not_input_invalid_or_synthetic_success(
    source_binding_double: SimpleNamespace, payload: Any,
) -> None:
    state = source_binding_double
    _only(state, "stream-normalizer")
    outcome = _differential(state, payload)
    assert outcome[0] == "failure"
    with pytest.raises(Err) as error:
        state.call("normalize", state.codec.dumps(payload))
    assert error.value.value == Error(ErrorCode.INTERNAL,
                                      "stream normalizer provider validation failed")


@pytest.mark.parametrize("payload", ["null", "[]", '"str"', '{"x":1,"x":2}',
                                     '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}',
                                     '{"x":', '{"x":"\\ud800"}'])
def test_invalid_codec_input_does_not_invoke_original_source(
    source_binding_double: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    state = source_binding_double
    def forbidden(client: Any) -> Any:
        pytest.fail("invalid export input reached original provider")
    monkeypatch.setattr(state.adapter, state.factory, forbidden)
    with pytest.raises(Err) as error:
        state.call(state.operation, payload)
    assert error.value.value == Error(
        ErrorCode.INVALID, f"invalid {state.label} request",
    )


@pytest.mark.parametrize("operation", ["", "tokenize", "generate", "other", None, []])
def test_invalid_operation_identity_cannot_invoke_provider(
    source_binding_double: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    operation: Any,
) -> None:
    state = source_binding_double
    def forbidden(client: Any) -> Any:
        pytest.fail("unknown identity reached original provider")
    monkeypatch.setattr(state.adapter, state.factory, forbidden)
    with pytest.raises(Err) as error:
        state.call(operation, "{}")
    assert error.value.value == Error(
        ErrorCode.INVALID, f"invalid {state.label} request",
    )


@pytest.mark.parametrize("stage", ["decode", "provider", "encode"])
@pytest.mark.parametrize("failure", [RecursionError, MemoryError])
def test_injected_resource_failures_keep_terminal_typed_limit(
    source_binding_double: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    stage: str, failure: type[Exception],
) -> None:
    state = source_binding_double
    def explode(*args: Any) -> Any:
        raise failure("private resource detail")
    if stage == "provider":
        monkeypatch.setattr(state.adapter, state.factory, lambda client: explode)
    else:
        monkeypatch.setattr(
            state.adapter, "loads" if stage == "decode" else "dumps", explode,
        )
    payload = ({} if state.provider == "usage-cost" else
               {"request_id": "r", "value": [{"type": "finish"}]})
    with pytest.raises(Err) as error:
        state.call(state.operation, state.codec.dumps(payload))
    assert error.value.value == Error(ErrorCode.LIMIT_EXCEEDED,
                                      f"{state.label} resource limit exceeded")


def test_output_budget_failure_distinct_from_input_or_arithmetic_failure(
    source_binding_double: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = source_binding_double
    calls = []
    original = getattr(state.adapter, state.factory)
    def factory(client: Any) -> Any:
        operation = original(client)
        def invoke(name: str, payload: Any) -> Any:
            calls.append(name)
            return operation(name, payload)
        return invoke
    monkeypatch.setattr(state.adapter, state.factory, factory)
    def exceeds(value: Any) -> str:
        raise state.codec.OrderedJSONError("private bound detail")
    monkeypatch.setattr(state.adapter, "dumps", exceeds)
    payload = ({} if state.provider == "usage-cost" else
               {"request_id": "r", "value": [{"type": "finish"}]})
    with pytest.raises(Err) as error:
        state.call(state.operation, state.codec.dumps(payload))
    assert error.value.value == Error(ErrorCode.INTERNAL,
                                      f"{state.label} provider output failed")
    assert len(calls) == 1


def test_no_interpreter_safety_setting_changes(
    source_binding_double: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = source_binding_double
    before = sys.get_int_max_str_digits(), sys.getrecursionlimit()
    assert before[0] == 4300
    monkeypatch.setattr(state.adapter.sys, "get_int_max_str_digits", lambda: 640)
    with pytest.raises(Err) as error:
        state.call(state.operation, "{}")
    assert error.value.value.code is ErrorCode.INVALID
    assert sys.getrecursionlimit() == before[1]


def test_real_decoder_exhaustion_keeps_runtime_limit_boundary(
    source_binding_double: SimpleNamespace,
) -> None:
    state = source_binding_double
    # CPython 3.13.5 can decode depth 5000 despite getrecursionlimit()==1000.
    # Depth 10000 is within wire node/byte budgets but exhausts its decoder.
    payload = '{"data":' + '[' * 10000 + '0' + ']' * 10000 + '}'
    with pytest.raises(Err) as error:
        state.call(state.operation, payload)
    assert error.value.value == Error(ErrorCode.LIMIT_EXCEEDED,
                                      f"{state.label} resource limit exceeded")


@pytest.mark.parametrize("bound", ["bytes", "nodes", "integer"])
def test_real_input_codec_limits_are_invalid_not_provider_failures(
    source_binding_double: SimpleNamespace, bound: str,
) -> None:
    state = source_binding_double
    if bound == "bytes":
        payload = '{"data":"' + "a" * state.codec.MAX_BYTES + '"}'
    elif bound == "nodes":
        payload = '{"data":[' + ','.join("0" for _ in range(100000)) + ']}'
    else:
        payload = '{"data":' + "9" * 4301 + '}'
    with pytest.raises(Err) as error:
        state.call(state.operation, payload)
    assert error.value.value == Error(
        ErrorCode.INVALID, f"invalid {state.label} request",
    )


@pytest.mark.parametrize("bound", ["bytes", "nodes"])
@pytest.mark.parametrize("source_binding_double", ["stream-normalizer"], indirect=True)
def test_real_stream_projection_expansion_fails_without_truncated_success(
    source_binding_double: SimpleNamespace, bound: str,
) -> None:
    state = source_binding_double
    count = 5000 if bound == "bytes" else 6000
    request_id = "r" * 1000 if bound == "bytes" else "r"
    payload = {"request_id": request_id, "value": [
        {"type": "text_delta"} for _ in range(count)
    ] + [{"type": "finish"}]}
    encoded = state.codec.dumps(payload)
    assert len(encoded) < state.codec.MAX_BYTES
    with pytest.raises(Err) as error:
        state.call("normalize", encoded)
    assert error.value.value == Error(ErrorCode.INTERNAL,
                                      "stream normalizer provider output failed")


@pytest.mark.parametrize("source_binding_double", ["usage-cost"], indirect=True)
def test_maximum_data_integer_can_overflow_original_float_conversion(
    source_binding_double: SimpleNamespace,
) -> None:
    state = source_binding_double
    huge = int("9" * 4300)
    payload = {"usage": {"input_tokens": huge, "output_tokens": 1},
               "pricing": {"input": 1, "output": 1}}
    assert _differential(state, payload)[0:2] == ("failure", "OverflowError")
    with pytest.raises(Err) as error:
        state.call("calculate", state.codec.dumps(payload))
    assert error.value.value == Error(ErrorCode.INTERNAL,
                                      "usage cost provider arithmetic failed")


def test_arbitrary_python_objects_are_not_data_parity_claims(
    source_binding_double: SimpleNamespace,
) -> None:
    state = source_binding_double
    class CustomNumber:
        def __float__(self) -> float:
            return 3.0
    payload = ({"usage": {"input_tokens": CustomNumber(), "output_tokens": 1},
                "pricing": {"input": 1, "output": 1}}
               if state.provider == "usage-cost" else
               {"request_id": "r", "value": ({"type": "finish"},)})
    assert _differential(state, payload)[0] == "ok"
    with pytest.raises((TypeError, ValueError)):
        state.codec.dumps(payload)
