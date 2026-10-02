"""Selected public operation client, independent model context and real evidence."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .store import digest

CONVERSATION = "tobkiri.resource.conversation.v1"
CONVERSATION_OP = "rumi_conversation_store_pack.conversation-resource"
MODEL = "tobkiri.resource.ai.model.profile.v1"
MODEL_OP = "rumi_model_registry_pack.model-profile-resource"
GENERATE = "tobkiri.service.ai.generate.v1"
GENERATE_OP = "rumi_ai_gateway_pack.ai-gateway.generate"
AGENT = "tobkiri.action.agent.run.v1"
AGENT_OP = "rumi_agent_runtime_service_pack.agent-runtime-control"


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
        references = [
            str(x["id"]) for x in messages if isinstance(x, Mapping) and x.get("id")
        ]
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
                    "conversation_model_profile_id": conversation.get("model"),
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
        model = self.resolve_model(plan["settings"], conversation)
        request = {
            "request_id": request_id,
            "idempotency_key": request_id,
            "profile_id": self.profile_id,
            "model_profile_id": model["resolved_profile_id"],
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": json.dumps(data)},
            ],
            "parameters": {
                "max_tokens": 2048,
                "thinking_level": model.get("thinking_level"),
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
        elif isinstance(output, str):
            value = json.loads(output)
        else:
            raise ValueError("independent generation output is invalid")
        value["model_resolution"] = model
        value["run_context_id"] = f"independent-{digest(request_id)[:24]}"
        return value

    def execute(
        self,
        plan: Mapping[str, Any],
        conversation: Mapping[str, Any],
        item: Mapping[str, Any],
        projection: Mapping[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        """Execute the selected agent's ordinary approval-aware tool runtime."""
        result = self.client.invoke(
            AGENT,
            AGENT_OP,
            {
                "operation": "execute",
                "profile_id": self.profile_id,
                "agent_profile_id": plan["settings"]["executor"],
                "conversation_id": plan["conversation_id"],
                "conversation_revision": conversation["conversation_revision"],
                "run_id": run_id,
                "idempotency_key": run_id,
                "task_context": {
                    "version": "tobkiri.context-projection.v1",
                    "plan_id": plan["id"],
                    "plan_revision": plan["revision"],
                    "goal": projection.get("context"),
                    "todo": dict(item),
                    "instructions": projection.get("instructions", []),
                },
            },
        )
        # Evidence comes from actual tool receipts, never the assistant's prose.
        evidence = []
        verified = []
        for criterion in item["criteria"]:
            if not criterion.startswith("tool_success:"):
                continue
            operation = criterion.removeprefix("tool_success:")
            for receipt in result.get("tool_results", []):
                if not isinstance(receipt, Mapping):
                    continue
                intent, value = receipt.get("intent", {}), receipt.get("result", {})
                if (
                    isinstance(intent, Mapping)
                    and isinstance(value, Mapping)
                    and intent.get("operation") == operation
                    and value.get("status") in {"ok", "completed", "success"}
                ):
                    evidence.append(
                        {
                            "reference": f"tool-result:{digest(receipt)}",
                            "verified": True,
                            "criterion": criterion,
                        }
                    )
                    verified.append(criterion)
                    break
        return {**dict(result), "evidence": evidence, "verified_criteria": verified}
