from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from domain.external.event import ExternalEvent
from domain.external.input_profile import InputProfile
from domain.external.redaction import redact_sensitive
from domain.external.response import RumiResponse
from domain.input.envelope import RumiInputEnvelope


KNOWN_ACTIONS = {
    "reply_text",
    "store_only",
    "summarize_then_reply",
    "run_browser_use",
    "run_computer_use",
    "run_python",
    "run_tool",
    "send_file_if_allowed",
    "ask_for_approval",
}
TOOL_ACTIONS = {
    "run_browser_use": "browser_use",
    "run_computer_use": "computer_use",
    "run_python": "python",
    "run_tool": "tool",
}
EXTERNAL_REPLY_ACTIONS = {"reply_text", "summarize_then_reply", "send_file_if_allowed"}
SENSITIVITY_VALUES = {"public", "private", "local_only"}
_INTERPOLATION_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class ResponsePromptDecision:
    action: str = "reply_text"
    reason: str = ""
    instruction: str = ""
    response_style: str = ""
    sensitivity: str = "public"
    requires_approval: bool = False
    approved_for_execution: bool = False
    fallback: bool = False
    error: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def sends_external_reply(self) -> bool:
        return self.sensitivity != "local_only" and self.action in EXTERNAL_REPLY_ACTIONS

    @property
    def allows_external_reply(self) -> bool:
        return self.sends_external_reply

    @property
    def executable(self) -> bool:
        return not self.requires_approval or self.approved_for_execution

    @property
    def tool_name(self) -> str:
        return str(self.metadata.get("tool") or TOOL_ACTIONS.get(self.action, "") or "")

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "action": self.action,
            "reason": self.reason,
            "instruction": self.instruction,
            "response_style": self.response_style,
            "sensitivity": self.sensitivity,
            "requires_approval": self.requires_approval,
            "approved_for_execution": self.approved_for_execution,
            "allows_external_reply": self.allows_external_reply,
            "executable": self.executable,
            "fallback": self.fallback,
            "output": redact_sensitive(dict(self.output)),
            "metadata": dict(self.metadata),
        }
        if self.error:
            payload["error"] = self.error
        if self.tool_name:
            payload["tool"] = self.tool_name
        return payload


class ResponsePromptPolicy:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config) if isinstance(config, dict) else {}
        self.enabled = bool(self.config.get("enabled"))
        self.model = str(self.config.get("model") or "inherit").strip() or "inherit"
        self.mode = str(self.config.get("mode") or "plan_only").strip() or "plan_only"
        self.system_prompt = str(
            self.config.get("system_prompt")
            or "You are a response routing planner for Rumi. Decide only from allowed actions and return strict JSON."
        )
        self.user_prompt = str(
            self.config.get("user_prompt")
            or (
                "Provider: ${event.provider}\n"
                "Scope: ${event.scope.type}:${event.scope.id}\n"
                "Actor: ${event.actor.id}\n"
                "User input: ${input.text}\n"
                "Assistant result: ${response.text}"
            )
        )
        self.allowed_actions = self._parse_allowed_actions(self.config.get("allowed_actions"))
        self.allowed_outputs = self._parse_allowed_outputs(self.config.get("allowed_outputs"))
        fallback_action = str(self.config.get("fallback_action") or "reply_text").strip()
        self.fallback_action = fallback_action if fallback_action in self.allowed_actions else sorted(self.allowed_actions)[0]
        tools_value = self.config.get("tools")
        self.tools: dict[str, dict[str, object]] = (
            {
                str(key): {str(item_key): item_value for item_key, item_value in value.items()}
                for key, value in tools_value.items()
                if isinstance(value, dict)
            }
            if isinstance(tools_value, dict)
            else {}
        )

    @classmethod
    def from_profile(cls, profile: InputProfile | dict[str, Any] | None) -> "ResponsePromptPolicy":
        spec: dict[str, Any]
        if isinstance(profile, InputProfile):
            spec = profile.spec
        elif isinstance(profile, dict):
            spec = profile
        else:
            spec = {}
        config = spec.get("response_prompt") if isinstance(spec.get("response_prompt"), dict) else {}
        return cls(config)

    def decide(
        self,
        event: ExternalEvent | dict[str, Any],
        *,
        envelope: RumiInputEnvelope | dict[str, Any] | None = None,
        response: RumiResponse | dict[str, Any] | None = None,
        input_text: str = "",
        response_text: str = "",
        llm_client: Callable[..., Any] | Any | None = None,
        approved_for_execution: bool = False,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ResponsePromptDecision:
        event = event if isinstance(event, ExternalEvent) else ExternalEvent.from_dict(event)
        envelope = self._coerce_envelope(envelope, input_text=input_text)
        response = self._coerce_response(response, response_text=response_text)
        context = context or {}
        base_metadata = dict(metadata if isinstance(metadata, dict) else {})

        if not self.enabled:
            return self._fallback_decision("policy disabled", base_metadata)
        if self.mode != "plan_only":
            return self._fallback_decision("unsupported response prompt mode: " + self.mode, base_metadata)

        if isinstance(context.get("response_prompt_decision"), dict):
            raw = context["response_prompt_decision"]
        else:
            llm_client = llm_client or context.get("response_prompt_llm")
            if llm_client is None:
                return self._fallback_decision("llm client not configured", base_metadata)
            rendered = self.render_prompts(event, envelope=envelope, response=response)
            try:
                raw = self._call_llm(llm_client, rendered)
            except Exception as exc:  # pragma: no cover - callers inject exact failures.
                error_metadata = dict(base_metadata)
                error_metadata["error_type"] = exc.__class__.__name__
                return self._fallback_decision("llm call failed", error_metadata, error=str(exc))

        data = self._parse_json_object(raw)
        if data is None:
            return self._fallback_decision("invalid json", base_metadata, error=self._llm_text(raw))
        return self._decision_from_payload(data, base_metadata, approved_for_execution)

    def render_prompts(
        self,
        event: ExternalEvent | dict[str, Any],
        *,
        envelope: RumiInputEnvelope | dict[str, Any] | None = None,
        response: RumiResponse | dict[str, Any] | None = None,
        input_text: str = "",
        response_text: str = "",
    ) -> dict[str, str]:
        event = event if isinstance(event, ExternalEvent) else ExternalEvent.from_dict(event)
        envelope = self._coerce_envelope(envelope, input_text=input_text)
        response = self._coerce_response(response, response_text=response_text)
        tokens = self._safe_tokens(event, envelope=envelope, response=response)
        return {
            "system": self._render_template(self.system_prompt, tokens),
            "user": self._render_template(self.user_prompt, tokens),
        }

    def build_messages(
        self,
        *,
        event: ExternalEvent,
        envelope: RumiInputEnvelope,
        response: RumiResponse,
    ) -> list[dict[str, str]]:
        rendered = self.render_prompts(event, envelope=envelope, response=response)
        return [
            {"role": "system", "content": rendered["system"]},
            {"role": "user", "content": rendered["user"]},
        ]

    def _decision_from_payload(
        self,
        data: dict[str, Any],
        metadata: dict[str, Any],
        approved_for_execution: bool,
    ) -> ResponsePromptDecision:
        action = str(data.get("action") or "").strip()
        if action not in self.allowed_actions:
            rejected_metadata = dict(metadata)
            rejected_metadata["rejected_action"] = action or "missing action"
            return self._fallback_decision("action not allowed", rejected_metadata)
        if action not in KNOWN_ACTIONS:
            rejected_metadata = dict(metadata)
            rejected_metadata["rejected_action"] = action
            return self._fallback_decision("unknown action", rejected_metadata)

        tool_name = self._tool_name_for_decision(data, action)
        if tool_name and not self._tool_enabled(tool_name):
            rejected_metadata = dict(metadata)
            rejected_metadata["rejected_tool"] = tool_name
            return self._fallback_decision("tool action disabled", rejected_metadata)

        sensitivity = str(data.get("sensitivity") or "public").strip().lower() or "public"
        if sensitivity not in SENSITIVITY_VALUES:
            sensitivity = "public"
        decision_metadata = dict(metadata)
        if isinstance(data.get("metadata"), dict):
            decision_metadata.update(data["metadata"])
        if tool_name:
            decision_metadata["tool"] = tool_name
        output = self._parse_output(data)
        if output and not self._output_allowed(output):
            rejected_metadata = dict(metadata)
            rejected_metadata["rejected_output"] = redact_sensitive(output)
            return self._fallback_decision("output target not allowed", rejected_metadata)

        requires_approval = bool(data.get("requires_approval")) or self._requires_approval(action, tool_name)
        return ResponsePromptDecision(
            action=action,
            reason=str(data.get("reason") or ""),
            instruction=str(data.get("instruction") or ""),
            response_style=str(data.get("response_style") or ""),
            sensitivity=sensitivity,
            requires_approval=requires_approval,
            approved_for_execution=bool(approved_for_execution or data.get("approved_for_execution")),
            output=output,
            metadata=decision_metadata,
        )

    def _fallback_decision(
        self,
        reason: str,
        metadata: dict[str, Any] | None = None,
        *,
        error: str = "",
    ) -> ResponsePromptDecision:
        fallback_metadata = dict(metadata if isinstance(metadata, dict) else {})
        fallback_metadata["allowed_actions"] = sorted(self.allowed_actions)
        return ResponsePromptDecision(
            action=self.fallback_action,
            reason=reason,
            sensitivity="public",
            fallback=True,
            error=error,
            output={},
            metadata=fallback_metadata,
        )

    def _parse_allowed_actions(self, value: Any) -> set[str]:
        if isinstance(value, list):
            actions = {str(item).strip() for item in value if str(item).strip()}
        elif isinstance(value, str) and value.strip():
            actions = {value.strip()}
        else:
            actions = {"reply_text"}
        actions = {item for item in actions if item in KNOWN_ACTIONS}
        return actions or {"reply_text"}

    @staticmethod
    def _parse_allowed_outputs(value: Any) -> set[str]:
        if not isinstance(value, list):
            return set()
        return {str(item).strip() for item in value if str(item).strip()}

    @staticmethod
    def _parse_output(data: dict[str, Any]) -> dict[str, Any]:
        raw = data.get("output") if isinstance(data.get("output"), dict) else data.get("target")
        output = dict(raw) if isinstance(raw, dict) else {}
        output_profile_id = str(data.get("output_profile_id") or output.get("output_profile_id") or "").strip()
        if output_profile_id:
            output["output_profile_id"] = output_profile_id
        provider = str(data.get("output_provider") or output.get("provider") or "").strip()
        if provider:
            output["provider"] = provider
        return output

    def _output_allowed(self, output: dict[str, Any]) -> bool:
        if not self.allowed_outputs:
            return True
        output_id = str(output.get("output_profile_id") or output.get("id") or "").strip()
        provider = str(output.get("provider") or "").strip()
        return bool((output_id and output_id in self.allowed_outputs) or (provider and provider in self.allowed_outputs))

    def _tool_name_for_decision(self, data: dict[str, Any], action: str) -> str:
        tool = data.get("tool") or data.get("tool_name")
        if isinstance(tool, dict):
            tool = tool.get("name")
        if tool:
            return str(tool).strip()
        return TOOL_ACTIONS.get(action, "")

    def _tool_enabled(self, tool_name: str) -> bool:
        config_value = self.tools.get(tool_name)
        config: dict[str, object] = (
            {str(key): item for key, item in config_value.items()}
            if isinstance(config_value, dict)
            else {}
        )
        if not config:
            return False
        return bool(config.get("enabled"))

    def _requires_approval(self, action: str, tool_name: str = "") -> bool:
        if action == "ask_for_approval":
            return True
        tool_name = tool_name or TOOL_ACTIONS.get(action, "")
        if not tool_name:
            return False
        config_value = self.tools.get(tool_name)
        config: dict[str, object] = (
            {str(key): item for key, item in config_value.items()}
            if isinstance(config_value, dict)
            else {}
        )
        if tool_name in {"computer_use", "tool"}:
            return bool(config.get("requires_approval", True))
        return bool(config.get("requires_approval", False))

    def _call_llm(self, llm_client: Any, rendered: dict[str, str]) -> Any:
        payload = {
            "model": self.model,
            "mode": self.mode,
            "messages": [
                {"role": "system", "content": rendered["system"]},
                {"role": "user", "content": rendered["user"]},
            ],
            "response_format": {"type": "json_object"},
            "allowed_actions": sorted(self.allowed_actions),
        }
        if callable(llm_client):
            try:
                return llm_client(payload)
            except TypeError:
                return llm_client(
                    system_prompt=rendered["system"],
                    user_prompt=rendered["user"],
                    model=self.model,
                    mode=self.mode,
                )
        if hasattr(llm_client, "decide"):
            return llm_client.decide(payload)
        if hasattr(llm_client, "complete"):
            return llm_client.complete(self.model, payload["messages"], [], {"response_format": payload["response_format"]})
        raise TypeError("llm_client must be callable or expose decide/complete")

    @staticmethod
    def _parse_json_object(raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, dict):
            if "action" in raw:
                return raw
            text = ResponsePromptPolicy._llm_text(raw)
            if not text:
                return None
            raw = text
        if not isinstance(raw, str):
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _llm_text(raw: Any) -> str:
        if isinstance(raw, str):
            return raw.strip()
        if not isinstance(raw, dict):
            return ""
        content = raw.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            return "\n".join(parts).strip()
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message_value = first.get("message")
            message: dict[str, object] = (
                {str(key): item for key, item in message_value.items()}
                if isinstance(message_value, dict)
                else {}
            )
            return str(message.get("content") or first.get("text") or "").strip()
        return ""

    @staticmethod
    def _render_template(template: str, tokens: dict[str, str]) -> str:
        return _INTERPOLATION_RE.sub(lambda match: tokens.get(match.group(1).strip(), ""), template)

    @staticmethod
    def _safe_tokens(event: ExternalEvent, *, envelope: RumiInputEnvelope, response: RumiResponse) -> dict[str, str]:
        return {
            "event.provider": str(event.provider or ""),
            "event.workspace.type": str(event.workspace.type or ""),
            "event.workspace.id": str(event.workspace.id or ""),
            "event.scope.type": str(event.scope.type or ""),
            "event.scope.id": str(event.scope.id or ""),
            "event.actor.type": str(event.actor.type or ""),
            "event.actor.id": str(event.actor.id or ""),
            "event.conversation.type": str(event.conversation.type or ""),
            "event.conversation.id": str(event.conversation.id or ""),
            "event.event.id": str(event.event.get("id") or ""),
            "event.event.message_id": str(event.event.get("message_id") or ""),
            "event.event.type": str(event.event.get("type") or ""),
            "input.text": str(envelope.input or ""),
            "input.role": str(envelope.role or ""),
            "response.text": str(response.text or ""),
        }

    @staticmethod
    def _coerce_envelope(envelope: RumiInputEnvelope | dict[str, Any] | None, *, input_text: str = "") -> RumiInputEnvelope:
        if isinstance(envelope, RumiInputEnvelope):
            return envelope
        if isinstance(envelope, dict):
            return RumiInputEnvelope.from_dict(envelope)
        return RumiInputEnvelope(role="user", input=input_text, chat={}, source={}, metadata={})

    @staticmethod
    def _coerce_response(response: RumiResponse | dict[str, Any] | None, *, response_text: str = "") -> RumiResponse:
        if isinstance(response, RumiResponse):
            return response
        if isinstance(response, dict):
            return RumiResponse.from_result(response)
        return RumiResponse(text=response_text)


def decide_response_prompt(
    config: dict[str, Any] | None,
    event: ExternalEvent | dict[str, Any],
    *,
    input_text: str = "",
    response_text: str = "",
    llm_client: Callable[..., Any] | Any | None = None,
    approved_for_execution: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ResponsePromptPolicy(config).decide(
        event,
        input_text=input_text,
        response_text=response_text,
        llm_client=llm_client,
        approved_for_execution=approved_for_execution,
        metadata=metadata,
    ).as_dict()
