"""Finite saved-turn callbacks behind authenticated transport and captured Broker.

The supervisor owns signature, binding digest, nonce and continuation ordering.
These callbacks own input scope and dispatch selection, never guest authority.
Route readiness is not a credential/network probe or a promise of AI success.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

from tobkiri_host.saved_turn_plan import TARGETS, TOOL, SavedToolFrame
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.saved_conversation import (
    MAX_SAVED_IMAGE_COUNT,
    MAX_SAVED_INPUT_BYTES,
    is_saved_user_content,
    saved_user_text,
    validate_saved_conversation_context,
    validate_saved_conversation_input,
)

from tobkiri_protocol.saved_tools import MAX_SAVED_TOOL_HOPS, saved_tool_messages, saved_tool_logs
from tobkiri_protocol.saved_context import (
    PROMPT_TARGET,
    resolved_saved_prompt,
    saved_prompt_digest,
    saved_prompt_reference,
)

from ..authority.v4 import AuthorityDenied

Target = tuple[str, str]


def project_saved_ai_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Cross the strict guest ABI with reply data, not floating-point telemetry."""
    return {
        "status": value.get("status"),
        "output": value.get("output"),
        "tool_intents": value.get("tool_intents", []),
    }


READINESS: Target = (
    "tobkiri.resource.ai.readiness.v1",
    "rumi_ai_gateway_pack.ai-gateway.preflight",
)
REQUIRED_TARGETS = (*dict.fromkeys(TARGETS), READINESS)
DEFINITION: Target = (
    "tobkiri.resource.tool.definition.v1",
    "rumi_tool_registry_pack.tool-definition-resource",
)
TOOL_TARGETS = (DEFINITION, TOOL)
ALLOWED_TARGETS = (*REQUIRED_TARGETS, *TOOL_TARGETS, PROMPT_TARGET)
Dispatch = Callable[[object, Target, Mapping[str, Any]], Mapping[str, Any]]
RequireTargets = Callable[[object, tuple[Target, ...]], None]
ResolveThinkingParameters = Callable[[str, str], Mapping[str, Any]]
ResolveSavedInputCapabilities = Callable[[str], Mapping[str, Any]]


def _request(outer: object) -> dict[str, Any]:
    payload = getattr(outer, "payload", None)
    if not isinstance(payload, Mapping):
        raise AuthorityDenied("saved bridge initial input is missing")
    return validate_saved_conversation_input(payload)["request"]


def _require_resolved_context(conversation: Mapping[str, Any]) -> None:
    """Reject owned context that the text-only saved path cannot resolve."""
    try:
        validate_saved_conversation_context(conversation)
    except ValueError as error:
        raise AuthorityDenied(str(error)) from error


def _contains_inline_images(messages: list[Mapping[str, Any]]) -> bool:
    """Return whether owner-built messages retain any saved inline image block."""
    return any(
        isinstance(message.get("content"), list)
        and any(
            isinstance(block, Mapping) and block.get("type") == "image_url"
            for block in message["content"]
        )
        for message in messages
    )


def _messages(
    conversation: Mapping[str, Any],
    *,
    flatten_text_blocks: bool = False,
    system_prompt: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Independently constrain selected history and bounded inline images."""
    _require_resolved_context(conversation)
    prompt_id = saved_prompt_reference(conversation)
    if (system_prompt is None) != (prompt_id is None) or (
        system_prompt is not None and system_prompt["prompt_id"] != prompt_id
    ):
        raise AuthorityDenied("saved bridge system prompt is unresolved")
    prefix = (
        [{"role": "system", "content": system_prompt["body"]}]
        if system_prompt is not None and system_prompt["body"]
        else []
    )
    messages = conversation.get("messages")
    if not isinstance(messages, list) or len(messages) > 200:
        raise AuthorityDenied("saved bridge owner history is invalid")
    by_id: dict[str, Mapping[str, Any]] = {}
    for message in messages:
        if not isinstance(message, Mapping):
            raise AuthorityDenied("saved bridge owner message is invalid")
        identifier = message.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in by_id:
            raise AuthorityDenied("saved bridge owner message identity is invalid")
        by_id[identifier] = message
    current = conversation.get("current_node_id")
    if not messages:
        if current is not None:
            raise AuthorityDenied("saved bridge empty history has a current node")
        return prefix
    if not isinstance(current, str) or current not in by_id:
        raise AuthorityDenied("saved bridge current message is unavailable")
    if all(message.get("parent_id") is None for message in messages):
        selected = messages[: messages.index(by_id[current]) + 1]
    else:
        selected = []
        seen: set[str] = set()
        node: object = current
        while node is not None:
            if not isinstance(node, str) or node not in by_id or node in seen:
                raise AuthorityDenied("saved bridge history is cyclic or incomplete")
            seen.add(node)
            selected.append(by_id[node])
            node = by_id[node].get("parent_id")
        selected.reverse()
    result: list[dict[str, Any]] = list(prefix)
    for message in selected:
        content = message.get("content")
        text_only = isinstance(content, str) or (
            isinstance(content, list)
            and bool(content)
            and all(
                isinstance(part, Mapping)
                and set(part) == {"type", "text"}
                and part["type"] == "text"
                and isinstance(part["text"], str)
                for part in content
            )
        )
        bounded_user_content = message.get("role") == "user" and is_saved_user_content(content)
        if (
            message.get("role") not in {"system", "user", "assistant"}
            or message.get("status") != "complete"
            or not (text_only or bounded_user_content)
            or message.get("parts")
            or message.get("widget")
        ):
            raise AuthorityDenied("saved bridge additional content resolution is required")
        metadata = message.get("metadata") or {}
        trace = saved_tool_messages(metadata.get("saved_tool_messages", []))
        logs = message.get("tool_logs")
        if (trace and message["role"] != "assistant") or canonical_json(
            [] if logs is None else logs
        ) != canonical_json(saved_tool_logs(trace)):
            raise AuthorityDenied("saved bridge owned tool transcript is invalid")
        if flatten_text_blocks:
            # Readiness accepts text-only messages. Preserve every tool argument
            # and result as text there; generation receives the exact structures.
            result.extend(
                {"role": "assistant", "content": canonical_json(item).decode()} for item in trace
            )
        else:
            result.extend(trace)
        # Retain the owner's exact text blocks for guest equality checks and
        # generation. Only the text-only readiness API receives joined text.
        if flatten_text_blocks and bounded_user_content:
            content = saved_user_text(content)
        elif flatten_text_blocks and isinstance(content, list):
            content = "".join(part["text"] for part in content)
        result.append({"role": message["role"], "content": content})
    if flatten_text_blocks:
        return result
    return _bounded_inline_image_history(result)


def _bounded_inline_image_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest saved images and make omitted earlier images explicit."""
    images: list[tuple[int, int]] = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if isinstance(content, list):
            images.extend(
                (message_index, block_index)
                for block_index, block in enumerate(content)
                if isinstance(block, Mapping) and block.get("type") == "image_url"
            )
    retained = set(images[-MAX_SAVED_IMAGE_COUNT:])
    if len(retained) == len(images):
        return messages
    bounded: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            bounded.append(message)
            continue
        omitted = 0
        blocks: list[dict[str, Any]] = []
        for block_index, block in enumerate(content):
            if isinstance(block, Mapping) and block.get("type") == "image_url" and (
                message_index, block_index
            ) not in retained:
                omitted += 1
                continue
            blocks.append(dict(block))
        if omitted:
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        "[Earlier inline image omitted from this saved-turn context "
                        "because of the bounded image limit.]"
                    ),
                }
            )
        bounded.append({**message, "content": blocks})
    return bounded


def _saved_read_projection(conversation: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the guest state needed to prevent a duplicate append.

    Image bytes stay Host-owned until the generation stage.  The pure guest
    only needs message identities to detect an existing turn and the selected
    branch/model to form its fixed next intent.
    """
    messages = conversation.get("messages")
    if not isinstance(messages, list) or len(messages) > 200:
        raise AuthorityDenied("saved bridge owner history is invalid")
    identifiers: list[dict[str, str]] = []
    for message in messages:
        identifier = message.get("id") if isinstance(message, Mapping) else None
        if not isinstance(identifier, str) or not identifier:
            raise AuthorityDenied("saved bridge owner message identity is invalid")
        identifiers.append({"id": identifier})
    return {
        "id": conversation.get("id"),
        "conversation_revision": conversation.get("conversation_revision"),
        "model_reference": conversation.get("model_reference"),
        "current_node_id": conversation.get("current_node_id"),
        "agent_id": conversation.get("agent_id"),
        "system_prompt_id": conversation.get("system_prompt_id"),
        "messages": identifiers,
    }


class SavedBridgeCallbacks:
    """Bind the four guest stages to finite Host-owned dispatch callbacks."""

    def __init__(
        self,
        dispatch: Dispatch,
        require_targets: RequireTargets,
        resolve_thinking_parameters: ResolveThinkingParameters | None = None,
        resolve_saved_input_capabilities: ResolveSavedInputCapabilities | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._require_targets = require_targets
        self._resolve_thinking_parameters = resolve_thinking_parameters
        self._resolve_saved_input_capabilities = resolve_saved_input_capabilities

    def _require_image_input_capability(self, model_reference: str) -> None:
        """Require owner-proven image support before a saved image is persisted."""
        if self._resolve_saved_input_capabilities is None:
            raise AuthorityDenied(
                "Choose an image-capable model before sending this conversation."
            )
        try:
            capabilities = self._resolve_saved_input_capabilities(model_reference)
        except Exception as error:
            raise AuthorityDenied(
                "Choose an image-capable model before sending this conversation."
            ) from error
        if (
            not isinstance(capabilities, Mapping)
            or (
                capabilities.get("supports_image_input") is not True
                and capabilities.get("supports_vision") is not True
            )
        ):
            raise AuthorityDenied(
                "Choose an image-capable model before sending this conversation."
            )

    def _conversation(self, outer: object, request: Mapping[str, Any]) -> Mapping[str, Any]:
        outcome = self._dispatch(
            outer,
            TARGETS[0],
            {
                "operation": "get",
                "conversation_id": request["conversation_id"],
            },
        )
        value = outcome.get("value")
        conversation = value.get("conversation") if isinstance(value, Mapping) else None
        if (
            outcome.get("status") != "ok"
            or not isinstance(conversation, Mapping)
            or conversation.get("id") != request["conversation_id"]
        ):
            raise AuthorityDenied("saved bridge conversation is unavailable")
        return conversation

    def _tools(self, outer: object, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if request.get("tool_selection", {}).get("mode", "none") == "none":
            return {"tools": [], "definitions": {}}
        self._require_targets(outer, TOOL_TARGETS)
        outcome = self._dispatch(
            outer,
            DEFINITION,
            {
                "operation": "select",
                "selection": request["tool_selection"],
            },
        )
        value = outcome.get("value")
        if (
            outcome.get("status") != "ok"
            or not isinstance(value, Mapping)
            or set(value) != {"tools", "definitions"}
            or not isinstance(value["tools"], list)
            or not isinstance(value["definitions"], dict)
            or len(value["tools"]) != len(value["definitions"])
            or len(canonical_json(dict(value))) > 512 * 1024
        ):
            raise AuthorityDenied("saved bridge tool selection is unavailable")
        names = []
        for tool in value["tools"]:
            function = tool.get("function") if isinstance(tool, dict) else None
            if (
                not isinstance(function, dict)
                or tool.get("type") != "function"
                or not isinstance(function.get("name"), str)
                or not isinstance(function.get("parameters"), dict)
            ):
                raise AuthorityDenied("saved bridge selected tool schema is invalid")
            name = function["name"]
            digest = value["definitions"].get(name)
            if (
                name in names
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise AuthorityDenied("saved bridge selected tool identity is invalid")
            names.append(name)
        return value

    def _system_prompt(
        self,
        outer: object,
        conversation: Mapping[str, Any],
    ) -> dict[str, str] | None:
        """Read the explicit prompt through the current captured owner edge."""
        try:
            prompt_id = saved_prompt_reference(conversation)
            if prompt_id is None:
                return None
            self._require_targets(outer, (PROMPT_TARGET,))
            outcome = self._dispatch(
                outer,
                PROMPT_TARGET,
                {
                    "operation": "get",
                    "prompt_id": prompt_id,
                },
            )
            value = outcome.get("value")
            profile_id = getattr(getattr(outer, "context", None), "profile_id", None)
            if (
                outcome.get("status") != "ok"
                or not isinstance(value, Mapping)
                or not isinstance(profile_id, str)
                or not profile_id
            ):
                raise ValueError("saved conversation prompt is unavailable")
            return resolved_saved_prompt(value, prompt_id=prompt_id, profile_id=profile_id)
        except ValueError as error:
            raise AuthorityDenied(str(error)) from error

    def preflight(self, outer: object) -> None:
        """Check every captured target and owned context before any user append."""
        request = _request(outer)
        self._require_targets(outer, REQUIRED_TARGETS)
        tools = self._tools(outer, request)
        conversation = self._conversation(outer, request)
        prompt = self._system_prompt(outer, conversation)
        revision = conversation.get("conversation_revision")
        if type(revision) is not int or revision != request["conversation_revision"]:
            raise AuthorityDenied("saved bridge conversation revision changed")
        model = conversation.get("model_reference")
        if not isinstance(model, str) or not model.strip():
            raise AuthorityDenied("saved bridge owned model is unavailable")
        owner_messages = _messages(conversation, system_prompt=prompt)
        requires_image_input = _contains_inline_images(owner_messages) or _contains_inline_images(
            [{"role": "user", "content": request["content"]}]
        )
        if requires_image_input:
            self._require_image_input_capability(model)
        payload: dict[str, Any] = {
            "model_profile_id": model,
            "messages": [
                *_messages(conversation, flatten_text_blocks=True, system_prompt=prompt),
                {"role": "user", "content": saved_user_text(request["content"])},
            ],
        }
        if requires_image_input:
            payload["modalities"] = ["image", "text"]
        if tools["tools"]:
            payload["tool_calling"] = True
        if len(canonical_json(payload)) > MAX_SAVED_INPUT_BYTES:
            raise AuthorityDenied("saved bridge readiness input exceeds budget")
        outcome = self._dispatch(outer, READINESS, payload)
        value = outcome.get("value")
        if (
            outcome.get("status") != "ok"
            or not isinstance(value, Mapping)
            or value.get("ready") is not True
            or value.get("model_profile_id") != model
        ):
            raise AuthorityDenied("saved bridge AI route is unavailable")

    def __call__(
        self, outer: object, frame: Mapping[str, Any] | SavedToolFrame
    ) -> Mapping[str, Any]:
        """Dispatch authenticated continuation scope through captured targets."""
        request = _request(outer)
        enabled = request.get("tool_selection", {}).get("mode", "none") != "none"
        trace: list[dict[str, Any]] = []
        if enabled:
            if not isinstance(frame, SavedToolFrame) or frame.initial_digest != canonical_digest(
                {"request": request}
            ):
                raise AuthorityDenied("saved bridge requires Host-checked tool scope")
            stage = frame.stage
            # Tool-stage scope may contain assistant calls awaiting their results.
            trace = strict_loads(frame.tool_messages, max_bytes=40 * 1024, max_depth=12)
            if stage != "tool":
                trace = saved_tool_messages(trace)
            frame = frame.frame
        elif isinstance(frame, SavedToolFrame):
            raise AuthorityDenied("saved bridge unexpected tool scope")
        else:
            hop = frame.get("hop")
            if type(hop) is not int or not 0 <= hop < len(TARGETS):
                raise AuthorityDenied("saved bridge frame identity is invalid")
            stage = ("read", "user", "ai", "assistant")[hop]
        hop = frame.get("hop")
        if (
            frame.get("kind") != "tobkiri.packvm.continuation.request.v2"
            or type(frame.get("version")) is not int
            or frame["version"] != 2
            or frame.get("request_id")
            != getattr(getattr(outer, "context", None), "request_id", None)
            or type(hop) is not int
            or not 0 <= hop < (MAX_SAVED_TOOL_HOPS if enabled else len(TARGETS))
        ):
            raise AuthorityDenied("saved bridge frame identity is invalid")
        target = {
            "read": TARGETS[0],
            "user": TARGETS[1],
            "ai": TARGETS[2],
            "assistant": TARGETS[3],
            "tool": TOOL,
        }.get(stage)
        if target is None or frame.get("target") != {
            "contract_id": target[0],
            "operation_id": target[1],
        }:
            raise AuthorityDenied("saved bridge stage target is invalid")
        self._require_targets(outer, REQUIRED_TARGETS)
        payload = frame.get("payload")
        if not isinstance(payload, Mapping) or len(canonical_json(dict(payload))) > MAX_SAVED_INPUT_BYTES:
            raise AuthorityDenied("saved bridge payload is invalid")
        if stage == "read":
            if dict(payload) != {"operation": "get", "conversation_id": request["conversation_id"]}:
                raise AuthorityDenied("saved bridge conversation read is out of scope")
            conversation = self._conversation(outer, request)
            value: dict[str, Any] = {"conversation": _saved_read_projection(conversation)}
            prompt = self._system_prompt(outer, conversation)
            if prompt is not None:
                value["system_prompt_digest"] = saved_prompt_digest(prompt)
            return {"status": "ok", "value": value}
        elif stage in {"user", "assistant"}:
            self._check_append(
                request, payload, 1 if stage == "user" else 3, trace if stage == "assistant" else []
            )
            if stage == "user":
                self._tools(outer, request)
                conversation = self._conversation(outer, request)
                _require_resolved_context(conversation)
                prompt = self._system_prompt(outer, conversation)
                if payload.get("system_prompt_digest") != saved_prompt_digest(prompt):
                    raise AuthorityDenied("saved bridge system prompt changed before append")
                if (
                    payload["expected_conversation_revision"] != request["conversation_revision"]
                    or conversation.get("conversation_revision") != request["conversation_revision"]
                    or payload["message"]["parent_id"] != conversation.get("current_node_id")
                ):
                    raise AuthorityDenied("saved bridge selected branch changed")
                payload = {
                    key: value for key, value in payload.items() if key != "system_prompt_digest"
                }
        elif stage == "ai":
            conversation = self._conversation(outer, request)
            prompt = self._system_prompt(outer, conversation)
            expected_fields = {"model_reference", "requirements"}
            if prompt is not None:
                expected_fields.add("system_prompt_digest")
            if (
                set(payload) != expected_fields
                or payload.get("system_prompt_digest") != saved_prompt_digest(prompt)
                or payload["model_reference"] != conversation.get("model_reference")
                or payload["requirements"] != {"request_surface": "conversation.saved"}
            ):
                raise AuthorityDenied("saved bridge AI input differs from the owner")
            selected = self._tools(outer, request)
            arguments = {
                key: value for key, value in payload.items() if key != "system_prompt_digest"
            }
            arguments["messages"] = [
                *_messages(conversation, system_prompt=prompt),
                *trace,
            ]
            owner_requirements = dict(payload["requirements"])
            if _contains_inline_images(arguments["messages"]):
                self._require_image_input_capability(str(conversation["model_reference"]))
                owner_requirements["modalities"] = ["image", "text"]
            arguments["requirements"] = owner_requirements
            if selected["tools"]:
                arguments.update(
                    {
                        "tools": selected["tools"],
                        "requirements": {**owner_requirements, "tool_calling": True},
                        "parameters": {
                            "tool_choice": "required"
                            if request["tool_selection"].get("must_use") and not trace
                            else "auto"
                        },
                    }
                )
            if self._resolve_thinking_parameters is not None:
                model_reference = conversation.get("model_reference")
                conversation_id = conversation.get("id")
                if (
                    not isinstance(model_reference, str)
                    or not model_reference
                    or not isinstance(conversation_id, str)
                    or conversation_id != request["conversation_id"]
                ):
                    raise AuthorityDenied("saved bridge owner model is unavailable")
                try:
                    thinking_parameters = self._resolve_thinking_parameters(
                        model_reference, conversation_id
                    )
                except AuthorityDenied:
                    raise
                except Exception as error:
                    raise AuthorityDenied(
                        "saved bridge thinking parameters are unavailable"
                    ) from error
                if not isinstance(thinking_parameters, Mapping):
                    raise AuthorityDenied("saved bridge thinking parameters are invalid")
                if "tool_choice" in thinking_parameters:
                    raise AuthorityDenied("saved bridge thinking cannot set tool choice")
                parameters = arguments.get("parameters", {})
                if not isinstance(parameters, Mapping):
                    raise AuthorityDenied("saved bridge AI parameters are invalid")
                arguments["parameters"] = {
                    **dict(thinking_parameters),
                    **dict(parameters),
                }
            if len(canonical_json(arguments)) > MAX_SAVED_INPUT_BYTES:
                raise AuthorityDenied("saved bridge AI input exceeds budget")
            outcome = self._dispatch(outer, target, arguments)
            if (
                enabled
                and outcome.get("status") == "ok"
                and isinstance(outcome.get("value"), Mapping)
            ):
                return {
                    **outcome,
                    "value": {**outcome["value"], "tool_definitions": selected["definitions"]},
                }
            return outcome
        else:
            if not enabled or set(payload) != {
                "tool_id",
                "tool_call_id",
                "arguments",
                "expected_definition_hash",
            }:
                raise AuthorityDenied("saved bridge tool invocation is out of scope")
            self._require_targets(outer, TOOL_TARGETS)
        return self._dispatch(outer, target, payload)

    @staticmethod
    def _check_append(
        request: Mapping[str, Any],
        payload: Mapping[str, Any],
        hop: int,
        trace: list[dict[str, Any]] | None = None,
    ) -> None:
        message = payload.get("message")
        role = "user" if hop == 1 else "assistant"
        trace = trace or []
        metadata = {"turn_id": request["turn_id"]}
        fields = {"id", "role", "content", "parent_id", "metadata", "status"}
        append_fields = {
            "operation",
            "conversation_id",
            "expected_conversation_revision",
            "message",
        }
        if "system_prompt_digest" in payload:
            digest = payload["system_prompt_digest"]
            if (
                hop != 1
                or not isinstance(digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            ):
                raise AuthorityDenied("saved bridge system prompt binding is invalid")
            append_fields.add("system_prompt_digest")
        if trace:
            metadata["saved_tool_messages"] = trace
            fields.add("tool_logs")

        def message_id(kind: str) -> str:
            identity = [request["conversation_id"], request["turn_id"], kind]
            return "message:" + canonical_digest(identity).removeprefix("sha256:")

        if (
            set(payload) != append_fields
            or payload["operation"] != "append"
            or payload["conversation_id"] != request["conversation_id"]
            or not isinstance(message, Mapping)
            or set(message) != fields
            or message["id"] != message_id(role)
            or message["role"] != role
            or canonical_json(message["metadata"]) != canonical_json(metadata)
            or canonical_json(message.get("tool_logs", []))
            != canonical_json(saved_tool_logs(trace))
            or message["status"] != "complete"
            or (hop == 1 and message["content"] != request["content"])
            or (hop == 3 and message["parent_id"] != message_id("user"))
        ):
            raise AuthorityDenied("saved bridge append is out of scope")


def project_saved_tool_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Carry normalized tool content as text without loosening the guest JSON ABI."""
    if (
        value.get("status") not in {"success", "error"}
        or type(value.get("is_error")) is not bool
        or value["is_error"] != (value["status"] == "error")
    ):
        raise ValueError("saved tool result is not normalized")
    content = json.dumps(
        {key: value.get(key) for key in ("status", "result", "error")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(content.encode()) > 16 * 1024:
        raise ValueError("saved tool result exceeds its bound")
    return {
        "tool_id": value.get("tool_id"),
        "tool_call_id": value.get("tool_call_id"),
        "content": content,
    }
