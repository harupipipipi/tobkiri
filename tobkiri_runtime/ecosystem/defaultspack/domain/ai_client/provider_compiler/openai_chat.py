from __future__ import annotations

import json
from typing import Any

from domain.ai_client.bridge_plan import PlannedProviderRequest
from domain.ai_client.provider_compiler.base import (
    CompiledProviderRequest,
    ProviderCompiler,
    standard_response_to_ir,
)
from domain.ai_client.providers.openai_provider import OpenAIProvider
from domain.chat.ir import RumiStreamEventIR
from domain.chat.ir_legacy_adapter import ir_to_legacy_standard_messages


class OpenAIChatCompiler(ProviderCompiler):
    api_family = "openai_chat"
    path = "/chat/completions"

    def compile_complete(self, planned: PlannedProviderRequest) -> CompiledProviderRequest:
        params = self._translate_params(planned)
        messages = ir_to_legacy_standard_messages(planned.ir)
        provider = OpenAIProvider()
        body: dict[str, Any] = {"model": planned.model, "messages": provider.build_request(messages)}
        if planned.provider_tools:
            body["tools"] = planned.provider_tools
        self._copy_chat_params(body, params)
        return CompiledProviderRequest(
            api_family=self.api_family,
            provider_id=str(planned.provider_capabilities.get("provider_id") or "openai"),
            model=planned.model,
            path=self.path,
            body=body,
            warnings=[item.to_dict() for item in planned.warnings],
            dropped_features=[item.to_dict() for item in planned.dropped_features],
            trace={"planner": planned.to_dict()},
            legacy_messages=messages,
            metadata=dict(planned.metadata or {}),
        )

    def parse_response(self, raw: dict[str, Any], compiled: CompiledProviderRequest):
        return standard_response_to_ir(OpenAIProvider().parse_response(raw))

    def parse_stream_chunk(self, raw: dict[str, Any], compiled: CompiledProviderRequest) -> list[RumiStreamEventIR]:
        events: list[RumiStreamEventIR] = []
        choices = raw.get("choices", []) if isinstance(raw, dict) else []
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        for choice in choices if isinstance(choices, list) else []:
            delta = choice.get("delta") if isinstance(choice, dict) and isinstance(choice.get("delta"), dict) else {}
            text = delta.get("content")
            if text:
                events.append(RumiStreamEventIR(type="content_delta", delta={"type": "text", "text": str(text)}))
            reasoning = OpenAIProvider._reasoning_text_from_mapping(delta)
            if reasoning:
                events.append(RumiStreamEventIR(type="reasoning_delta", delta={"type": "text", "text": reasoning}))
            for tool_call in delta.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function_def = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                call_id = str(tool_call.get("id") or tool_call.get("index") or "")
                name = str(function_def.get("name") or "")
                if tool_call.get("id") or name:
                    events.append(RumiStreamEventIR(type="tool_call_start", metadata={"id": call_id, "name": name}))
                if function_def.get("arguments"):
                    events.append(
                        RumiStreamEventIR(
                            type="tool_call_delta",
                            metadata={"id": call_id, "name": name, "arguments_chunk": str(function_def.get("arguments"))},
                        )
                    )
            if choice.get("finish_reason"):
                events.append(
                    RumiStreamEventIR(
                        type="stream_end",
                        finish_reason=str(choice.get("finish_reason") or "stop"),
                        usage=usage,
                    )
                )
        if usage and not events:
            events.append(RumiStreamEventIR(type="stream_end", finish_reason="stop", usage=usage))
        return events

    @staticmethod
    def _translate_params(planned: PlannedProviderRequest) -> dict[str, Any]:
        params = dict(planned.params or {})
        thinking_level = str(params.pop("thinking_level", "") or "").strip()
        if thinking_level in {"low", "medium", "high", "xhigh"} and "reasoning_effort" not in params:
            params["reasoning_effort"] = "high" if thinking_level == "xhigh" else thinking_level
        return params

    @staticmethod
    def _copy_chat_params(body: dict[str, Any], params: dict[str, Any]) -> None:
        for key in (
            "temperature",
            "max_tokens",
            "max_completion_tokens",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "stop",
            "response_format",
            "reasoning_effort",
            "tool_choice",
            "parallel_tool_calls",
            "stream_options",
        ):
            if key in params:
                body[key] = params[key]
        extra_body = params.get("extra_body")
        if isinstance(extra_body, dict):
            body.update(extra_body)


def dumps_body(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, sort_keys=True)
