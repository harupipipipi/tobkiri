"""Author an owner-read/typed-context Workflow using published exact bindings."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from author_contract import render

OWNER_REVISION = "sha256:254d3ee4bdbe11bf21f658bbb3b89a1d6510ef7dbf77fb9108dcc4b426496156"


def workflow_template(*, pack_id: str = "acceptance.temporal.context",
                      function_id: str | None = None) -> dict[str, Any]:
    """Pin exact Functions; only captured owner execution establishes provenance."""
    contract = render(pack_id)
    selected = function_id if function_id is not None else f"{pack_id}.reduce"
    if not isinstance(selected, str) or not selected:
        raise ValueError("exact canonical Function ID is required")
    return {"workflow_intent_api_version": "io.tobkiri.profile-workflow-intent.v1",
            "name": "Tobkiri owner-bound timing context", "max_concurrency": 1,
            "steps": [
                {"id": "owner", "request": {
                    "function_id": "rumi_conversation_store_pack.conversation-store.resource",
                    "contract_id": "tobkiri.resource.conversation.v1",
                    "contract_revision_digest": OWNER_REVISION,
                    "operation_id": "rumi_conversation_store_pack.conversation-resource",
                    "input": {"operation": "get", "profile_id": "${inputs.profile_id}",
                              "conversation_id": "${inputs.conversation_id}"}}},
                {"id": "context", "depends_on": ["owner"], "request": {
                    "contract_id": contract["contract_id"],
                    "contract_revision_digest": contract["revision_digest"],
                    "operation_id": "timing.project.owner", "function_id": selected,
                    "input": {"profile_id": "${inputs.profile_id}", "owner_snapshot":
                              "${steps.owner.output.value.conversation}"}}}]}


def render_workflow(*, payload: dict[str, Any],
                    pack_id: str = "acceptance.temporal.context",
                    function_id: str | None = None) -> dict[str, Any]:
    """Render explicit owner-read input without resolving or authenticating authority."""
    if (not isinstance(payload, dict)
            or set(payload) != {"operation", "profile_id", "conversation_id"}
            or payload["operation"] != "get"):
        raise ValueError("documented owner-read fields are required")
    workflow = workflow_template(pack_id=pack_id, function_id=function_id)
    workflow["steps"][0]["request"]["input"] = deepcopy(payload)
    return workflow
