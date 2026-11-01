"""Routed HTTP proof for the Workflow v4 dynamic capability surface.

Each request crosses the real boundary: HTTP route -> panel auth -> replay
guard -> finite ``allowed_payload_keys`` -> exact Workflow normalization ->
captured ``V4DispatchSession`` -> RequestBroker -> Workflow Pack provider.
No fixture session stands in for dispatch, and no ``pack.*`` contribution is
admitted by identity other than the verified Workflow targets.
"""

from __future__ import annotations

from copy import deepcopy
import time
import uuid
from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
    capture_profile,
    prepare_profile_confirmation,
)
from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.pack_control_v4 import (
    CONTROL_PRESENTATION_CONTRACT,
    PACK_CONTROL_CONTRACT,
)
from core_runtime.panel_auth import PanelAuthManager
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from ecosystem.defaultspack.defaultspack.http_contract_composition import (
    defaultspack_capability_snapshot,
)
from ecosystem.defaultspack.defaultspack.http_dynamic_targets import (
    defaultspack_dynamic_capability_targets,
)
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)
from ecosystem.defaultspack.defaultspack.workflow_presentation import (
    normalize_workflow_request,
)
from tests.conformance_support.host_contract import host_contract_for_session
from tests.test_conversation_v4_capability_binding import (
    _authenticate,
    _contract,
    _request,
)
from tests.test_workflow_v4_pack_control_integration import (
    PACK_ID,
    _bundle_root,
    _capture_control_session,
    _capture_defaultspack_dispatch,
    _invoke,
)

pytestmark = pytest.mark.contract


RUN_OPS = (
    "definition.archive",
    "definition.compile-preview",
    "definition.create",
    "definition.delete",
    "definition.get",
    "definition.list",
    "definition.publish",
    "definition.update",
    "definition.validate",
    "operation.palette",
    "run.advance",
    "run.cancel",
    "run.create",
    "run.get",
    "run.observe",
    "run.pause",
    "run.reconcile-recovery",
    "run.resume",
    "run.step.execute",
    "run.step.resume",
    "run.step.retry",
)


def _activate_workflow_pack() -> None:
    """Run the full pack install/approve/enable + Profile change ceremony."""

    session = _capture_control_session()
    try:
        _invoke(session, PACK_CONTROL_CONTRACT, "pack.install", {"pack_id": PACK_ID})
        candidate = _invoke(
            session,
            PACK_CONTROL_CONTRACT,
            "approval.candidate",
            {"pack_id": PACK_ID},
        )
        _invoke(
            session,
            PACK_CONTROL_CONTRACT,
            "approval.approve",
            {"pack_id": PACK_ID, "candidate_id": candidate["candidate_id"]},
        )
        _invoke(session, PACK_CONTROL_CONTRACT, "pack.enable", {"pack_id": PACK_ID})
        profile = _invoke(
            session, CONTROL_PRESENTATION_CONTRACT, "profile.read"
        )
        desired = [
            item["pack_id"]
            for item in profile["data"]["profile_document"]["packs"]
            if item.get("role") != "application"
        ]
        resolved = _invoke(
            session,
            CONTROL_PRESENTATION_CONTRACT,
            "profile.change.resolve",
            {
                "profile_id": "defaults",
                "expected_profile_revision": profile["profile_revision"],
                "expected_plan_digest": profile["plan_digest"],
                "desired_pack_ids": desired,
            },
        )
        reviewed = _invoke(
            session,
            CONTROL_PRESENTATION_CONTRACT,
            "profile.change.review",
            {
                "candidate_id": resolved["candidate_id"],
                "candidate_digest": resolved["candidate_digest"],
            },
        )
        approved = _invoke(
            session,
            CONTROL_PRESENTATION_CONTRACT,
            "profile.change.approve",
            {
                "candidate_id": reviewed["candidate_id"],
                "candidate_digest": reviewed["candidate_digest"],
            },
        )
        activated = _invoke(
            session,
            CONTROL_PRESENTATION_CONTRACT,
            "profile.change.activate",
            {
                "approval_id": approved["approval_id"],
                "approval_digest": approved["approval_digest"],
            },
        )
        assert activated["state"] == "active"
    finally:
        close = getattr(session, "close", None)
        if close is not None:
            close()


def _workflow_catalog(session: object, active=None) -> dict[str, object]:
    """Project the real captured Workflow bindings into a UI catalog."""

    operations: list[dict[str, object]] = []
    artifact_digest = ""
    for contract_id, selected in (
        ("tobkiri.workflow.v4", RUN_OPS),
        ("tobkiri.workflow.stop.v4", ("run.stop",)),
    ):
        for meta in session.provider_metadata(contract_id):
            if meta.get("operation_id") not in selected:
                continue
            artifact_digest = artifact_digest or str(meta["artifact_digest"])
            operations.append(
                {
                    "invokable": True,
                    "contract_id": contract_id,
                    "operation_id": meta["operation_id"],
                    "provider_id": meta["provider_id"],
                    "function_id": meta["function_id"],
                }
            )
    expected = {
        (edge["contract_id"], edge["operation_id"])
        for edge in (active or capture_default_profile()).resolved.profile["requested_edges"]
        if edge["caller_function_id"] == "shell.tauri.default"
        and edge["contract_id"] in {"tobkiri.workflow.v4", "tobkiri.workflow.stop.v4"}
    }
    assert {(item["contract_id"], item["operation_id"]) for item in operations} == expected
    return {
        "packs": [
            {
                "pack_id": PACK_ID,
                "artifact_digest": "sha256:" + "f" * 64,
                "pack_artifact_digest": artifact_digest,
                "enabled": True,
                "approved": True,
                "operations": operations,
            }
        ]
    }


def _capability_request(
    session: object,
    snapshot: object,
    contribution_id: str,
    contract_id: str,
    payload: dict[str, object],
) -> dict[str, object]:
    """Build one exact capability envelope matching the captured identity."""

    return {
        "request_id": str(uuid.uuid4()),
        "expires_at": time.time() + 45,
        "profile_id": session.profile_id,
        "profile_revision": session.profile_revision,
        "activation_id": session.activation_id,
        "plan_hash": session.plan_digest,
        "catalog_hash": snapshot.catalog_hash,
        "contribution_id": contribution_id,
        "owner_pack_id": PACK_ID,
        "contract_id": contract_id,
        "payload": payload,
    }


def _post(
    server: PackAPIServer,
    headers: dict[str, str],
    body: dict[str, object],
) -> tuple[int, dict[str, object]]:
    # The replay guard demands a fresh canonical request identity per call.
    status, response, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/ui/capability/invoke"),
        body=body,
        headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    return status, response


def _workflow_document(step_target: dict[str, object], *, skipped: bool) -> dict[str, object]:
    """One linear step bound to an explicitly admitted text operation."""

    step: dict[str, object] = {
        "id": "speak",
        "request": {
            "contract_id": step_target["contract_id"],
            "contract_revision_digest": step_target["contract_revision_digest"],
            "operation_id": step_target["operation_id"],
            "function_principal_id": step_target["function_principal_id"],
            "input": {"text": "${inputs.text}", "model_profile_id": "fixture.model"},
        },
        "retry": {"max_attempts": 1, "backoff_ms": 0},
    }
    if skipped:
        step["when"] = "false"
    return {
        "workflow_api_version": "io.tobkiri.workflow.v4",
        "name": "HTTP routed workflow",
        "max_concurrency": 1,
        "steps": [step],
    }


def test_workflow_operations_route_through_real_http_and_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authoring, run control, and stop reach the real Broker over HTTP."""

    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    capture_default_profile(
        confirmation=prepare_default_profile_confirmation()
    )
    _activate_workflow_pack()
    # This case proves approval and Stop, not live model inference. Narrow an
    # existing signed edge explicitly rather than assuming Defaults still asks
    # for approval on a Profile-granted pure operation.
    definitions = ProfileDefinitionStore(tmp_path / "user-data")
    fixture = deepcopy(dict(definitions.get_profile("defaults").profile))
    changed = 0
    for edge in fixture["requested_edges"]:
        if (edge["caller_function_id"] == "tobkiri.workflow.provider"
                and edge["contract_id"] == "tobkiri.service.ai.text.generate.v1"):
            edge["authority_mode"] = "interactive_only"
            changed += 1
    assert changed == 1
    definitions.create_profile(fixture, profile_id="wfhttp", display_name="HTTP approval proof")
    active = capture_profile("wfhttp", confirmation=prepare_profile_confirmation("wfhttp"))
    authority = AuthorityStore(tmp_path / "user-data" / "authority" / "v4.sqlite3")
    session = _capture_defaultspack_dispatch(
        active,
        bundle_root=_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=authority,
    )
    binding = HTTPContractBinding(
        method="POST",
        path="/api/ui/capability/invoke",
        presentation="capability_result",
        targets=(),
        application_id="runtime.tauri.application.default",
        route_namespace="defaultspack",
        profile_id=session.profile_id,
        profile_revision=session.profile_revision,
        activation_id=session.activation_id,
        plan_digest=session.plan_digest,
    )
    catalog = _workflow_catalog(session, active)
    snapshot = defaultspack_capability_snapshot(
        binding, session=session, catalog=catalog
    )
    projected_ops = {target.operation_id for target in snapshot.targets}
    # Every captured operation projects as pack.<pack_id>.<op>; run.observe is
    # admitted but still optional until its provider handler lands.
    assert all(
        target.contribution_id == f"pack.{PACK_ID}.{target.operation_id}"
        for target in snapshot.targets
    )
    assert projected_ops == {operation["operation_id"] for operation in catalog["packs"][0]["operations"]}
    assert "run.observe" in projected_ops
    assert "run.stop" in projected_ops
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(
            bootstrap_secret="conversation-test-bootstrap"
        ),
        dispatch_session=session,
        contract_bindings=(binding,),
        capability_snapshot_factory=defaultspack_capability_snapshot,
        application_presentation=DefaultspackHTTPPresentation(),
        host_contract=host_contract_for_session(session),
    )
    server.start()
    try:
        server.handler_class._capability_catalog_cache = catalog
        cookie, csrf, origin = _authenticate(server)
        headers = {
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
        }

        def call(
            operation_id: str,
            payload: dict[str, object],
            *,
            contract_id: str = "tobkiri.workflow.v4",
        ) -> dict[str, object]:
            status, response = _post(
                server,
                headers,
                _capability_request(
                    session,
                    snapshot,
                    f"pack.{PACK_ID}.{operation_id}",
                    contract_id,
                    payload,
                ),
            )
            assert status == 200, response
            assert response["success"] is True, response
            return dict(response["data"])

        # Palette first: the document binds a real captured operation.
        palette = call("operation.palette", {})
        step_target = next(
            item
            for item in palette["operations"]
            if item["contract_id"] == "tobkiri.service.ai.text.generate.v1"
        )
        document = _workflow_document(step_target, skipped=True)
        validated = call("definition.validate", {"document": document})
        assert validated.get("errors", []) == []
        created = call(
            "definition.create",
            {"definition_id": "workflow.http-routed", "document": document},
        )
        call(
            "definition.publish",
            {
                "definition_id": "workflow.http-routed",
                "if_match": created["etag"],
            },
        )
        fetched = call(
            "definition.get", {"definition_id": "workflow.http-routed"}
        )
        assert fetched["state"] == "published"
        listed = call("definition.list", {})
        assert "workflow.http-routed" in {
            item["definition_id"] for item in listed["definitions"]
        }

        # Run lifecycle over HTTP: create -> advance -> get.
        run = call(
            "run.create",
            {
                "definition_id": "workflow.http-routed",
                "run_id": "workflow-http-run",
                "revision_digest": fetched["revision_digest"],
                "inputs": {"text": "hi"},
            },
        )
        assert run["state"] == "queued"
        assert run["revision_digest"] == fetched["revision_digest"]
        status, rejected = _post(server, headers, _capability_request(
            session, snapshot, f"pack.{PACK_ID}.run.create", "tobkiri.workflow.v4",
            {"definition_id": "workflow.http-routed", "run_id": "wrong-revision",
             "revision_digest": "sha256:" + "f" * 64, "inputs": {}},
        ))
        assert status != 200, rejected
        call("run.advance", {"run_id": "workflow-http-run"})
        finished = call("run.get", {"run_id": "workflow-http-run"})
        assert finished["run"]["state"] == "succeeded"
        observed = call("run.observe", {"run_id": "workflow-http-run"})
        assert observed["run"]["state"] == "succeeded"
        assert "inputs" not in observed["run"]
        assert all("request" not in attempt for attempt in observed["attempts"])

        # An approval-gated run is stopped through the separate stop route.
        gated_document = _workflow_document(step_target, skipped=False)
        call(
            "definition.create",
            {
                "definition_id": "workflow.http-gated",
                "document": gated_document,
            },
        )
        gated = call(
            "definition.get", {"definition_id": "workflow.http-gated"}
        )
        call(
            "definition.publish",
            {
                "definition_id": "workflow.http-gated",
                "if_match": gated["etag"],
            },
        )
        call(
            "run.create",
            {
                "definition_id": "workflow.http-gated",
                "run_id": "workflow-http-stop",
                "inputs": {"text": "hi"},
            },
        )
        waiting = call("run.advance", {"run_id": "workflow-http-stop"})
        assert waiting["run"]["state"] == "waiting_approval"
        stopped = call(
            "run.stop",
            {"run_id": "workflow-http-stop"},
            contract_id="tobkiri.workflow.stop.v4",
        )
        assert stopped["state"] == "cancelled"

        # The finite key surface and the exact identity gate both deny.
        status, denied = _post(
            server,
            headers,
            _capability_request(
                session,
                snapshot,
                f"pack.{PACK_ID}.run.advance",
                "tobkiri.workflow.v4",
                {"run_id": "workflow-http-run", "bogus": True},
            ),
        )
        assert status == 400, denied
        status, denied = _post(
            server,
            headers,
            _capability_request(
                session,
                snapshot,
                f"pack.{PACK_ID}.run.advance",
                "tobkiri.workflow.v4",
                {},
            ),
        )
        assert status == 400, denied
        status, denied = _post(
            server,
            headers,
            _capability_request(
                session,
                snapshot,
                f"pack.{PACK_ID}.run.stop",
                "tobkiri.workflow.v4",
                {"run_id": "workflow-http-stop"},
            ),
        )
        assert status == 404, denied
    finally:
        server.handler_class._capability_catalog_cache = None
        server.stop()
        session.close()


def test_workflow_normalize_denies_unknown_and_malformed_payloads() -> None:
    """The normalizer itself fails closed on shape and key violations."""

    session = type(
        "Session", (), {"assert_current": staticmethod(lambda: None)}
    )()
    binding = HTTPContractBinding(
        method="POST",
        path="/api/ui/capability/invoke",
        presentation="capability_result",
        targets=(),
    )
    catalog = {
        "packs": [
            {
                "pack_id": PACK_ID,
                "artifact_digest": "sha256:" + "a" * 64,
                "pack_artifact_digest": "sha256:" + "b" * 64,
                "enabled": True,
                "approved": True,
                "operations": [
                    {
                        "invokable": True,
                        "contract_id": "tobkiri.workflow.v4",
                        "operation_id": "run.create",
                        "provider_id": "tobkiri.workflow.provider",
                        "function_id": "tobkiri.workflow.provider",
                    },
                    {
                        "invokable": True,
                        "contract_id": "tobkiri.workflow.stop.v4",
                        "operation_id": "run.stop",
                        "provider_id": "tobkiri.workflow.stop.provider",
                        "function_id": "tobkiri.workflow.stop.provider",
                    },
                ],
            }
        ]
    }
    targets = {
        target.operation_id: target
        for target in defaultspack_dynamic_capability_targets(
            binding, catalog=catalog
        )
    }
    presentation = DefaultspackHTTPPresentation()

    # Exact target, exact keys pass through unchanged.
    normalized = presentation.normalize_payload(
        targets["run.create"],
        {"definition_id": "d1", "inputs": {"k": "v"}},
        session=session,
        workspace_binding_resolver=None,
    )
    assert normalized == {"definition_id": "d1", "inputs": {"k": "v"}}

    pinned = {"definition_id": "d1", "revision_digest": "sha256:" + "a" * 64}
    assert presentation.normalize_payload(
        targets["run.create"], pinned, session=session,
        workspace_binding_resolver=None,
    ) == pinned
    for bad in (
        {"definition_id": "d1", "revision_digest": "invalid"},
        {"definition_id": "d1", "revision_digest": 1},
        {"definition_id": "d1", "inputs": "not-a-mapping"},
        {"definition_id": " d1 "},
        {"definition_id": ""},
        {"definition_id": "d1", "profile_id": "attacker"},
        {"inputs": {}},
    ):
        with pytest.raises(ValueError):
            presentation.normalize_payload(
                targets["run.create"],
                bad,
                session=session,
                workspace_binding_resolver=None,
            )
    for bad in ({}, {"run_id": "r", "extra": 1}):
        with pytest.raises(ValueError):
            presentation.normalize_payload(
                targets["run.stop"],
                bad,
                session=session,
                workspace_binding_resolver=None,
            )

    # Direct normalizer use still enforces the verified identity tuple.
    forged = type(
        "Target",
        (),
        {
            "owner_pack_id": PACK_ID,
            "contribution_id": f"pack.{PACK_ID}.run.stop",
            "contract_id": "tobkiri.workflow.stop.v4",
            "operation_id": "run.stop",
            "provider_id": "attacker.provider",
            "function_id": "attacker.provider",
        },
    )()
    with pytest.raises(ValueError):
        normalize_workflow_request(forged, {"run_id": "r"})

    # A non-Workflow pack.* contribution still hits the closed fallthrough.
    other = type(
        "Target",
        (),
        {
            "owner_pack_id": "other_pack",
            "contribution_id": "pack.other_pack.read",
            "input_schema": b"",
            "contract_id": "tobkiri.workflow.v4",
            "operation_id": "run.get",
            "provider_id": "tobkiri.workflow.provider",
            "function_id": "tobkiri.workflow.provider",
        },
    )()
    with pytest.raises(ValueError):
        presentation.normalize_payload(
            other,
            {"run_id": "r"},
            session=session,
            workspace_binding_resolver=None,
        )
