"""Direct coverage for the V4 dispatch boundary replacing legacy dispatch tests."""

from __future__ import annotations

from typing import Any, Mapping

from tobkiri_host.runtime import V4DispatchSession


class _RecordingBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any, Mapping[str, Any]]] = []

    def invoke(
        self,
        frame: Any,
        context: Any,
        *,
        effect_scope: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append((frame, context, effect_scope))
        return {"status": "captured"}


def test_v4_dispatch_session_uses_captured_context_and_scope() -> None:
    """Dispatch must use host callbacks, never identity fields in payloads."""
    broker = _RecordingBroker()
    context = object()
    scope = {"path": "workspace/read-only"}
    context_calls: list[tuple[str, str]] = []
    scope_calls: list[tuple[str, str, Mapping[str, Any]]] = []

    session = V4DispatchSession(
        broker=broker,  # type: ignore[arg-type]
        context_for=lambda contract, operation: (
            context_calls.append((contract, operation)) or context
        ),
        effect_scope_for=lambda contract, operation, payload: (
            scope_calls.append((contract, operation, payload)) or scope
        ),
        providers={},
        profile_id="profile:captured",
        plan_digest="sha256:" + "1" * 64,
    )
    payload = {
        "message": "hello",
        "profile_id": "profile:attacker",
        "activation_id": "activation:attacker",
        "approved": True,
    }

    assert session.invoke(
        "tobkiri.service.example.v1",
        "read",
        payload,
        version_range=">=1,<2",
    ) == {"status": "captured"}

    assert context_calls == [("tobkiri.service.example.v1", "read")]
    assert scope_calls == [("tobkiri.service.example.v1", "read", payload)]
    assert len(broker.calls) == 1
    frame, received_context, received_scope = broker.calls[0]
    assert received_context is context
    assert received_scope is scope
    assert frame.contract_id == "tobkiri.service.example.v1"
    assert frame.operation_id == "read"
    assert frame.version_range == ">=1,<2"
    assert frame.payload == payload
    assert not hasattr(frame, "profile_id")
    assert not hasattr(frame, "activation_id")


def test_v4_dispatch_session_leaves_omitted_version_to_captured_broker() -> None:
    """An omitted range uses the Broker/catalog immutable binding contract."""
    broker = _RecordingBroker()
    session = V4DispatchSession(
        broker=broker,  # type: ignore[arg-type]
        context_for=lambda _contract, _operation: object(),
        effect_scope_for=lambda _contract, _operation, _payload: {},
        providers={},
        profile_id="profile:captured",
        plan_digest="sha256:" + "1" * 64,
    )

    session.invoke("tobkiri.service.example.v1", "read", {})

    assert broker.calls[0][0].version_range is None
