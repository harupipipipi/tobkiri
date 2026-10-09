"""Finite read-only tool adapters for captured-profile chat references."""

from __future__ import annotations

import re
from typing import Any, Mapping

from core_runtime.host_provider_function_v4 import (
    HostFunction,
    SingleOperationHostFactoryV4,
)

PACK = "rumi_default_tools_pack"
LOCAL_CONTRACT = "tobkiri.service.tool.local.operation.v1"
LOCAL_OPERATION = f"{PACK}.chat-reference-operation"
LOCAL_FUNCTION = f"{PACK}.chat-reference-tools"
REFERENCE = "tobkiri.resource.chat.reference.v1"
REFERENCE_OPERATION = "rumi_conversation_store_pack.chat-reference-read"


def _bind(context: Any) -> HostFunction:
    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        invocation.assert_current()
        if set(payload) != {"tool_id", "tool_call_id", "arguments"}:
            raise ValueError("chat reference tool payload is invalid")
        call = payload["tool_call_id"]
        if (
            not isinstance(call, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", call) is None
        ):
            raise ValueError("chat reference tool call identity is invalid")
        arguments = payload["arguments"]
        if not isinstance(arguments, Mapping):
            raise ValueError("chat reference arguments are invalid")
        profile = invocation.envelope.context.profile_id
        if profile != context.profile_id:
            raise PermissionError("chat reference captured profile changed")
        request: dict[str, Any] = {"profile_id": profile}
        if payload["tool_id"] == "chat_list_targets":
            if set(arguments) - {"limit", "cursor"}:
                raise ValueError("chat list target arguments are invalid")
            if "limit" in arguments and (
                type(arguments["limit"]) is not int
                or not 1 <= arguments["limit"] <= 100
            ):
                raise ValueError("chat target page limit is invalid")
            if "cursor" in arguments and (
                not isinstance(arguments["cursor"], str)
                or not 1 <= len(arguments["cursor"]) <= 96
            ):
                raise ValueError("chat target page cursor is invalid")
            request.update(arguments)
            request["operation"] = "list"
        elif payload["tool_id"] == "chat_resolve_target":
            if (
                set(arguments) != {"kind", "id"}
                or arguments["kind"] not in {"chat", "group"}
                or not isinstance(arguments["id"], str)
                or not arguments["id"]
                or len(arguments["id"]) > 256
            ):
                raise ValueError("chat resolve target arguments are invalid")
            request.update(operation="resolve", references=[dict(arguments)])
        else:
            raise ValueError("chat reference tool is unavailable")
        client = invocation.contract_client(
            allowed_contract_ids=frozenset({REFERENCE}),
            consumer_pack_id=PACK,
            include_credentials=False,
        )
        invocation.assert_current()
        result = client.invoke(REFERENCE, REFERENCE_OPERATION, request)
        if not isinstance(result, Mapping) or result.get("profile_id") != profile:
            raise PermissionError("chat reference result profile changed")
        return dict(result)

    return invoke


HOST_PROVIDER_FACTORY = {
    LOCAL_FUNCTION: SingleOperationHostFactoryV4(
        LOCAL_FUNCTION,
        LOCAL_CONTRACT,
        LOCAL_OPERATION,
        _bind,
    )
}
