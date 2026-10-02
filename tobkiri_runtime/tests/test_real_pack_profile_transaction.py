"""Real-process Pack-to-Profile transaction coverage for the v4 HTTP surface."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import pytest

from core_runtime.authority.v4 import (
    AuthorityScope,
    AuthorityStore,
    DomainBoundary,
    ExecutionDomain,
    FunctionPrincipal,
)
from core_runtime.bootstrap.production_v4 import (
    _commit_plan_authority,
    _packvm_approval_provenance,
)
from ecosystem.defaultspack.domain.runtime_v4 import (
    ActiveDefaultProfile,
    ResolvedDefaultProfile,
)
from tobkiri_protocol.canonical import canonical_digest

ROOT = Path(__file__).resolve().parents[1]
# ``rumi_git_read_pack`` is now part of the Defaults closure.  Keep this
# transaction on a genuinely optional PackVM Pack so it still proves that
# approval/enablement alone cannot mint execution authority.
TARGET_PACK = "rumi_media_inspect_service_pack"
BOOTSTRAP_SECRET = "isolated-host-owned-panel-bootstrap-secret"
NATIVE_PACKVM_ACCEPTANCE_ENV = "TOBKIRI_RUN_NATIVE_PACKVM_ACCEPTANCE"


_CHILD = r"""
import json
import os
import sys
from pathlib import Path

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
    load_frontend_contract_bindings,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import get_panel_auth_manager
from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
    install_defaultspack_profile_runtime,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
    defaultspack_packvm_backend_factory,
    defaultspack_runtime_capture_inputs,
)
from ecosystem.defaultspack.defaultspack.http_contract_composition import (
    defaultspack_capability_binding,
    defaultspack_capability_snapshot_mapping,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)


ROOT = Path(os.environ["TOBKIRI_TEST_RUNTIME_ROOT"])
BUNDLE_ROOT = Path(sys.argv[1])
from core_runtime.bootstrap import profile_capture

profile_capture._bundle_root = lambda _base_dir=None: BUNDLE_ROOT
install_defaultspack_profile_runtime()
MAP_PATH = (
    ROOT
    / "ecosystem"
    / "defaultspack"
    / "defaultspack"
    / "frontend_contract_map.v4.json"
)
USER_DATA = Path(os.environ["TOBKIRI_USER_DATA"])


def _capture():
    active_pointer = (
        USER_DATA / "workspaces" / "defaults" / "activation" / "active.json"
    )
    if active_pointer.is_file():
        active = capture_default_profile()
    else:
        active = capture_default_profile(
            confirmation=prepare_default_profile_confirmation(),
        )
    authority = AuthorityStore(USER_DATA / "authority" / "v4.sqlite3")
    packvm_lifecycle = None
    packvm_backend_factory = None
    if os.environ.get("TOBKIRI_TEST_NATIVE_PACKVM") == "1":
        from core_runtime.packvm_lifecycle_v4 import PackVMLifecycleV4
        from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
            default_packvm_provisioner,
        )

        packvm_lifecycle = PackVMLifecycleV4(default_packvm_provisioner())
        if packvm_lifecycle.production_backend_registration() is None:
            raise RuntimeError(
                "native PackVM acceptance requires provisioned signed direct-VZ facts"
            )
        packvm_backend_factory = defaultspack_packvm_backend_factory(packvm_lifecycle)
    session = capture_production_dispatch(
        active,
        bundle_root=BUNDLE_ROOT,
        ecosystem_root=ROOT / "ecosystem",
        authority_store=authority,
        packvm_provisioner=packvm_backend_factory,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        capability_binding_snapshot_factory=defaultspack_capability_snapshot_mapping,
        capability_binding_selector=defaultspack_capability_binding,
    )
    catalog = BundledCatalog.load(BUNDLE_ROOT)
    bindings = load_frontend_contract_bindings(
        MAP_PATH,
        catalog.packs["runtime.tauri.application.default"],
    )
    manager = get_panel_auth_manager()
    server = PackAPIServer(
        port=0,
        panel_auth_manager=manager,
        dispatch_session=session,
        contract_bindings=bindings,
        application_presentation=DefaultspackHTTPPresentation(),
        packvm_lifecycle=packvm_lifecycle,
        runtime_capture_factory=lambda active=None: defaultspack_runtime_capture_inputs(
            active,
            packvm_provisioner=packvm_lifecycle,
            bundle_root=BUNDLE_ROOT,
        ),
    )

    def publish_current_host_contract() -> None:
        current = capture_default_profile()
        contract_path = Path(os.environ["TOBKIRI_HOST_CONTRACT_PATH"])
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
        payload.update(
            {
                "schema_version": "tobkiri.host-contract.v1",
                "profile_id": str(current.resolved.profile["profile_id"]),
                "profile_revision": str(current.resolved.plan["profile_revision"]),
                "activation_id": str(current.activation["activation_id"]),
                "plan_digest": str(current.resolved.plan["plan_digest"]),
            }
        )
        replacement = contract_path.with_suffix(".replacement")
        replacement.write_text(
            json.dumps(payload, sort_keys=True),
            encoding="utf-8",
        )
        replacement.chmod(0o600)
        os.replace(replacement, contract_path)

    original_refresh = server._refresh_runtime_capture

    def refresh(session=None) -> None:
        publish_current_host_contract()
        original_refresh(
            session,
            lifecycle_generation=server._lifecycle_generation,
        )

    server._refresh_runtime_capture = refresh
    server.start()
    return server, active, manager


def _state(server, active, manager):
    effective = [
        (str(item["identity"]), str(item["artifact_digest"]))
        for item in active.resolved.lock["effective_set"]
    ]
    return {
        "port": server.port,
        "profile_id": str(active.resolved.profile["profile_id"]),
        "profile_revision": str(active.resolved.plan["profile_revision"]),
        "plan_digest": str(active.resolved.plan["plan_digest"]),
        "activation_id": str(active.activation["activation_id"]),
        "effective_pack_set": effective,
        "manager_is_host_singleton": manager is get_panel_auth_manager(),
        "server_uses_host_manager": server._panel_auth_manager is manager,
    }


def _close(server):
    session = server._dispatch_session
    server.stop()
    if session is not None:
        session.close()


server, active, manager = _capture()
print(json.dumps(_state(server, active, manager), sort_keys=True), flush=True)
if sys.stdin.readline().strip() != "stop":
    raise RuntimeError("stop command is required")
_close(server)
"""


def _write_host_contract(user_data: Path, active: ActiveDefaultProfile) -> Path:
    from tests.conformance_support.host_contract import host_contract

    user_data.mkdir(mode=0o700, exist_ok=True)
    user_data.chmod(0o700)
    path = user_data / "host_contract.json"
    path.write_text(
        json.dumps(
            host_contract(
                profile_id=str(active.resolved.profile["profile_id"]),
                profile_revision=str(active.resolved.plan["profile_revision"]),
                activation_id=str(active.activation["activation_id"]),
                plan_digest=str(active.resolved.plan["plan_digest"]),
                values={"panel_bootstrap_secret": BOOTSTRAP_SECRET},
            ),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _spawn_child(
    env: Mapping[str, str],
) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(packaged_profile_bundle_root())],
        env=dict(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        process.terminate()
        _stdout, stderr = process.communicate(timeout=10)
        raise AssertionError(f"production child exited before readiness: {stderr}")
    try:
        state = json.loads(line)
    except json.JSONDecodeError as error:
        process.terminate()
        process.communicate(timeout=10)
        raise AssertionError(
            f"production child emitted invalid readiness: {line!r}"
        ) from error
    assert isinstance(state, dict)
    return process, state


def _stop_child(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        assert process.stdin is not None
        process.stdin.write("stop\n")
        process.stdin.flush()
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    return stdout, stderr


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: object | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any], list[tuple[str, str]]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = dict(headers or {})
    if encoded is not None:
        request_headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    response_headers = response.getheaders()
    connection.close()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"raw": raw}
    assert isinstance(payload, dict)
    return response.status, payload, response_headers


def _contract(method: str, target: str) -> str:
    return "/api/contracts/defaultspack/" + quote(f"{method.upper()} {target}", safe="")


def _authenticate(port: int) -> dict[str, str]:
    status, payload, _ = _request(
        port,
        "POST",
        "/api/panel/auth/bootstrap",
        body={},
        headers={"X-Rumi-Desktop-Bootstrap": "wrong-secret"},
    )
    assert status == 401, payload
    status, payload, _ = _request(
        port,
        "POST",
        "/api/panel/auth/bootstrap",
        body={"bootstrap_secret": BOOTSTRAP_SECRET},
    )
    assert status == 401, payload
    status, bootstrap, _ = _request(
        port,
        "POST",
        "/api/panel/auth/bootstrap",
        body={},
        headers={"X-Rumi-Desktop-Bootstrap": BOOTSTRAP_SECRET},
    )
    assert status == 200, bootstrap
    code = bootstrap.get("data", {}).get("code")
    assert isinstance(code, str) and code
    origin = f"http://127.0.0.1:{port}"
    status, exchange, response_headers = _request(
        port,
        "POST",
        "/api/panel/auth/exchange",
        body={"code": code},
        headers={"Origin": origin},
    )
    assert status == 200, exchange
    cookie = next(
        value for key, value in response_headers if key.lower() == "set-cookie"
    ).split(";", 1)[0]
    csrf = exchange.get("data", {}).get("csrf_token")
    assert isinstance(csrf, str) and csrf
    return {"cookie": cookie, "csrf": csrf, "origin": origin}


def _contract_request(
    port: int,
    auth: Mapping[str, str],
    method: str,
    target: str,
    *,
    body: object | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Cookie": auth["cookie"],
        "X-Tobkiri-Request-ID": str(uuid.uuid4()),
    }
    if method.upper() != "GET":
        headers.update({"Origin": auth["origin"], "X-Rumi-CSRF": auth["csrf"]})
    status, payload, _ = _request(
        port,
        method,
        _contract(method, target),
        body=body,
        headers=headers,
    )
    return status, payload


def _catalog(port: int, auth: Mapping[str, str]) -> dict[str, Any]:
    status, payload = _contract_request(port, auth, "GET", "/api/pack-control/catalog")
    assert status == 200, payload
    data = payload.get("data")
    assert isinstance(data, dict)
    return data


def _activate_current_profile(
    port: int,
    auth: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one complete Profile ceremony through the production HTTP surface."""

    status, payload = _contract_request(
        port,
        auth,
        "GET",
        "/api/runtime-surface/profile",
    )
    assert status == 200, payload
    profile = payload["data"]
    desired = [
        item["pack_id"]
        for item in profile["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]
    status, resolved = _contract_request(
        port,
        auth,
        "POST",
        "/api/runtime-surface/profile-change/resolve",
        body={
            "profile_id": "defaults",
            "expected_profile_revision": profile["profile_revision"],
            "expected_plan_digest": profile["plan_digest"],
            "desired_pack_ids": desired,
        },
    )
    assert status == 200, resolved
    assert resolved["data"]["state"] == "resolved"
    status, reviewed = _contract_request(
        port,
        auth,
        "POST",
        "/api/runtime-surface/profile-change/review",
        body={
            "candidate_id": resolved["data"]["candidate_id"],
            "candidate_digest": resolved["data"]["candidate_digest"],
        },
    )
    assert status == 200, reviewed
    assert reviewed["data"]["state"] == "reviewed"
    status, approved = _contract_request(
        port,
        auth,
        "POST",
        "/api/runtime-surface/profile-change/approve",
        body={
            "candidate_id": reviewed["data"]["candidate_id"],
            "candidate_digest": reviewed["data"]["candidate_digest"],
        },
    )
    assert status == 200, approved
    assert approved["data"]["state"] == "approved"
    activation_request = {
        "approval_id": approved["data"]["approval_id"],
        "approval_digest": approved["data"]["approval_digest"],
    }
    status, activated = _contract_request(
        port,
        auth,
        "POST",
        "/api/runtime-surface/profile-change/activate",
        body=activation_request,
    )
    assert status == 200, activated
    assert activated["data"]["state"] == "active"
    assert activated["data"]["authoritative_snapshot"]["state"] == "ready"
    return activation_request, activated["data"]


def _pack_status(port: int, auth: Mapping[str, str], pack_id: str) -> dict[str, Any]:
    """Read status from the canonical Host Pack-control catalog.

    The dynamic UI catalog intentionally projects only currently invokable
    application operations. Pack lifecycle state remains Host-owned and is
    read through the exact Pack-control route.
    """

    catalog = _catalog(port, auth)
    return next(
        item for item in catalog["packs"] if item["pack_id"] == pack_id
    )


def _disk_profile_state(user_data: Path) -> dict[str, Any]:
    state_root = user_data / "workspaces" / "defaults" / "activation"
    pointer = json.loads((state_root / "active.json").read_text(encoding="utf-8"))
    envelope_name = pointer["envelope_path"]
    envelope = json.loads(
        (state_root / "activations" / envelope_name).read_text(encoding="utf-8")
    )
    profile = envelope["profile"]
    lock = envelope["lock"]
    plan = envelope["plan"]
    activation = envelope["activation"]
    return {
        "profile_id": str(profile["profile_id"]),
        "profile_revision": str(plan["profile_revision"]),
        "plan_digest": str(plan["plan_digest"]),
        "activation_id": str(activation["activation_id"]),
        "profile_authority_digest": str(
            activation["profile_authority_snapshot_digest"]
        ),
        "security_epoch": int(activation["security_epoch"]),
        "fencing_token": int(activation["fencing_token"]),
        "packvm_target_principal_ids": [
            FunctionPrincipal.from_dict(item["function_principal"]).principal_id
            for item in plan["bindings"]
            if str(item["execution_kind"]) == "pack_vm"
        ],
        "effective_pack_set": [
            [str(item["identity"]), str(item["artifact_digest"])]
            for item in lock["effective_set"]
        ],
    }


def _activation_snapshots(user_data: Path) -> dict[str, bytes]:
    directory = user_data / "workspaces" / "defaults" / "activation" / "activations"
    return {
        path.name: path.read_bytes()
        for path in directory.glob("*.json")
        if path.is_file() and not path.is_symlink()
    }


def _assert_snapshots_immutable(before: Mapping[str, bytes], user_data: Path) -> None:
    current = _activation_snapshots(user_data)
    for name, content in before.items():
        assert current.get(name) == content


def _exercise_real_pack_profile_transaction(
    tmp_path: Path,
    *,
    expect_authenticated_packvm: bool,
) -> None:
    """Exercise one Pack approval/activation transaction over real loopback HTTP."""

    user_data = tmp_path / "user-data"
    from core_runtime.bootstrap.profile_capture import (
        capture_default_profile,
        prepare_default_profile_confirmation,
    )
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )

    install_defaultspack_profile_runtime()
    active = capture_default_profile(
        base_dir=user_data,
        confirmation=prepare_default_profile_confirmation(base_dir=user_data),
    )
    contract_path = _write_host_contract(user_data, active)
    log_dir = tmp_path / "logs"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                (
                    str(ROOT),
                    str(ROOT / "ecosystem" / "defaultspack"),
                    env.get("PYTHONPATH", ""),
                )
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "RUMI_LOG_DIR": str(log_dir),
            "RUMI_USER_DATA": str(user_data),
            "TOBKIRI_USER_DATA": str(user_data),
            "TOBKIRI_HOST_CONTRACT_PATH": str(contract_path),
            "TOBKIRI_TEST_RUNTIME_ROOT": str(ROOT),
        }
    )
    if expect_authenticated_packvm:
        env["TOBKIRI_TEST_NATIVE_PACKVM"] = "1"
    else:
        env.pop("TOBKIRI_TEST_NATIVE_PACKVM", None)
    children: list[subprocess.Popen[str]] = []
    public_output: list[str] = []
    try:
        first_process, first_state = _spawn_child(env)
        children.append(first_process)
        assert first_state["profile_id"] == "defaults"
        assert first_state["manager_is_host_singleton"] is True
        assert first_state["server_uses_host_manager"] is True
        initial_disk = _disk_profile_state(user_data)
        initial_snapshots = _activation_snapshots(user_data)
        assert first_state["profile_revision"] == initial_disk["profile_revision"]
        assert first_state["effective_pack_set"] == initial_disk["effective_pack_set"]
        initial_set = {item[0] for item in initial_disk["effective_pack_set"]}
        assert TARGET_PACK not in initial_set

        first_auth = _authenticate(int(first_state["port"]))
        initial_catalog = _catalog(int(first_state["port"]), first_auth)
        assert initial_catalog["profile_id"] == initial_disk["profile_id"]
        assert initial_catalog["profile_revision"] == initial_disk["profile_revision"]
        assert initial_catalog["plan_digest"] == initial_disk["plan_digest"]
        initial_pack = next(
            item for item in initial_catalog["packs"] if item["pack_id"] == TARGET_PACK
        )
        assert initial_pack["installed"] is False
        assert initial_pack["enabled"] is False
        assert initial_pack["approved"] is False

        status, installed = _contract_request(
            int(first_state["port"]),
            first_auth,
            "POST",
            "/api/pack-control/install",
            body={"pack_id": TARGET_PACK},
        )
        assert status == 200, installed
        status, candidate = _contract_request(
            int(first_state["port"]),
            first_auth,
            "POST",
            "/api/pack-control/approval-candidate",
            body={"pack_id": TARGET_PACK},
        )
        assert status == 200, candidate
        candidate_id = candidate["data"]["candidate_id"]
        status, approved = _contract_request(
            int(first_state["port"]),
            first_auth,
            "POST",
            "/api/pack-control/approval-approve",
            body={"pack_id": TARGET_PACK, "candidate_id": candidate_id},
        )
        assert status == 200, approved
        assert approved["data"]["approved"] is True
        status, enabled = _contract_request(
            int(first_state["port"]),
            first_auth,
            "POST",
            "/api/pack-control/enable",
            body={"pack_id": TARGET_PACK},
        )
        assert status == 200, enabled
        assert enabled["data"]["enabled"] is True
        # A Profile activation publishes a new Host contract/capture and
        # deliberately invalidates panel sessions bound to the old one.
        first_auth = _authenticate(int(first_state["port"]))

        enabled_catalog = _catalog(int(first_state["port"]), first_auth)
        enabled_disk = _disk_profile_state(user_data)
        enabled_authority_binding = dict(enabled_disk)
        enabled_set = {item[0] for item in enabled_disk["effective_pack_set"]}
        assert TARGET_PACK in enabled_set
        assert enabled_disk["profile_id"] == initial_disk["profile_id"]
        assert enabled_disk["profile_revision"] != initial_disk["profile_revision"]
        assert enabled_disk["activation_id"] != initial_disk["activation_id"]
        assert enabled_catalog["profile_revision"] == enabled_disk["profile_revision"]
        assert enabled_catalog["plan_digest"] == enabled_disk["plan_digest"]
        enabled_snapshots = _activation_snapshots(user_data)
        enabled_pack = next(
            item for item in enabled_catalog["packs"] if item["pack_id"] == TARGET_PACK
        )
        assert enabled_pack["enabled"] is True
        approval_path = (
            user_data
            / "pack_control"
            / "approvals"
            / "defaults"
            / f"{TARGET_PACK}.json"
        )
        stable_pack_approval = approval_path.read_bytes()
        # Repeat the full UI-facing ceremony twice without changing the Pack
        # catalog.  Each activation is a new immutable authority generation;
        # the persistent Pack approval and stable principals remain the same.
        first_activation_request, first_activation = _activate_current_profile(
            int(first_state["port"]),
            first_auth,
        )
        first_ceremony_disk = _disk_profile_state(user_data)
        assert first_activation["activation_id"] == first_ceremony_disk["activation_id"]
        assert first_ceremony_disk["activation_id"] != enabled_disk["activation_id"]
        first_auth = _authenticate(int(first_state["port"]))
        second_activation_request, second_activation = _activate_current_profile(
            int(first_state["port"]),
            first_auth,
        )
        second_ceremony_disk = _disk_profile_state(user_data)
        assert (
            second_activation["activation_id"] == second_ceremony_disk["activation_id"]
        )
        assert (
            second_ceremony_disk["activation_id"]
            != first_ceremony_disk["activation_id"]
        )
        assert second_ceremony_disk["effective_pack_set"] == (
            first_ceremony_disk["effective_pack_set"]
        )
        assert approval_path.read_bytes() == stable_pack_approval
        first_auth = _authenticate(int(first_state["port"]))
        status, replay = _contract_request(
            int(first_state["port"]),
            first_auth,
            "POST",
            "/api/runtime-surface/profile-change/activate",
            body=first_activation_request,
        )
        # Profile activation authority is one-shot. Replaying an approval from
        # the previous capture must fail closed instead of rolling authority
        # back to the earlier activation.
        assert status == 403, replay
        assert replay["data"]["code"] == "UNAPPROVED"
        assert second_activation_request != first_activation_request
        first_auth = _authenticate(int(first_state["port"]))
        enabled_disk = second_ceremony_disk
        enabled_snapshots = _activation_snapshots(user_data)

        operation_before_restart = _pack_status(
            int(first_state["port"]), first_auth, TARGET_PACK
        )
        assert operation_before_restart["pack_id"] == TARGET_PACK
        assert operation_before_restart["enabled"] is True
        _assert_snapshots_immutable(initial_snapshots, user_data)
        _assert_snapshots_immutable(enabled_snapshots, user_data)
        stdout, stderr = _stop_child(first_process)
        public_output.extend([stdout, stderr])

        second_process, second_state = _spawn_child(env)
        children.append(second_process)
        assert second_state["profile_id"] == enabled_disk["profile_id"]
        assert second_state["profile_revision"] == enabled_disk["profile_revision"]
        assert second_state["effective_pack_set"] == enabled_disk["effective_pack_set"]
        second_auth = _authenticate(int(second_state["port"]))
        persisted_catalog = _catalog(int(second_state["port"]), second_auth)
        assert persisted_catalog["profile_revision"] == enabled_disk["profile_revision"]
        persisted_pack = next(
            item
            for item in persisted_catalog["packs"]
            if item["pack_id"] == TARGET_PACK
        )
        assert persisted_pack["enabled"] is True
        operation_after_restart = _pack_status(
            int(second_state["port"]), second_auth, TARGET_PACK
        )
        assert operation_after_restart["enabled"] is True

        approved_payload = approval_path.read_bytes()
        status, disabled = _contract_request(
            int(second_state["port"]),
            second_auth,
            "POST",
            "/api/pack-control/disable",
            body={"pack_id": TARGET_PACK},
        )
        assert status == 200, disabled
        assert disabled["data"]["enabled"] is False
        second_auth = _authenticate(int(second_state["port"]))
        disabled_catalog = _catalog(int(second_state["port"]), second_auth)
        disabled_disk = _disk_profile_state(user_data)
        disabled_set = {item[0] for item in disabled_disk["effective_pack_set"]}
        assert TARGET_PACK not in disabled_set
        assert disabled_disk["profile_revision"] != enabled_disk["profile_revision"]
        assert disabled_disk["activation_id"] != enabled_disk["activation_id"]
        assert disabled_catalog["profile_revision"] == disabled_disk["profile_revision"]
        disabled_pack = next(
            item for item in disabled_catalog["packs"] if item["pack_id"] == TARGET_PACK
        )
        assert disabled_pack["installed"] is True
        assert disabled_pack["approved"] is True
        assert disabled_pack["enabled"] is False
        assert (
            _pack_status(int(second_state["port"]), second_auth, TARGET_PACK)["enabled"]
            is False
        )
        disabled_snapshots = _activation_snapshots(user_data)

        status, revoked = _contract_request(
            int(second_state["port"]),
            second_auth,
            "POST",
            "/api/pack-control/approval-revoke",
            body={"pack_id": TARGET_PACK},
        )
        assert status == 200, revoked
        revoked_data = revoked["data"]
        assert revoked_data["approved"] is False
        assert revoked_data["enabled"] is False
        assert revoked_data["approval_status"] == "revoked"
        revoked_payload = json.loads(approval_path.read_text(encoding="utf-8"))
        assert revoked_payload["revoked"] is True
        assert revoked_payload["approval_revision"] == revoked_data["approval_revision"]
        second_auth = _authenticate(int(second_state["port"]))

        status, revoke_replay = _contract_request(
            int(second_state["port"]),
            second_auth,
            "POST",
            "/api/pack-control/approval-revoke",
            body={"pack_id": TARGET_PACK},
        )
        assert status == 409, revoke_replay
        status, enable_replay = _contract_request(
            int(second_state["port"]),
            second_auth,
            "POST",
            "/api/pack-control/enable",
            body={"pack_id": TARGET_PACK},
        )
        assert status == 409, enable_replay

        approval_path.write_bytes(approved_payload)
        replay_catalog = _catalog(int(second_state["port"]), second_auth)
        replay_pack = next(
            item for item in replay_catalog["packs"] if item["pack_id"] == TARGET_PACK
        )
        assert replay_pack["approved"] is False
        assert replay_pack["approval_reason"] == "approval_revoked"
        status, replay_enable = _contract_request(
            int(second_state["port"]),
            second_auth,
            "POST",
            "/api/pack-control/enable",
            body={"pack_id": TARGET_PACK},
        )
        assert status == 409, replay_enable
        _assert_snapshots_immutable(initial_snapshots, user_data)
        _assert_snapshots_immutable(enabled_snapshots, user_data)
        _assert_snapshots_immutable(disabled_snapshots, user_data)
        stdout, stderr = _stop_child(second_process)
        public_output.extend([stdout, stderr])

        third_process, third_state = _spawn_child(env)
        children.append(third_process)
        assert third_state["profile_id"] == disabled_disk["profile_id"]
        assert third_state["profile_revision"] == disabled_disk["profile_revision"]
        assert third_state["effective_pack_set"] == disabled_disk["effective_pack_set"]
        third_auth = _authenticate(int(third_state["port"]))
        final_catalog = _catalog(int(third_state["port"]), third_auth)
        final_pack = next(
            item for item in final_catalog["packs"] if item["pack_id"] == TARGET_PACK
        )
        assert final_pack["enabled"] is False
        assert final_pack["approved"] is False
        assert final_pack["approval_reason"] == "approval_revoked"
        assert (
            _pack_status(int(third_state["port"]), third_auth, TARGET_PACK)["enabled"]
            is False
        )
        status, final_enable = _contract_request(
            int(third_state["port"]),
            third_auth,
            "POST",
            "/api/pack-control/enable",
            body={"pack_id": TARGET_PACK},
        )
        assert status == 409, final_enable
        _assert_snapshots_immutable(initial_snapshots, user_data)
        _assert_snapshots_immutable(enabled_snapshots, user_data)
        _assert_snapshots_immutable(disabled_snapshots, user_data)
        stdout, stderr = _stop_child(third_process)
        public_output.extend([stdout, stderr])

        with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
            events = authority.audit_events()
            grants = authority.list_grants()
            providers = authority.list_provider_authorities()
            approvals = {
                approval_id: authority.get_approval(approval_id)
                for approval_id in {
                    grant.approval_id
                    for grant in grants
                    if grant.approval_id is not None
                }
            }
            all_domains = authority.list_domains()
            domains = {domain.domain_id: domain for domain in all_domains}
        profile_id = str(enabled_disk["profile_id"])
        grant_prefix = f"grant.{profile_id}.profile-pack-vm."
        provider_prefix = f"provider.{profile_id}.profile-pack-vm."
        approval_prefix = f"approval.{profile_id}.profile-pack-vm."
        dynamic_grants = [
            grant for grant in grants if grant.grant_id.startswith(grant_prefix)
        ]
        dynamic_providers = [
            provider
            for provider in providers
            if provider.record_id.startswith(provider_prefix)
        ]
        principals_by_operation: dict[str, set[str]] = {}
        for provider in dynamic_providers:
            principals_by_operation.setdefault(
                provider.provider.operation_id, set()
            ).add(provider.provider.principal_id)
        dynamic_approval_ids = {
            str(record["record_id"])
            for event in events
            if event["event_type"] == "authority_records_committed"
            for record in event["payload"].get("records", [])
            if record["record_type"] == "approval"
            and str(record["record_id"]).startswith(approval_prefix)
        }
        if expect_authenticated_packvm:
            authority_bindings = {
                str(item["activation_id"]): item
                for item in (
                    enabled_authority_binding,
                    first_ceremony_disk,
                    second_ceremony_disk,
                )
            }
            assert set(authority_bindings).issubset(
                {grant.activation_id for grant in dynamic_grants}
            )
            assert len({grant.grant_id for grant in dynamic_grants}) == len(
                dynamic_grants
            )
            assert principals_by_operation
            assert all(
                len(principals) == 1 for principals in principals_by_operation.values()
            )
            assert len(dynamic_approval_ids) >= 3
            providers_by_id = {
                provider.record_id: provider for provider in dynamic_providers
            }
            for grant in dynamic_grants:
                binding = authority_bindings.get(grant.activation_id)
                if binding is None:
                    continue
                assert grant.target.principal_id in set(
                    binding["packvm_target_principal_ids"]
                )
                approval = approvals[grant.approval_id]
                provider_id = grant.grant_id.replace("grant.", "provider.", 1)
                provider = providers_by_id[provider_id]
                domain = domains[provider.execution_domain_id]
                assert approval is not None
                assert domain is not None
                assert grant.profile_id == binding["profile_id"] == "defaults"
                assert (
                    grant.profile_authority_digest
                    == binding["profile_authority_digest"]
                )
                assert grant.security_epoch == binding["security_epoch"]
                assert approval.profile_id == binding["profile_id"]
                assert approval.security_epoch == binding["security_epoch"]
                assert approval.snapshot_digest == canonical_digest(
                    {
                        "ceremony": "defaults.activate",
                        "activation_id": binding["activation_id"],
                        "plan_digest": binding["plan_digest"],
                        "profile_authority_snapshot_digest": binding[
                            "profile_authority_digest"
                        ],
                        "security_epoch": binding["security_epoch"],
                        "scope": grant.scope.to_dict(),
                        "pack_approval_revision": None,
                    }
                )
                assert provider.security_epoch == binding["security_epoch"]
                assert provider.trust_provenance_digest == canonical_digest(
                    {
                        "source": "locked-defaults-profile",
                        "plan_digest": binding["plan_digest"],
                        "target": provider.provider.to_dict(),
                    }
                )
                assert domain.profile_id == binding["profile_id"]
                assert domain.activation_id == binding["activation_id"]
                assert domain.security_epoch == binding["security_epoch"]
                assert domain.fencing_token == binding["fencing_token"]
        else:
            # Optional-Pack approval, client enablement, and an immutable Plan
            # are insufficient to mint execution authority.  Without an exact
            # authenticated production backend there is no target domain,
            # Grant, Provider authority, or Approval bundle for this artifact.
            assert dynamic_grants == []
            assert dynamic_providers == []
            assert dynamic_approval_ids == set()
            packvm_target_principal_ids = {
                principal_id
                for binding in (
                    enabled_authority_binding,
                    first_ceremony_disk,
                    second_ceremony_disk,
                )
                for principal_id in binding["packvm_target_principal_ids"]
            }
            assert packvm_target_principal_ids
            assert all(
                packvm_target_principal_ids.isdisjoint(domain.principal_ids)
                for domain in all_domains
            )
        assert any(
            event["event_type"] == "pack_approval_revoked"
            and event["event_state"] == "committed"
            and event["payload"]["pack_id"] == TARGET_PACK
            for event in events
        )
        assert any(event["event_type"] == "activation" for event in events)

        assert BOOTSTRAP_SECRET not in "".join(public_output)
        for path in user_data.rglob("*"):
            if path.is_file() and path != contract_path:
                assert BOOTSTRAP_SECRET.encode("utf-8") not in path.read_bytes()
        if log_dir.exists():
            assert all(
                BOOTSTRAP_SECRET.encode("utf-8") not in path.read_bytes()
                for path in log_dir.rglob("*")
                if path.is_file()
            )

        print(
            "PACK_PROFILE_E2E "
            + json.dumps(
                {
                    "initial": initial_disk,
                    "enabled": enabled_disk,
                    "disabled_after_revoke": disabled_disk,
                    "operation_before_restart": operation_before_restart,
                    "operation_after_restart": operation_after_restart,
                    "effective_set_added": sorted(enabled_set - initial_set),
                    "effective_set_removed": sorted(enabled_set - disabled_set),
                    "audit_event_count": len(events),
                },
                sort_keys=True,
                default=list,
            )
        )
    finally:
        for process in children:
            if process.poll() is None:
                try:
                    _stop_child(process)
                except (
                    AssertionError,
                    BrokenPipeError,
                    OSError,
                    subprocess.TimeoutExpired,
                ):
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=10)


def test_real_pack_profile_transaction_fails_closed_without_authenticated_packvm(
    tmp_path: Path,
) -> None:
    """Keep optional-Pack authority absent without a verified PackVM backend."""

    _exercise_real_pack_profile_transaction(
        tmp_path,
        expect_authenticated_packvm=False,
    )


def test_packvm_authority_rejects_ambiguous_caller_artifact_provenance() -> None:
    """A shared digest cannot choose one Profile Pack identity for authority."""

    valid, approval_pack_id = _packvm_approval_provenance(
        caller_artifact_digest="sha256:" + "a" * 64,
        target_pack_id="profile-a-pack",
        optional_pack_ids={"profile-a-pack", "profile-b-pack"},
        pack_ids_by_artifact_digest={
            "sha256:" + "a" * 64: {"profile-a-pack", "profile-b-pack"},
        },
    )

    assert valid is False
    assert approval_pack_id is None


def test_packvm_authority_binds_each_profile_activation_without_cross_talk() -> None:
    """Profile A/B produce disjoint activation-bound authority bundles."""

    class EmptyAuthorityStore:
        @staticmethod
        def get_host_extension_trust(record_id: str) -> None:
            return None

        @staticmethod
        def get_approval(record_id: str) -> None:
            return None

        @staticmethod
        def get_provider_authority(record_id: str) -> None:
            return None

        @staticmethod
        def get_grant(record_id: str) -> None:
            return None

    class RecordingAuthorityControl:
        def __init__(self) -> None:
            self.bundles: list[tuple[Any, Any, Any]] = []

        def commit_approval_bundle(
            self,
            approval: Any,
            *,
            host_extension_trust: Any,
            provider_authorities: tuple[Any, ...],
            grants: tuple[Any, ...],
        ) -> None:
            assert host_extension_trust is None
            self.bundles.append((approval, provider_authorities[0], grants[0]))

    def digest(label: str) -> str:
        return canonical_digest({"label": label})

    caller = FunctionPrincipal(
        parent_artifact_digest=digest("caller-artifact"),
        function_implementation_digest=digest("caller-function"),
        function_id="caller.function",
        contract_revision_digest=digest("caller-contract"),
        operation_id="caller.operation",
    )
    target = FunctionPrincipal(
        parent_artifact_digest=digest("target-artifact"),
        function_implementation_digest=digest("target-function"),
        function_id="target.function",
        contract_revision_digest=digest("target-contract"),
        operation_id="target.operation",
    )
    scope = AuthorityScope(
        capability="target.read",
        semantics_digest=digest("target-semantics"),
    )
    control = RecordingAuthorityControl()

    for index, profile_id in enumerate(("profile-a", "profile-b"), start=1):
        activation_id = f"activation.{profile_id}"
        active = ActiveDefaultProfile(
            resolved=ResolvedDefaultProfile(
                profile={"profile_id": profile_id},
                lock={},
                plan={},
            ),
            activation={
                "activation_id": activation_id,
                "created_at": f"2026-08-29T00:00:0{index}Z",
                "plan_digest": digest(f"plan-{profile_id}"),
                "profile_authority_snapshot_digest": digest(f"authority-{profile_id}"),
                "security_epoch": index,
                "fencing_token": index,
            },
        )
        domain = ExecutionDomain(
            domain_id=f"domain.{profile_id}",
            profile_id=profile_id,
            activation_id=activation_id,
            boot_epoch=1,
            process_identity=f"process.{profile_id}",
            authenticated_channel_digest=digest(f"channel-{profile_id}"),
            sandbox_profile_digest=digest(f"sandbox-{profile_id}"),
            resource_namespace=f"resource.{profile_id}",
            principals=(target,),
            boundary=DomainBoundary.DEDICATED_PROCESS,
            security_epoch=index,
            fencing_token=index,
        )
        _commit_plan_authority(
            active=active,
            store=EmptyAuthorityStore(),
            control=control,
            caller=caller,
            target=target,
            contract_id="target.contract",
            caller_publisher_lineage="publisher.caller",
            target_publisher_lineage="publisher.target",
            target_domain=domain,
            scope=scope,
            authority_label="profile-pack-vm",
            pack_approval_revision=digest(f"approval-{profile_id}"),
        )

    assert len(control.bundles) == 2
    for expected_profile, bundle in zip(
        ("profile-a", "profile-b"), control.bundles, strict=True
    ):
        approval, provider, grant = bundle
        assert approval.profile_id == expected_profile
        assert grant.profile_id == expected_profile
        assert grant.activation_id == f"activation.{expected_profile}"
        assert f".{expected_profile}.profile-pack-vm." in approval.approval_id
        assert f".{expected_profile}.profile-pack-vm." in provider.record_id
        assert f".{expected_profile}.profile-pack-vm." in grant.grant_id
        assert provider.execution_domain_id == f"domain.{expected_profile}"


def test_interactive_only_plan_authority_mints_no_static_grant() -> None:
    """An interactive edge commits reachability but no approval-derived Grant."""

    class EmptyAuthorityStore:
        @staticmethod
        def get_host_extension_trust(record_id: str) -> None:
            return None

        @staticmethod
        def get_approval(record_id: str) -> None:
            return None

        @staticmethod
        def get_provider_authority(record_id: str) -> None:
            return None

        @staticmethod
        def get_grant(record_id: str) -> None:
            return None

    class RecordingAuthorityControl:
        def __init__(self) -> None:
            self.provider_bundles: list[tuple[Any, Any]] = []

        def commit_provider_authority_bundle(
            self,
            *,
            host_extension_trust: Any,
            provider_authorities: tuple[Any, ...],
        ) -> None:
            assert host_extension_trust is None
            assert len(provider_authorities) == 1
            self.provider_bundles.append(
                (host_extension_trust, provider_authorities[0])
            )

        def commit_approval_bundle(self, *args: Any, **kwargs: Any) -> None:
            pytest.fail("interactive-only edge must not create an approval bundle")

    def digest(label: str) -> str:
        return canonical_digest({"label": label})

    caller = FunctionPrincipal(
        parent_artifact_digest=digest("caller-artifact"),
        function_implementation_digest=digest("caller-function"),
        function_id="caller.function",
        contract_revision_digest=digest("caller-contract"),
        operation_id="caller.operation",
    )
    target = FunctionPrincipal(
        parent_artifact_digest=digest("target-artifact"),
        function_implementation_digest=digest("target-function"),
        function_id="target.function",
        contract_revision_digest=digest("target-contract"),
        operation_id="target.operation",
    )
    active = ActiveDefaultProfile(
        resolved=ResolvedDefaultProfile(
            profile={"profile_id": "profile-interactive"}, lock={}, plan={}
        ),
        activation={
            "activation_id": "activation.profile-interactive",
            "created_at": "2026-08-29T00:00:00Z",
            "plan_digest": digest("plan"),
            "profile_authority_snapshot_digest": digest("authority"),
            "security_epoch": 1,
            "fencing_token": 1,
        },
    )
    domain = ExecutionDomain(
        domain_id="domain.profile-interactive",
        profile_id="profile-interactive",
        activation_id="activation.profile-interactive",
        boot_epoch=1,
        process_identity="process.profile-interactive",
        authenticated_channel_digest=digest("channel"),
        sandbox_profile_digest=digest("sandbox"),
        resource_namespace="resource.profile-interactive",
        principals=(target,),
        boundary=DomainBoundary.DEDICATED_PROCESS,
        security_epoch=1,
        fencing_token=1,
    )
    control = RecordingAuthorityControl()

    _commit_plan_authority(
        active=active,
        store=EmptyAuthorityStore(),
        control=control,
        caller=caller,
        target=target,
        contract_id="target.contract",
        caller_publisher_lineage="publisher.caller",
        target_publisher_lineage="publisher.target",
        target_domain=domain,
        scope=AuthorityScope(
            capability="target.read",
            semantics_digest=digest("target-semantics"),
        ),
        authority_mode="interactive_only",
    )

    assert len(control.provider_bundles) == 1
    _, provider = control.provider_bundles[0]
    assert provider.provider == target
    assert provider.execution_domain_id == domain.domain_id


@pytest.mark.skipif(
    os.environ.get(NATIVE_PACKVM_ACCEPTANCE_ENV) != "1",
    reason=(
        "native acceptance requires a provisioned signed direct-VZ helper, "
        "verified boot assets, and allocation-scoped authenticated transport"
    ),
)
def test_real_pack_profile_transaction_with_native_authenticated_packvm(
    tmp_path: Path,
) -> None:
    """Mint activation-bound PackVM authority only with native verified facts."""

    _exercise_real_pack_profile_transaction(
        tmp_path,
        expect_authenticated_packvm=True,
    )
