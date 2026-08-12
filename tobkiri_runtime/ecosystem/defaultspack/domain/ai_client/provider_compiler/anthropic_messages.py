from __future__ import annotations

import json

from domain.ai_client.bridge_plan import PlannedProviderRequest
from domain.ai_client.provider_compiler.base import CompiledProviderRequest, ProviderCompiler, standard_response_to_ir
from domain.ai_client.providers.anthropic_provider import AnthropicProvider
from domain.chat.ir_legacy_adapter import ir_to_legacy_standard_messages


class AnthropicMessagesCompiler(ProviderCompiler):
    api_family = "anthropic_messages"

    def compile_complete(self, planned: PlannedProviderRequest):
        legacy = ir_to_legacy_standard_messages(planned.ir)
        messages = []
        system_parts = []
        for message in legacy:
            role = message.get("role", "user")
            if role == "system":
                system_parts.append(str(message.get("content") or ""))
                continue
            content = _anthropic_content(message)
            messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
        tools = []
        for tool in planned.provider_tools:
            function_def = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            if function_def:
                tools.append(
                    {
                        "name": function_def.get("name", ""),
                        "description": function_def.get("description", ""),
                        "input_schema": function_def.get("parameters", {"type": "object", "properties": {}}),
                    }
                )
        body = {"model": planned.model, "messages": messages, "max_tokens": planned.params.get("max_tokens", planned.params.get("max_completion_tokens", 1024))}
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if tools:
            body["tools"] = tools
        return CompiledProviderRequest(
            api_family=self.api_family,
            provider_id=str(planned.provider_capabilities.get("provider_id") or "anthropic"),
            model=planned.model,
            path="/v1/messages",
            body=body,
            warnings=[item.to_dict() for item in planned.warnings],
            dropped_features=[item.to_dict() for item in planned.dropped_features],
            trace={"planner": planned.to_dict()},
            legacy_messages=legacy,
            metadata={**dict(planned.metadata or {}), "tool_runtime": "client"},
        )

    def parse_response(self, raw, compiled):
        blocks = []
        thinking_parts = []
        for part in raw.get("content", []) if isinstance(raw, dict) else []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif part.get("type") == "tool_use":
                blocks.append({"type": "tool_use", "id": part.get("id", ""), "name": part.get("name", ""), "input": part.get("input", {})})
            elif part.get("type") in {"thinking", "reasoning", "trace"}:
                text = AnthropicProvider._private_reasoning_text(part)
                if text:
                    thinking_parts.append(text)
        metadata = {"api_family": compiled.api_family, "tool_runtime": compiled.metadata.get("tool_runtime")}
        if thinking_parts:
            metadata["thinking"] = {
                "state": "completed",
                "transcript": "\n\n".join(thinking_parts),
                "source": "provider_reasoning_trace",
            }
        return standard_response_to_ir(
            {
                "content": blocks,
                "finish_reason": raw.get("stop_reason") or "stop",
                "usage": {
                    "input_tokens": (raw.get("usage") or {}).get("input_tokens", 0),
                    "output_tokens": (raw.get("usage") or {}).get("output_tokens", 0),
                    "total_tokens": (raw.get("usage") or {}).get("input_tokens", 0) + (raw.get("usage") or {}).get("output_tokens", 0),
                },
                "metadata": metadata,
            }
        )


def _anthropic_content(message):
    content = message.get("content", "")
    if message.get("tool_calls"):
        parts = []
        if content:
            parts.append({"type": "text", "text": str(content)})
        for tool_call in message.get("tool_calls", []) or []:
            function_def = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            args = function_def.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"value": args}
            parts.append({"type": "tool_use", "id": tool_call.get("id", ""), "name": function_def.get("name", ""), "input": args})
        return parts
    if message.get("role") == "tool":
        return [{"type": "tool_result", "tool_use_id": message.get("tool_call_id", ""), "content": str(content or "")}]
    if isinstance(content, list):
        return [{"type": "text", "text": str(part.get("text") or part)} for part in content]
    return str(content or "")
