"""Saved-turn tool routing with exact catalog-owned capability evidence.

A saved model profile bound to a Provider connection resolves through a
synthetic catalog descriptor whose capabilities are unverified
(``capabilities: []``). Turning every selected tool into a hard
``tool_calling`` routing requirement excludes the candidate unless an exact
provider catalog record supplies the capability. ``auto`` selection without
``must_use`` only offers tools to the model, while ``must_use`` or an explicit
``manual`` selection keeps the fail-closed requirement.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from core_runtime.bootstrap.saved_bridge import (
    ALLOWED_TARGETS,
    DEFINITION,
    READINESS,
    SavedBridgeCallbacks,
    project_saved_ai_result,
    project_saved_tool_result,
)
from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)
from ecosystem.defaultspack.defaultspack.model_profile_presentation import (
    normalize_model_profile_save,
)
from ecosystem.defaultspack.runtime import saved_conversation as saved
from ecosystem.rumi_ai_gateway_pack.runtime import gateway
from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import (
    create_route_quote_operation,
)
from ecosystem.rumi_ai_pipeline_pack.runtime.pipeline import (
    create_failover_operation,
    create_prepare_operation,
)
from ecosystem.rumi_ai_routing_pack.runtime.router import create_route_operation
from ecosystem.rumi_ai_tool_bridge_pack.runtime.bridge import (
    create_tool_intent_operation,
)
from ecosystem.rumi_ai_usage_pack.runtime.usage import create_cost_operation
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from ecosystem.rumi_model_catalog_pack.runtime.catalog import (
    create_model_catalog_operation,
)
from tobkiri_host.saved_turn_plan import TOOL, SavedToolFrame
from tobkiri_protocol.canonical import canonical_digest, canonical_json


class _SavedRouteSession:
    """Captured V4 session over one connection-bound saved model profile.

    Routing, request preparation, tool-intent normalization, usage cost and
    the bundled catalog run the real pack operations; only the model-profile
    owner read, the Provider registry snapshot, health evidence and provider
    execution are fixtures.
    """

    profile_id = "defaults"
    plan_digest = "sha256:" + "0" * 64

    def __init__(self, model_id: str = "account-visible-model") -> None:
        self.connection_id = "connection/openrouter:main"
        self.record = normalize_model_profile_save(
            {
                "model_profile_id": "daily",
                "model_id": model_id,
                "provider_instance_id": self.connection_id,
                "display_name": "Daily",
                "expected_revision": 0,
                "provider_registry_revision": 1,
            }
        )["record"]
        self.provider_requests: list[dict[str, Any]] = []
        self.catalog_requests: list[dict[str, Any]] = []
        self.respond_with_calculator = False

    def provider_metadata(
        self, contract_id: str
    ) -> tuple[Mapping[str, Any], ...]:
        providers = {
            gateway.CATALOG_CONTRACT: (
                {
                    "provider_instance_id": "catalog-main",
                    "operation_id": gateway.CATALOG_GENERATE_OPERATION,
                },
            ),
            gateway.HEALTH_CONTRACT: (
                {
                    "provider_instance_id": "health-main",
                    "operation_id": gateway.HEALTH_GENERATE_OPERATION,
                },
            ),
            gateway.GENERATE_PROVIDER_CONTRACT: (
                {
                    "provider_instance_id": "provider.compatibility.generate",
                    "operation_id": gateway.GENERATE_PROVIDER_OPERATION,
                },
            ),
            gateway.PROVIDER_REGISTRY_CONTRACT: (
                {
                    "provider_instance_id": "registry-main",
                    "operation_id": gateway.PROVIDER_REGISTRY_GENERATE_OPERATION,
                },
            ),
        }
        return providers.get(contract_id, ())

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, Any]:
        del version_range
        if contract_id == gateway.MODEL_PROFILE_CONTRACT:
            return {"profile": self.record, "resolved_profile_id": "daily"}
        if contract_id == gateway.PROVIDER_REGISTRY_CONTRACT:
            return {
                "revision": 1,
                "providers": [
                    {
                        "provider_instance_id": self.connection_id,
                        "adapter_id": "openai-compatible",
                        "endpoint": "https://openrouter.ai/api/v1",
                        "display_name": "OpenRouter main",
                        "enabled": True,
                    }
                ],
            }
        if contract_id == gateway.CATALOG_CONTRACT:
            self.catalog_requests.append(dict(payload))
            return create_model_catalog_operation(None)(operation_id, payload)
        if contract_id == gateway.REQUEST_PREPARE_CONTRACT:
            return create_prepare_operation(None)(operation_id, payload)
        if contract_id == gateway.ROUTING_CONTRACT:
            return create_route_operation(None)(operation_id, payload)
        if contract_id == gateway.TOOL_BRIDGE_CONTRACT:
            return create_tool_intent_operation(None)(operation_id, payload)
        if contract_id == gateway.USAGE_CONTRACT:
            return create_cost_operation(None)(operation_id, payload)
        if contract_id == gateway.FAILOVER_CONTRACT:
            return create_failover_operation(None)(operation_id, payload)
        if contract_id == gateway.HEALTH_CONTRACT:
            return {"providers": []}
        if contract_id == gateway.GENERATE_PROVIDER_CONTRACT:
            self.provider_requests.append(dict(payload))
            if self.respond_with_calculator and len(self.provider_requests) == 1:
                return {
                    "status": "ok",
                    "output": "",
                    "tool_intents": [{
                        "id": "call-1",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression":"13829+12312"}',
                        },
                    }],
                    "finish_reason": "tool_calls",
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                }
            return {
                "status": "ok",
                "output": "Done",
                "finish_reason": "stop",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        raise AssertionError(contract_id)


def _bridge_setup(
    tmp_path: Path,
    selection: Mapping[str, Any],
    *,
    model_id: str = "account-visible-model",
    tool_name: str = "file_read",
) -> tuple[ConversationStore, Any, list, SavedBridgeCallbacks, _SavedRouteSession]:
    """Bind saved callbacks to a real owner store and the real AI route."""
    store = ConversationStore("defaults", user_data_root=tmp_path)
    store.create(
        {"id": "conversation-1", "model_reference": "daily"},
        expected_revision=0,
    )
    session = _SavedRouteSession(model_id)
    client = GlobalContractClient(
        session=session,
        allowed_contract_ids=gateway._GENERATE_ALLOWED_CONTRACTS,
        consumer_pack_id="rumi_ai_gateway_pack",
    )
    readiness = create_route_quote_operation(client)
    generate = gateway.create_generate_operation(client)
    outer = SimpleNamespace(
        context=SimpleNamespace(request_id="host-request", profile_id="defaults"),
        payload={
            "request": {
                "turn_id": "turn-1",
                "conversation_id": "conversation-1",
                "conversation_revision": 1,
                "content": "Read the selected information",
                "tool_selection": dict(selection),
            }
        },
    )
    calls: list[tuple[Any, dict[str, Any]]] = []

    def require_targets(request: object, targets: tuple) -> None:
        assert request is outer
        assert all(target in ALLOWED_TARGETS for target in targets)

    def dispatch(
        request: object, target: tuple[str, str], payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        assert request is outer
        calls.append((target, deepcopy(payload)))
        if target == saved.TARGETS[0]:
            value = {"conversation": store.get(payload["conversation_id"])}
        elif target == saved.TARGETS[1]:
            value = store.append_message(
                payload["conversation_id"],
                payload["message"],
                expected_conversation_revision=payload[
                    "expected_conversation_revision"
                ],
                saved_input=outer.payload,
            )
        elif target == DEFINITION:
            assert payload == {
                "operation": "select",
                "selection": dict(selection),
            }
            parameters = (
                {"type": "object", "properties": {"expression": {"type": "string"}}}
                if tool_name == "calculator"
                else {"type": "object"}
            )
            value = {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": (
                                "Calculate" if tool_name == "calculator" else "Read"
                            ),
                            "parameters": parameters,
                        },
                    }
                ],
                "definitions": {tool_name: "b" * 64},
            }
        elif target == READINESS:
            # The production bridge would wrap a routing error into a bounded
            # unavailable result; surfacing it keeps the assertion exact.
            return {"status": "ok", "value": readiness(READINESS[1], payload)}
        elif target == saved.TARGETS[2]:
            # The production dispatch projects the provider telemetry out of
            # the strict guest ABI (production_v4.saved_dispatch).
            return {
                "status": "ok",
                "value": project_saved_ai_result(generate("generate", payload)),
            }
        elif target == TOOL:
            assert payload["tool_id"] == tool_name
            assert payload["expected_definition_hash"] == "b" * 64
            value = project_saved_tool_result({
                "tool_id": tool_name,
                "tool_call_id": payload["tool_call_id"],
                "status": "success",
                "is_error": False,
                "result": {"value": 26141},
            })
        else:
            raise AssertionError(target)
        return {"status": "ok", "value": value}

    callbacks = SavedBridgeCallbacks(dispatch, require_targets)
    return store, outer, calls, callbacks, session


def test_connection_route_uses_exact_reviewed_catalog_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact reviewed connection/model pair uses catalog-owned rates."""

    from ecosystem.rumi_model_catalog_pack.runtime import catalog

    monkeypatch.setattr(
        catalog,
        "_openrouter_inventory",
        lambda: pytest.fail("saved preflight must not refresh public inventory"),
    )

    session = _SavedRouteSession("deepseek/deepseek-r1-0528")
    session.record["metadata"]["pricing"] = {
        "input": 9.0,
        "output": 9.0,
        "currency": "EUR",
    }
    client = GlobalContractClient(
        session=session,
        allowed_contract_ids=gateway._GENERATE_ALLOWED_CONTRACTS,
        consumer_pack_id="rumi_ai_gateway_pack",
    )

    resolved = gateway.create_generate_operation(client)(
        "resolve",
        {
            "model_profile_id": "daily",
            "messages": [{"role": "user", "content": "hello"}],
            "requirements": {"deepthink": True},
        },
    )

    assert resolved["pricing"] == {
        "input": 0.0000005,
        "output": 0.00000215,
        "currency": "USD",
    }
    assert session.catalog_requests == [{
        "provider_id": "openrouter",
        "model_id": "deepseek/deepseek-r1-0528",
        "catalog_source": "bundled_approved",
    }]


def test_connection_route_keeps_unknown_mimo_pricing_unavailable() -> None:
    """A public inventory entry outside the reviewed set stays unpriced."""

    session = _SavedRouteSession("xiaomi/mimo-v2.6-flash")
    client = GlobalContractClient(
        session=session,
        allowed_contract_ids=gateway._GENERATE_ALLOWED_CONTRACTS,
        consumer_pack_id="rumi_ai_gateway_pack",
    )

    resolved = gateway.create_generate_operation(client)(
        "resolve",
        {
            "model_profile_id": "daily",
            "messages": [{"role": "user", "content": "hello"}],
            "requirements": {"deepthink": True},
        },
    )

    assert resolved["pricing"] == {
        "input": None,
        "output": None,
        "currency": "USD",
    }
    assert session.catalog_requests == []


@pytest.mark.parametrize(
    ("connection_update", "model_update", "inventory_update"),
    [
        ({"endpoint": "https://example.com/v1"}, {}, {}),
        ({}, {"provider_id": "other"}, {}),
        ({}, {"provider_model_id": "xiaomi/mimo-v2.6-flash"}, {}),
        ({}, {"currency": "EUR"}, {}),
        ({}, {"input_cost": float("nan")}, {}),
        ({}, {"catalog_source": "live"}, {}),
        ({}, {}, {"source": "live"}),
    ],
)
def test_connection_catalog_pricing_requires_exact_trusted_evidence(
    connection_update: Mapping[str, Any],
    model_update: Mapping[str, Any],
    inventory_update: Mapping[str, Any],
) -> None:
    """Loose provider, model, currency, rate, or provenance matches fail closed."""

    connection = {
        "adapter_id": "openai-compatible",
        "endpoint": "https://openrouter.ai/api/v1",
        **connection_update,
    }
    model = {
        "provider_id": "openrouter",
        "provider_model_id": "deepseek/deepseek-r1-0528",
        "input_cost": 0.0000005,
        "output_cost": 0.00000215,
        "currency": "USD",
        "catalog_revision": "catalog-1",
        "catalog_provider_instance_id": "catalog-main",
        "catalog_source": "bundled_approved",
        **model_update,
    }
    inventory = {
        "catalog-main": {
            "openrouter": {
                "source": "bundled_approved",
                **inventory_update,
            }
        }
    }

    assert gateway._saved_connection_catalog_pricing(
        [model],
        inventory,
        connection,
        "deepseek/deepseek-r1-0528",
    ) == {}


@pytest.mark.parametrize(
    ("connection_update", "model_update", "inventory_update"),
    [
        ({"endpoint": "https://example.com/v1"}, {}, {}),
        ({"adapter_id": "other"}, {}, {}),
        ({}, {"provider_id": "other"}, {}),
        ({}, {"provider_model_id": "other/model"}, {}),
        ({}, {"model_id": "openrouter/other/model"}, {}),
        ({}, {"capabilities": []}, {}),
        ({}, {"metadata": {"capability_source": "saved_profile"}}, {}),
        ({}, {}, {"source": "unavailable"}),
        ({}, {}, {"stale": True}),
    ],
)
def test_connection_capabilities_require_exact_provider_owned_evidence(
    connection_update: Mapping[str, Any],
    model_update: Mapping[str, Any],
    inventory_update: Mapping[str, Any],
) -> None:
    """Connection and profile claims never fill a missing catalog capability."""

    connection = {
        "adapter_id": "openai-compatible",
        "endpoint": "https://openrouter.ai/api/v1",
        **connection_update,
    }
    model = {
        "model_id": "openrouter/xiaomi/mimo-v2.6-flash",
        "provider_id": "openrouter",
        "provider_model_id": "xiaomi/mimo-v2.6-flash",
        "capabilities": ["tool_calling"],
        "catalog_provider_instance_id": "catalog-main",
        "metadata": {
            "capability_source": "openrouter_models_api",
            "capability_confidence": "provider_reported",
            "source_endpoint": "/models?output_modalities=all",
        },
        **model_update,
    }
    inventory = {
        "catalog-main": {
            "openrouter": {
                "source": "last_known_good",
                "stale": False,
                **inventory_update,
            }
        }
    }
    actual = gateway._saved_connection_catalog_capabilities(
        [model], inventory, connection, "xiaomi/mimo-v2.6-flash"
    )
    assert actual == frozenset()


@pytest.mark.parametrize("source", ["live", "last_known_good"])
def test_connection_capabilities_accept_fresh_reported_inventory(
    source: str,
) -> None:
    """An exact OpenRouter model can contribute its provider-reported tools."""
    capabilities = gateway._saved_connection_catalog_capabilities(
        [{
            "model_id": "openrouter/xiaomi/mimo-v2.6-flash",
            "provider_id": "openrouter",
            "provider_model_id": "xiaomi/mimo-v2.6-flash",
            "capabilities": ["tool_calling", "thinking"],
            "catalog_provider_instance_id": "catalog-main",
            "metadata": {
                "capability_source": "openrouter_models_api",
                "capability_confidence": "provider_reported",
                "source_endpoint": "/models?output_modalities=all",
            },
        }],
        {"catalog-main": {"openrouter": {"source": source, "stale": False}}},
        {
            "adapter_id": "openai-compatible",
            "endpoint": "https://openrouter.ai/api/v1",
        },
        "xiaomi/mimo-v2.6-flash",
    )
    assert capabilities == {"tool_calling", "thinking"}


def test_unrecognized_provider_failure_remains_generic() -> None:
    """Arbitrary provider diagnostics do not cross the guest boundary."""

    from core_runtime.bootstrap.production_v4 import (
        _provider_unavailable_bridge_result,
    )

    provider_error = GlobalContractInvocationError(
        "PROVIDER_SECRET_DETAIL",
        "provider-specific detail that must not cross the boundary",
    )

    assert _provider_unavailable_bridge_result(provider_error) == {
        "status": "error",
        "error": {
            "code": "PROVIDER_UNAVAILABLE",
            "message": "The verified AI capability is unavailable.",
        },
    }


def _tool_frame(
    intent: Mapping[str, Any], request: Mapping[str, Any]
) -> SavedToolFrame:
    """Rebuild the Host-local checked tool scope for one guest intent."""
    state = intent["state"]
    return SavedToolFrame(
        frame={
            "kind": "tobkiri.packvm.continuation.request.v2",
            "version": 2,
            "request_id": "host-request",
            "hop": intent["hop"],
            "target": intent["target"],
            "payload": intent["payload"],
        },
        initial_digest=canonical_digest({"request": request}),
        tool_messages=canonical_json(state["tool_messages"]),
        stage=state["stage"],
        expected_conversation_revision=None,
        expected_current_node_id=None,
    )


def _drive_turn(outer: Any, callbacks: SavedBridgeCallbacks) -> dict[str, Any]:
    """Run read -> user -> ai -> assistant through the real guest steps."""
    intent = saved.start(outer.payload["request"])
    while intent.get("kind") == "tobkiri.packvm.continuation.intent.v2":
        outcome = callbacks(
            outer, _tool_frame(intent, outer.payload["request"])
        )
        intent = saved.resume(intent["state"], outcome)
    return intent


def test_auto_tool_selection_resolves_connection_bound_saved_profile(
    tmp_path: Path,
) -> None:
    """An auto tool offer must not become a hard routing requirement.

    The connection-bound profile resolves through the synthetic descriptor
    without capability evidence; the turn still carries the selected tools
    to the provider so the model may use them.
    """
    store, outer, calls, callbacks, session = _bridge_setup(
        tmp_path, {"mode": "auto"}
    )

    callbacks.preflight(outer)

    readiness = [payload for target, payload in calls if target == READINESS]
    assert readiness == [
        {
            "model_profile_id": "daily",
            "messages": [
                {"role": "user", "content": "Read the selected information"}
            ],
        }
    ]
    selections = [payload for target, payload in calls if target == DEFINITION]
    assert selections == [{"operation": "select", "selection": {"mode": "auto"}}]

    result = _drive_turn(outer, callbacks)
    assert result["status"] == "ok"

    ai = [payload for target, payload in calls if target == saved.TARGETS[2]]
    assert len(ai) == 1
    assert [tool["function"]["name"] for tool in ai[0]["tools"]] == ["file_read"]
    assert "tool_calling" not in ai[0]["requirements"]
    assert ai[0]["parameters"]["tool_choice"] == "auto"
    # The tools still reached the connection-bound provider invocation.
    assert len(session.provider_requests) == 1
    assert session.provider_requests[0]["tools"] == ai[0]["tools"]
    assert session.provider_requests[0]["provider_connection_id"] == (
        session.connection_id
    )
    messages = store.get("conversation-1")["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]


def test_saved_tool_offer_keeps_selected_thinking_level(tmp_path: Path) -> None:
    _store, outer, calls, callbacks, session = _bridge_setup(
        tmp_path, {"mode": "auto"}
    )
    outer.payload["request"]["thinking_level"] = "high"

    callbacks.preflight(outer)
    result = _drive_turn(outer, callbacks)

    assert result["status"] == "ok"
    ai = [payload for target, payload in calls if target == saved.TARGETS[2]]
    assert ai[0]["parameters"] == {
        "thinking_level": "high",
        "tool_choice": "auto",
    }
    assert session.provider_requests[0]["parameters"]["thinking_level"] == "high"


@pytest.mark.parametrize(
    "selection",
    [
        {"mode": "auto", "must_use": True},
        {"mode": "manual", "include": ["calculator"]},
    ],
)
def test_required_calculator_uses_exact_openrouter_catalog_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: Mapping[str, Any],
) -> None:
    """A provider-reported tool capability permits the full saved tool round."""
    from ecosystem.rumi_model_catalog_pack.runtime import catalog

    model = catalog._normalize_openrouter_model({
        "id": "xiaomi/mimo-v2.6-flash",
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["tools", "tool_choice"],
    })
    assert model is not None
    monkeypatch.setattr(
        catalog, "_openrouter_inventory", lambda: ([model], "live", False)
    )
    store, outer, calls, callbacks, session = _bridge_setup(
        tmp_path,
        selection,
        model_id="xiaomi/mimo-v2.6-flash",
        tool_name="calculator",
    )
    session.respond_with_calculator = True

    callbacks.preflight(outer)
    result = _drive_turn(outer, callbacks)

    assert result["status"] == "ok"
    assert len(session.provider_requests) == 2
    assert session.provider_requests[0]["tools"][0]["function"]["name"] == (
        "calculator"
    )
    assert session.provider_requests[0]["provider_connection_id"] == (
        session.connection_id
    )
    assert any(target == TOOL for target, _payload in calls)
    assert session.catalog_requests
    assert all(request == {
        "provider_id": "openrouter",
        "model_id": "xiaomi/mimo-v2.6-flash",
    } for request in session.catalog_requests)
    assert [message["role"] for message in store.get("conversation-1")["messages"]] == [
        "user", "assistant"
    ]


@pytest.mark.parametrize(
    "selection",
    [
        {"mode": "auto", "must_use": True},
        {"mode": "manual", "include": ["file_read"]},
    ],
)
def test_required_tool_selection_fails_closed_on_connection_bound_profile(
    tmp_path: Path, selection: Mapping[str, Any]
) -> None:
    """An explicit or required tool selection stays a hard requirement."""
    _store, outer, calls, callbacks, _session = _bridge_setup(tmp_path, selection)

    with pytest.raises(GlobalContractInvocationError) as raised:
        callbacks.preflight(outer)
    assert raised.value.code == "unresolved_profile"
    readiness = [payload for target, payload in calls if target == READINESS]
    assert readiness and readiness[0]["requirements"]["tool_calling"] is True

    # The AI stage applies the same hard requirement before generation, so a
    # forged or bypassed preflight cannot smuggle tools into the route.
    intent = saved.start(outer.payload["request"])
    intent = saved.resume(
        intent["state"],
        callbacks(outer, _tool_frame(intent, outer.payload["request"])),
    )
    intent = saved.resume(
        intent["state"],
        callbacks(outer, _tool_frame(intent, outer.payload["request"])),
    )
    with pytest.raises(GlobalContractInvocationError) as raised_again:
        callbacks(outer, _tool_frame(intent, outer.payload["request"]))
    assert raised_again.value.code == "unresolved_profile"
    ai = [payload for target, payload in calls if target == saved.TARGETS[2]]
    assert ai and ai[0]["requirements"]["tool_calling"] is True
