"""Real-process launch coverage for a named active Profile."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"


_CHILD = r"""
import json
import os
import sys
from pathlib import Path

from core_runtime.bootstrap import profile_capture
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.packvm_lifecycle_v4 import PackVMLifecycleV4
from core_runtime.panel_auth import PanelAuthManager
from defaultspack import desktop_app
from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
    default_packvm_provisioner,
)
from tobkiri_host.backends import REQUIRED_PRODUCTION_GATES, BackendStatus
from tobkiri_host.credential_store import host_credential_store_factory
from tobkiri_host.models import ExecutionKind


BUNDLE_ROOT = Path(sys.argv[1])
USER_DATA = Path(os.environ["TOBKIRI_USER_DATA"])
profile_capture._bundle_root = lambda _base_dir=None: BUNDLE_ROOT

session = None
server = None
credential_factory_calls = []


class ReadyPackVMBackend:
    status = BackendStatus(
        backend_id="tobkiri.python-pack-v4",
        execution_kind=ExecutionKind.PACK_VM,
        platform="any",
        backend_digest="sha256:" + "1" * 64,
        production_enabled=True,
        conformance_only=False,
        satisfied_gates=REQUIRED_PRODUCTION_GATES,
    )

    def __init__(self):
        self.artifact_resolver = None
        self.target_domain_resolver = None
        self.capability_bridge = None

    def bind_artifact_resolver(self, resolver):
        self.artifact_resolver = resolver

    def bind_target_domain_resolver(self, resolver):
        self.target_domain_resolver = resolver

    def bind_capability_bridge(self, callback):
        self.capability_bridge = callback

    def materialize(self, _binding, _reservation_id):
        raise AssertionError("named Profile launch must not execute a Pack")

    def invoke(self, _request):
        raise AssertionError("named Profile launch must not invoke a Pack")

    def cancel(self, _request_id):
        raise AssertionError("named Profile launch must not cancel a Pack")

    def terminate(self, _domain_id):
        raise AssertionError("named Profile launch must not terminate a Pack")


def credential_store_factory(*, user_data_root):
    credential_factory_calls.append(str(user_data_root))
    return host_credential_store_factory(user_data_root=user_data_root)


try:
    lifecycle = PackVMLifecycleV4(default_packvm_provisioner())
    session, bindings = desktop_app._restore_active_profile_contracts(
        lifecycle,
        credential_store_factory=credential_store_factory,
        packvm_backend_factory=lambda: ReadyPackVMBackend(),
    )
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(
            bootstrap_secret="named-profile-launch-test-secret"
        ),
        dispatch_session=session,
        contract_bindings=bindings,
        packvm_lifecycle=lifecycle,
    )
    server.start()
    default_activation = (
        USER_DATA / "workspaces" / "defaults" / "activation" / "active.json"
    )
    print(
        json.dumps(
            {
                "defaults_activation": default_activation.is_file(),
                "credential_factory_calls": len(credential_factory_calls),
                "port": server.port,
                "profile_id": session.profile_id,
                "plan_digest": session.plan_digest,
                "route_count": len(bindings),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    if sys.stdin.readline().strip() != "stop":
        raise RuntimeError("stop command is required")
finally:
    if server is not None:
        server.stop()
    if session is not None:
        session.close()
"""


def _read_child_state(process: subprocess.Popen[str]) -> dict[str, Any]:
    """Read the first readiness record emitted by the child process."""

    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        stderr = process.stderr.read() if process.stderr is not None else ""
        raise AssertionError(f"named Profile child exited before readiness: {stderr}")
    payload = json.loads(line)
    assert isinstance(payload, dict)
    return payload


def _stop_child(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Stop the child and return its remaining output."""

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


def test_named_profile_launch_has_no_defaults_activation_prerequisite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launch Application/Host/Shell from A without a Defaults activation."""

    from core_runtime.active_profile_store_v4 import ActiveProfileStore
    from core_runtime.bootstrap.profile_capture import (
        capture_profile,
        host_profile_catalog,
        prepare_profile_confirmation,
    )
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
    from tests.conformance_support.packaged_profile import (
        packaged_profile_bundle_root,
    )
    from tests.conformance_support.host_contract import host_contract

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    host_profile_catalog()
    definitions = ProfileDefinitionStore(user_data)
    defaults = definitions.get_profile("defaults")
    assert defaults is not None
    definitions.duplicate_profile(
        "defaults",
        new_profile_id="profile-a",
        display_name="Profile A",
        expected_profile_revision=defaults.profile_revision,
    )
    active = capture_profile(
        "profile-a",
        confirmation=prepare_profile_confirmation("profile-a"),
    )
    assert active.resolved.profile["profile_id"] == "profile-a"
    default_activation = (
        user_data / "workspaces" / "defaults" / "activation" / "active.json"
    )
    assert not default_activation.exists()
    assert ActiveProfileStore(user_data).require(verify_snapshot=True).profile_id == (
        "profile-a"
    )
    contract_path = user_data / "host_contract.json"
    contract_path.write_text(
        json.dumps(
            host_contract(
                profile_id=str(active.resolved.profile["profile_id"]),
                profile_revision=str(active.resolved.plan["profile_revision"]),
                activation_id=str(active.activation["activation_id"]),
                plan_digest=str(active.resolved.plan["plan_digest"]),
                values={
                    "panel_bootstrap_secret": "named-profile-launch-test-secret"
                },
            )
        ),
        encoding="utf-8",
    )
    contract_path.chmod(0o600)

    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(
                (
                    str(ROOT),
                    str(DEFAULTSPACK_ROOT),
                    environment.get("PYTHONPATH", ""),
                )
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TOBKIRI_TEST_RUNTIME_ROOT": str(ROOT),
            "TOBKIRI_HOST_CONTRACT_PATH": str(contract_path),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _CHILD, str(packaged_profile_bundle_root())],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        state = _read_child_state(process)
        assert state["defaults_activation"] is False
        assert state["credential_factory_calls"] == 1
        assert state["profile_id"] == "profile-a"
        assert isinstance(state["plan_digest"], str)
        assert state["plan_digest"].startswith("sha256:")
        assert int(state["route_count"]) > 0

        connection = http.client.HTTPConnection(
            "127.0.0.1", int(state["port"]), timeout=10
        )
        connection.request("GET", "/health")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        assert response.status == 200, payload
        assert payload["success"] is True
    finally:
        if process.poll() is None:
            _stop_child(process)
