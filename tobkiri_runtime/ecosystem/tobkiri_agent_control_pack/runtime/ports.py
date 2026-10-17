"""Selected public operation client, independent model context and real evidence."""

from __future__ import annotations

import json
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input
from tobkiri_protocol.saved_task_context import VERSION, validate_saved_task_context

from .store import digest

CONVERSATION = "tobkiri.resource.conversation.v1"
CONVERSATION_OP = "rumi_conversation_store_pack.conversation-resource"
MODEL = "tobkiri.resource.ai.model.profile.v1"
MODEL_OP = "rumi_model_registry_pack.model-profile-resource"
GENERATE = "tobkiri.service.ai.generate.v1"
GENERATE_OP = "rumi_ai_gateway_pack.ai-gateway.generate"
SAVED = "tobkiri.action.turn.saved.v1"
SAVED_OP = "rumi_turn_runtime_pack.turn-saved"


class Ports:
    """Invoke exact versioned operation IDs; credentials never enter Pack state."""

    def __init__(self, client: Any, profile_id: str) -> None:
        self.client = client
        self.profile_id = profile_id

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        """Read transcript/evidence through the conversation owner's public view."""
        value = self.client.invoke(
            CONVERSATION,
            CONVERSATION_OP,
            {
                "operation": "get",
                "profile_id": self.profile_id,
                "conversation_id": conversation_id,
            },
        )
        conversation = value["conversation"]
        if conversation["id"] != conversation_id:
            raise PermissionError("conversation scope changed")
        return dict(conversation)

    def evidence(self, conversation: Mapping[str, Any]) -> dict[str, Any]:
        """Retain actual message/tool result references, not a self-report summary."""
        messages = conversation.get("messages", [])[-200:]
        references = [str(x["id"]) for x in messages if isinstance(x, Mapping) and x.get("id")]
        return {
            "conversation_revision": conversation["conversation_revision"],
            "reference_ids": references,
            "messages": messages,
            "available": bool(references),
            "limitations": ["external artifacts require a selected read-only provider"],
        }

    def resolve_model(
        self, settings: Mapping[str, Any], conversation: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Record requested and resolved model policy, including explicit failures."""
        value = self.client.invoke(
            MODEL,
            MODEL_OP,
            {
                "operation": "policy.resolve",
                "profile_id": self.profile_id,
                "model_policy": settings["model_policy"],
                "thinking_policy": settings["thinking_policy"],
                "context": {
                    "conversation_model_profile_id": conversation.get("model_reference")
                    or conversation.get("model"),
                    "conversation_thinking_level": conversation.get("thinking_level"),
                },
                **(
                    {"snapshot_receipt": settings["snapshot_receipt"]}
                    if settings.get("snapshot_receipt")
                    else {}
                ),
            },
        )
        if value.get("error") or not value.get("resolved_profile_id"):
            raise RuntimeError("review model policy is unavailable")
        return dict(value)

    def generate(
        self,
        plan: Mapping[str, Any],
        conversation: Mapping[str, Any],
        instruction: str,
        data: Mapping[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        """Run a bounded independent context through the public AI gateway."""
        body = json.dumps(data)
        if len(body.encode("utf-8")) > 128 * 1024:
            raise ValueError("independent review context exceeds its bounded budget")
        model = self.resolve_model(plan["settings"], conversation)
        request = {
            "request_id": request_id,
            "idempotency_key": request_id,
            "profile_id": self.profile_id,
            "model_profile_id": model["resolved_profile_id"],
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": body},
            ],
            "parameters": {
                "max_tokens": 2048,
                **(
                    {"thinking_level": model["thinking_level"]}
                    if model.get("thinking_level") is not None
                    else {}
                ),
            },
            "tools": [],
            "requirements": {"tool_calling": False},
            "maximum_cost_microusd": 100000,
        }
        result = self.client.invoke(GENERATE, GENERATE_OP, request)
        if result.get("status") not in {"ok", "completed"}:
            raise RuntimeError("independent generation failed")
        output = result.get("output", result.get("text"))
        if isinstance(output, Mapping):
            value = dict(output)
        elif isinstance(output, list):
            value = json.loads(
                "".join(
                    part["text"]
                    for part in output
                    if isinstance(part, Mapping) and part.get("type") == "text"
                )
            )
        elif isinstance(output, str):
            value = json.loads(output)
        else:
            raise ValueError("independent generation output is invalid")
        value["model_resolution"] = model
        value["run_context_id"] = f"independent-{digest(request_id)[:24]}"
        return value

    def task_context(
        self,
        plan: Mapping[str, Any],
        projection: Mapping[str, Any],
        input_id: str,
        item: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Build finite provenance items without mutating the original user content."""
        items = []
        if plan.get("goal"):
            items.append(
                {
                    "id": plan["goal"]["id"],
                    "kind": "goal",
                    "body": json.dumps(
                        {
                            "body": plan["goal"]["body"],
                            "constraints": plan["goal"]["constraints"],
                            "criteria": plan["goal"]["criteria"],
                            "context": projection.get("context"),
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        if item is not None:
            items.append(
                {
                    "id": item["id"],
                    "kind": "todo",
                    "body": json.dumps(
                        {
                            "body": item["body"],
                            "criteria": item["criteria"],
                            "dependencies": item["dependencies"],
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        items.extend(
            {
                "id": event["id"],
                "kind": "instruction",
                "body": event["body"],
            }
            for event in projection.get("instructions", [])
        )
        if not items:
            return None
        return validate_saved_task_context(
            {
                "version": VERSION,
                "source_id": plan["id"],
                "conversation_id": plan["conversation_id"],
                "recipient_id": plan["settings"]["executor"],
                "input_id": input_id,
                "source_revision": plan["revision"],
                "generation": plan["generation"],
                "items": items,
            },
            conversation_id=plan["conversation_id"],
            turn_id=input_id,
        )

    def accepted_receipt(
        self, initial: Mapping[str, Any], accepted_digest: str | None = None
    ) -> dict[str, Any] | None:
        """Read exact immutable acceptance from the public conversation owner."""
        initial = validate_saved_conversation_input(initial)
        request = initial["request"]
        response = self.client.invoke(
            CONVERSATION,
            CONVERSATION_OP,
            {
                "operation": "saved_receipt",
                "profile_id": self.profile_id,
                "turn_id": request["turn_id"],
            },
        )
        receipt = response.get("receipt")
        if receipt is None:
            return None
        expected_user = "message:" + canonical_digest(
            [
                request["conversation_id"],
                request["turn_id"],
                "user",
            ]
        ).removeprefix("sha256:")
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("turn_id") != request["turn_id"]
            or receipt.get("conversation_id") != request["conversation_id"]
            or receipt.get("input_digest") != (accepted_digest or canonical_digest(initial))
            or receipt.get("initial_revision") != request["conversation_revision"]
            or receipt.get("user_revision") != request["conversation_revision"] + 1
            or receipt.get("user_message_id") != expected_user
        ):
            raise ValueError("canonical saved input acceptance does not match")
        return dict(receipt)

    def execute(
        self,
        plan: Mapping[str, Any],
        conversation: Mapping[str, Any],
        item: Mapping[str, Any],
        projection: Mapping[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        """Execute one exact captured saved turn and verify its accepted lineage."""
        if (
            conversation.get("agent_id")
            or plan["settings"]["executor"] != f"conversation:{plan['conversation_id']}"
        ):
            return {"status": "unavailable", "reason": "assigned_agent_not_supported"}
        context = self.task_context(plan, projection, run_id, item)
        initial = validate_saved_conversation_input(
            {
                "request": {
                    "turn_id": run_id,
                    "conversation_id": plan["conversation_id"],
                    "conversation_revision": conversation["conversation_revision"],
                    "content": item["body"],
                    "tool_selection": {"mode": "auto"},
                    **({"task_context": context} if context else {}),
                }
            }
        )
        result = dict(self.client.invoke(SAVED, SAVED_OP, initial))
        context_receipt = result.get("input_context_receipt")
        accepted_digest = None
        if context_receipt is not None:
            if (
                not isinstance(context_receipt, Mapping)
                or context_receipt.get("source_input_digest") != canonical_digest(initial)
                or not isinstance(context_receipt.get("accepted_input_digest"), str)
                or not context_receipt["accepted_input_digest"].startswith("sha256:")
            ):
                raise ValueError("canonical input context lineage does not match")
            accepted_digest = context_receipt["accepted_input_digest"]
        receipt = self.accepted_receipt(initial, accepted_digest)
        turn = result.get("turn", {})
        if result.get("status") == "completed" and (
            receipt is None
            or not isinstance(turn, Mapping)
            or turn.get("id") != run_id
            or turn.get("input_digest") != (accepted_digest or canonical_digest(initial))
            or turn.get("result_reference") != receipt.get("result_reference")
            or not receipt.get("result_reference")
        ):
            raise ValueError("canonical saved completion receipt is unavailable")
        evidence, verified = [], []
        if result.get("status") == "completed":
            if receipt is None:
                raise ValueError("canonical completion has no acceptance receipt")
            source = self.conversation(plan["conversation_id"])
            reference = receipt["result_reference"]
            message = next(
                (
                    entry
                    for entry in source.get("messages", [])
                    if entry.get("id") == reference["assistant_message_id"]
                ),
                None,
            )
            if not message or message.get("parent_id") != receipt["user_message_id"]:
                raise ValueError("canonical saved completion source is unavailable")
            for criterion in item["criteria"]:
                if not criterion.startswith("tool_success:"):
                    continue
                operation = criterion.removeprefix("tool_success:")
                for log in message.get("tool_logs", []):
                    value = log.get("result")
                    try:
                        value = json.loads(value) if isinstance(value, str) else value
                    except ValueError:
                        continue
                    if (
                        log.get("tool_name") == operation
                        and isinstance(value, Mapping)
                        and value.get("status") in {"ok", "completed", "success"}
                    ):
                        evidence.append(
                            {
                                "reference": f"{reference['assistant_message_id']}:{log['tool_call_id']}",
                                "verified": True,
                                "criterion": criterion,
                            }
                        )
                        verified.append(criterion)
                        break
        return {
            **result,
            "input_accepted": receipt is not None,
            "accepted_receipt": receipt,
            "evidence": evidence,
            "verified_criteria": verified,
        }
