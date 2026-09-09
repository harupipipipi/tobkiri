import os
import sys

from tobkiri_protocol.settings_state import SettingsOwnerPort

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error
from domain.ai_client.gateway import AIClient as AIClient
from domain.ai_client.gateway_contract_client import ContractLLMGateway
from domain.chat.run_request import validate_chat_run_input
from domain.chat.idempotency import IdempotencyConflictError, reserve_chat_operation
from domain.chat.store import ChatStore
from domain.chat.stream_engine import ChatRunEngine
from domain.chat.stream_engine import _InlineThoughtFilter as _InlineThoughtFilter
from domain.stream.events import to_legacy_chat_stream_event


def _fallback_send(
    input_data, context, *, settings_owner: SettingsOwnerPort | None = None,
):
    # Compatibility shim: keep the old helper name, but route through the
    # unified run engine instead of the legacy threaded send-path fallback.
    yield from _engine_events(
        _input_with_default_empty_tools(input_data), context,
        settings_owner=settings_owner,
    )


def _input_with_default_empty_tools(input_data):
    # Legacy helper name kept for import compatibility; omitted tools now mean auto-selection.
    return input_data


def _engine_events(
    input_data, context, *, settings_owner: SettingsOwnerPort | None = None,
):
    try:
        engine_context = dict(context or {}) if isinstance(context, dict) else {}
        engine_context.setdefault("run_source", "blocks.chat.stream")
        for event in ChatRunEngine(
            gateway=ContractLLMGateway(), settings_owner=settings_owner,
        ).stream(
            input_data,
            engine_context,
            stream_mode=True,
        ):
            legacy = to_legacy_chat_stream_event(event)
            if legacy is not None:
                yield legacy
    except ValueError as exc:
        yield {"type": "error", "error": {"message": str(exc)}}
    except Exception as exc:
        yield {"type": "error", "error": {"message": "AI request failed: " + str(exc)}}


def run(input_data, context, *, settings_owner: SettingsOwnerPort | None = None):
    """Stream using the caller-bound owner, never one supplied in the payload."""
    validation_error = validate_chat_run_input(input_data if isinstance(input_data, dict) else {})
    if validation_error:
        return error(validation_error, "INVALID_INPUT")
    conversation_id = input_data.get("conversation_id") if isinstance(input_data, dict) else None
    store = ChatStore()
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        return error("Conversation not found", "NOT_FOUND")
    try:
        engine_context = reserve_chat_operation(input_data, context)
    except IdempotencyConflictError as exc:
        response = error(str(exc), "IDEMPOTENCY_CONFLICT")
        response["_http_status"] = 409
        return response
    except ValueError as exc:
        response = error(str(exc), "INVALID_INPUT")
        response["_http_status"] = 400
        return response
    reservation = engine_context.get("_chat_idempotency_reservation")
    claim = reservation.get("claim") if isinstance(reservation, dict) else None
    if (
        getattr(claim, "status", "") == "in_progress"
        and getattr(claim, "state", "") == "replay"
    ):
        response = error(
            "This chat operation is already in progress",
            "IDEMPOTENCY_IN_PROGRESS",
        )
        response["_http_status"] = 409
        return response
    return {
        "_sse": True,
        "events": _engine_events(
            _input_with_default_empty_tools(input_data), engine_context,
            settings_owner=settings_owner,
        ),
    }
