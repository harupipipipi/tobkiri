"""Labeled source binding doubles, NOT guest composition, Gate or Core proof.

Original-source oracles and the actual provider/caller Python adapters execute
locally. These tests cannot substitute for built guest/import-dispatch receipts.
"""

from __future__ import annotations

import ast
from copy import deepcopy
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
PACK = ROOT / "packs/local.ai-gateway-finalizer-python"
BRIDGE = ROOT / "packs/local.ai-tool-bridge-ordered-python"
TOOL = ROOT / "packs/local.ai-gateway-tool-caller-python"
USAGE = ROOT / "packs/local.ai-usage-cost-python"
STREAM = ROOT / "packs/local.ai-stream-normalizer-python"
SCRIPT = ROOT / "scripts/wasm/prepare_gateway_finalizer.py"


def _module(
    name: str, path: Path, monkeypatch: pytest.MonkeyPatch | None = None,
) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    if monkeypatch is not None:
        monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


prepare = _module("gateway_finalizer_extraction", SCRIPT)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_source_slices_rewrites_and_dependencies_are_exact(
    tmp_path: Path,
) -> None:
    checked = prepare.verify(PACK / "python")
    result = prepare.prepare(tmp_path)
    assert result == checked
    assert result["build_performed"] is False
    assert result["production_enabled"] is False
    assert result["protocol"] == "ordered-finite-json-v2"
    output = (tmp_path / result["output"]["path"]).read_bytes()
    assert _digest(output) == result["output"]["sha256"]
    for item in result["sources"] + result["transport_dependencies"]:
        content = (ROOT / item["path"]).read_bytes()
        assert len(content) == item["bytes"]
        assert _digest(content) == item["sha256"]
    for item in result["slices"] + result["rewrites"]:
        content = (ROOT / item["source_path"]).read_bytes()
        before = content[item["source_start_byte"]:item["source_end_byte"]]
        after = output[item["output_start_byte"]:item["output_end_byte"]]
        assert before == b"".join(content.splitlines(keepends=True)[
            item["source_start_line"] - 1:item["source_end_line"]
        ])
        assert after == b"".join(output.splitlines(keepends=True)[
            item["output_start_line"] - 1:item["output_end_line"]
        ])
        assert _digest(before) == item["sha256"]
        if item in result["rewrites"]:
            assert before.decode() == item["before_text"]
            assert after.decode() == item["after_text"]
            assert _digest(after) == item["after_sha256"]
            assert item["reason"]
        else:
            assert before == after
    assert {item["symbol"] for item in result["rewrites"]} == {
        "_tool_intents", "_maybe_packvm_safe_response",
    }
    assert _digest((tmp_path / "inventory.json").read_bytes()) == (
        result["inventory"]["sha256"]
    )
    assert {item["path"] for item in result["transport_dependencies"]} == {
        *prepare.CODEC_PATHS, prepare.CORE_WIT, *prepare.DEPENDENCY_WIT,
        prepare.TRANSITIVE_WIT, *prepare.DEPENDENCY_SOURCES,
    }


def test_nine_real_edges_and_reviewed_production_seam_are_bound() -> None:
    inventory = json.loads((PACK / "python/inventory.json").read_bytes())
    assert len(inventory["caller_edges"]) == 9
    assert [item["symbol"] for item in inventory["production_seam_calls"]] == [
        "_finalize_stream_result", "_finalize_generate_result",
    ]
    for edge in inventory["caller_edges"]:
        assert len(edge["declarations"]) == 2
        for item in edge["declarations"]:
            data = json.loads((ROOT / item["path"]).read_bytes())
            for key in item["json_pointer"].strip("/").split("/"):
                data = data[int(key)] if isinstance(data, list) else data[key]
            for key in ("caller_function_id", "contract_id", "operation_id",
                        "target_provider_id"):
                assert data[key] == edge[key]
    assert "_invoke" in inventory["not_migrated"]
    assert "production registration and activation" in inventory["not_migrated"]


def test_required_three_imports_typed_outcome_and_no_authority_implementation() -> None:
    wit = (PACK / "wit/pack.wit").read_text()
    for identity in (
        "local:ai-gateway-tool-caller-python/caller@1.0.0",
        "local:ai-usage-cost-python/usage-cost@1.0.0",
        "local:ai-stream-normalizer-python/stream-normalizer@1.0.0",
    ):
        assert f"import {identity};" in wit
    assert wit.count("result<finalization-outcome, error>") == 2
    assert "finalized(string)" in wit and "domain-rejected(domain-error)" in wit
    assert "include tobkiri:core/pack-minimal@1.0.0" in wit
    source = (PACK / "python/original_gateway_finalizer.py").read_text()
    tree = ast.parse(source)
    assert [node.module for node in tree.body if isinstance(node, ast.ImportFrom)] == [
        "__future__", "dataclasses", "typing",
    ]
    functions = {node.name for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    assert "_invoke" not in functions
    assert "_packvm_safe_response" not in functions
    assert "create_tool_intent_operation" not in functions
    assert "message_task_state" in functions and "Candidate" in functions
    for file in ("ordered_json.py", "ordered_json_v2.py"):
        assert not (PACK / file).exists() and not (PACK / "python" / file).exists()
    for file in (PACK / "python").glob("*.py"):
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(part in (node.module or "") for part in (
                    "host_provider", "core_runtime", "canonical", "socket",
                    "http", "subprocess", "original_bridge", "usage_cost_adapter",
                    "stream_normalizer_adapter", "gateway_tool_caller_adapter",
                ))


@pytest.mark.parametrize("path", [
    prepare.GATEWAY, prepare.LIFECYCLE, *prepare.CODEC_PATHS,
    *prepare.DEPENDENCY_WIT, prepare.TRANSITIVE_WIT, *prepare.DEPENDENCY_SOURCES,
])
def test_full_source_changes_invalidate_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    prepare.prepare(tmp_path)
    capture = prepare._capture
    monkeypatch.setattr(prepare, "_capture", lambda item: capture(item) + (
        b"\n# changed omitted source\n" if item == path else b""
    ))
    with pytest.raises(ValueError):
        prepare.verify(tmp_path)


@pytest.mark.parametrize("name", prepare.REWRITES)
def test_rewrite_refuses_unreviewed_changed_source(
    monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    capture = prepare._capture
    old = (b'    return _packvm_safe_response(value) if required else dict(value)'
           if name == "_maybe_packvm_safe_response" else
           b'    return [dict(item) for item in values if isinstance(item, Mapping)]')
    monkeypatch.setattr(prepare, "_capture", lambda path: capture(path).replace(
        old, old + b"  # changed source",
    ) if path == prepare.GATEWAY else capture(path))
    with pytest.raises(ValueError, match="unreviewed rewrite source"):
        prepare._prepared_files()


@pytest.mark.parametrize("kind", ["leaf", "ancestor", "hardlink", "fifo"])
def test_preparation_refuses_unsafe_output_without_writes(
    tmp_path: Path, kind: str,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("untouched")
    output = real
    leaf = real / "original_gateway_finalizer.py"
    if kind == "leaf":
        leaf.symlink_to(sentinel)
    elif kind == "ancestor":
        output = tmp_path / "link"
        output.symlink_to(real, target_is_directory=True)
    elif kind == "hardlink":
        os.link(sentinel, leaf)
    else:
        os.mkfifo(leaf)
    with pytest.raises(ValueError):
        prepare.prepare(output)
    assert sentinel.read_text() == "untouched"
    assert not (real / "inventory.json").exists()


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


@dataclass
class DomainError:
    code: str
    message: str


@dataclass
class Finalized:
    value: str


@dataclass
class DomainRejected:
    value: DomainError


def _original_gateway(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Compile original selected AST nodes independently, with no rewrites/Host."""
    gateway = ast.parse((ROOT / prepare.GATEWAY).read_bytes())
    lifecycle = ast.parse((ROOT / prepare.LIFECYCLE).read_bytes())
    dispatch = ast.parse((ROOT / prepare.DISPATCH).read_bytes())
    imports = [node for node in gateway.body if isinstance(node, ast.ImportFrom)
               and node.module in ("__future__", "dataclasses", "typing")]
    names = {*prepare.SYMBOLS, *prepare.REWRITES,
             "TOOL_BRIDGE_CONTRACT", "TOOL_BRIDGE_GENERATE_OPERATION",
             "TOOL_BRIDGE_STREAM_OPERATION"}

    def selected(tree: ast.Module, wanted: set[str]) -> list[ast.stmt]:
        return [node for node in tree.body if (
            isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted
        ) or (isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in wanted
            for target in node.targets
        ))]

    body = (imports + selected(dispatch, {"GlobalContractInvocationError"})
            + selected(lifecycle, set(prepare.TASK_STATE_SYMBOLS))
            + selected(gateway, names))
    module = ModuleType("independent_original_gateway_finalizer_oracle")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(ast.Module(body=body, type_ignores=[]), prepare.GATEWAY, "exec"),
         module.__dict__)
    return module


@pytest.fixture
def source_bindings(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Real Python source behavior under explicitly labeled binding doubles."""
    world = ModuleType("wit_world")
    exports = ModuleType("wit_world.exports")
    imports = ModuleType("wit_world.imports")
    for name in ("Lifecycle", "Finalizer", "Caller", "UsageCost",
                 "StreamNormalizer", "Bridge"):
        setattr(exports, name, type(name, (), {}))
    variant = ModuleType("wit_world.exports.finalizer")
    variant.DomainError = DomainError
    variant.FinalizationOutcome = Finalized | DomainRejected
    variant.FinalizationOutcome_Finalized = Finalized
    variant.FinalizationOutcome_DomainRejected = DomainRejected
    binding_types = ModuleType("wit_world.imports.types")
    binding_types.Error, binding_types.ErrorCode = Error, ErrorCode
    component_types = ModuleType("componentize_py_types")
    component_types.Err = Err
    world.exports, world.imports = exports, imports
    modules = [world, exports, imports, variant, binding_types, component_types]
    for name in ("caller", "usage_cost", "stream_normalizer", "bridge"):
        module = ModuleType(f"wit_world.imports.{name}")
        setattr(imports, name, module)
        modules.append(module)
    for module in modules:
        monkeypatch.setitem(sys.modules, module.__name__, module)
    for directory in (BRIDGE, BRIDGE / "python", TOOL / "python", USAGE / "python",
                      STREAM / "python", PACK / "python"):
        monkeypatch.syspath_prepend(str(directory))
    codec = _module("ordered_json_v2", BRIDGE / "ordered_json_v2.py", monkeypatch)
    for name, directory in (
        ("original_bridge", BRIDGE / "python"), ("caller_adapter", BRIDGE / "python"),
        ("caller_adapter_v2", BRIDGE / "python"),
        ("original_gateway_tool_caller", TOOL / "python"),
        ("gateway_tool_caller_adapter", TOOL / "python"),
        ("original_usage_cost", USAGE / "python"),
        ("usage_cost_adapter", USAGE / "python"),
        ("original_stream_normalizer", STREAM / "python"),
        ("stream_normalizer_adapter", STREAM / "python"),
        ("original_gateway_finalizer", PACK / "python"),
    ):
        _module(name, directory / f"{name}.py", monkeypatch)
    state = SimpleNamespace(codec=codec, calls=[], bridge_calls=[], overrides={},
                            failure_at=None, failure=None)
    bridge_app = _module("bridge_source_app_finalizer_fixture",
                         BRIDGE / "python/app_v2.py", monkeypatch).Bridge()

    def bridge_call(operation: str, payload: str) -> str:
        state.bridge_calls.append((operation, payload))
        return bridge_app.normalize(operation, payload)

    imports.bridge.normalize = bridge_call
    tool_app = _module("tool_source_app_finalizer_fixture", TOOL / "python/app.py",
                       monkeypatch).Caller()
    usage_app = _module("usage_source_app_finalizer_fixture", USAGE / "python/app.py",
                        monkeypatch).UsageCost()
    stream_app = _module(
        "stream_source_app_finalizer_fixture", STREAM / "python/app.py", monkeypatch,
    ).StreamNormalizer()

    def binding(label: str, operation: str, payload: str, function: Any) -> str:
        state.calls.append((label, operation, payload))
        if len(state.calls) == state.failure_at:
            raise state.failure
        if len(state.calls) in state.overrides:
            return state.overrides[len(state.calls)]
        return function()

    imports.caller.generate_intents = lambda payload: binding(
        "tool", "generate-intents", payload, lambda: tool_app.generate_intents(payload),
    )
    imports.caller.stream_intents = lambda payload: binding(
        "tool", "stream-intents", payload, lambda: tool_app.stream_intents(payload),
    )
    imports.usage_cost.calculate = lambda operation, payload: binding(
        "usage", operation, payload, lambda: usage_app.calculate(operation, payload),
    )
    imports.stream_normalizer.normalize = lambda operation, payload: binding(
        "stream", operation, payload, lambda: stream_app.normalize(operation, payload),
    )
    state.original = sys.modules["original_gateway_finalizer"]
    state.oracle = _original_gateway(monkeypatch)
    state.adapter = _module("gateway_finalizer_adapter",
                            PACK / "python/gateway_finalizer_adapter.py", monkeypatch)
    state.app = _module("finalizer_source_app_fixture", PACK / "python/app.py",
                        monkeypatch).Finalizer()
    state.usage_original = (
        sys.modules["original_usage_cost"].create_cost_operation(None)
    )
    state.stream_original = (
        sys.modules["original_stream_normalizer"].create_stream_normalize_operation(
            None,
        )
    )
    state.tool_original = (
        sys.modules["original_bridge"].create_tool_intent_operation(None)
    )
    return state


def _candidate() -> dict[str, Any]:
    return {
        "model_id": "model-recorded", "provider_instance_id": "provider-recorded",
        "catalog_provider_instance_id": "catalog-recorded", "catalog_revision": "r7",
        "capabilities": ["text", "tools"], "modalities": ["text"],
        "context_length": 16000, "input_cost": 0.1, "output_cost": 0.2,
        "priority": 3, "available": True, "health": "healthy",
        "health_observed_at": 12.5, "raw": {"currency": "EUR", "approved": True},
    }


def _payload(operation: str = "generate", value: Any = None) -> dict[str, Any]:
    result = {
        "value": {"output": "answer", "usage": {"input_tokens": 2, "output_tokens": 3},
                  "tool_intents": [{"name": "echo", "arguments": {"z": 1, "a": 2}}],
                  "finish_reason": "stop"} if value is None else value,
        "request_id": " request-id ", "candidate": _candidate(),
        "attempts": [{"attempt": 1, "error_code": "provider_unavailable"}],
        "packvm_response": False,
    }
    if operation == "stream":
        result["attempt_number"] = 2
        if value is None:
            result["value"]["events"] = [
                {"type": "tool_intent_delta", "tool_intent": {"name": "first"}},
                {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 3}},
                {"type": "text_delta", "delta": "delta"},
                {"type": "tool_intent_delta", "tool_intent": {"name": "second"}},
                {"type": "usage",
                 "usage": {"prompt_tokens": 4, "completion_tokens": 5}},
                {"type": "finish", "finish_reason": "stop"},
            ]
    return result


def _oracle(state: SimpleNamespace, operation: str, data: dict[str, Any]) -> str:
    oracle = state.oracle

    class OriginalSourceDispatch:
        """Only an oracle dispatch double, not a Host or production implementation."""

        def invoke(self, contract: str, identity: str, payload: Any) -> Any:
            if contract == oracle.USAGE_CONTRACT:
                return state.usage_original(identity, payload)
            if contract == oracle.STREAM_NORMALIZE_CONTRACT:
                return state.stream_original(identity, payload)
            assert contract == oracle.TOOL_BRIDGE_CONTRACT
            return state.tool_original("normalize", payload)

    candidate = oracle.Candidate(**{
        **data["candidate"],
        "capabilities": frozenset(data["candidate"]["capabilities"]),
        "modalities": frozenset(data["candidate"]["modalities"]),
    })
    arguments = (OriginalSourceDispatch(), data["value"], data["request_id"], candidate)
    if operation == "generate":
        result = oracle._finalize_generate_result(
            *arguments, data["attempts"], streaming=False, packvm_response=False,
        )
    else:
        result = oracle._finalize_stream_result(
            *arguments, data["attempt_number"], data["attempts"], packvm_response=False,
        )
    return state.codec.dumps(result)


@pytest.mark.parametrize("operation", ["generate", "stream"])
def test_success_matches_original_byte_and_order_semantics(
    source_bindings: SimpleNamespace, operation: Any,
) -> None:
    state = source_bindings
    data = _payload(operation)
    data["value"]["output"] = {"float": 1.0, "negative_zero": -0.0,
                              "large": 2**64, "nested": {"z": 2, "a": 1}}
    expected = _oracle(state, operation, deepcopy(data))
    actual = getattr(state.app, operation)(state.codec.dumps(data))
    assert isinstance(actual, Finalized) and actual.value == expected
    parsed = state.codec.loads(actual.value)
    assert list(parsed["output"]) == list(data["value"]["output"])
    assert type(parsed["output"]["float"]) is float
    assert math.copysign(1, parsed["output"]["negative_zero"]) == -1
    assert parsed["model_id"] == "model-recorded"
    assert parsed["provider_instance_id"] == "provider-recorded"
    assert parsed["attempts"] == data["attempts"]
    if operation == "generate":
        assert parsed["catalog_provider_instance_id"] == "catalog-recorded"
        assert parsed["usage_cost"]["pricing_revision"] == "r7"
        assert parsed["usage_cost"]["currency"] == "EUR"
    assert all(item["approved"] is False for item in parsed["tool_intents"])


@pytest.mark.parametrize("metadata,state_name", [
    ({"task_state": "completed"}, "completed"),
    ({"task_state": "idle"}, "unknown"),
    ({"task_state": "waiting_user"}, "waiting_user"),
    ({"approval_required": True}, "waiting_approval"),
    ({"thinking": {"state": "running"}}, "running"),
    ({"cancelled": True}, "cancelled"),
    ({"task_state": "failed"}, "failed"),
])
def test_original_task_state_projection(
    source_bindings: SimpleNamespace, metadata: Any, state_name: Any,
) -> None:
    state = source_bindings
    data = _payload()
    data["value"]["metadata"] = metadata
    expected = _oracle(state, "generate", deepcopy(data))
    actual = state.app.generate(state.codec.dumps(data)).value
    assert actual == expected
    assert state.codec.loads(actual)["task_state"] == state_name
    assert "metadata" not in state.codec.loads(actual)


def test_dependency_order_and_exact_operation_identities(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    data = _payload("stream")
    state.app.stream(state.codec.dumps(data))
    assert [(kind, operation) for kind, operation, _ in state.calls] == [
        ("stream", "rumi_ai_stream_pack.ai-stream-normalize"),
        ("usage", "rumi_ai_usage_pack.ai-usage-cost.stream"),
        ("usage", "rumi_ai_usage_pack.ai-usage-cost.stream"),
        ("tool", "stream-intents"), ("tool", "stream-intents"),
        ("tool", "stream-intents"),
    ]
    normalize = state.codec.loads(state.calls[0][2])
    assert list(normalize) == ["request_id", "provider_attempt", "value"]
    assert normalize["provider_attempt"] == 2
    usage = state.codec.loads(state.calls[1][2])
    assert usage["pricing"] == {"input": 0.1, "output": 0.2,
                                "currency": "EUR", "unit": "usd_per_token"}
    assert [state.codec.loads(call[2])["intents"][0]["name"]
            for call in state.calls[3:]] == ["first", "second", "echo"]
    state.calls.clear()
    state.app.generate(state.codec.dumps(_payload()))
    assert [(kind, operation) for kind, operation, _ in state.calls] == [
        ("tool", "generate-intents"),
        ("usage", "rumi_ai_usage_pack.ai-usage-cost.generate"),
    ]


@pytest.mark.parametrize("code", list(ErrorCode))
@pytest.mark.parametrize("operation,position", [
    ("generate", 1), ("generate", 2), *[("stream", n) for n in range(1, 7)],
])
def test_every_imported_error_arm_is_exact_terminal_and_never_domain(
    source_bindings: SimpleNamespace, code: Any, operation: Any, position: Any,
) -> None:
    state = source_bindings
    record = Error(code, "exact upstream message: \u2603")
    state.failure_at, state.failure = position, Err(record)
    payload = state.codec.dumps(_payload(operation))
    with pytest.raises(Err) as caught:
        getattr(state.app, operation)(payload)
    assert caught.value.value is record
    assert len(state.calls) == position
    state.calls.clear()
    with pytest.raises(state.adapter.ImportedDependencyFailure) as internal:
        state.adapter.invoke(operation, payload)
    assert internal.value.core_error is record
    assert internal.value.retryable is False
    assert not isinstance(internal.value, state.original.GlobalContractInvocationError)
    assert len(state.calls) == position


@pytest.mark.parametrize("provider,code,message", [
    ({"status": "error", "error_code": "invalid", "message": "domain invalid"},
     "invalid", "domain invalid"),
    ({"status": "error", "error_code": "unavailable", "message": "domain unavailable"},
     "unavailable", "domain unavailable"),
    ({"status": "error"}, "provider_unavailable", "provider failed"),
    ([], "invalid_response", "provider result must be an object"),
    (3, "invalid_response", "provider result must be an object"),
])
def test_original_domain_rejection_is_not_outer_core_error(
    source_bindings: SimpleNamespace, provider: Any, code: Any, message: Any,
) -> None:
    state = source_bindings
    data = _payload(value=provider)
    actual = state.app.generate(state.codec.dumps(data))
    assert actual == DomainRejected(DomainError(code, message))
    assert state.calls == []
    with pytest.raises(state.oracle.GlobalContractInvocationError) as original:
        _oracle(state, "generate", data)
    assert original.value.code == code and str(original.value) == message


@pytest.mark.parametrize("operation,position,reply", [
    ("generate", 1, None), ("generate", 1, "{}"), ("generate", 1, "[null]"),
    ("generate", 1, "not json"), ("generate", 1, "[NaN]"),
    ("generate", 1, '[{"a":1,"a":2}]'),
    ("generate", 2, "[]"), ("generate", 2, "null"),
    ("stream", 1, "{}"), ("stream", 1, '{"events":null}'),
    ("stream", 1, '{"events":[null]}'),
    ("stream", 4, "[]"),
])
def test_malformed_success_is_terminal_and_not_domain(
    source_bindings: SimpleNamespace, operation: Any, position: Any, reply: Any,
) -> None:
    state = source_bindings
    state.overrides[position] = reply
    with pytest.raises(Err) as caught:
        getattr(state.app, operation)(state.codec.dumps(_payload(operation)))
    assert caught.value.value == Error(
        ErrorCode.INTERNAL, "Gateway finalization failed",
    )
    assert len(state.calls) == position


def test_usage_success_mapping_is_not_rewritten_or_recomputed(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    state.overrides[2] = '{"future":"kept","zero":-0.0}'
    actual = state.codec.loads(state.app.generate(state.codec.dumps(_payload())).value)
    assert actual["usage_cost"] == {"future": "kept", "zero": -0.0}
    assert math.copysign(1, actual["usage_cost"]["zero"]) == -1


@pytest.mark.parametrize("failure", [RuntimeError("private transport"),
                                     ValueError("private parser")])
def test_unknown_import_failure_does_not_become_retryable_domain(
    source_bindings: SimpleNamespace, failure: Any,
) -> None:
    state = source_bindings
    state.failure_at, state.failure = 1, failure
    with pytest.raises(state.adapter.DependencyTransportFailure) as caught:
        state.adapter.invoke("generate", state.codec.dumps(_payload()))
    assert caught.value.retryable is False
    assert "private" not in str(caught.value)
    assert len(state.calls) == 1


@pytest.mark.parametrize("mutate", [
    lambda data: data.update(packvm_response=True),
    lambda data: data.update(packvm_response=0),
    lambda data: data.pop("packvm_response"),
    lambda data: data.update(extra="not supported"),
    lambda data: data.update(request_id=3),
    lambda data: data.update(attempts=[None]),
    lambda data: data["candidate"].update(input_cost=True),
    lambda data: data["candidate"].update(priority=1.0),
    lambda data: data["candidate"].update(modalities=[None]),
    lambda data: data["candidate"].update(raw=[]),
    lambda data: data["candidate"].update(extra=True),
])
def test_invalid_boundary_fields_call_no_dependency(
    source_bindings: SimpleNamespace, mutate: Any,
) -> None:
    state = source_bindings
    data = _payload()
    mutate(data)
    with pytest.raises(Err) as caught:
        state.app.generate(state.codec.dumps(data))
    assert caught.value.value.code is ErrorCode.INVALID
    assert state.calls == []


@pytest.mark.parametrize("attempt", [False, 0, -1, 1.0, "1"])
def test_invalid_stream_attempt_is_not_authority_or_fallback(
    source_bindings: SimpleNamespace, attempt: Any,
) -> None:
    state = source_bindings
    data = _payload("stream")
    data["attempt_number"] = attempt
    with pytest.raises(Err) as caught:
        state.app.stream(state.codec.dumps(data))
    assert caught.value.value.code is ErrorCode.INVALID
    assert state.calls == []


@pytest.mark.parametrize("text", ["{}", "[]", '{"a":1,"a":2}', "NaN", "1e9999",
                                  "9" * 4301, " " * (4 * 1024 * 1024 + 1)],
                         ids=["empty", "array", "duplicate", "nan", "infinity",
                              "integer-limit", "byte-limit"])
def test_codec_failures_call_no_dependency(
    source_bindings: SimpleNamespace, text: Any,
) -> None:
    state = source_bindings
    with pytest.raises(Err) as caught:
        state.app.generate(text)
    assert caught.value.value.code is ErrorCode.INVALID
    assert state.calls == []


@pytest.mark.parametrize("exception", [RecursionError, MemoryError])
def test_injected_local_resource_mapping_is_limit_exceeded(
    source_bindings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, exception: Any,
) -> None:
    state = source_bindings

    def exhausted(_: str) -> Any:
        raise exception("private resource detail")

    monkeypatch.setattr(state.adapter, "loads", exhausted)
    with pytest.raises(Err) as caught:
        state.app.generate("{}")
    assert caught.value.value.code is ErrorCode.LIMIT_EXCEEDED
    assert "private" not in caught.value.value.message
    assert state.calls == []


def test_large_exact_output_integer_and_default_runtime_settings(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    before = (sys.getrecursionlimit(), sys.get_int_max_str_digits())
    data = _payload()
    data["value"]["output"] = 10**4299
    actual = state.app.generate(state.codec.dumps(data)).value
    assert state.codec.loads(actual)["output"] == 10**4299
    assert before == (sys.getrecursionlimit(), sys.get_int_max_str_digits())
    assert before[1] == 4300


@pytest.mark.parametrize("usage,price,expected", [
    ({"input_tokens": 10**400, "output_tokens": 1}, 1, "arithmetic"),
    ({"input_tokens": 1e308, "output_tokens": 1e308}, 1e308, "arithmetic"),
])
def test_usage_overflow_is_imported_failure_not_zero_or_domain(
    source_bindings: SimpleNamespace, usage: Any, price: Any, expected: Any,
) -> None:
    state = source_bindings
    data = _payload()
    data["value"]["usage"] = usage
    data["candidate"]["input_cost"] = price
    with pytest.raises(Err) as caught:
        state.app.generate(state.codec.dumps(data))
    assert caught.value.value == Error(
        ErrorCode.INTERNAL, f"usage cost provider {expected} failed",
    )
    assert len(state.calls) == 2


@pytest.mark.parametrize("usage", [{}, {"input_tokens": -1, "output_tokens": 2},
                                    {"input_tokens": "NaN", "output_tokens": 2}])
def test_original_unknown_usage_stays_unknown_not_zero(
    source_bindings: SimpleNamespace, usage: Any,
) -> None:
    state = source_bindings
    data = _payload()
    data["value"]["usage"] = usage
    actual = state.app.generate(state.codec.dumps(data)).value
    assert actual == _oracle(state, "generate", deepcopy(data))
    cost = state.codec.loads(actual)["usage_cost"]
    assert cost["known"] is False and cost["cost"] is None


@pytest.mark.parametrize("events", [[], [{"type": "text_delta"}],
    [{"type": "finish"}, {"type": "text_delta"}],
    [{"type": "error"}, {"type": "finish"}], [{"type": "unknown"}], [None]])
def test_stream_terminal_rules_are_enforced_by_real_source_dependency(
    source_bindings: SimpleNamespace, events: Any,
) -> None:
    state = source_bindings
    data = _payload("stream")
    data["value"]["events"] = events
    with pytest.raises(Err) as caught:
        state.app.stream(state.codec.dumps(data))
    assert caught.value.value == Error(
        ErrorCode.INTERNAL, "stream normalizer provider validation failed",
    )
    assert len(state.calls) == 1


def test_error_terminal_is_preserved_without_synthetic_finish(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    data = _payload("stream")
    data["value"]["events"] = [{"type": "error", "error_code": "provider_failed"}]
    actual = state.app.stream(state.codec.dumps(data)).value
    assert actual == _oracle(state, "stream", deepcopy(data))
    events = state.codec.loads(actual)["events"]
    assert len(events) == 1 and events[0]["type"] == "error"


def test_source_loop_partial_mutation_stops_at_failure_without_rollback(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    events = [
        {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 2}},
        {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 4}},
        {"type": "tool_intent_delta", "tool_intent": {"name": "first"}},
        {"type": "tool_intent_delta", "tool_intent": {"name": "second"}},
        {"type": "tool_intent_delta", "tool_intent": {"name": "third"}},
    ]
    originals = [event.get("tool_intent") for event in events]
    client = state.adapter.ImportedDependencies()
    candidate = state.adapter._candidate(_candidate())
    record = Error(ErrorCode.CANCELLED, "cancelled at second tool")
    state.failure_at, state.failure = 4, Err(record)
    state.original._attach_stream_usage_cost(client, events, candidate)
    with pytest.raises(state.adapter.ImportedDependencyFailure) as caught:
        state.original._attach_stream_tool_intents(client, events, "r")
    assert caught.value.core_error is record
    assert all("usage_cost" in event for event in events[:2])
    assert events[2]["tool_intent"] is not originals[2]
    assert events[2]["tool_intent"]["intent_id"] == "r:tool:0"
    assert events[3]["tool_intent"] is originals[3]
    assert events[4]["tool_intent"] is originals[4]
    assert len(state.calls) == 4


def test_tool_caller_mapping_filter_and_event_fallback_index_remain_original(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    data = _payload("stream")
    actual = state.codec.loads(state.app.stream(state.codec.dumps(data)).value)
    intents = [event["tool_intent"] for event in actual["events"]
               if event["type"] == "tool_intent_delta"]
    assert [intent["intent_id"] for intent in intents] == [
        "request-id:tool:0", "request-id:tool:0",
    ]
    # Original _tool_intents' Mapping filtering runs inside the imported caller.
    imports = sys.modules["wit_world.imports"]
    original_bridge = imports.bridge.normalize
    imports.bridge.normalize = lambda operation, payload: (
        '{"intents":[null,4,{"z":1.0,"a":-0.0},"skip"]}'
    )
    try:
        result = state.codec.loads(
            state.app.generate(state.codec.dumps(_payload())).value,
        )
        assert result["tool_intents"] == [{"z": 1.0, "a": -0.0}]
    finally:
        imports.bridge.normalize = original_bridge


def test_unsupported_dependencies_and_operations_do_not_dispatch(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    for contract, operation in (("other", "normalize"),
        (state.original.USAGE_CONTRACT, "calculate"),
        (state.original.STREAM_NORMALIZE_CONTRACT, "validate")):
        with pytest.raises(state.adapter.FinalizerInputError):
            state.adapter.ImportedDependencies().invoke(contract, operation, {})
    with pytest.raises(state.adapter.FinalizerInputError):
        state.adapter.invoke("text-generate", state.codec.dumps(_payload()))
    assert state.calls == []


def test_packvm_transport_is_rejected_not_silently_downgraded(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    for required in (True, None, 0, "false"):
        with pytest.raises(ValueError, match="Host PackVM"):
            state.original._maybe_packvm_safe_response({"cost": 0.5}, required)
    assert state.original._maybe_packvm_safe_response({"cost": 0.5}, False) == {
        "cost": 0.5,
    }
    assert state.calls == []


def test_runtime_diagnostic_has_no_dependency_call(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    assert state.app.runtime_info() == state.codec.dumps({
        "python": platform.python_version(), "unicode": unicodedata.unidata_version,
    })
    assert state.calls == []


@pytest.mark.parametrize("intents", [
    None, {}, {"name": "ignored"}, 0, "ignored", False,
])
def test_stream_non_list_top_level_intents_reach_original_fallback(
    source_bindings: SimpleNamespace, intents: Any,
) -> None:
    state = source_bindings
    data = _payload("stream")
    data["value"]["tool_intents"] = intents
    actual = state.app.stream(state.codec.dumps(data)).value
    assert actual == _oracle(state, "stream", deepcopy(data))
    assert state.codec.loads(actual)["tool_intents"] == []
    # The finalizer sends DATA unchanged; fallback belongs to the real provider.
    assert state.codec.loads(state.calls[-1][2])["intents"] == intents


def test_real_native_json_depth_exhaustion_is_not_invalid_or_domain(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    data = _payload()
    prefix = state.codec.dumps(data)
    raw = prefix.replace('"answer"', "[" * 10000 + "0" + "]" * 10000)
    assert len(raw) < state.codec.MAX_BYTES
    with pytest.raises(Err) as caught:
        state.app.generate(raw)
    assert caught.value.value.code is ErrorCode.LIMIT_EXCEEDED
    assert state.calls == []


def test_actual_stream_projection_node_expansion_is_terminal(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    data = _payload("stream")
    data["value"]["events"] = [
        {"type": "text_delta"} for _ in range(6000)
    ] + [{"type": "finish"}]
    raw = state.codec.dumps(data)
    assert len(raw) < state.codec.MAX_BYTES
    with pytest.raises(Err) as caught:
        state.app.stream(raw)
    assert caught.value.value == Error(
        ErrorCode.INTERNAL, "stream normalizer provider output failed",
    )
    assert len(state.calls) == 1


def test_actual_finalizer_node_expansion_after_usage_calls_is_terminal(
    source_bindings: SimpleNamespace,
) -> None:
    state = source_bindings
    data = _payload("stream")
    data["value"]["events"] = [
        {"type": "usage", "usage": {}} for _ in range(3000)
    ] + [{"type": "finish"}]
    raw = state.codec.dumps(data)
    with pytest.raises(Err) as caught:
        state.app.stream(raw)
    assert caught.value.value == Error(
        ErrorCode.INTERNAL, "Gateway finalization failed",
    )
    assert len(state.calls) == 3002


@pytest.mark.parametrize("position", ["encode", "invoke", "decode"])
@pytest.mark.parametrize("failure", [MemoryError, RecursionError])
def test_injected_resource_mapping_at_each_exchange_phase(
    source_bindings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, position: Any,
    failure: Any,
) -> None:
    """Exception mapping only; this is not evidence of real guest exhaustion."""
    state = source_bindings

    def exhausted(*_: Any, **__: Any) -> Any:
        raise failure("private mapping fixture detail")

    payload = state.codec.dumps(_payload())
    if position == "encode":
        monkeypatch.setattr(state.adapter, "dumps", exhausted)
    elif position == "invoke":
        state.failure_at, state.failure = 1, failure("private mapping fixture detail")
    else:
        original_loads = state.adapter.loads
        calls = 0

        def fail_reply(text: str) -> Any:
            nonlocal calls
            calls += 1
            return original_loads(text) if calls == 1 else exhausted()

        monkeypatch.setattr(state.adapter, "loads", fail_reply)
    with pytest.raises(Err) as caught:
        state.app.generate(payload)
    assert caught.value.value.code is ErrorCode.LIMIT_EXCEEDED
    assert len(state.calls) == (0 if position == "encode" else 1)
    assert "private" not in caught.value.value.message


def test_malformed_internal_domain_record_cannot_cross_wit(
    source_bindings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = source_bindings

    def bad_domain(*_: Any, **__: Any) -> Any:
        raise state.original.GlobalContractInvocationError(4, "malformed fixture")

    monkeypatch.setattr(state.adapter, "_finalize_generate_result", bad_domain)
    with pytest.raises(Err) as caught:
        state.app.generate(state.codec.dumps(_payload()))
    assert caught.value.value == Error(
        ErrorCode.INTERNAL, "Gateway finalization failed",
    )
    assert state.calls == []


REPLAY_CASES = json.loads((PACK / "source-cases.json").read_bytes())["cases"]


@pytest.mark.parametrize("case", REPLAY_CASES, ids=lambda case: case["id"])
def test_deterministic_catalog_uses_real_source_dependencies(
    source_bindings: SimpleNamespace, case: dict[str, Any],
) -> None:
    state = source_bindings
    raw = state.codec.dumps(case["payload"])
    if case["expected_kind"] == "core-error":
        with pytest.raises(Err) as caught:
            getattr(state.app, case["operation"])(raw)
        assert caught.value.value.code.value == case["expected_error"]["code"]
        assert caught.value.value.message == case["expected_error"]["message"]
    else:
        actual = getattr(state.app, case["operation"])(raw)
        if case["expected_kind"] == "domain-rejected":
            assert actual == DomainRejected(DomainError(**case["expected_error"]))
        else:
            assert actual.value == _oracle(
                state, case["operation"], deepcopy(case["payload"]),
            )
    assert len(state.calls) == case["expected_dependency_call_count"]
