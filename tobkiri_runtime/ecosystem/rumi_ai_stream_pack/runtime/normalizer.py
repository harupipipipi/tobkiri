"""Normalize typed provider stream events without provider-specific logic."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Mapping

from core_runtime.global_contract_dispatch import GlobalContractInvocationError

_ALLOWED_TYPES = {
    "text_delta",
    "thinking_delta",
    "tool_intent_delta",
    "usage",
    "finish",
    "error",
}


def create_stream_normalize_operation(client: Any):
    """Create a pure typed stream normalizer."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"normalize", "validate"}:
            raise ValueError(f"unknown stream normalization operation: {name}")
        request_id = str(payload.get("request_id") or "").strip()
        provider_attempt = int(payload.get("provider_attempt") or 1)
        value = payload.get("value")
        events = value.get("events") if isinstance(value, Mapping) else value
        if (
            not request_id
            or not isinstance(events, Iterable)
            or isinstance(events, (str, bytes, Mapping))
        ):
            raise GlobalContractInvocationError(
                "invalid_response",
                "provider stream must contain iterable events and request_id",
            )
        normalized = []
        finished = False
        for sequence, event in enumerate(events):
            if not isinstance(event, Mapping):
                raise GlobalContractInvocationError(
                    "invalid_response", "stream event must be an object"
                )
            event_type = str(event.get("type") or "")
            if event_type not in _ALLOWED_TYPES:
                raise GlobalContractInvocationError(
                    "invalid_response", f"unknown stream event type: {event_type}"
                )
            if finished:
                raise GlobalContractInvocationError(
                    "invalid_response", "stream emitted an event after finish"
                )
            normalized.append(
                {
                    "request_id": request_id,
                    "sequence": sequence,
                    "type": event_type,
                    "delta": event.get("delta"),
                    "tool_intent": event.get("tool_intent"),
                    "usage": event.get("usage"),
                    "finish_reason": event.get("finish_reason"),
                    "error_code": event.get("error_code"),
                    "provider_attempt": provider_attempt,
                }
            )
            finished = event_type in {"finish", "error"}
        if not normalized or not finished:
            raise GlobalContractInvocationError(
                "invalid_response", "stream is missing a terminal event"
            )
        return {"events": normalized}

    return operation

