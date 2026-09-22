from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from domain.temporal_context import add_temporal_context_message, current_datetime_context

from .client import AIClient
from .providers.stub_provider import StubProvider


class LLMGateway:
    """Thin gateway that keeps orchestration concerns out of provider adapters."""

    def __init__(
        self,
        client: Optional[AIClient] = None,
    ) -> None:
        self._client = client or AIClient()

    def complete(self, request: Dict[str, Any]) -> Dict[str, Any]:
        model = str(request.get("model", ""))
        messages = _messages_with_temporal_context(request)
        tools = list(request.get("tools", []))
        params = self._params_for_client(request)
        return self._client.complete(model, messages, tools=tools, params=params)

    def stream(self, request: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        model = str(request.get("model", ""))
        messages = _messages_with_temporal_context(request)
        tools = list(request.get("tools", []))
        params = self._params_for_client(request)
        return self._client.stream(model, messages, tools=tools, params=params)

    def _params_for_client(self, request: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(request.get("params", {}))
        params.pop("_v4_authority_kernel_admitted", None)
        authority_context = request.get("authority_context")
        if isinstance(authority_context, dict) and callable(
            getattr(self._client, "_check_authority_for_model_api", None)
        ):
            params["_authority_context"] = dict(authority_context)
        return params

    def supports_stream(self, model: str) -> bool:
        return bool(self._client.supports_stream(model))

    def resolve_provider(self, model: str) -> tuple[Any, Any]:
        return self._client.resolve_provider(model)

    def has_real_provider(self, model: str) -> bool:
        provider, _ = self.resolve_provider(model)
        return not isinstance(provider, StubProvider)

    def runtime_model_matches(self, model: str) -> list[dict[str, Any]]:
        return list(self._client._runtime_model_matches(str(model or "")))


def _messages_with_temporal_context(request: Dict[str, Any]) -> list[dict[str, Any]]:
    messages = list(request.get("messages", []))
    context: dict[str, Any] = {}
    for key in ("context", "runtime_context"):
        value = request.get(key)
        if isinstance(value, dict):
            context.update(value)
    params = request.get("params")
    if isinstance(params, dict):
        context.update(params)
    context.update(
        {
            key: request.get(key)
            for key in ("timezone", "time_zone", "user_timezone", "tz")
            if request.get(key)
        }
    )
    temporal_context = current_datetime_context(context)
    add_temporal_context_message(messages, context, temporal_context=temporal_context)
    return messages
