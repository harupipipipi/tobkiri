"""Real Authority/Broker approval and MCP child under an isolated Profile.

The Profile and packaged Shell artifact are test fixtures. This is not native,
external-provider, process-containment, or ordinary Defaults UI acceptance.
"""

import json
from pathlib import Path
import shutil

import pytest

from core_runtime.authority.ui_operator import sign_ui_operator
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture, runtime
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from core_runtime.mcp.connection_owner import CONNECT, CONTRACT_ID, DISCONNECT, LIST, PREPARE
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import create_runtime_surface_services
from ecosystem.rumi_workspace_mount_pack.runtime.mounts import WorkspaceMountStore
from scripts.generate_profile_artifacts import render
from scripts.generate_packaged_defaultspack_v4_bundle import package_bundle
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
from tests.test_mcp_connection_owner import connection_request as connection_request


_EFFECT = "tobkiri.service.interactive-effect.v1"
_APPROVAL = "tobkiri.service.interactive-approval.v1"
_COORDINATOR = "rumi_host_authority_bridge_pack.host-authority.interactive-effect"
_OWNER = "tobkiri.mcp.connections"
_RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def _edge(caller, provider, contract, operation, mode="profile_grant"):
    return {
        "caller_function_id": caller,
        "target_provider_id": provider,
        "contract_id": contract,
        "operation_id": operation,
        "authority_mode": mode,
        "requested_scope_template": {
            "capability": "operation.invoke",
            "dimensions": {"contract": [contract], "operation": [operation]},
            "quotas": {},
            "exact_request_digest": None,
            "opaque": False,
        },
    }


@pytest.fixture
def mcp_session(tmp_path, monkeypatch):
    source_bundle = packaged_profile_bundle_root()
    destination = tmp_path / "packaged-defaultspack"
    shutil.copytree(source_bundle.parent, destination)
    bundle = destination / "v4"
    intent_path = bundle / "defaults.profile.intent.v1.json"
    # The packaged bundle intentionally omits source-only intent files.
    source_intent = _RUNTIME_ROOT / "ecosystem/defaultspack/v4/defaults.profile.intent.v1.json"
    intent = json.loads(source_intent.read_text())
    intent["packs"].append(
        {
            "artifact_digest": None,
            "pack_id": "tobkiri_mcp_connection_pack",
            "role": "provider",
        }
    )
    intent["requested_edges"].extend(
        [
            _edge(_COORDINATOR, _OWNER, CONTRACT_ID, PREPARE),
            _edge(_COORDINATOR, _OWNER, CONTRACT_ID, CONNECT, "interactive_only"),
            _edge("shell.tauri.default", _OWNER, CONTRACT_ID, LIST),
            _edge("shell.tauri.default", _OWNER, CONTRACT_ID, DISCONNECT),
            _edge(
                _OWNER,
                "rumi_workspace_mount_pack.workspace-mount.resource",
                "tobkiri.resource.workspace.v1",
                "rumi_workspace_mount_pack.workspace-resource",
            ),
        ]
    )
    intent_path.write_text(json.dumps(intent, indent=2) + "\n")
    outputs = render(
        bundle_root=bundle,
        intent_path=intent_path,
        compatibility_path=bundle / "defaults.profile.v4.json",
        lock_path=bundle / "defaults.profile.lock.v5.json",
        provenance_path=bundle / "defaults.release.provenance.json",
        source_bundle_root=_RUNTIME_ROOT / "ecosystem/defaultspack/v4",
    )
    for path, contents in outputs.items():
        if path.exists():
            path.chmod(0o600)
        path.write_bytes(contents)
    # Rebind through the official packager, which verifies the existing test
    # binary and pins the complete Shell identity. Never hand-edit those pins.
    shell = json.loads((source_bundle / "shell.tauri.default.shell.v1.json").read_text())
    variant = shell["launch"]["variants"][0]
    package_bundle(
        bundle_root=bundle, artifact_root=destination / "platform-artifacts",
        **{key: variant[key] for key in (
            "relative_path", "entrypoint", "platform", "architecture", "bundle_identity",
        )},
        source_provenance_file=(
            source_bundle.parent.parent
            / "sealed-source-owner/source/packaging-source-provenance.v1.json"
        ),
    )
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setattr(profile_capture, "_bundle_root", lambda _base_dir=None: bundle)
    monkeypatch.setattr(runtime, "_bundle_root", lambda _base_dir=None: bundle)
    active = profile_capture.capture_default_profile(
        confirmation=profile_capture.prepare_default_profile_confirmation(),
    )
    store = AuthorityStore(user_data / "authority/v4.sqlite3")
    session = capture_production_dispatch(
        active,
        bundle_root=bundle,
        ecosystem_root=_RUNTIME_ROOT / "ecosystem",
        authority_store=store,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
    )
    mounts = WorkspaceMountStore("defaults", user_data_root=user_data)
    mounted = mounts.mount("workspace", str(tmp_path), expected_revision=0)
    mounts.select("workspace", expected_revision=mounted["revision"])
    try:
        yield session, store
    finally:
        session.close()
        store.close()


def test_real_broker_approves_one_owned_mcp_start_and_rejects_foreign_resume(
    mcp_session,
    connection_request,
):
    session, authority = mcp_session

    def invoke(contract, operation, payload, *, owner="mcp-owner-session"):
        return session.invoke(contract, operation, {**payload, "_session_id": owner})

    pending = invoke(
        _EFFECT,
        "interactive_effect.manage",
        {
            "phase": "prepare",
            "effect_kind": "mcp_connect",
            "request": connection_request,
        },
    )
    assert pending["state"] == "approval_pending"
    assert invoke(CONTRACT_ID, LIST, {}) == {"connections": []}
    resume = {"phase": "resume", "effect_id": pending["effect_id"]}
    assert invoke(_EFFECT, "interactive_effect.manage", resume)["state"] == "approval_pending"
    with pytest.raises(GlobalContractInvocationError):
        invoke(_EFFECT, "interactive_effect.manage", resume, owner="foreign-session")
    # An identifier or a request-shaped payload does not grant the execute edge.
    with pytest.raises(GlobalContractInvocationError):
        invoke(CONTRACT_ID, CONNECT, {"request": connection_request, "plan": {}})
    approval_id = pending["approval_request_id"]
    approval = invoke(_APPROVAL, "interactive_approval.get", {"request_id": approval_id})
    invoke(
        _APPROVAL,
        "interactive_approval.approve",
        {
            "request_id": approval_id,
            "confirmation_text": "EXECUTE",
            "ui_operator": sign_ui_operator(
                approval_id,
                nonce="mcp-connection-approval",
                decision="approve",
                request_snapshot_digest=approval["request_snapshot_digest"],
                typed_confirmation_digest=approval["typed_confirmation_digest"],
            ),
        },
    )
    assert invoke(_EFFECT, "interactive_effect.manage", resume)["state"] == "succeeded"
    connections = invoke(CONTRACT_ID, LIST, {})["connections"]
    assert len(connections) == 1 and connections[0]["status"] == "connected"
    assert invoke(CONTRACT_ID, LIST, {}, owner="foreign-session") == {"connections": []}
    assert invoke(_EFFECT, "interactive_effect.manage", resume)["state"] == "succeeded"
    assert invoke(CONTRACT_ID, LIST, {})["connections"] == connections
    invoke(CONTRACT_ID, DISCONNECT, {"connection_id": connections[0]["connection_id"]})
    assert invoke(CONTRACT_ID, LIST, {}) == {"connections": []}
    assert "committed" in {event["event_state"] for event in authority.audit_events()}
