"""Author exact selected Workflow intent without guessing a runtime principal."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from author_contract import render


def workflow_template(*, pack_id: str = "acceptance.temporal.context",
                      function_id: str | None = None) -> dict[str, Any]:
    """Pin exact source Function; lifecycle inputs remain supplied and untrusted.

    Only the selected compiler can match the actual captured operation palette
    and obtain its unique principal. This declaration creates no authority.
    """
    contract = render(pack_id)
    selected_function = function_id if function_id is not None else f"{pack_id}.reduce"
    if not isinstance(selected_function, str) or not selected_function:
        raise ValueError("exact canonical Function ID is required")
    return {"workflow_intent_api_version": "io.tobkiri.profile-workflow-intent.v1",
            "name": "Tobkiri temporal reducer source intent", "max_concurrency": 1,
            "steps": [{"id": "temporal.reduce", "request": {
                "contract_id": contract["contract_id"],
                "contract_revision_digest": contract["revision_digest"],
                "operation_id": "temporal.reduce", "function_id": selected_function,
                "input": {"namespace": "${inputs.namespace}",
                          "state": "${inputs.state}", "event": "${inputs.event}"},
            }}]}


def render_workflow(*, payload: dict[str, Any],
                    pack_id: str = "acceptance.temporal.context",
                    function_id: str | None = None) -> dict[str, Any]:
    """Render source input without authenticating lifecycle or resolving authority."""
    if not isinstance(payload, dict):
        raise ValueError("operation input must be an object")
    workflow = workflow_template(pack_id=pack_id, function_id=function_id)
    workflow["steps"][0]["request"]["input"] = deepcopy(payload)
    return workflow
