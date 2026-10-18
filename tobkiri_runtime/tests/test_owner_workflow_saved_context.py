"""Real owner/bridge/Workflow execution with explicit in-process test ports, not PackVM."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest
from jsonschema import Draft202012Validator

from core_runtime.workflow_v4.engine import WorkflowEngineV4
from core_runtime.workflow_v4.models import InvocationOutcome
from core_runtime.workflow_v4.provider import WorkflowProviderV4
from core_runtime.workflow_v4.store import WorkflowStoreV4
from core_runtime.resolved_profile_scope import (
    V4ResolvedProfileView,
    activate_resolved_profile,
    restore_resolved_profile,
)
import core_runtime.profile_content_projection as projections
from core_runtime.bootstrap.profile_context_workflow import WORKFLOW_TARGET
from tests.test_conversation_lifecycle_saved_boundary import _exchange, BASE_MS
from tests.test_conversation_lifecycle_pack import _confirm
from tests.test_saved_bridge_callbacks import _frame
from tests.test_workflow_v4 import Authority, Invoker
from ecosystem.defaultspack.runtime import saved_conversation as saved
from acceptance.independent_extensions.temporal_context.source.temporal import tobkiri_packvm_invoke
from tobkiri_protocol.canonical import canonical_digest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "acceptance/independent_extensions/temporal_context/acceptance.temporal.context"


@pytest.mark.parametrize("elapsed,has_gap", [(3599000, False), (3600000, True)])
@pytest.mark.parametrize(
    "function_id", ["acceptance.temporal.context.reduce", "independent.renamed.context"]
)
def test_owner_read_dependency_execution_reaches_saved_system_context_once(
    tmp_path,
    monkeypatch,
    elapsed,
    has_gap,
    function_id,
):
    now = [BASE_MS]
    monkeypatch.setattr(
        "ecosystem.rumi_conversation_store_pack.runtime.store._now_ms", lambda: now[0]
    )
    exchange = _exchange(
        tmp_path / "owner", {"status": "ok", "output": "Done", "finish_reason": "stop"}
    )
    for _ in range(4):
        exchange.step()
    _confirm(exchange.store, exchange.outer.payload)
    now[0] += elapsed
    exchange.outer.payload["request"] = {
        **exchange.outer.payload["request"],
        "turn_id": "turn-2",
        "conversation_revision": exchange.store.get("conversation-1")["conversation_revision"],
        "content": "unchanged user text",
    }
    content = tmp_path / "profile_projections/component"
    shutil.copytree(COMPONENT / "content", content)
    workflow_path = next((content / "workflows").glob("*.workflow.intent.v1.json"))
    document = json.loads(workflow_path.read_text())
    document["steps"][1]["request"]["function_id"] = function_id
    workflow_path.write_text(json.dumps(document))
    monkeypatch.setattr(projections, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(projections, "PROJECTION_ROOT", tmp_path / "profile_projections")
    selection, _ = projections.resolve_intent_projection(
        {
            "projection_id": "component",
            "kind": "profile_content",
            "artifact_root": "profile_projections/component",
            "content_digest": None,
        }
    )
    schemas = {}
    operations = []
    contract = json.loads((COMPONENT / "contracts.v4.json").read_text())["contracts"][0]
    for step in document["steps"]:
        request = step["request"]
        if step["id"] == "owner":
            schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["profile_id", "conversation_id", "operation"],
                "properties": {
                    "profile_id": {"const": "defaults"},
                    "conversation_id": {"const": "conversation-1"},
                    "operation": {"const": "get"},
                },
            }
        else:
            operation = next(
                item
                for item in contract["operations"]
                if item["operation_id"] == "timing.project.owner"
            )
            schema = contract["schema_catalog"][operation["input_schema_digest"]]
        schema_digest = canonical_digest(schema)
        schemas[schema_digest] = schema
        operations.append(
            {
                **{
                    key: request[key]
                    for key in ("contract_id", "contract_revision_digest", "operation_id")
                },
                "function_principal_id": step["id"] + ".captured-test",
                "provider_id": request["function_id"],
                "input_schema_digest": schema_digest,
                "effect_ceiling": [],
            }
        )

    class Catalog:
        def snapshot(self):
            return {
                "catalog_digest": canonical_digest(operations),
                "security_epoch": 1,
                "activation": {
                    "activation_id": "isolated-test",
                    "activation_digest": "sha256:" + "1" * 64,
                },
                "operations": operations,
            }

    class Validator:
        def validate(self, digest, value):
            return [
                error.message for error in Draft202012Validator(schemas[digest]).iter_errors(value)
            ]

    class Port(Invoker):
        def invoke(self, request, *, authority):
            assert authority.dispatch_token.startswith("one-shot-")
            self.requests.append(deepcopy(request))
            if request["operation_id"] == "rumi_conversation_store_pack.conversation-resource":
                assert request["input"]["profile_id"] == "defaults"
                value = {"conversation": exchange.store.get(request["input"]["conversation_id"])}
            else:
                # Explicit in-process conformance port, never a production VM replacement.
                value = tobkiri_packvm_invoke(request["operation_id"], request["input"])
            return InvocationOutcome(output={"status": "ok", "value": value})

    authority = Authority()
    port = Port()
    store = WorkflowStoreV4(tmp_path / "workflow.sqlite3")
    provider = WorkflowProviderV4(
        WorkflowEngineV4(
            store=store, catalog=Catalog(), authority=authority, invoker=port,
            validator=Validator(), clock=lambda: 100.0,
        )
    )
    original = exchange.callback._dispatch

    def dispatch(outer, target, payload):
        if target == WORKFLOW_TARGET:
            return {"status": "ok", "value": provider.invoke("run.selected", payload)}
        return original(outer, target, payload)

    exchange.callback._dispatch = dispatch
    token = activate_resolved_profile(
        V4ResolvedProfileView(
            "defaults", "sha256:" + "1" * 64, "sha256:" + "2" * 64, (), (), (), (selection,)
        )
    )
    try:
        intent = saved.start(exchange.outer.payload["request"])
        for _ in range(3):
            intent = saved.resume(
                intent["state"], exchange.callback(exchange.outer, _frame(intent))
            )
        messages = [
            payload["messages"] for target, payload in exchange.calls if target == saved.TARGETS[2]
        ][-1]
        timing = [
            item
            for item in messages
            if item["role"] == "system" and "[Temporal context]" in item["content"]
        ]
        assert len(timing) == int(has_gap)
        assert messages[-1] == {"role": "user", "content": "unchanged user text"}
        assert len(port.requests) == 2 and authority.commit_count == 2
        assert (
            port.requests[1]["input"]["owner_snapshot"]["lifecycle"]["active_user_received_at_ms"]
            == now[0]
        )
    finally:
        restore_resolved_profile(token)
        store.close()
