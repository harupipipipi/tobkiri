"""Production Broker evidence reaches one real Host receiver, never descendants."""
from dataclasses import replace

import pytest

from core_runtime.host_provider_function_v4 import SingleOperationHostFactoryV4
from core_runtime.invocation_evidence import EvidenceDenied
from core_runtime.workflow_v4.evidence import EVIDENCE_KIND
from tests.conformance_support.host_profile import captured_host_profile
from tests.test_mcp_host_broker import _edge
from tests.test_voice_workflow_e2e_v4 import _invoke, _run_step, _SESSION_ID

VALIDATE = ("tobkiri.service.tool.arguments.validate.v1", "rumi_tool_validation_pack.tool-arguments-validate")
NORMALIZE = ("tobkiri.service.tool.result.normalize.v1", "rumi_tool_result_pack.tool-result-normalize")
VALIDATOR = "rumi_tool_validation_pack.tool-validation.arguments"
NORMALIZER = "rumi_tool_result_pack.tool-result.normalize"
VALUE = {"text": "unchanged", "count": 7}


def test_real_host_receiver_rejects_json_forgery_and_nested_inheritance(tmp_path, monkeypatch):
    original_capture = SingleOperationHostFactoryV4.capture
    observed, held, denied = [], [], []
    normalizer_principals = set()
    def capture(factory, context):
        captured = original_capture(factory, context)
        if factory.function_id not in {VALIDATOR, NORMALIZER}:
            return captured
        normalizer_principals.update(item.principal_ref.value for item in context.catalog_bindings
                                     if item.function.function_id == NORMALIZER)
        contributions = []
        for contribution in captured.contributions:
            original = contribution.invoke
            def invoke(operation, payload, invocation, original=original, function_id=factory.function_id):
                caller = invocation.envelope.context.caller_principal.value
                if function_id == VALIDATOR and caller in normalizer_principals:
                    with pytest.raises(EvidenceDenied):
                        invocation.invocation_evidence(EVIDENCE_KIND)
                    denied.append("descendant")
                elif function_id == NORMALIZER:
                    if isinstance(payload["value"], dict) and payload["value"].get("forged"):
                        with pytest.raises(EvidenceDenied):
                            invocation.invocation_evidence(EVIDENCE_KIND)
                        denied.append("direct-json")
                    else:
                        view = invocation.invocation_evidence(EVIDENCE_KIND)
                        observed.append((view.read("current"), view.read("input:/value")))
                        held.append(view)
                        result = invocation.contract_client(
                            allowed_contract_ids=frozenset({VALIDATE[0]}),
                            consumer_pack_id="rumi_tool_result_pack", include_credentials=False,
                        ).invoke(*VALIDATE, {"schema": {}, "arguments": VALUE})
                        assert result["arguments"] == VALUE
                return original(operation, payload, invocation)
            contributions.append(replace(contribution, invoke=invoke))
        return replace(captured, contributions=tuple(contributions))
    monkeypatch.setattr(SingleOperationHostFactoryV4, "capture", capture)
    edges = [
        _edge("shell.tauri.default", NORMALIZER, *NORMALIZE),
        _edge(NORMALIZER, VALIDATOR, *VALIDATE),
    ]
    with captured_host_profile(tmp_path, monkeypatch, packs=(), backends=(), edges=edges) as (session, _):
        palette = _invoke(session, "operation.palette", {})["operations"]
        def request(target, values):
            operation = next(item for item in palette if (item["contract_id"], item["operation_id"]) == target)
            return {key: operation[key] for key in ("contract_id", "contract_revision_digest", "operation_id", "function_principal_id")} | {"input": values}
        normal_input = {"tool_id": "fixture", "tool_call_id": "call.fixture", "executor_provider_instance_id": "fixture", "executor_content_hash": "fixture", "value": "${steps.check.output.arguments}"}
        document = {"workflow_api_version": "io.tobkiri.workflow.v4", "steps": [
            {"id": "check", "request": request(VALIDATE, {"schema": {}, "arguments": VALUE})},
            {"id": "normalize", "depends_on": ["check"], "request": request(NORMALIZE, normal_input)},
        ]}
        created = _invoke(session, "definition.create", {"definition_id": "evidence.receiver", "document": document})
        _invoke(session, "definition.publish", {"definition_id": "evidence.receiver", "if_match": created["etag"]})
        run = _invoke(session, "run.create", {"definition_id": "evidence.receiver", "run_id": "host-proof", "inputs": {}})
        assert _run_step(session, "host-proof", "check")["state"] == "succeeded"
        assert _run_step(session, "host-proof", "normalize")["state"] == "succeeded"
        current, lineage = observed[0]
        assert current["run_id"] == run["run_id"]
        assert current["revision_digest"] == run["revision_digest"]
        assert current["step_id"] == "normalize"
        assert lineage["value"] == VALUE
        assert lineage["source_step"] == "check"
        assert [item["step_id"] for item in lineage["lineage"]] == ["check"]
        with pytest.raises(EvidenceDenied):
            held[0].read("current")
        session.invoke(*NORMALIZE, {
            **normal_input, "_session_id": _SESSION_ID,
            "value": {"forged": True, "run_id": "host-proof", "attempt_id": current["attempt_id"], "revision_digest": run["revision_digest"]},
        })
        assert denied == ["descendant", "direct-json"]
