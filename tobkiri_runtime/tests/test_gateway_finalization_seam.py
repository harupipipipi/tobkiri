"""Freeze the production Gateway success seam against the pre-extraction source.

The branch bodies below are frozen from 92f38e3241b802605a4af59ba35aae72ee73cdbd.
They are test oracles, never runtime replacements. The existing Host boundary
suite separately exercises real Broker error custody; these offline tests cover
helper extraction, operation order, reference identity, and partial mutation.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from copy import deepcopy
import hashlib
import inspect
import json
import socket
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from core_runtime.global_contract_dispatch import (
    GlobalContractInvocationError,
    GlobalContractUnavailable,
)
from ecosystem.rumi_ai_gateway_pack.runtime import gateway
from tests.test_ai_gateway_pack import FakeContractClient
from tobkiri_host.errors import AuthorizationError, ProviderExecutionError
from tobkiri_protocol.canonical import canonical_json


_DECISION_TIME = 1_700_000_000.0
_REQUEST_ID = "gateway-finalization-request"
_FROZEN_BRANCHES = '''
def baseline_generate(
    client, value, request_id, attempt_candidate, attempts, *,
    streaming, packvm_response,
):
    result = _normalize_result(value, request_id, attempt_candidate)
    result["tool_intents"] = _tool_intents(
        client,
        result["tool_intents"],
        request_id,
        streaming=streaming,
    )
    result["usage_cost"] = _usage_cost(
        client,
        result["usage"],
        attempt_candidate,
        result["usage_provenance"],
        streaming=streaming,
    )
    result["attempts"] = attempts
    return _maybe_packvm_safe_response(result, packvm_response)


def baseline_stream(
    client, value, request_id, attempt_candidate, attempt_number, attempts, *,
    packvm_response,
):
    normalized = client.invoke(
        STREAM_NORMALIZE_CONTRACT,
        STREAM_NORMALIZE_OPERATION,
        {
            "request_id": request_id,
            "provider_attempt": attempt_number,
            "value": value,
        },
    )
    events = (
        normalized.get("events")
        if isinstance(normalized, Mapping)
        else None
    )
    if not isinstance(events, list):
        raise GlobalContractInvocationError(
            "invalid_response",
            "stream normalizer returned an invalid result",
        )
    _attach_stream_usage_cost(client, events, attempt_candidate)
    _attach_stream_tool_intents(client, events, request_id)
    return _maybe_packvm_safe_response({
        "status": "ok", "output": value.get("output", ""),
        "tool_intents": _tool_intents(
            client, value.get("tool_intents", []), request_id,
            streaming=True,
        ),
        "finish_reason": value.get("finish_reason"),
        "delivery_mode": value.get("delivery_mode", "buffered"),
        "request_id": request_id,
        "model_id": attempt_candidate.model_id,
        "provider_instance_id": attempt_candidate.provider_instance_id,
        "events": events,
        "attempts": attempts,
    }, packvm_response)
'''

_BASELINE_AST_DIGESTS = {
    '_invoke': (
        'b7000e901ec4476b3630512e566859f034162b83bdbf8fe2c9a39cedd85cab0c'
    ),
    'create_generate_operation': (
        '9c7d2157cd08f9426ca4c6ab0bef5a1979311b2b884c60ee01ba8221302233af'
    ),
    'create_stream_operation': (
        'b2c2d10c17f274f1e59bf078ebcf66c9d7a2bbc7d387fa8598431cd9fc46e436'
    ),
    'AIGatewayHostFactoryV4': (
        'ac22e916f8ff100833720d810c2a732b948a0eced1f2f4c7a5d69e7d7dbc1432'
    ),
    '_normalize_result': (
        '6449662a43710b5352ce8bb29ccb47a07ecb41a7e9214f51e606970c7903f4b6'
    ),
    '_attach_stream_usage_cost': (
        '00717c7e27409c3890d7135dc36276bf446afa1a80bca95171607ec846c3e2bd'
    ),
    '_attach_stream_tool_intents': (
        'ce57093411e1c309f2807841869ea6e20d5d0d3753c1fab318d62c08d57a584e'
    ),
    '_tool_intents': (
        '5a6f62a2d527c527ba2c60963095bbde839224de07d6aecfd0e473d065994eea'
    ),
    '_usage_cost': (
        '8d8e52af28cf9ed3a686c88e6b793b4044dd611930f821efecf0af6711e044fa'
    ),
    '_maybe_packvm_safe_response': (
        'bd3c8faab64cecefd36fae8e856d8b7e89f3a7155f4ca63c95ac6fa089f22b31'
    ),
    '_packvm_safe_response': (
        'f664c658ed8237b3f773b14ae84396d97780594339f8df242e8ccf2c53dda615'
    ),
    '_packvm_safe_value': (
        '396ebb55b9d6bf69d615f4a8b0fffe40d2cee6d3b1ffb6cb6528e1ac197ccc08'
    ),
}


@pytest.fixture(autouse=True)
def offline_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No provider traffic, shared clocks, or persistent diagnostic changes."""
    attempts: list[str] = []

    def forbid_network(*_args: object, **_kwargs: object) -> NoReturn:
        attempts.append("socket")
        raise AssertionError("network is forbidden in finalization tests")

    for name in ("socket", "create_connection", "socketpair"):
        monkeypatch.setattr(socket, name, forbid_network)
    monkeypatch.setattr(
        gateway, "time", SimpleNamespace(time=lambda: _DECISION_TIME)
    )
    monkeypatch.setattr(gateway, "_DIAGNOSTICS", [])
    yield
    assert not attempts


@pytest.fixture
def candidate() -> gateway.Candidate:
    return gateway.Candidate(
        model_id="model-a",
        provider_instance_id="adapter-a",
        catalog_provider_instance_id="catalog-main",
        catalog_revision="catalog-r1",
        capabilities=frozenset({"tool_calling"}),
        modalities=frozenset({"text"}),
        context_length=128000,
        input_cost=0.25,
        output_cost=0.5,
        priority=10,
        available=True,
        health="healthy",
        health_observed_at=_DECISION_TIME,
        raw={"currency": "USD"},
    )


class FinalizationClient(FakeContractClient):
    """Return owned fixture objects so copying and partial mutation are visible."""

    def __init__(
        self, fail_at: int | None = None, error: Exception | None = None
    ) -> None:
        super().__init__()
        self.value = {
            "output": {"numbers": [1.25, 2]},
            "usage": {"input_tokens": 2.5, "output_tokens": 1},
            "tool_intents": [self.intent("top")],
            "finish_reason": "tool_calls",
        }
        self.events = [
            {"type": "tool_intent_delta", "tool_intent": self.intent("first")},
            {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 1}},
            {"type": "tool_intent_delta", "tool_intent": self.intent("second")},
            {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 2}},
            {"type": "finish", "finish_reason": "tool_calls"},
        ]
        self.normalized: Any = {"events": self.events}
        self.fail_at = fail_at
        self.error = error or ProviderExecutionError("fixture dependency failed")
        self.dependency_calls = 0

    @staticmethod
    def intent(identifier: str) -> dict[str, Any]:
        return {
            "id": identifier,
            "name": "echo",
            "arguments": {"decimal": 0.125, "integer": 7},
        }

    def invoke(
        self,
        contract_id: str,
        operation: str,
        payload: dict[str, Any],
        *,
        provider_instance_id: str | None = None,
    ) -> Any:
        if contract_id in {
            gateway.STREAM_NORMALIZE_CONTRACT,
            gateway.TOOL_BRIDGE_CONTRACT,
            gateway.USAGE_CONTRACT,
        }:
            self.dependency_calls += 1
            if self.dependency_calls == self.fail_at:
                self.calls.append(
                    (contract_id, operation, provider_instance_id, dict(payload))
                )
                raise self.error
        if contract_id in {
            gateway.GENERATE_PROVIDER_CONTRACT,
            gateway.STREAM_PROVIDER_CONTRACT,
            gateway.STREAM_NORMALIZE_CONTRACT,
        }:
            self.calls.append(
                (contract_id, operation, provider_instance_id, dict(payload))
            )
            if contract_id == gateway.STREAM_NORMALIZE_CONTRACT:
                return self.normalized
            return self.value
        result = super().invoke(
            contract_id, operation, payload, provider_instance_id=provider_instance_id
        )
        if contract_id == gateway.HEALTH_CONTRACT:
            for provider in result["providers"]:
                provider["observed_at"] = _DECISION_TIME
        return result


def finalize(
    mode: str,
    baseline: bool,
    client: FinalizationClient,
    candidate: gateway.Candidate,
    attempts: list[dict[str, Any]],
    *,
    packvm_response: bool = False,
    streaming: Any = False,
) -> dict[str, Any]:
    namespace = vars(gateway).copy()
    if baseline:
        exec(compile(_FROZEN_BRANCHES, "<frozen-gateway-branches>", "exec"), namespace)
        function = namespace[f"baseline_{mode}"]
    else:
        function = getattr(gateway, f"_finalize_{mode}_result")
    if mode == "stream":
        return function(
            client, client.value, _REQUEST_ID, candidate, 2, attempts,
            packvm_response=packvm_response,
        )
    return function(
        client, client.value, _REQUEST_ID, candidate, attempts,
        streaming=streaming, packvm_response=packvm_response,
    )


def _function_ast(source: str) -> ast.FunctionDef:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def _ast_digest(node: ast.AST) -> str:
    """Ignore version-specific empty AST fields, never executable syntax."""
    def stable(value: Any) -> Any:
        if isinstance(value, ast.AST):
            return [type(value).__name__, [
                [name, stable(field)] for name, field in ast.iter_fields(value)
                if field is not None and field != []
            ]]
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    encoded = json.dumps(stable(node), separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_frozen_source_identity_and_exact_branch_extraction() -> None:
    """Reinsert the frozen branches to prove surrounding _invoke stayed intact."""
    branches = {
        node.name: node for node in ast.parse(_FROZEN_BRANCHES).body
        if isinstance(node, ast.FunctionDef)
    }
    for mode in ("generate", "stream"):
        actual = _function_ast(
            inspect.getsource(getattr(gateway, f"_finalize_{mode}_result"))
        )
        assert ast.dump(ast.Module(actual.body[1:], [])) == ast.dump(
            ast.Module(branches[f"baseline_{mode}"].body, [])
        )

    class RestoreBranches(ast.NodeTransformer):
        def visit_If(self, node: ast.If) -> ast.If:
            if (
                isinstance(node.test, ast.Name)
                and node.test.id == "streaming"
                and isinstance(node.body[0], ast.Return)
                and isinstance(node.body[0].value, ast.Call)
                and isinstance(node.body[0].value.func, ast.Name)
                and node.body[0].value.func.id == "_finalize_stream_result"
            ):
                node.body = branches["baseline_stream"].body
                return node
            return self.generic_visit(node)

        def visit_Return(self, node: ast.Return) -> Any:
            if (
                isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_finalize_generate_result"
            ):
                return branches["baseline_generate"].body
            return node

    for name, expected in _BASELINE_AST_DIGESTS.items():
        node = ast.parse(inspect.getsource(getattr(gateway, name))).body[0]
        if name == "_invoke":
            node = RestoreBranches().visit(node)
        assert _ast_digest(node) == expected, name
    for identifier, factory in (
        (gateway._GENERATE_FUNCTION_ID, gateway.create_generate_operation),
        (gateway._STREAM_FUNCTION_ID, gateway.create_stream_operation),
    ):
        assert (
            gateway.HOST_PROVIDER_FACTORY[identifier]._operation_factory is factory
        )


@pytest.mark.parametrize("mode", ["generate", "stream"])
@pytest.mark.parametrize("packvm_response", [False, True])
def test_frozen_success_order_identity_and_numeric_transport(
    mode: str, packvm_response: bool, candidate: gateway.Candidate
) -> None:
    runs = []
    for baseline in (True, False):
        client = FinalizationClient()
        original_value = deepcopy(client.value)
        original_events = list(client.events)
        attempts = [{"attempt": 1, "error_code": "provider_unavailable"}]
        result = finalize(
            mode, baseline, client, candidate, attempts,
            packvm_response=packvm_response,
        )
        assert client.value == original_value
        assert (result["attempts"] is attempts) is not packvm_response
        assert (result["output"] is client.value["output"]) is not packvm_response
        if mode == "stream":
            assert (result["events"] is client.events) is not packvm_response
            assert all(a is b for a, b in zip(client.events, original_events))
            assert [call[0] for call in client.calls] == [
                gateway.STREAM_NORMALIZE_CONTRACT,
                gateway.USAGE_CONTRACT, gateway.USAGE_CONTRACT,
                gateway.TOOL_BRIDGE_CONTRACT, gateway.TOOL_BRIDGE_CONTRACT,
                gateway.TOOL_BRIDGE_CONTRACT,
            ]
            assert client.calls[0][3]["value"] is client.value
            assert client.calls[0][3]["provider_attempt"] == 2
            assert result["events"][0]["tool_intent"]["intent_id"] == "first"
            usage_cost = result["events"][1]["usage_cost"]
        else:
            assert result["usage"] is not client.value["usage"]
            assert [call[0] for call in client.calls] == [
                gateway.TOOL_BRIDGE_CONTRACT, gateway.USAGE_CONTRACT,
            ]
            usage_cost = result["usage_cost"]
        intent = result["tool_intents"][0]
        assert intent["intent_id"] == "top"
        assert intent["approved"] is intent["authority_granted"] is False
        assert intent["executes"] is False
        assert intent["arguments"]["integer"] == 7
        assert type(result["output"]["numbers"][1]) is int
        if packvm_response:
            canonical_json(result)
            assert result["output"]["numbers"][0] == "1.25"
            assert intent["arguments"]["decimal"] == "0.125"
            assert isinstance(usage_cost["cost"], str)
        else:
            assert result["output"]["numbers"][0] == 1.25
            assert isinstance(usage_cost["cost"], float)
        runs.append((result, client.calls, client.events))
    assert runs[0] == runs[1]


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5, 6])
def test_stream_dependency_failure_preserves_partial_mutation(
    fail_at: int, candidate: gateway.Candidate
) -> None:
    runs = []
    for baseline in (True, False):
        client = FinalizationClient(fail_at=fail_at)
        original_intents = [client.events[index]["tool_intent"] for index in (0, 2)]
        attempts = [{"attempt": 1}]
        with pytest.raises(ProviderExecutionError) as raised:
            finalize("stream", baseline, client, candidate, attempts)
        assert raised.value is client.error
        assert len(client.calls) == fail_at
        assert ("usage_cost" in client.events[1]) is (fail_at > 2)
        assert ("usage_cost" in client.events[3]) is (fail_at > 3)
        for index, original, threshold in (
            (0, original_intents[0], 4), (2, original_intents[1], 5),
        ):
            assert (client.events[index]["tool_intent"] is original) is (
                fail_at <= threshold
            )
        assert attempts == [{"attempt": 1}]
        runs.append((client.calls, client.events, client.value))
    assert runs[0] == runs[1]


@pytest.mark.parametrize("normalized", [None, {}, {"events": ()}, {"events": None}])
def test_stream_shape_check_precedes_event_dependencies(
    normalized: Any, candidate: gateway.Candidate
) -> None:
    for baseline in (True, False):
        client = FinalizationClient()
        client.normalized = normalized
        with pytest.raises(GlobalContractInvocationError) as raised:
            finalize("stream", baseline, client, candidate, [])
        assert raised.value.code == "invalid_response"
        assert str(raised.value) == "stream normalizer returned an invalid result"
        assert len(client.calls) == 1


@pytest.mark.parametrize("mode", ["generate", "stream"])
def test_response_adapter_is_last_and_preserves_prior_mutation_on_failure(
    mode: str, candidate: gateway.Candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = gateway._maybe_packvm_safe_response
    observed = []

    def adapter(value: Mapping[str, Any], required: bool) -> dict[str, Any]:
        observed.append(value)
        assert value["attempts"] is attempts
        assert value["tool_intents"][0]["intent_id"] == "top"
        if mode == "generate":
            assert "usage_cost" in value
        else:
            assert "usage_cost" in value["events"][3]
            assert value["events"][2]["tool_intent"]["intent_id"] == "second"
        return original(value, required)

    monkeypatch.setattr(gateway, "_maybe_packvm_safe_response", adapter)
    for baseline in (True, False):
        client = FinalizationClient()
        client.value["output"] = float("nan")
        attempts = [{"attempt": 1}]
        with pytest.raises(GlobalContractInvocationError) as raised:
            finalize(mode, baseline, client, candidate, attempts, packvm_response=True)
        assert raised.value.code == "invalid_response"
        assert len(client.calls) == (6 if mode == "stream" else 2)
    assert len(observed) == 2


@pytest.mark.parametrize("streaming", [False, True])
def test_invoke_calls_actual_seam_after_provider_return(
    streaming: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FinalizationClient()
    mode = "stream" if streaming else "generate"
    name = f"_finalize_{mode}_result"
    original = getattr(gateway, name)
    seen = []

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert args[0] is client
        assert args[1] is client.value
        assert args[2] == _REQUEST_ID
        assert args[3].provider_instance_id == "adapter-a"
        assert args[-1] == []
        assert kwargs["packvm_response"] is True
        if streaming:
            assert args[4] == 1
        else:
            assert kwargs["streaming"] is streaming
        assert client.calls[-1][0] == (
            gateway.STREAM_PROVIDER_CONTRACT if streaming
            else gateway.GENERATE_PROVIDER_CONTRACT
        )
        result = original(*args, **kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(gateway, name, spy)
    factory = (
        gateway.create_stream_operation if streaming
        else gateway.create_generate_operation
    )
    result = factory(client)(
        "invoke", {
            "request_id": _REQUEST_ID,
            "_session_id": "session.packvm-bridge.fixture",
            "requirements": {"preferred_model_id": "model-a"},
        }
    )
    assert seen == [result]
    assert seen[0] is result
    assert result["output"]["numbers"] == ["1.25", 2]
    provider_contracts = {
        gateway.GENERATE_PROVIDER_CONTRACT, gateway.STREAM_PROVIDER_CONTRACT,
    }
    assert sum(call[0] in provider_contracts for call in client.calls) == 1


@pytest.mark.parametrize("mode,fail_at", [
    ("generate", 1), ("generate", 2),
    ("stream", 1), ("stream", 2), ("stream", 4), ("stream", 6),
])
@pytest.mark.parametrize("error_type", [AuthorizationError, ProviderExecutionError])
def test_invoke_host_classified_dependency_errors_do_not_retry(
    mode: str, fail_at: int, error_type: type[Exception]
) -> None:
    """The companion Host boundary suite covers the real Broker classification."""
    error = error_type("fixture Host error")
    client = FinalizationClient(fail_at=fail_at, error=error)
    factory = (
        gateway.create_stream_operation if mode == "stream"
        else gateway.create_generate_operation
    )
    with pytest.raises(error_type) as raised:
        factory(client)("invoke", {
            "request_id": _REQUEST_ID,
            "allow_failover": True,
            "idempotency_key": "fixture-replay-safe",
        })
    assert raised.value is error
    contracts = [call[0] for call in client.calls]
    assert (
        contracts.count(gateway.GENERATE_PROVIDER_CONTRACT)
        + contracts.count(gateway.STREAM_PROVIDER_CONTRACT)
    ) == 1
    assert gateway.FAILOVER_CONTRACT not in contracts


def test_falsey_streaming_argument_reaches_original_dependency_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FalseStreaming:
        def __bool__(self) -> bool:
            return False

    streaming = FalseStreaming()
    client = FinalizationClient()
    seen = []
    for name in ("_finalize_generate_result", "_tool_intents", "_usage_cost"):
        original = getattr(gateway, name)

        def spy(
            *args: Any, _name: str = name, _original: Any = original,
            **kwargs: Any,
        ) -> Any:
            assert kwargs["streaming"] is streaming
            seen.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(gateway, name, spy)
    gateway._invoke(client, {"request_id": _REQUEST_ID}, streaming=streaming)
    assert seen == ["_finalize_generate_result", "_tool_intents", "_usage_cost"]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("unavailable", [False, True])
def test_existing_caught_contract_failure_policy_and_attempts_are_unchanged(
    streaming: bool, unavailable: bool,
) -> None:
    """Do not turn direct fixture contract failures into Host terminal errors."""
    error = (
        GlobalContractUnavailable("fixture unavailable") if unavailable
        else GlobalContractInvocationError("invalid_response", "fixture invalid")
    )
    code = "provider_unavailable" if unavailable else "invalid_response"
    client = FinalizationClient(fail_at=1, error=error)
    payload = {
        "request_id": _REQUEST_ID,
        "allow_failover": True,
        "idempotency_key": "fixture-replay-safe",
        "requirements": {"preferred_model_id": "model-a"},
    }
    if streaming:
        with pytest.raises(GlobalContractInvocationError) as raised:
            gateway._invoke(client, payload, streaming=True)
        assert raised.value.code == code
        assert sum(
            call[0] == gateway.STREAM_PROVIDER_CONTRACT for call in client.calls
        ) == 1
        assert all(call[0] != gateway.FAILOVER_CONTRACT for call in client.calls)
    else:
        result = gateway._invoke(client, payload, streaming=False)
        assert result["model_id"] == "model-b"
        assert result["attempts"] == [{
            "attempt": 1,
            "model_id": "model-a",
            "provider_instance_id": "adapter-a",
            "error_code": code,
        }]
        assert sum(
            call[0] == gateway.GENERATE_PROVIDER_CONTRACT for call in client.calls
        ) == 2
        assert sum(
            call[0] == gateway.FAILOVER_CONTRACT for call in client.calls
        ) == 1
