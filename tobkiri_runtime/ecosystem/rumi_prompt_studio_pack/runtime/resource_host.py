"""Profile-bound reads from the existing Prompt Studio owner."""

from __future__ import annotations

import re
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    HostProviderCaptureContextV4,
    HostProviderInvocationContextV4,
)
from core_runtime.host_provider_function_v4 import (
    HostFunction,
    SingleOperationHostFactoryV4,
)
from ecosystem.rumi_prompt_studio_pack.runtime.service import PromptStudioService
from ecosystem.rumi_prompt_studio_pack.runtime.store import PromptStudioStore
from tobkiri_protocol.canonical import canonical_json

PACK_ID = "rumi_prompt_studio_pack"
FUNCTION_ID = f"{PACK_ID}.prompt-studio.resource"
CONTRACT_ID = "tobkiri.resource.prompt.studio.v1"
OPERATION_ID = f"{PACK_ID}.prompt-studio-resource"
_PROMPT_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,255}\Z")


def _bind(context: HostProviderCaptureContextV4) -> HostFunction:
    if context.user_data_root is None:
        raise PermissionError("prompt owner root is unavailable")
    store = PromptStudioStore(context.profile_id, user_data_root=context.user_data_root)

    def invoke(
        payload: Mapping[str, Any], invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        action = payload.get("operation")
        if (
            action not in ("get", "list", "edge_states")
            or payload.get("profile_id", context.profile_id) != context.profile_id
            or set(payload) - {"profile_id"}
            != ({"operation", "prompt_id"} if action == "get" else {"operation"})
        ):
            raise PermissionError("prompt resource request is invalid")
        if action == "get":
            prompt_id = payload["prompt_id"]
            if not isinstance(prompt_id, str) or _PROMPT_ID.fullmatch(prompt_id) is None:
                raise ValueError("prompt identity is invalid")
            result = PromptStudioService._get(store, payload)
        elif action == "list":
            result = store.snapshot()
        else:
            result = PromptStudioService._edge_states(store)
        invocation.assert_current()
        if len(canonical_json(result)) > 512 * 1024:
            raise ValueError("prompt resource exceeds the response budget")
        return result

    return invoke


HOST_PROVIDER_FACTORY = SingleOperationHostFactoryV4(
    function_id=FUNCTION_ID,
    contract_id=CONTRACT_ID,
    operation_id=OPERATION_ID,
    bind=_bind,
)
