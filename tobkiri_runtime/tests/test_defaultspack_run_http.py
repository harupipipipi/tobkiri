from __future__ import annotations

from typing import Any, Mapping

import pytest

from core_runtime.di_container import DIContainer
from core_runtime.global_contract_dispatch import GlobalContractUnavailable
from ecosystem.defaultspack.runtime import conversation
from tobkiri_host.runtime import V4DispatchSession


_REQUEST = {"messages": [{"role": "user", "content": "hello"}]}


class _CapturedBroker:
    """Host Broker fixture used by the canonical conversation entrypoint."""

    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = dict(response)
        self.calls: list[tuple[Any, Any, Mapping[str, Any]]] = []

    def invoke(
        self,
        frame: Any,
        context: Any,
        *,
        effect_scope: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append((frame, context, effect_scope))
        return self.response


def _bind_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile_id: str,
    providers: Mapping[str, tuple[Mapping[str, Any], ...]],
) -> _CapturedBroker:
    broker = _CapturedBroker({"output": "hello", "tool_intents": []})
    session = V4DispatchSession(
        broker=broker,  # type: ignore[arg-type]
        context_for=lambda _contract, _operation: {"source": "captured-host"},
        effect_scope_for=lambda _contract, _operation, _payload: {
            "effect": "conversation.complete"
        },
        providers=providers,
        profile_id=profile_id,
        plan_digest="sha256:" + "1" * 64,
    )
    container = DIContainer()
    container.set_instance("v4_dispatch_session", session)
    monkeypatch.setattr(conversation, "get_container", lambda: container)
    return broker


def test_conversation_requires_a_captured_v4_dispatch_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical entrypoint refuses to start without Host composition."""
    monkeypatch.setattr(conversation, "get_container", DIContainer)

    with pytest.raises(
        GlobalContractUnavailable,
        match="Pack v4 dispatch session is required for conversation",
    ):
        conversation.invoke(_REQUEST)


def test_conversation_rejects_a_session_without_broker_profile_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed captured session cannot become a legacy registry fallback."""
    container = DIContainer()
    container.set_instance("v4_dispatch_session", object())
    monkeypatch.setattr(conversation, "get_container", lambda: container)

    with pytest.raises(
        GlobalContractUnavailable,
        match="live registry lookup is disabled",
    ):
        conversation.invoke(_REQUEST)


def test_conversation_dispatches_through_complete_captured_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified profile reaches the pinned GlobalContractClient/Broker path."""
    providers = {
        conversation.AI_GENERATE_CONTRACT: (
            {
                "provider_id": conversation.AI_GENERATE_OPERATION,
                "operation_id": conversation.AI_GENERATE_OPERATION,
                "profile_id": "profile:test-v4",
                "plan_digest": "sha256:" + "1" * 64,
            },
        ),
        conversation.AI_STREAM_CONTRACT: (
            {
                "provider_id": conversation.AI_STREAM_OPERATION,
                "operation_id": conversation.AI_STREAM_OPERATION,
                "profile_id": "profile:test-v4",
                "plan_digest": "sha256:" + "1" * 64,
            },
        ),
    }
    broker = _bind_session(
        monkeypatch,
        profile_id="profile:test-v4",
        providers=providers,
    )

    result = conversation.invoke(_REQUEST)

    assert result["content"] == [{"type": "text", "text": "hello"}]
    assert result["tool_calls"] == []
    assert len(broker.calls) == 1
    frame, context, effect_scope = broker.calls[0]
    assert frame.contract_id == conversation.AI_GENERATE_CONTRACT
    assert frame.operation_id == conversation.AI_GENERATE_OPERATION
    assert frame.payload == {
        "messages": _REQUEST["messages"],
        "profile_id": "profile:test-v4",
        "requirements": {"request_surface": "defaultspack.conversation"},
    }
    assert context == {"source": "captured-host"}
    assert effect_scope == {"effect": "conversation.complete"}
