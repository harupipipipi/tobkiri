"""Expose the managed model browser through captured tool/Broker operations."""

from __future__ import annotations

import json
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

PACK_ID = "rumi_browser_host_service_pack"
FUNCTION = f"{PACK_ID}.browser-host.tool"
CONTRACT = "tobkiri.service.tool.browser.operation.v1"
OPERATION = f"{PACK_ID}.browser-tool-execute"
_OBSERVE = "tobkiri.resource.browser.host.v1"
_CONTROL = "tobkiri.action.browser.host.v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_MAX_TOOL_RESULT_BYTES = 48 * 1024
_ACTIONS: dict[str, tuple[str, str]] = {
    "browser.session": (_OBSERVE, "browser.session.get"),
    "browser.profiles.list": (_OBSERVE, "browser.profiles.list"),
    "browser.tabs": (_OBSERVE, "browser.tabs.list"),
    "browser.cookies.list": (_OBSERVE, "browser.cookies.list"),
    "browser.downloads.list": (_OBSERVE, "browser.downloads.list"),
    "browser.runtime.status": (_OBSERVE, "browser.runtime.status"),
    "browser.extensions.list": (_OBSERVE, "browser.extensions.list"),
    "browser.devtools.inspect": (_OBSERVE, "browser.devtools.inspect"),
    "browser.capture.page": (_OBSERVE, "browser.capture.page"),
    "browser.open_url": (_CONTROL, "browser.navigate"),
    "browser.select_tab": (_CONTROL, "browser.tab.select"),
    "browser.session.create": (_CONTROL, "browser.session.create"),
    "browser.session.close": (_CONTROL, "browser.session.close"),
    "browser.profile.create": (_CONTROL, "browser.profile.create"),
    "browser.profile.set_active": (_CONTROL, "browser.profile.set_active"),
    "browser.profile.delete": (_CONTROL, "browser.profile.delete"),
    "browser.profile.clear_cache": (_CONTROL, "browser.profile.clear_cache"),
    "browser.profile.clear_cookies": (_CONTROL, "browser.profile.clear_cookies"),
    "browser.cookies.import": (_CONTROL, "browser.cookies.import"),
    "browser.cookies.delete": (_CONTROL, "browser.cookies.delete"),
    "browser.download.collect": (_CONTROL, "browser.download.collect"),
    "browser.runtime.start": (_CONTROL, "browser.runtime.start"),
    "browser.runtime.stop": (_CONTROL, "browser.runtime.stop"),
    "browser.extensions.install": (_CONTROL, "browser.extensions.install"),
    "browser.extensions.remove": (_CONTROL, "browser.extensions.remove"),
    "browser.devtools.evaluate": (_CONTROL, "browser.devtools.evaluate"),
    "browser.network.capture": (_CONTROL, "browser.network.capture"),
}
_SECRET_KEYS = frozenset({
    "password", "secret", "token", "authorization", "cookie", "set-cookie",
    "api_key", "credential", "credentials", "expression", "script",
})


def _bind(_context: HostProviderCaptureContextV4) -> HostFunction:
    def invoke(
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        if (
            set(payload) != {"tool_id", "tool_call_id", "arguments"}
            or payload["tool_id"] != "browser_managed"
            or not isinstance(payload["tool_call_id"], str)
            or _IDENTIFIER.fullmatch(payload["tool_call_id"]) is None
            or not isinstance(payload["arguments"], Mapping)
        ):
            raise ValueError("managed browser tool invocation is invalid")
        arguments = payload["arguments"]
        if (
            set(arguments) - {"payload"} != {"action"}
            or not isinstance(arguments["action"], str)
            or not isinstance(arguments.get("payload", {}), Mapping)
            or arguments["action"] not in _ACTIONS
        ):
            raise ValueError("managed browser tool action is invalid")
        action = arguments["action"]
        contract, operation = _ACTIONS[action]
        access = "observe" if contract == _OBSERVE else "control"
        provider_function = f"{PACK_ID}.browser-host.{access}"
        provider_operation = f"{PACK_ID}.browser-host-{access}"
        invocation.assert_current()
        client = invocation.contract_client(
            allowed_contract_ids=frozenset({_OBSERVE, _CONTROL}),
            consumer_pack_id=PACK_ID,
            include_credentials=False,
        )
        candidates = [
            item for item in client.providers(contract)
            if item.get("function_id") == provider_function
            and item.get("operation_id") == provider_operation
            and item.get("backend_id")
            and not item.get("backend_unavailable_reason")
        ]
        if len(candidates) != 1:
            raise PermissionError("selected managed browser provider is unavailable")
        # A model action is data. Only the captured nested edge can authorize
        # the browser operation, with the original deadline and cancellation.
        raw = client.invoke(
            contract,
            provider_operation,
            {"operation": operation, "arguments": dict(arguments.get("payload", {}))},
            provider_instance_id=candidates[0]["provider_instance_id"],
        )
        if not isinstance(raw, Mapping) or raw.get("type") == "host_intent":
            raise RuntimeError("managed browser provider returned an invalid result")
        safe = _model_value(dict(raw))
        if action == "browser.capture.page":
            screenshot = safe.get("screenshot")
            if isinstance(screenshot, dict):
                screenshot.pop("data_url", None)
            safe["image_forwarding_supported"] = False
            safe["message"] = (
                "Screenshot metadata only: the text tool transport cannot forward images. "
                "Use browser.devtools.inspect for page DOM data."
            )
        model_result = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
        result = {
            "result": model_result,
            "is_error": bool(raw.get("is_error", False)),
            "widget": {"type": "browser", **safe},
        }
        # The canonical model loop keeps result envelopes below 64 KiB.
        # Account for UTF-8 and nested JSON escapes so useful inspection is
        # preserved instead of being replaced by a size-only receipt.
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > _MAX_TOOL_RESULT_BYTES:
            result["result"] = json.dumps({
                "action": action, "truncated": True,
                "result_preview": model_result.encode("utf-8")[:8_000].decode(
                    "utf-8", errors="ignore",
                ),
            }, ensure_ascii=False)
            result["widget"] = {"type": "browser", "action": action, "truncated": True}
        return result

    return invoke


def _model_value(value: Any, *, cookie_values: bool = False, depth: int = 0) -> Any:
    """Keep structured inspection useful without echoing browser credentials."""
    if depth >= 16:
        return "[truncated-depth]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if (
                normalized in _SECRET_KEYS
                or normalized.endswith("_token")
                or (cookie_values and normalized == "value")
                or normalized in {"base64", "data_url", "screenshot_base64"}
            ):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = _model_value(
                    item, cookie_values=cookie_values or normalized == "cookies",
                    depth=depth + 1,
                )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _model_value(item, cookie_values=cookie_values, depth=depth + 1)
            for item in value[:512]
        ]
    if isinstance(value, str):
        return value[:60_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


HOST_PROVIDER_FACTORY = SingleOperationHostFactoryV4(
    function_id=FUNCTION,
    contract_id=CONTRACT,
    operation_id=OPERATION,
    bind=_bind,
)
