"""Sealed-profile numeric Flow proof through real HTTP, owners, and Broker.

Uses deterministic local tool validation/normalization; no model or remote I/O.
Run with the repository's normal packaged-profile fixtures after resealing.
"""
from pathlib import Path
import struct

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile, prepare_default_profile_confirmation,
)
from core_runtime.global_contracts.http_contract_dispatch import HTTPContractBinding
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from core_runtime.workflow_v4.data_codec import outcome_digest, request_digest
from ecosystem.defaultspack.defaultspack.http_contract_composition import (
    defaultspack_capability_snapshot,
)
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)
from tests.conformance_support.host_contract import host_contract_for_session
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
from tests.test_conversation_v4_capability_binding import _authenticate
from tests.test_workflow_v4_pack_control_integration import _capture_defaultspack_dispatch
from tests.test_workflow_v4_http_surface import (
    _workflow_catalog, _capability_request, _post, PACK_ID,
)
from tobkiri_protocol.data_codec import RECORD_MARKER, VERSION

RUNTIME = Path(__file__).resolve().parents[1]
VALIDATE = "tobkiri.service.tool.arguments.validate.v1"
NORMALIZE = "tobkiri.service.tool.result.normalize.v1"


def test_captured_numeric_chain_reaches_http_run_get(tmp_path, monkeypatch):
    """Persist real numeric owner output, bind it downstream, and read via UI HTTP."""
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    active = capture_default_profile(
        confirmation=prepare_default_profile_confirmation(),
    )
    authority = AuthorityStore(user_data / "authority/v4.sqlite3")
    session = _capture_defaultspack_dispatch(
        active, bundle_root=packaged_profile_bundle_root(),
        ecosystem_root=RUNTIME / "ecosystem", authority_store=authority,
    )
    binding = HTTPContractBinding(
        method="POST", path="/api/ui/capability/invoke",
        presentation="capability_result", targets=(),
        application_id="runtime.tauri.application.default",
        route_namespace="defaultspack", profile_id=session.profile_id,
        profile_revision=session.profile_revision,
        activation_id=session.activation_id, plan_digest=session.plan_digest,
    )
    catalog = _workflow_catalog(session)
    snapshot = defaultspack_capability_snapshot(
        binding, session=session, catalog=catalog,
    )
    server = PackAPIServer(
        port=0, dispatch_session=session, contract_bindings=(binding,),
        panel_auth_manager=PanelAuthManager(
            bootstrap_secret="conversation-test-bootstrap",
        ),
        capability_snapshot_factory=defaultspack_capability_snapshot,
        application_presentation=DefaultspackHTTPPresentation(),
        host_contract=host_contract_for_session(session),
    )
    server.start()
    try:
        server.handler_class._capability_catalog_cache = catalog
        cookie, csrf, origin = _authenticate(server)
        headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}

        def call(operation, payload):
            status, response = _post(server, headers, _capability_request(
                session, snapshot, f"pack.{PACK_ID}.{operation}",
                "tobkiri.workflow.v4", payload,
            ))
            assert status == 200 and response["success"], (status, response)
            return response["data"]

        targets = {
            item["contract_id"]: item
            for item in call("operation.palette", {})["operations"]
        }

        def step(name, contract, payload, depends_on):
            target = targets[contract]
            return {
                "id": name, "depends_on": depends_on,
                "request": {
                    **{key: target[key] for key in (
                        "contract_id", "contract_revision_digest",
                        "operation_id", "function_principal_id",
                    )}, "input": payload,
                },
                "retry": {"max_attempts": 1, "backoff_ms": 0},
            }

        numbers = [0.375, -0.0, 5e-324]
        tag = {RECORD_MARKER: {"version": VERSION, "paths": [["value"]]}}
        document = {
            "workflow_api_version": "io.tobkiri.workflow.v4",
            "name": "Numeric HTTP owner chain", "max_concurrency": 1,
            "steps": [
                step("validate", VALIDATE, {
                    "schema": {"type": "object"},
                    "arguments": {
                        "numbers": "${inputs.numbers}",
                        "literal": 0.375, "tag": tag,
                    },
                }, []),
                step("normalize", NORMALIZE, {
                    "tool_id": "numeric.fixture", "tool_call_id": "call.numeric",
                    "executor_provider_instance_id": "fixture.owner",
                    "executor_content_hash": "fixture.digest",
                    "value": "${steps.validate.output.arguments}",
                }, ["validate"]),
            ],
        }
        made = call("definition.create", {
            "definition_id": "numeric.http", "document": document,
        })
        call("definition.publish", {
            "definition_id": "numeric.http", "if_match": made["etag"],
        })
        call("run.create", {
            "definition_id": "numeric.http", "run_id": "numeric-http",
            "inputs": {"numbers": numbers},
        })
        call("run.advance", {"run_id": "numeric-http"})
        call("run.advance", {"run_id": "numeric-http"})
        view = call("run.get", {"run_id": "numeric-http"})
        assert view["run"]["state"] == "succeeded", view
        attempts = {item["step_id"]: item for item in view["attempts"]}
        assert set(attempts) == {"validate", "normalize"}
        result = attempts["normalize"]["outcome"]["result"]
        assert result["literal"] == 0.375 and result["tag"] == tag
        for actual, expected in zip(result["numbers"], numbers, strict=True):
            assert struct.pack(">d", actual) == struct.pack(">d", expected)
        assert attempts["normalize"]["request"]["input"]["value"] == result
        assert len(attempts["normalize"]["output_bindings"]) == 1
        for attempt in attempts.values():
            assert attempt["state"] == "succeeded"
            assert attempt["dispatch_admission"] == "admitted"
            assert attempt["request_digest"] == request_digest(attempt["request"])
            assert attempt["outcome_digest"] == outcome_digest({
                "output": attempt["outcome"], "error_code": None,
                "ambiguous_effect": False, "timed_out": False,
            })
    finally:
        server.stop()
        session.close()
        authority.close()
