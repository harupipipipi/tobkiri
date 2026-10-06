"""Finite saved-turn callbacks behind authenticated transport and captured Broker.

The supervisor owns signature, binding digest, nonce and continuation ordering.
These callbacks own input scope and dispatch selection, never guest authority.
Route readiness is not a credential/network probe or a promise of AI success.
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Callable, Mapping, Sequence

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
from tobkiri_protocol.turn_progress_v1 import (
    AI_STREAM, ACTION, ACTION_OPERATION, RESOURCE, RESOURCE_OPERATION,
    TTL_SECONDS, payload_digest,
)
from tobkiri_protocol.saved_context import (
    PROMPT_TARGET,
    resolved_saved_prompt,
    saved_prompt_digest,
    saved_prompt_reference,
)
from tobkiri_protocol.conversation_lifecycle import (
    active_task_gap_context, saved_terminal_finish_reason, task_gap_prompt,
)
from tobkiri_protocol.saved_task_context import saved_task_context_messages
from tobkiri_protocol.conversation_context import (
    context_link, resolve_request_context, resolved_thinking_level,
)

from ..authority.v4 import AuthorityDenied

Target = tuple[str, str]


def project_saved_ai_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Cross the strict guest ABI with reply data, not floating-point telemetry."""
    return {
        "status": value.get("status"),
        "output": value.get("output"),
        "tool_intents": value.get("tool_intents", []),
        "finish_reason": saved_terminal_finish_reason(value),
    }


READINESS: Target = (
    "tobkiri.resource.ai.route.quote.v1",
    "rumi_ai_gateway_pack.ai-gateway.route-quote",
)
STRATEGY: Target = (
    "tobkiri.service.ai.strategy.dispatch.v1",
    "rumi_ai_strategy_runtime_pack.ai-strategy.dispatch",
)
STRATEGY_CATALOG: Target = (
    "tobkiri.resource.ai.strategy.catalog.v1",
    "rumi_ai_strategy_runtime_pack.ai-strategy.catalog",
)
REQUIRED_TARGETS = (*dict.fromkeys(TARGETS), READINESS)
DEFINITION: Target = (
    "tobkiri.resource.tool.definition.v1",
    "rumi_tool_registry_pack.tool-definition-resource",
)
TOOL_TARGETS = (DEFINITION, TOOL)
ALLOWED_TARGETS = (
    *REQUIRED_TARGETS,
    STRATEGY,
    STRATEGY_CATALOG,
    *TOOL_TARGETS,
    PROMPT_TARGET,
    AI_STREAM, (ACTION, ACTION_OPERATION), (RESOURCE, RESOURCE_OPERATION),
)
Dispatch = Callable[[object, Target, Mapping[str, Any]], Mapping[str, Any]]
RequireTargets = Callable[[object, tuple[Target, ...]], None]


def _required_targets(request: Mapping[str, Any]) -> tuple[Target, ...]:
    """Return only the direct or strategy execution edge selected by owner state."""

    strategy_selected = request.get("strategy_reference") is not None
    ai_target = STRATEGY if strategy_selected else TARGETS[2]
    preflight_target = STRATEGY_CATALOG if strategy_selected else READINESS
    return (
        *dict.fromkeys(
            (TARGETS[0], TARGETS[1], ai_target, TARGETS[3], preflight_target)
        ),
    )


_WALL_MONOTONIC_OFFSET = time.time() - time.monotonic()
_STRATEGY_MAXIMUM_COST_MICROUSD = 1_000_000


def _strategy_maximum_cost(request: Mapping[str, Any]) -> int:
    """Apply the immutable Host ceiling and an optional lower user limit."""

    requested = request.get(
        "strategy_maximum_cost_microusd",
        _STRATEGY_MAXIMUM_COST_MICROUSD,
    )
    if type(requested) is not int or requested <= 0:
        raise AuthorityDenied("saved bridge strategy cost limit is invalid")
    return min(requested, _STRATEGY_MAXIMUM_COST_MICROUSD)


def _strategy_deadline(outer: object) -> int:
    """Project the immutable Host deadline to bounded Unix milliseconds."""

    value = getattr(outer, "deadline_monotonic", None)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= time.monotonic()
    ):
        raise AuthorityDenied("saved bridge strategy deadline is unavailable")
    # Floor instead of rounding so the Pack-visible fence can never outlive
    # the Host-owned monotonic deadline. Integer milliseconds also remain in
    # the strict canonical JSON subset used at the PackVM boundary.
    return math.floor((float(value) + _WALL_MONOTONIC_OFFSET) * 1000)


def _strategy_idempotency_key(
    request: Mapping[str, Any],
    conversation: Mapping[str, Any],
    trace: Sequence[Mapping[str, Any]],
    *,
    request_id: str,
) -> str:
    """Bind one billable strategy step to immutable owner state and tool trace."""

    generation_step = canonical_digest(
        {
            "turn_id": request["turn_id"],
            "conversation_revision": conversation.get("conversation_revision"),
            "current_node_id": conversation.get("current_node_id"),
            "tool_trace": list(trace),
        }
    ).removeprefix("sha256:")[:32]
    return f"{request_id}:strategy:{generation_step}"


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


def _contains_inline_images(messages: Sequence[Mapping[str, Any]]) -> bool:
    """Return whether owner-built messages retain saved inline image blocks."""
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
    """Independently constrain the selected owner history to resolved text."""
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
    task_gap = active_task_gap_context(conversation)
    if task_gap is not None:
        prefix.append({"role": "system", "content": task_gap_prompt(task_gap)})
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
        bounded_user_content = (
            message.get("role") == "user" and is_saved_user_content(content)
        )
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


def _bounded_inline_image_history(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only the newest bounded saved image set in owner generation context."""
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
            if (
                isinstance(block, Mapping)
                and block.get("type") == "image_url"
                and (message_index, block_index) not in retained
            ):
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
    """Return only guest state needed to detect duplicate saved appends."""
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


def _requires_tool_calling(request: Mapping[str, Any]) -> bool:
    """Return whether the tool selection is a hard routing requirement.

    An ``auto`` selection without ``must_use`` only offers tools to the
    model; it must not exclude a saved profile whose tool capability is
    unverified (connection-bound profiles resolve through a catalog
    descriptor without capability evidence). An explicit ``manual``
    selection or ``must_use`` stays fail-closed.
    """
    selection = request.get("tool_selection")
    if not isinstance(selection, Mapping):
        return False
    return bool(selection.get("must_use")) or selection.get("mode") == "manual"


class SavedBridgeCallbacks:
    """Bind the four guest stages to finite Host-owned dispatch callbacks."""

    def __init__(
        self,
        dispatch: Dispatch,
        require_targets: RequireTargets,
        stream_available: Callable[[object, Mapping[str, Any], Mapping[str, Any]], bool] | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._require_targets = require_targets
        self._stream_available = stream_available

    def _stream_ready(
        self, outer: object, request: Mapping[str, Any], conversation: Mapping[str, Any],
    ) -> bool:
        """Resolve an exact stream route before selecting its first signed intent."""
        if self._stream_available is None:
            return False
        try:
            if not self._stream_available(outer, request, conversation):
                return False
            prompt = self._system_prompt(outer, conversation)
            requirements: dict[str, Any] = {}
            if _contains_inline_images(request["content"]):
                requirements["modalities"] = ["image", "text"]
            outcome = self._dispatch(outer, READINESS, {
                "model_profile_id": conversation["model_reference"],
                "messages": [
                    *_messages(conversation, flatten_text_blocks=True, system_prompt=prompt),
                    {"role": "user", "content": saved_user_text(request["content"])},
                    *saved_task_context_messages(request),
                ],
                "requirements": requirements, "delivery_mode": "incremental",
            })
            value = outcome.get("value")
            return outcome.get("status") == "ok" and isinstance(value, Mapping) and value.get("ready") is True
        except (AuthorityDenied, ValueError, RuntimeError):
            return False

    def _conversation(self, outer: object, request: Mapping[str, Any]) -> Mapping[str, Any]:
        conversation = self._read_conversation(outer, request["conversation_id"])
        try:
            return resolve_request_context(
                conversation, request,
                getattr(getattr(outer, "context", None), "profile_id", ""),
                lambda parent_id: self._read_conversation(outer, parent_id),
            )
        except ValueError as error:
            raise AuthorityDenied(str(error)) from error

    def _read_conversation(self, outer: object, conversation_id: str) -> Mapping[str, Any]:
        outcome = self._dispatch(
            outer,
            TARGETS[0],
            {
                "operation": "get",
                "conversation_id": conversation_id,
            },
        )
        value = outcome.get("value")
        conversation = value.get("conversation") if isinstance(value, Mapping) else None
        if (
            outcome.get("status") != "ok"
            or not isinstance(conversation, Mapping)
            or conversation.get("id") != conversation_id
        ):
            raise AuthorityDenied("saved bridge conversation is unavailable")
        return conversation

    def _recheck_linked_context(
        self, outer: object, request: Mapping[str, Any], conversation: Mapping[str, Any],
    ) -> None:
        """Fence dependency-read changes immediately before effect admission."""
        if context_link(conversation) is None:
            return
        fresh = self._conversation(outer, request)
        if any(
            fresh.get(field) != conversation.get(field)
            for field in ("conversation_revision", "current_node_id")
        ):
            raise AuthorityDenied("saved bridge linked conversation changed before effect")

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
        self._require_targets(outer, _required_targets(request))
        tools = self._tools(outer, request)
        conversation = self._conversation(outer, request)
        prompt = self._system_prompt(outer, conversation)
        revision = conversation.get("conversation_revision")
        if type(revision) is not int or revision != request["conversation_revision"]:
            raise AuthorityDenied("saved bridge conversation revision changed")
        model = conversation.get("model_reference")
        if not isinstance(model, str) or not model.strip():
            raise AuthorityDenied("saved bridge owned model is unavailable")
        strategy_reference = request.get("strategy_reference")
        if strategy_reference is not None:
            outcome = self._dispatch(outer, STRATEGY_CATALOG, {})
            value = outcome.get("value")
            strategies = value.get("strategies") if isinstance(value, Mapping) else None
            matches = (
                [
                    item
                    for item in strategies
                    if isinstance(item, Mapping)
                    and item.get("strategy_reference") == strategy_reference
                ]
                if isinstance(strategies, list)
                else []
            )
            if outcome.get("status") != "ok" or len(matches) != 1:
                raise AuthorityDenied(
                    "saved bridge selected AI strategy is unavailable"
                )
            self._recheck_linked_context(outer, request, conversation)
            return
        owner_messages = _messages(conversation, system_prompt=prompt)
        requires_image_input = _contains_inline_images(
            owner_messages
        ) or _contains_inline_images(
            [{"role": "user", "content": request["content"]}]
        )
        requirements: dict[str, Any] = {}
        if requires_image_input:
            requirements["modalities"] = ["image", "text"]
        if tools["tools"] and _requires_tool_calling(request):
            requirements["tool_calling"] = True
        payload: dict[str, Any] = {
            "model_profile_id": model,
            "messages": [
                *_messages(conversation, flatten_text_blocks=True, system_prompt=prompt),
                {"role": "user", "content": saved_user_text(request["content"])},
                *saved_task_context_messages(request),
            ],
            **({"requirements": requirements} if requirements else {}),
        }
        if len(canonical_json(payload)) > MAX_SAVED_INPUT_BYTES:
            raise AuthorityDenied("saved bridge readiness input exceeds budget")
        self._recheck_linked_context(outer, request, conversation)
        outcome = self._dispatch(outer, READINESS, payload)
        value = outcome.get("value")
        if (
            outcome.get("status") != "ok"
            or not isinstance(value, Mapping)
            or value.get("ready") is not True
        ):
            # The bridge result only ever carries a bounded Host-owned code;
            # surface it so a terminal preflight rejection can report the
            # diagnostic instead of an undifferentiated denial.
            error = outcome.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            raise AuthorityDenied(
                "saved bridge AI route is unavailable",
                code=code
                if type(code) is str and re.fullmatch(r"[A-Za-z0-9_]{1,64}", code)
                else "authority_denied",
            )

    def __call__(
        self, outer: object, frame: Mapping[str, Any] | SavedToolFrame
    ) -> Mapping[str, Any]:
        """Dispatch authenticated continuation scope through captured targets."""
        request = _request(outer)
        enabled = request.get("tool_selection", {}).get("mode", "none") != "none"
        trace: list[dict[str, Any]] = []
        owner_revision: int | None = None
        owner_current_node_id: str | None = None
        ai_mode = "buffered"
        if isinstance(frame, SavedToolFrame):
            owner_revision = frame.expected_conversation_revision
            owner_current_node_id = frame.expected_current_node_id
            ai_mode = frame.ai_mode
            stage = frame.stage
            if enabled:
                if frame.initial_digest != canonical_digest({"request": request}):
                    raise AuthorityDenied("saved bridge requires Host-checked tool scope")
                # Tool-stage scope may contain assistant calls awaiting their results.
                trace = strict_loads(
                    frame.tool_messages, max_bytes=40 * 1024, max_depth=12
                )
                if stage != "tool":
                    trace = saved_tool_messages(trace)
            frame = frame.frame
        elif enabled:
            raise AuthorityDenied("saved bridge requires Host-checked tool scope")
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
            "ai": STRATEGY
            if request.get("strategy_reference") is not None
            else AI_STREAM if ai_mode == "incremental" else TARGETS[2],
            "assistant": TARGETS[3],
            "tool": TOOL,
        }.get(stage)
        if target is None or frame.get("target") != {
            "contract_id": target[0],
            "operation_id": target[1],
        }:
            raise AuthorityDenied("saved bridge stage target is invalid")
        self._require_targets(outer, _required_targets(request))
        payload = frame.get("payload")
        if (
            not isinstance(payload, Mapping)
            or len(canonical_json(dict(payload))) > MAX_SAVED_INPUT_BYTES
        ):
            raise AuthorityDenied("saved bridge payload is invalid")
        if stage == "read":
            if dict(payload) != {"operation": "get", "conversation_id": request["conversation_id"]}:
                raise AuthorityDenied("saved bridge conversation read is out of scope")
            conversation = self._conversation(outer, request)
            value: dict[str, Any] = {
                "conversation": _saved_read_projection(conversation)
            }
            # The route selection is an authenticated Host outcome; it never
            # comes from Guest input or redirects an already sealed AI intent.
            if (
                self._stream_available is not None
                and request.get("strategy_reference") is None
                and self._stream_ready(outer, request, conversation)
            ):
                value["delivery_mode"] = "incremental"
            elif self._stream_available is not None:
                value["delivery_mode"] = "buffered"
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
            else:
                conversation = self._conversation(outer, request)
                self._require_acknowledged_branch(
                    conversation, owner_revision, owner_current_node_id,
                )
        elif stage == "ai":
            conversation = self._conversation(outer, request)
            self._require_acknowledged_branch(
                conversation, owner_revision, owner_current_node_id
            )
            prompt = self._system_prompt(outer, conversation)
            strategy_reference = request.get("strategy_reference")
            provider_payload = payload
            if strategy_reference is not None:
                if (
                    set(payload) != {
                        "strategy_reference",
                        "request",
                    }
                    or payload.get("strategy_reference") != strategy_reference
                    or not isinstance(payload.get("request"), Mapping)
                ):
                    raise AuthorityDenied(
                        "saved bridge strategy input differs from the owner"
                    )
                provider_payload = payload["request"]
            expected_fields = {"model_reference", "requirements"}
            if prompt is not None:
                expected_fields.add("system_prompt_digest")
            expected_requirements: dict[str, Any] = {
                "request_surface": "conversation.saved"
            }
            if (
                set(provider_payload) != expected_fields
                or provider_payload.get("system_prompt_digest")
                != saved_prompt_digest(prompt)
                or provider_payload["model_reference"]
                != conversation.get("model_reference")
                or provider_payload["requirements"] != expected_requirements
            ):
                raise AuthorityDenied("saved bridge AI input differs from the owner")
            selected = self._tools(outer, request)
            arguments = {
                key: value
                for key, value in provider_payload.items()
                if key != "system_prompt_digest"
            }
            arguments["messages"] = [
                *_messages(conversation, system_prompt=prompt),
                *saved_task_context_messages(request),
                *trace,
            ]
            requirements = dict(provider_payload["requirements"])
            if _contains_inline_images(arguments["messages"]):
                requirements["modalities"] = ["image", "text"]
            arguments["requirements"] = requirements
            thinking_level = resolved_thinking_level(conversation, request)
            if thinking_level is not None:
                # The selected level comes from the validated initial request,
                # never from the guest's AI intent or a resumed frame.
                arguments["parameters"] = {
                    "thinking_level": thinking_level
                }
            if selected["tools"]:
                if _requires_tool_calling(request):
                    requirements["tool_calling"] = True
                parameters = dict(arguments.get("parameters") or {})
                parameters["tool_choice"] = (
                    "required"
                    if request["tool_selection"].get("must_use") and not trace
                    else "auto"
                )
                arguments.update(
                    {
                        "tools": selected["tools"],
                        "requirements": requirements,
                        "parameters": parameters,
                    }
                )
            if strategy_reference is not None:
                request_id = str(
                    getattr(getattr(outer, "context", None), "request_id", "")
                )
                if not request_id:
                    raise AuthorityDenied(
                        "saved bridge strategy request identity is unavailable"
                    )
                arguments.update(
                    {
                        "request_id": request_id,
                        "idempotency_key": _strategy_idempotency_key(
                            request,
                            conversation,
                            trace,
                            request_id=request_id,
                        ),
                        "deadline": _strategy_deadline(outer),
                        "maximum_cost_microusd": _strategy_maximum_cost(request),
                    }
                )
                if prompt is not None:
                    arguments["system_prompt_digest"] = saved_prompt_digest(
                        prompt
                    )
            if len(canonical_json(arguments)) > MAX_SAVED_INPUT_BYTES:
                raise AuthorityDenied("saved bridge AI input exceeds budget")
            dispatch_arguments = (
                {
                    "strategy_reference": strategy_reference,
                    "request": arguments,
                }
                if strategy_reference is not None
                else arguments
            )
            self._recheck_linked_context(outer, request, conversation)
            if target == AI_STREAM:
                outer_context = getattr(outer, "context", None)
                arguments["request_id"] = str(getattr(outer_context, "request_id", ""))
                arguments["deadline"] = min(
                    int(time.time()) + TTL_SECONDS,
                    int(_strategy_deadline(outer) / 1000),
                )
                progress = self._dispatch(outer, (ACTION, ACTION_OPERATION), {
                    "phase": "begin", "turn_id": request["turn_id"],
                    "conversation_id": request["conversation_id"],
                    "conversation_revision": owner_revision,
                    "parent_id": owner_current_node_id,
                    "input_digest": canonical_digest({"request": request}),
                    "request_id": arguments["request_id"],
                    "ai_input_digest": payload_digest(arguments),
                })
                progress_value = progress.get("value")
                if (
                    progress.get("status") != "ok"
                    or not isinstance(progress_value, Mapping)
                    or not isinstance(progress_value.get("progress_id"), str)
                ):
                    raise AuthorityDenied("saved live progress reservation failed")
                arguments["progress_id"] = progress_value["progress_id"]
            outcome = self._dispatch(outer, target, dispatch_arguments)
            additions: dict[str, Any] = {}
            if (
                outcome.get("status") == "ok"
                and isinstance(outcome.get("value"), Mapping)
            ):
                if enabled:
                    additions["tool_definitions"] = selected["definitions"]
            if additions:
                return {
                    **outcome,
                    "value": {**outcome["value"], **additions},
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
            conversation = self._conversation(outer, request)
            self._require_acknowledged_branch(
                conversation, owner_revision, owner_current_node_id
            )
            self._require_targets(outer, TOOL_TARGETS)
            if ai_mode == "incremental":
                try:
                    progress = self._dispatch(
                        outer,
                        (ACTION, ACTION_OPERATION),
                        {
                            "phase": "begin",
                            "turn_id": request["turn_id"],
                            "conversation_id": request["conversation_id"],
                            "conversation_revision": owner_revision,
                            "parent_id": owner_current_node_id,
                            "input_digest": canonical_digest({"request": request}),
                            "request_id": str(
                                getattr(getattr(outer, "context", None), "request_id", "")
                            ),
                            "ai_input_digest": payload_digest(payload),
                        },
                    )
                    value = progress.get("value")
                    if progress.get("status") == "ok" and isinstance(value, Mapping):
                        identity = value.get("progress_id")
                        if isinstance(identity, str):
                            payload = {**payload, "progress_id": identity}
                except Exception:
                    # Provisional display failure cannot prevent or retry the effect.
                    pass
        self._recheck_linked_context(outer, request, conversation)
        return self._dispatch(outer, target, payload)

    @staticmethod
    def _require_acknowledged_branch(
        conversation: Mapping[str, Any],
        revision: int | None,
        current_node_id: str | None,
    ) -> None:
        """Fence provider and tool effects to the Host-acknowledged user node."""
        if revision is None and current_node_id is None:
            # Direct unit callbacks do not model the Host's local wrapper. The
            # production supervisor always supplies one through SavedHostExchange.
            return
        if (
            type(revision) is not int
            or revision < 1
            or not isinstance(current_node_id, str)
            or not current_node_id
            or conversation.get("conversation_revision") != revision
            or conversation.get("current_node_id") != current_node_id
        ):
            raise AuthorityDenied(
                "saved bridge acknowledged branch changed before execution"
            )

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
        if role == "assistant":
            fields.add("finish_reason")
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
            or (role == "assistant" and message.get("finish_reason") not in {
                None, "stop", "waiting_user", "waiting_approval", "cancelled",
                "error", "running",
            })
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
