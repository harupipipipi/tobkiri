"""Actual two-owner Workflow/Broker execution, without native VM claims."""
from pathlib import Path
from copy import deepcopy

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile, prepare_default_profile_confirmation,
    capture_profile, prepare_profile_confirmation,
)
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
from tests.test_voice_workflow_e2e_v4 import _invoke, _approve
from tobkiri_protocol.canonical import canonical_digest
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from tests.test_workflow_v4_pack_control_integration import _capture_defaultspack_dispatch

VALIDATE = "tobkiri.service.tool.arguments.validate.v1"
NORMALIZE = "tobkiri.service.tool.result.normalize.v1"
pytestmark = pytest.mark.contract


@pytest.mark.parametrize("interactive", [False, True])
def test_two_real_tool_owners_execute_through_workflow_approval_and_broker(tmp_path, monkeypatch, interactive):
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    profile_id = "defaults"
    if interactive:
        definitions = ProfileDefinitionStore(user_data)
        definition = definitions.get_profile("defaults")
        assert definition is not None
        fixture = deepcopy(dict(definition.profile))
        changed = 0
        for edge in fixture["requested_edges"]:
            if edge["caller_function_id"] == "tobkiri.workflow.provider" and edge["contract_id"] in {VALIDATE, NORMALIZE}:
                edge["authority_mode"] = "interactive_only"
                changed += 1
        assert changed == 2
        profile_id = "wfask"
        definitions.create_profile(fixture, profile_id=profile_id, display_name="Workflow approval proof")
        active = capture_profile(profile_id, confirmation=prepare_profile_confirmation(profile_id))
    authority = AuthorityStore(user_data / "authority/v4.sqlite3")
    session = _capture_defaultspack_dispatch(
        active, bundle_root=packaged_profile_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=authority,
    )
    try:
        palette = _invoke(session, "operation.palette", {})
        targets = {item["contract_id"]: item for item in palette["operations"]}
        value = {"items": [1, "two", None, True], "nested": {"password": "synthetic-test-value"}}
        schema = {
            "type": "object", "required": ["items", "nested"], "additionalProperties": False,
            "properties": {
                "items": {"type": "array", "minItems": 4, "maxItems": 4},
                "nested": {"type": "object", "required": ["password"],
                           "additionalProperties": False,
                           "properties": {"password": {"type": "string", "minLength": 1}}},
            },
        }
        def step(step_id, contract, payload, dependencies):
            target = targets[contract]
            return {"id": step_id, "depends_on": dependencies,
                    "request": {**{key: target[key] for key in (
                        "contract_id", "contract_revision_digest", "operation_id", "function_principal_id")},
                        "input": payload}, "retry": {"max_attempts": 1, "backoff_ms": 0}}
        document = {"workflow_api_version": "io.tobkiri.workflow.v4", "name": "Real owner conformance",
                    "max_concurrency": 1, "steps": [
                        step("validate", VALIDATE, {"schema": schema, "arguments": value}, []),
                        step("normalize", NORMALIZE, {
                            "tool_id": "fixture", "tool_call_id": "call.fixture",
                            "executor_provider_instance_id": "fixture.owner",
                            "executor_content_hash": "fixture.digest",
                            "value": "${steps.validate.output.arguments}"}, ["validate"]),
                    ]}
        created = _invoke(session, "definition.create", {"definition_id": "real.tool.chain", "document": document})
        _invoke(session, "definition.publish", {"definition_id": "real.tool.chain", "if_match": created["etag"]})
        _invoke(session, "run.create", {"definition_id": "real.tool.chain", "run_id": "real-tool-chain", "inputs": {}})
        def approved_step(step_id):
            _invoke(session, "run.advance", {"run_id": "real-tool-chain"})
            view = _invoke(session, "run.get", {"run_id": "real-tool-chain"})
            attempt = next(item for item in view["attempts"] if item["step_id"] == step_id)
            if not interactive:
                # The signed Profile already grants these pure operations.
                assert attempt["state"] == "succeeded", attempt
                assert not attempt.get("approval_request_id")
                return attempt
            assert attempt["state"] == "waiting_approval", attempt
            assert attempt.get("outcome") is None
            assert attempt["approval_request_id"].startswith("interactive-effect-")
            assert attempt["approval_request_id"] != attempt["authority_reservation_id"]
            _approve(session, attempt["approval_request_id"])
            return _invoke(session, "run.step.resume", {"run_id": "real-tool-chain", "step_id": step_id})

        checked = approved_step("validate")
        assert checked["state"] == "succeeded", checked
        assert checked["outcome"] == {"valid": True, "arguments": value, "errors": [], "coerced": False}
        normalized = approved_step("normalize")
        assert normalized["state"] == "succeeded", normalized
        assert normalized["outcome"]["result"] == {
            "items": [1, "two", None, True], "nested": {"password": "[REDACTED]"}}
        assert normalized["outcome"]["executor"] == {
            "provider_instance_id": "fixture.owner", "content_hash": "fixture.digest"}
        finished = _invoke(session, "run.get", {"run_id": "real-tool-chain"})
        assert finished["run"]["state"] == "succeeded"
        assert len(finished["attempts"]) == 2
        assert all(item["state"] == "succeeded" for item in finished["attempts"])
        by_step = {item["step_id"]: item for item in finished["attempts"]}
        assert by_step["normalize"]["request"]["input"]["value"] == value
        assert by_step["normalize"]["output_bindings"] == [{
            "step_id": "validate", "attempt_id": by_step["validate"]["attempt_id"],
            "path": "arguments", "output_digest": by_step["validate"]["outcome_digest"],
        }]
        for attempt in finished["attempts"]:
            assert attempt["dispatch_admission"] == "admitted"
            assert attempt["request_digest"] == canonical_digest(attempt["request"])
            assert attempt["outcome_digest"] == canonical_digest({
                "output": attempt["outcome"], "error_code": None,
                "ambiguous_effect": False, "timed_out": False,
            })
        session.close()
        authority.close()
        authority = AuthorityStore(user_data / "authority/v4.sqlite3")
        session = _capture_defaultspack_dispatch(
            capture_profile(profile_id), bundle_root=packaged_profile_bundle_root(),
            ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
            authority_store=authority,
        )
        restored = _invoke(session, "run.get", {"run_id": "real-tool-chain"})
        assert restored["run"]["state"] == "succeeded"
        assert restored["attempts"] == finished["attempts"]
    finally:
        session.close()
        authority.close()
