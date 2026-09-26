"""Saved-turn tool routing: auto offers tools, explicit stays fail-closed.

A saved model profile bound to a Provider connection resolves through a
synthetic catalog descriptor whose capabilities are unverified
(``capabilities: []``). Turning every selected tool into a hard
``tool_calling`` routing requirement excluded the only candidate
(``tool_calling_mismatch``) and wedged the turn with ``unresolved_profile``
before any write. ``auto`` selection without ``must_use`` only offers tools
to the model, so it must not become a routing requirement; ``must_use`` or
an explicit ``manual`` selection keeps the fail-closed requirement.
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
from ecosystem.rumi_ai_gateway_pack.runtime.preflight import (
    create_preflight_operation,
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
from tobkiri_host.saved_turn_plan import SavedToolFrame
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
                        "display_name": "OpenRouter main",
                        "enabled": True,
                    }
                ],
            }
        if contract_id == gateway.CATALOG_CONTRACT:
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
            return {
                "status": "ok",
                "output": "Done",
                "finish_reason": "stop",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        raise AssertionError(contract_id)


def _bridge_setup(
    tmp_path: Path, selection: Mapping[str, Any]
) -> tuple[ConversationStore, Any, list, SavedBridgeCallbacks, _SavedRouteSession]:
    """Bind saved callbacks to a real owner store and the real AI route."""
    store = ConversationStore("defaults", user_data_root=tmp_path)
    store.create(
        {"id": "conversation-1", "model_reference": "daily"},
        expected_revision=0,
    )
    session = _SavedRouteSession()
    client = GlobalContractClient(
        session=session,
        allowed_contract_ids=gateway._GENERATE_ALLOWED_CONTRACTS,
        consumer_pack_id="rumi_ai_gateway_pack",
    )
    readiness = create_preflight_operation(client)
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
            value = {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "file_read",
                            "description": "Read",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
                "definitions": {"file_read": "b" * 64},
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
        else:
            raise AssertionError(target)
        return {"status": "ok", "value": value}

    callbacks = SavedBridgeCallbacks(dispatch, require_targets)
    return store, outer, calls, callbacks, session


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
    assert readiness and readiness[0]["tool_calling"] is True

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
