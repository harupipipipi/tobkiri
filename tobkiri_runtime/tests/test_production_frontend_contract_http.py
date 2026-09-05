"""Real-server proof for the production frontend-to-Broker contract path."""

from __future__ import annotations

import http.client
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator, Mapping
from urllib.parse import quote

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding as FrontendContractBinding,
    HTTPContractTarget as FrontendContractTarget,
)
from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
    load_frontend_contract_bindings,
)
from ecosystem.defaultspack.defaultspack.http_contract_composition import (
    defaultspack_capability_binding,
    defaultspack_capability_snapshot,
    defaultspack_capability_snapshot_mapping,
)
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
    defaultspack_runtime_capture_inputs,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from ecosystem.defaultspack.domain.runtime_v4 import ActivationStore, BundledCatalog
from ecosystem.rumi_shell_policy_pack.runtime import policy as shell_policy
from tests.conformance_support.command_protocol_activation import (
    COMMAND_PROTOCOL_HTTP_CASES,
    file_snapshot,
)
from tobkiri_host.backends import (
    REQUIRED_PRODUCTION_GATES,
    BackendRegistry,
    BackendStatus,
)
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.models import ExecutionKind, OpaqueAuthorityRef, RuntimeEvidence
from tobkiri_protocol.canonical import canonical_digest
from tests.conformance_support.host_contract import host_contract


pytestmark = pytest.mark.contract


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_MUTATION_TIMEOUT_SECONDS = 10
EVENTUAL_RECONCILIATION_TIMEOUT_SECONDS = 30
BUNDLE_ROOT = RUNTIME_ROOT / "ecosystem" / "defaultspack" / "v4"
MAP_PATH = (
    RUNTIME_ROOT / "ecosystem" / "defaultspack" / "defaultspack" / "frontend_contract_map.v4.json"
)


class _ShellPolicyPackVmBackend:
    """Test supervisor for the one production-admitted shell policy PackVM ABI.

    The test owns the supervisor transport only.  It still asks production
    capture to bind the sealed artifact and target-domain resolvers, then
    calls the staged shell-policy entrypoint with the exact catalog operation.
    That makes a nested terminal prepare prove the actual PackVM policy edge
    without adding a product fallback for non-macOS test runs.
    """

    _PACK_ID = "rumi_shell_policy_pack"
    _FUNCTION_ID = "rumi_shell_policy_pack.shell-policy.inspect"
    _CONTRACT_ID = "tobkiri.service.shell.inspect.v1"
    _OPERATION_ID = "rumi_shell_policy_pack.shell-inspect"

    def __init__(self) -> None:
        self.status = BackendStatus(
            backend_id="tobkiri.python-pack-v4",
            execution_kind=ExecutionKind.PACK_VM,
            platform="any",
            backend_digest=canonical_digest({"backend": "test-shell-policy-packvm", "abi": 1}),
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )
        self._artifact_resolver = None
        self._target_domain_resolver = None
        self._target_domain_id: str | None = None

    def supports(self, binding: object) -> bool:
        """Admit only the exact shell-policy Function pinned by the Plan."""

        artifact = getattr(binding, "artifact", None)
        function = getattr(binding, "function", None)
        operation = getattr(binding, "operation", None)
        return bool(
            getattr(artifact, "pack_id", None) == self._PACK_ID
            and getattr(function, "function_id", None) == self._FUNCTION_ID
            and getattr(operation, "contract_id", None) == self._CONTRACT_ID
            and getattr(operation, "operation_id", None) == self._OPERATION_ID
        )

    def bind_artifact_resolver(self, resolver: object) -> None:
        """Accept the production-captured resolver exactly once."""

        assert self._artifact_resolver is None
        assert callable(resolver)
        self._artifact_resolver = resolver

    def bind_target_domain_resolver(self, resolver: object) -> None:
        """Accept the production authority domain resolver exactly once."""

        assert self._target_domain_resolver is None
        assert callable(resolver)
        self._target_domain_resolver = resolver

    def materialize(self, binding: object, reservation_id: str) -> RuntimeEvidence:
        """Materialize the verified policy artifact in its exact Host domain."""

        if not reservation_id or not self.supports(binding):
            raise BackendUnavailableError("test PackVM binding is unavailable")
        if self._artifact_resolver is None or self._target_domain_resolver is None:
            raise BackendUnavailableError("test PackVM capture is unavailable")
        artifact = self._artifact_resolver(binding)
        implementation_digest = getattr(
            getattr(binding, "function", None), "implementation_digest", None
        )
        if (
            getattr(artifact, "artifact_digest", None)
            != getattr(getattr(binding, "artifact", None), "digest", None)
            or getattr(artifact, "implementation_digest", None) != implementation_digest
        ):
            raise BackendUnavailableError("test PackVM artifact changed")
        domain_id = self._target_domain_resolver(binding)
        if not isinstance(domain_id, str) or not domain_id:
            raise BackendUnavailableError("test PackVM domain is unavailable")
        self._target_domain_id = domain_id
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(domain_id),
            executable_digest=str(implementation_digest),
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
        )

    def invoke(self, request: object) -> ProviderOutcome:
        """Execute just the real sealed shell-policy ABI over this test transport."""

        if (
            not isinstance(request, RequestEnvelope)
            or request.target_domain.value != self._target_domain_id
            or request.contract_id != self._CONTRACT_ID
            or request.operation_id != self._OPERATION_ID
        ):
            raise BackendUnavailableError("test PackVM envelope is invalid")
        return ProviderOutcome(
            shell_policy.tobkiri_packvm_invoke(
                request.operation_id,
                dict(request.payload),
            )
        )

    def cancel(self, request_id: str) -> None:
        """Accept the Broker's authenticated cancellation identity."""

        if not request_id:
            raise BackendUnavailableError("test PackVM cancellation is invalid")

    def terminate(self, domain_id: str) -> None:
        """Fence only the domain that production capture issued to this backend."""

        if domain_id != self._target_domain_id:
            raise BackendUnavailableError("test PackVM domain is invalid")


def _contract(method: str, target: str) -> str:
    return "/api/contracts/defaultspack/" + quote(f"{method.upper()} {target}", safe="")


def _request(
    server: PackAPIServer,
    method: str,
    path: str,
    *,
    body: object | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = EVENTUAL_RECONCILIATION_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, object], list[tuple[str, str]]]:
    # Bound real-server integration calls without imposing a product deadline
    # on synchronous integrity validation and runtime recapture.
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.port,
        timeout=timeout_seconds,
    )
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = dict(headers or {})
    if encoded is not None:
        request_headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    response_headers = response.getheaders()
    connection.close()
    return response.status, payload, response_headers


def _authenticate(server: PackAPIServer) -> tuple[str, str, str]:
    origin = f"http://127.0.0.1:{server.port}"
    status, bootstrap, _headers = _request(
        server,
        "POST",
        "/api/panel/auth/bootstrap",
        body={},
        headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
    )
    assert status == 200, bootstrap
    status, exchange, headers = _request(
        server,
        "POST",
        "/api/panel/auth/exchange",
        body={"code": bootstrap["data"]["code"]},
        headers={"Origin": origin},
    )
    assert status == 200
    cookie = next(value for key, value in headers if key.lower() == "set-cookie")
    return cookie.split(";", 1)[0], str(exchange["data"]["csrf_token"]), origin


def _captured_production_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    packvm_backends: BackendRegistry | None = None,
) -> Iterator[tuple[PackAPIServer, object, AuthorityStore]]:
    """Start one production capture, optionally with a test PackVM supervisor."""

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    contract_path = user_data / "host_contract.json"
    contract_path.write_text(
        json.dumps(
            host_contract(
                profile_id=str(active.resolved.profile["profile_id"]),
                profile_revision=str(active.resolved.plan["profile_revision"]),
                activation_id=str(active.activation["activation_id"]),
                plan_digest=str(active.resolved.plan["plan_digest"]),
                values={"panel_bootstrap_secret": "desktop-bootstrap"},
            )
        ),
        encoding="utf-8",
    )
    contract_path.chmod(0o600)
    monkeypatch.setenv("TOBKIRI_HOST_CONTRACT_PATH", str(contract_path))
    authority = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    bundle_root = packaged_profile_bundle_root()

    def runtime_capture_inputs(current: object | None = None):
        """Bind refreshes to this test's explicit packaged Profile bundle."""

        return replace(
            defaultspack_runtime_capture_inputs(current),
            bundle_root=bundle_root,
            ecosystem_root=RUNTIME_ROOT / "ecosystem",
        )

    catalog = BundledCatalog.load(bundle_root)
    bindings = load_frontend_contract_bindings(
        MAP_PATH,
        catalog.packs["runtime.tauri.application.default"],
    )
    session = capture_production_dispatch(
        active,
        bundle_root=bundle_root,
        ecosystem_root=RUNTIME_ROOT / "ecosystem",
        authority_store=authority,
        backends=packvm_backends,
        http_contract_bindings=bindings,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        capability_binding_snapshot_factory=defaultspack_capability_snapshot_mapping,
        capability_binding_selector=defaultspack_capability_binding,
    )
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="desktop-bootstrap"),
        dispatch_session=session,
        contract_bindings=bindings,
        runtime_capture_factory=runtime_capture_inputs,
        capability_snapshot_factory=defaultspack_capability_snapshot,
        application_presentation=DefaultspackHTTPPresentation(),
    )

    def publish_current_host_contract() -> None:
        """Model the Launcher writer at an explicit runtime refresh boundary."""

        current = profile_capture.capture_active_profile()
        contract_path.write_text(
            json.dumps(
                host_contract(
                    profile_id=str(current.resolved.profile["profile_id"]),
                    profile_revision=str(current.resolved.plan["profile_revision"]),
                    activation_id=str(current.activation["activation_id"]),
                    plan_digest=str(current.resolved.plan["plan_digest"]),
                    values={"panel_bootstrap_secret": "desktop-bootstrap"},
                )
            ),
            encoding="utf-8",
        )
        contract_path.chmod(0o600)

    original_refresh = server._refresh_runtime_capture

    def refresh(session: object | None = None) -> None:
        publish_current_host_contract()
        original_refresh(
            session,  # type: ignore[arg-type]
            lifecycle_generation=server._lifecycle_generation,
        )

    monkeypatch.setattr(server, "_refresh_runtime_capture", refresh)
    server.start()
    try:
        yield server, session, authority
    finally:
        server.stop()
        session.broker.close()
        authority.close()


@pytest.fixture
def production_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Capture the unmodified production HTTP server fixture."""

    yield from _captured_production_server(tmp_path, monkeypatch)


@pytest.fixture
def command_vertical_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Capture production HTTP with only the sealed policy PackVM test port."""

    yield from _captured_production_server(
        tmp_path,
        monkeypatch,
        packvm_backends=BackendRegistry((_ShellPolicyPackVmBackend(),)),
    )


def test_command_protocol_paths_are_inert_in_captured_production_http(
    production_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unpublished Command Protocol aliases cannot reach any mutable boundary."""

    server, session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    audit_count = len(authority.audit_events())
    journal = server._operation_journal
    assert journal is not None
    settings_path = (
        Path(os.environ["RUMI_USER_DATA"])
        / "defaultspack"
        / "shared"
        / "frontend_settings.json"
    )
    event_store_path = settings_path.with_name("command_invocation_events.sqlite3")
    offline_queue_path = settings_path.with_name("command_offline_queue.sqlite3")
    state_paths = (
        journal.path,
        event_store_path,
        offline_queue_path,
        settings_path,
    )
    state_before = {path: file_snapshot(path) for path in state_paths}
    assert state_before[journal.path] is None
    assert state_before[event_store_path] is None
    assert state_before[offline_queue_path] is None

    broker_invocations: list[str] = []
    broker_submissions: list[str] = []
    journal_writes: list[str] = []

    def unexpected_broker_invocation(*_args, **_kwargs) -> dict[str, object]:
        broker_invocations.append("invoke")
        return {"state": "error", "code": "TEST_BROKER_BLOCKED"}

    def unexpected_broker_submission(*_args, **_kwargs):
        broker_submissions.append("submit")
        raise AssertionError("Command Protocol reached Broker submission")

    monkeypatch.setattr(session.broker, "invoke", unexpected_broker_invocation)
    monkeypatch.setattr(
        session.broker._executor,
        "submit",
        unexpected_broker_submission,
    )

    for method_name in ("renew_session", "begin_operation", "finish_operation"):
        monkeypatch.setattr(
            journal,
            method_name,
            lambda *_args, _name=method_name, **_kwargs: journal_writes.append(_name),
        )

    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    for method, path, body in COMMAND_PROTOCOL_HTTP_CASES:
        status, payload, _ = _request(
            server,
            method,
            path,
            body=body,
            headers={
                **headers,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status == 404, (path, payload)
        assert payload["success"] is False
        assert payload["error"] == "Not found"

    assert broker_invocations == []
    assert broker_submissions == []
    assert journal_writes == []
    assert {path: file_snapshot(path) for path in state_paths} == state_before
    assert file_snapshot(journal.path) is None
    assert file_snapshot(event_store_path) is None
    assert file_snapshot(offline_queue_path) is None
    assert len(authority.audit_events()) == audit_count
    assert session.broker._executor._work_queue.empty()


def test_all_high_risk_commands_http_require_host_approval_and_run_once(
    command_vertical_server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise every Command ref through HTTP, Host approval, and one resume.

    This is intentionally one mounted repository.  The PackVM test transport
    is limited to the real shell-policy ABI; every command effect still uses
    the production adapter, Host coordinator, signed UI operator, and its
    captured Host Provider.
    """

    from core_runtime.authority.ui_operator import sign_ui_operator
    from ecosystem.rumi_workspace_mount_pack.runtime.mounts import WorkspaceMountStore

    server, session, _authority = command_vertical_server
    workspace = tmp_path / "workspace"
    remote = tmp_path / "vertical-remote.git"
    workspace.mkdir()
    subprocess.run(("git", "init", "-q", str(workspace)), check=True)
    subprocess.run(("git", "init", "--bare", "-q", str(remote)), check=True)
    for key, value in (
        ("user.email", "test@example.com"),
        ("user.name", "Tobkiri Test"),
    ):
        subprocess.run(("git", "-C", str(workspace), "config", key, value), check=True)
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    (workspace / "restore.txt").write_text("restore seed\n", encoding="utf-8")
    (workspace / "patch.txt").write_text("patch before\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(workspace), "add", "."), check=True)
    subprocess.run(("git", "-C", str(workspace), "commit", "-qm", "seed"), check=True)
    branch = subprocess.run(
        ("git", "-C", str(workspace), "symbolic-ref", "--short", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    approved_remote_url = "https://push.example.invalid/tobkiri/vertical.git"
    subprocess.run(
        ("git", "-C", str(workspace), "remote", "add", "origin", approved_remote_url),
        check=True,
    )

    mounts = WorkspaceMountStore(
        "defaults",
        user_data_root=Path(os.environ["TOBKIRI_USER_DATA"]),
    )
    mounted = mounts.mount("vertical", str(workspace), expected_revision=0)
    mounts.select("vertical", expected_revision=mounted["revision"])

    cookie, csrf, origin = _authenticate(server)
    headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}

    def post(path: str, body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
        status, response, _ = _request(
            server,
            "POST",
            _contract("POST", path),
            body=body,
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        return status, response

    def git_output(*args: str, check: bool = True) -> str:
        return subprocess.run(
            ("git", "-C", str(workspace), *args),
            check=check,
            capture_output=True,
            text=True,
        ).stdout

    # The product plan retains a safe HTTPS remote and performs its normal
    # URL validation, source/remote CAS, and lease construction.  Only the
    # final, already revalidated transport is redirected to an isolated bare
    # repository, so the vertical test never contacts the network.
    publish_contribution = next(
        contribution
        for backend in session.broker._backends.registered
        for contribution in getattr(backend, "_contributions", {}).values()
        if contribution.operation_id == "rumi_git_publish_pack.git-push"
    )
    provider_globals = publish_contribution.invoke.__globals__
    original_git = provider_globals["_git"]
    git_executable = provider_globals["_git_executable"]

    def local_final_push(
        repository: Path,
        args: list[str],
        *,
        timeout: int = 30,
        hardened: bool = False,
    ) -> str:
        if not args or args[0] != "push":
            return original_git(repository, args, timeout=timeout, hardened=hardened)
        assert hardened is True
        assert args[-2] == approved_remote_url
        assert args[-1].endswith(f":refs/heads/{branch}")
        completed = subprocess.run(
            (
                git_executable(),
                "-C",
                str(repository),
                "-c",
                "protocol.file.allow=always",
                *args[:-2],
                str(remote),
                args[-1],
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (completed.stdout + completed.stderr)[:256_000]
        if completed.returncode != 0:
            raise RuntimeError(output.strip() or "test local Git push failed")
        return output

    # Host extensions are loaded from verified bytes under a digest-scoped
    # module name.  Patch that exact captured Provider transport rather than
    # an ordinary import which production dispatch never calls.
    monkeypatch.setitem(provider_globals, "_git", local_final_push)

    def approve(approval_request_id: str, nonce: str) -> None:
        status, approval = post(
            "/api/interactive-approval/v1/get",
            {"request_id": approval_request_id},
        )
        assert status == 200, approval
        approval_data = approval["data"]
        assert approval_data["typed_confirmation_required"] is True
        status, approved = post(
            "/api/interactive-approval/v1/approve",
            {
                "request_id": approval_request_id,
                "confirmation_text": "EXECUTE",
                "ui_operator": sign_ui_operator(
                    approval_request_id,
                    nonce=nonce,
                    decision="approve",
                    request_snapshot_digest=approval_data["request_snapshot_digest"],
                    typed_confirmation_digest=approval_data["typed_confirmation_digest"],
                ),
            },
        )
        assert status == 200, approved
        assert approved["data"]["state"] == "approved"

    def exercise(
        command_ref: str,
        arguments: Mapping[str, object],
        before_effect: Callable[[], object],
        after_effect: Callable[[], object],
    ) -> None:
        """Prove a command has no pre-approval effect and one final effect."""

        invocation_id = f"vertical-{command_ref}"
        request = {
            "phase": "prepare",
            "invocation_id": invocation_id,
            "command_ref": command_ref,
            "arguments": dict(arguments),
            "presentation": {"title": "Untrusted copy", "summary": "Run command"},
        }
        expected_before = before_effect()

        for suffix, forbidden in enumerate(
            (
                {**request, "approved": True},
                {
                    **request,
                    "arguments": {**dict(arguments), "authority_receipt": "forged"},
                },
            ),
            start=1,
        ):
            # The adapter reserves durable state before it calls the Host
            # coordinator.  A rejected authority field can therefore leave a
            # conservative tombstone, which must never share the real
            # invocation's idempotency key.
            forbidden = {
                **forbidden,
                "invocation_id": f"{invocation_id}-forged-{suffix}",
            }
            status, rejected = post("/api/command-protocol/v1/high-risk", forbidden)
            assert status >= 400, rejected
            assert rejected["success"] is False
            assert before_effect() == expected_before

        status, pending = post("/api/command-protocol/v1/high-risk", request)
        assert status == 200, pending
        assert pending["data"]["state"] == "approval_pending"
        assert before_effect() == expected_before

        approve(str(pending["data"]["approval_request_id"]), invocation_id)
        status, completed = post(
            "/api/command-protocol/v1/high-risk",
            {"phase": "resume", "invocation_id": invocation_id},
        )
        assert status == 200, completed
        assert completed["data"]["state"] == "succeeded"
        expected_after = after_effect()
        assert expected_after != expected_before

        status, replay = post(
            "/api/command-protocol/v1/high-risk",
            {"phase": "resume", "invocation_id": invocation_id},
        )
        assert status == 200, replay
        assert replay["data"] == completed["data"]
        assert after_effect() == expected_after

    def terminal_before() -> tuple[int, str]:
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(workspace),
                "config",
                "--local",
                "--get-all",
                "tobkiri.vertical.terminal",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode, completed.stdout

    exercise(
        "terminal",
        {
            "command": [
                "git",
                "config",
                "--local",
                "--add",
                "tobkiri.vertical.terminal",
                "ran",
            ],
            "cwd": ".",
        },
        terminal_before,
        lambda: (0, git_output("config", "--local", "--get-all", "tobkiri.vertical.terminal")),
    )

    (workspace / "commit.txt").write_text("commit effect\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(workspace), "add", "commit.txt"), check=True)
    exercise(
        "commit",
        {"workspace_id": "vertical", "message": "vertical command commit"},
        lambda: git_output("rev-parse", "HEAD").strip(),
        lambda: git_output("rev-parse", "HEAD").strip(),
    )

    (workspace / "restore.txt").write_text("restore changed\n", encoding="utf-8")
    exercise(
        "restore",
        {
            "workspace_id": "vertical",
            "paths": ["restore.txt"],
            "source": "HEAD",
        },
        lambda: (workspace / "restore.txt").read_text(encoding="utf-8"),
        lambda: (workspace / "restore.txt").read_text(encoding="utf-8"),
    )

    patch = """diff --git a/patch.txt b/patch.txt
index ba35db0..e311d74 100644
--- a/patch.txt
+++ b/patch.txt
@@ -1 +1 @@
-patch before
+patch after
"""
    exercise(
        "patch",
        {"workspace_id": "vertical", "patch": patch},
        lambda: (workspace / "patch.txt").read_text(encoding="utf-8"),
        lambda: (workspace / "patch.txt").read_text(encoding="utf-8"),
    )

    remote_ref = f"refs/heads/{branch}"
    exercise(
        "push",
        {
            "workspace_id": "vertical",
            "remote": "origin",
            "branch": branch,
            "force_with_lease": False,
        },
        lambda: subprocess.run(
            ("git", "--git-dir", str(remote), "rev-parse", "--verify", remote_ref),
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        lambda: subprocess.run(
            ("git", "--git-dir", str(remote), "rev-parse", "--verify", remote_ref),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
    )


def test_named_profile_registry_crud_http_preserves_active_pointer_and_history(
    production_server,
) -> None:
    """Exercise the authenticated Profile registry through its real HTTP surface."""

    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    read_headers = {
        "Cookie": cookie,
        "X-Tobkiri-Request-ID": str(uuid.uuid4()),
    }
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
        "X-Tobkiri-Request-ID": str(uuid.uuid4()),
    }

    status, initial, _ = _request(
        server,
        "GET",
        "/api/v4/profiles",
        headers=read_headers,
    )
    assert status == 200, initial
    initial_registry = initial["data"]
    assert initial_registry["active_profile_id"] == "defaults"
    assert initial_registry["active_profile_revision"]
    generation = initial_registry["generation"]

    def mutate(action: str, body: dict[str, object]) -> dict[str, object]:
        status_code, payload, _ = _request(
            server,
            "POST",
            f"/api/v4/profiles/{action}",
            body=body,
            headers={
                **mutation_headers,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status_code == 200, payload
        return payload["data"]

    created = mutate(
        "create",
        {
            "profile_id": "profile-a",
            "display_name": "Profile A",
            "source_profile_id": "defaults",
            "expected_store_generation": generation,
        },
    )
    created_profile = next(
        profile
        for profile in created["profiles"]
        if profile["profile_id"] == "profile-a"
    )
    assert created_profile["profile_revision"]
    assert created_profile["parent_revision"] is None
    assert created["active_profile_id"] == initial_registry["active_profile_id"]
    assert created["active_profile_revision"] == initial_registry["active_profile_revision"]

    updated = mutate(
        "update",
        {
            "profile_id": "profile-a",
            "display_name": "Profile A updated",
            "expected_profile_revision": created_profile["profile_revision"],
            "expected_store_generation": created["generation"],
        },
    )
    updated_profile = next(
        profile
        for profile in updated["profiles"]
        if profile["profile_id"] == "profile-a"
    )
    assert updated_profile["profile_revision"] != created_profile["profile_revision"]
    assert updated_profile["parent_revision"] == created_profile["profile_revision"]
    assert updated["active_profile_id"] == "defaults"
    assert updated["active_profile_revision"] == initial_registry["active_profile_revision"]

    duplicated = mutate(
        "duplicate",
        {
            "profile_id": "profile-a",
            "new_profile_id": "profile-b",
            "display_name": "Profile B",
            "expected_profile_revision": updated_profile["profile_revision"],
            "expected_store_generation": updated["generation"],
        },
    )
    duplicated_profile = next(
        profile
        for profile in duplicated["profiles"]
        if profile["profile_id"] == "profile-b"
    )
    assert duplicated_profile["profile_revision"]
    assert duplicated_profile["parent_revision"] is None
    assert duplicated["active_profile_id"] == "defaults"

    deleted = mutate(
        "delete",
        {
            "profile_id": "profile-b",
            "expected_profile_revision": duplicated_profile["profile_revision"],
            "expected_store_generation": duplicated["generation"],
        },
    )
    assert all(profile["profile_id"] != "profile-b" for profile in deleted["profiles"])
    assert deleted["changed_profile"]["profile_id"] == "profile-b"
    assert deleted["changed_profile"]["tombstone"] is True
    assert deleted["action"] == "delete"
    assert deleted["active_profile_id"] == "defaults"
    assert deleted["active_profile_revision"] == initial_registry["active_profile_revision"]

    stale_status, stale, _ = _request(
        server,
        "POST",
        "/api/v4/profiles/update",
        body={
            "profile_id": "profile-a",
            "display_name": "stale",
            "expected_profile_revision": created_profile["profile_revision"],
            "expected_store_generation": deleted["generation"],
        },
        headers={
            **mutation_headers,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert stale_status == 409
    assert stale["success"] is False


def test_home_and_pack_workflow_use_only_real_broker_contracts(
    production_server,
) -> None:
    server, session, authority = production_server
    authority_path = authority.path
    cookie, csrf, origin = _authenticate(server)
    read_headers = {
        "Cookie": cookie,
        "X-Tobkiri-Request-ID": str(uuid.uuid4()),
    }
    before = len(authority.audit_events())
    status, dashboard, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/home/dashboard"),
        headers=read_headers,
    )
    assert status == 200
    assert dashboard["data"]["kernel"]["status"] == "running"
    events = authority.audit_events()
    assert len(events) == before + 4
    assert [event["event_state"] for event in events[-3:]] == [
        "reserved",
        "dispatched",
        "committed",
    ]

    status, catalog, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/pack-control/catalog"),
        headers={**read_headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200
    assert catalog["data"]["profile_id"] == "defaults"

    status, ui_catalog, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/ui/catalog"),
        headers={**read_headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200
    dynamic_host = ui_catalog["data"]["dynamic_host"]
    assert dynamic_host["profile_revision"] != dynamic_host["plan_hash"]
    assert dynamic_host["activation_id"] == session.activation_id
    status_contribution = next(
        item for item in dynamic_host["contributions"] if item["label"] == "pack.status"
    )
    assert status_contribution["resolved_profile_id"] == session.profile_id
    assert status_contribution["resolved_profile_revision"] == session.profile_revision
    assert status_contribution["resolved_activation_id"] == session.activation_id
    assert status_contribution["resolved_plan_hash"] == session.plan_digest

    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }

    audit_before_capability = len(authority.audit_events())
    status, capability_result, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/ui/capability/invoke"),
        body={
            "request_id": str(uuid.uuid4()),
            "expires_at": time.time() + 30,
            "profile_id": dynamic_host["profile_id"],
            "profile_revision": dynamic_host["profile_revision"],
            "activation_id": dynamic_host["activation_id"],
            "plan_hash": dynamic_host["plan_hash"],
            "catalog_hash": dynamic_host["catalog_hash"],
            "contribution_id": status_contribution["contribution_id"],
            "owner_pack_id": status_contribution["owner_pack_id"],
            "contract_id": status_contribution["action_contract"],
            "payload": {"pack_id": "defaultspack"},
        },
        headers={
            **mutation_headers,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert status == 200, capability_result
    assert capability_result["data"]["pack_id"] == "defaultspack"
    assert len(authority.audit_events()) == audit_before_capability + 3

    def post(
        target: str,
        body: dict[str, object],
        *,
        request_id: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        # This workflow proves durable state and route correctness. Allow the
        # real server to finish its complete integrity validation; dedicated
        # tests cover unknown mutation outcomes and eventual reconciliation.
        status_code, payload, _ = _request(
            server,
            "POST",
            _contract("POST", target),
            body=body,
            headers={
                **mutation_headers,
                "X-Tobkiri-Request-ID": request_id or str(uuid.uuid4()),
            },
            timeout_seconds=EVENTUAL_RECONCILIATION_TIMEOUT_SECONDS,
        )
        return status_code, payload

    # Use the production optional-Pack lifecycle fixture.  Picking the first
    # optional catalog row is not sufficient: declarative content Packs are
    # intentionally installable but have no runtime Function to enable.
    target_pack = "tobkiri_workflow_pack"
    target_row = next(
        item for item in catalog["data"]["packs"] if item["pack_id"] == target_pack
    )
    assert target_row["required"] is False
    assert post("/api/pack-control/install", {"pack_id": target_pack})[0] == 200
    never_approved_request = str(uuid.uuid4())
    denied_status, denied = post(
        "/api/pack-control/approval-revoke",
        {"pack_id": target_pack},
        request_id=never_approved_request,
    )
    replay_status, replayed_denial = post(
        "/api/pack-control/approval-revoke",
        {"pack_id": target_pack},
        request_id=never_approved_request,
    )
    assert denied_status == replay_status == 403
    assert denied == replayed_denial
    assert denied["data"]["code"] == "UNAPPROVED"
    assert denied["data"]["retryable"] is False
    candidate_status, candidate = post(
        "/api/pack-control/approval-candidate", {"pack_id": target_pack}
    )
    assert candidate_status == 200
    assert (
        post(
            "/api/pack-control/approval-approve",
            {
                "pack_id": target_pack,
                "candidate_id": candidate["data"]["candidate_id"],
            },
        )[0]
        == 200
    )
    enable_status, enabled = post("/api/pack-control/enable", {"pack_id": target_pack})
    assert enable_status == 200, enabled
    assert enabled["data"]["enabled"] is True
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    assert post("/api/pack-control/restart", {})[0] == 200
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    status, refreshed_catalog, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/ui/catalog"),
        headers={
            "Cookie": cookie,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert status == 200, refreshed_catalog
    refreshed_host = refreshed_catalog["data"]["dynamic_host"]
    refreshed_status = next(
        item for item in refreshed_host["contributions"] if item["label"] == "pack.status"
    )
    status, persisted = post(
        "/api/ui/capability/invoke",
        {
            "request_id": str(uuid.uuid4()),
            "expires_at": time.time() + 30,
            "profile_id": refreshed_host["profile_id"],
            "profile_revision": refreshed_host["profile_revision"],
            "activation_id": refreshed_host["activation_id"],
            "plan_hash": refreshed_host["plan_hash"],
            "catalog_hash": refreshed_host["catalog_hash"],
            "contribution_id": refreshed_status["contribution_id"],
            "owner_pack_id": refreshed_status["owner_pack_id"],
            "contract_id": refreshed_status["action_contract"],
            "payload": {"pack_id": target_pack},
        },
    )
    assert status == 200, persisted
    assert persisted["data"]["enabled"] is True
    assert post("/api/pack-control/disable", {"pack_id": target_pack})[0] == 200
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    revoke_status, revoked = post("/api/pack-control/approval-revoke", {"pack_id": target_pack})
    assert revoke_status == 200, revoked
    assert revoked["data"]["approved"] is False
    assert revoked["data"]["approval_status"] == "revoked"
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    assert post("/api/pack-control/restart", {})[0] == 200
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    catalog_status, after_revoke, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/pack-control/catalog"),
        headers={
            "Cookie": cookie,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert catalog_status == 200, after_revoke
    revoked_pack = next(
        item for item in after_revoke["data"]["packs"] if item["pack_id"] == target_pack
    )
    assert revoked_pack["approved"] is False
    assert revoked_pack["enabled"] is False
    assert revoked_pack["approval_reason"] == "approval_revoked"
    enable_status, denied = post("/api/pack-control/enable", {"pack_id": target_pack})
    assert enable_status == 409
    assert denied["data"]["code"] == "STALE_REVISION"

    with AuthorityStore(authority_path) as current_authority:
        assert any(
            event["event_type"] == "pack_approval_revoked" and event["event_state"] == "committed"
            for event in current_authority.audit_events()
        )

    with AuthorityStore(authority_path) as current_authority:
        audit_before_legacy = len(current_authority.audit_events())
    status, retired, _ = _request(
        server,
        "GET",
        "/api/panel/dashboard",
        headers={"Cookie": cookie},
    )
    assert status == 410
    assert retired["data"]["state"] == "legacy_api_retired"
    with AuthorityStore(authority_path) as current_authority:
        assert len(current_authority.audit_events()) == audit_before_legacy


def test_revoke_denials_respond_before_logging_and_release_for_retry(
    production_server,
) -> None:
    """Known denials remain bounded under logging delay and concurrent retry."""

    server, session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }

    def revoke(request_id: str) -> tuple[int, dict[str, object]]:
        status, payload, _headers = _request(
            server,
            "POST",
            _contract("POST", "/api/pack-control/approval-revoke"),
            body={"pack_id": "rumi_git_read_pack"},
            headers={
                **mutation_headers,
                "X-Tobkiri-Request-ID": request_id,
            },
        )
        return status, payload

    install_status, install_payload, _headers = _request(
        server,
        "POST",
        _contract("POST", "/api/pack-control/install"),
        body={"pack_id": "rumi_git_read_pack"},
        headers={
            **mutation_headers,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert install_status == 200, install_payload

    log_entered = threading.Event()
    initial_denials_logged = threading.Event()
    initial_access_logged = threading.Event()
    all_access_logged = threading.Event()
    release_log = threading.Event()
    denial_log_count = 0
    initial_access_log_count = 0
    access_log_count = 0
    log_count_lock = threading.Lock()
    delay_access_logs = threading.Event()

    class DelayedReplayAccessLog(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            nonlocal access_log_count, denial_log_count, initial_access_log_count
            message = record.getMessage()
            if message.startswith("Contract dispatch denied"):
                assert message.endswith(
                    "tobkiri.host.pack-control.v4/approval.revoke: UNAPPROVED"
                )
                with log_count_lock:
                    denial_log_count += 1
                    if denial_log_count == len(request_ids):
                        initial_denials_logged.set()
            elif message.startswith("API:"):
                assert '"POST /api/contracts/defaultspack/' in message
                status_and_length = message.rsplit(" ", 2)
                assert status_and_length[-2] == "403"
                assert int(status_and_length[-1]) > 0
                if not delay_access_logs.is_set():
                    with log_count_lock:
                        initial_access_log_count += 1
                        if initial_access_log_count == len(request_ids):
                            initial_access_logged.set()
                    return
                with log_count_lock:
                    access_log_count += 1
                    if access_log_count == len(request_ids):
                        all_access_logged.set()
                log_entered.set()
                release_log.wait()

    delayed_log = DelayedReplayAccessLog()
    api_logger = logging.getLogger("core_runtime.pack_api_server")
    original_log_level = api_logger.level
    api_logger.setLevel(logging.INFO)
    api_logger.addHandler(delayed_log)
    request_ids = [str(uuid.uuid4()) for _index in range(8)]
    initial = [revoke(request_id) for request_id in request_ids]
    assert all(status == 403 for status, _payload in initial)
    assert all(
        payload["data"]["code"] == "UNAPPROVED"
        for _status, payload in initial
    )
    assert all(
        payload["data"]["retryable"] is False
        for _status, payload in initial
    )
    assert initial_denials_logged.wait(timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS)
    assert denial_log_count == len(request_ids)
    # ``_request`` returns after it receives the complete response body; the
    # server deliberately writes its access entry later, after closing that
    # response.  Wait for that post-response boundary instead of assuming the
    # final handler has already reached ``finish`` on a loaded CI worker.
    assert initial_access_logged.wait(timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS)
    assert initial_access_log_count == len(request_ids)
    audit_after_initial = len(authority.audit_events())
    delay_access_logs.set()

    executor = ThreadPoolExecutor(max_workers=len(request_ids))
    try:
        responses = [executor.submit(revoke, request_id) for request_id in request_ids]
        assert log_entered.wait(timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS)
        completed, pending = wait(
            responses,
            timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS,
        )
        assert not pending
        assert len(completed) == len(request_ids)
        replayed = [response.result() for response in responses]
        assert replayed == initial
        # Every replay client received its complete denial body while the
        # first access log still held this Handler's serialization lock.
        assert not release_log.is_set()
        assert not all_access_logged.is_set()
        assert server.server is not None
        assert server.server._active_requests > 0
        # Exact terminal replay bypasses fresh mutation admission and adds no
        # audit side effects while handlers remain blocked after close.
        assert len(authority.audit_events()) == audit_after_initial
    finally:
        release_log.set()
        executor.shutdown(wait=True, cancel_futures=True)
        if server.server is not None:
            assert server.server.wait_for_request_drain(
                FRONTEND_MUTATION_TIMEOUT_SECONDS
            )
        api_logger.removeHandler(delayed_log)
        api_logger.setLevel(original_log_level)
        delayed_log.close()

    assert denial_log_count == len(request_ids)
    assert all_access_logged.wait(timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS)
    assert access_log_count == len(request_ids)
    assert server.server is not None
    assert server.server.wait_for_request_drain(FRONTEND_MUTATION_TIMEOUT_SECONDS)
    assert server.server._active_requests == 0
    retry_status, retry_payload = revoke(str(uuid.uuid4()))
    assert retry_status == 403
    assert retry_payload["data"]["code"] == "UNAPPROVED"
    assert len(authority.audit_events()) > audit_after_initial
    assert session.broker._executor._work_queue.empty()
    assert not session.broker._closed


def test_runtime_surface_reads_use_the_canonical_broker_contract(
    production_server,
) -> None:
    server, _session, authority = production_server
    cookie, _csrf, _origin = _authenticate(server)
    headers = {
        "Cookie": cookie,
        "X-Tobkiri-Request-ID": str(uuid.uuid4()),
    }

    targets = {
        "profile": "/api/runtime-surface/profile",
        "settings": "/api/runtime-surface/settings",
        "packs": "/api/runtime-surface/topology/packs",
        "contracts": "/api/runtime-surface/topology/contracts",
        "operations": "/api/runtime-surface/topology/operations",
        "principals": "/api/runtime-surface/topology/principals",
    }
    responses = {}
    for surface, target in targets.items():
        status, payload, _ = _request(
            server,
            "GET",
            _contract("GET", target),
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert status == 200, payload
        envelope = payload["data"]
        assert envelope["runtime_surface_api_version"] == ("io.tobkiri.launcher.runtime-surface.v4")
        assert envelope["surface"] == surface
        assert envelope["state"] == "ready"
        assert envelope["catalog_revision"].startswith("sha256:")
        assert all(
            set(record) == {"digest", "source_ref"} for record in envelope["records"].values()
        )
        responses[surface] = envelope

    assert responses["profile"]["data"]["profile"]["profile_id"] == "defaults"
    verified = [
        item
        for item in responses["operations"]["data"]["operations"]
        if item["schema"].get("input_schema")
    ]
    assert verified
    assert all(item["route"]["function_id"] for item in verified)
    assert any(event["event_state"] == "committed" for event in authority.audit_events())


def test_authoritative_profile_catalog_selection_completes_real_http_ceremony(
    production_server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.conformance_support.packaged_profile import (
        packaged_profile_bundle_root,
    )

    server, _session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    status, registry_response, _ = _request(
        server,
        "GET",
        "/api/v4/profiles",
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, registry_response
    registry = registry_response["data"]
    defaults = next(item for item in registry["profiles"] if item["profile_id"] == "defaults")
    for profile_id, display_name in (("alpha", "Alpha"), ("beta", "Beta")):
        status, registry_response, _ = _request(
            server,
            "POST",
            "/api/v4/profiles/duplicate",
            body={
                "profile_id": "defaults",
                "new_profile_id": profile_id,
                "display_name": display_name,
                "expected_profile_revision": defaults["profile_revision"],
                "expected_store_generation": registry["generation"],
            },
            headers={
                "Cookie": cookie,
                "Origin": origin,
                "X-Rumi-CSRF": csrf,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status == 200, registry_response
        registry = registry_response["data"]
    status, catalog_response, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profiles"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, catalog_response
    catalog = catalog_response["data"]["data"]
    assert {item["profile_id"] for item in catalog["profiles"]} >= {
        "defaults",
        "alpha",
        "beta",
    }
    assert catalog["selection"] == {
        "state": "active_execution",
        "selected_profile_id": "defaults",
        "execution_profile_id": "defaults",
    }
    selected = next(
        item for item in catalog["profiles"] if item["profile_id"] == "alpha"
    )
    assert selected["active"] is False
    assert selected["lifecycle_state"] == "available"
    status, profile_response, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, profile_response
    active = profile_response["data"]
    desired = [
        item["pack_id"]
        for item in selected["pack_closure"]
        if item["role"] not in {"base", "shell", "application", "dependency"}
    ]
    headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}

    def post(path: str, body: Mapping[str, object]):
        return _request(
            server,
            "POST",
            _contract("POST", path),
            body=body,
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )

    status, resolved, _ = post(
        "/api/runtime-surface/profile-change/resolve",
        {
            "profile_id": selected["profile_id"],
            "expected_profile_revision": active["profile_revision"],
            "expected_plan_digest": active["plan_digest"],
            "desired_pack_ids": desired,
            "profile_definition_digest": selected["definition"]["digest"],
            "profile_catalog_digest": catalog["catalog_digest"],
            "bundle_lock_digest": catalog["bundle_lock_digest"],
        },
    )
    assert status == 200, resolved
    assert resolved["data"]["state"] == "resolved", resolved
    status, reviewed, _ = post(
        "/api/runtime-surface/profile-change/review",
        {
            "candidate_id": resolved["data"]["candidate_id"],
            "candidate_digest": resolved["data"]["candidate_digest"],
        },
    )
    assert status == 200, reviewed
    status, approved, _ = post(
        "/api/runtime-surface/profile-change/approve",
        {
            "candidate_id": reviewed["data"]["candidate_id"],
            "candidate_digest": reviewed["data"]["candidate_digest"],
        },
    )
    assert status == 200, approved
    approval = approved["data"]["authority_approval"]
    assert approval["decision"] == "approved"
    assert authority.get_approval(approval["approval_id"]) is not None
    handler = server.handler_class
    assert handler is not None
    refresh = handler._runtime_refresh
    assert refresh is not None
    monkeypatch.setattr(handler, "_runtime_refresh", staticmethod(lambda _session: None))
    status, activated, _ = post(
        "/api/runtime-surface/profile-change/activate",
        {
            "approval_id": approval["approval_id"],
            "approval_digest": approved["data"]["approval_digest"],
        },
    )
    assert status == 200, activated
    assert activated["data"]["state"] == "active"
    contract_path = Path(os.environ["TOBKIRI_HOST_CONTRACT_PATH"])
    contract_path.write_text(
        json.dumps(
            host_contract(
                profile_id=str(activated["data"]["profile_id"]),
                profile_revision=str(
                    resolved["data"]["review"]["resolved_plan"]["profile_revision"]
                ),
                activation_id=str(activated["data"]["activation_id"]),
                plan_digest=str(activated["data"]["plan_digest"]),
                values={"panel_bootstrap_secret": "desktop-bootstrap"},
            )
        ),
        encoding="utf-8",
    )
    contract_path.chmod(0o600)
    refresh(None)
    cookie, _csrf, _origin = _authenticate(server)

    status, refreshed_profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, refreshed_profile
    refreshed = refreshed_profile["data"]
    refreshed_identity = (
        refreshed["profile_id"],
        refreshed["profile_revision"],
        refreshed["data"]["activation_record"]["activation_id"],
        refreshed["plan_digest"],
    )
    assert refreshed_identity == (
        activated["data"]["profile_id"],
        refreshed["profile_revision"],
        activated["data"]["activation_id"],
        activated["data"]["plan_digest"],
    )

    # A process restart must bind the registry to the Authority path, even if
    # an ambient environment override points at a different Host root.
    restart_active = profile_capture.capture_active_profile()
    authority_path = authority.path.resolve()
    authority_user_data = authority_path.parent.parent
    wrong_user_data = tmp_path / "wrong-user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(wrong_user_data))
    restart_authority = AuthorityStore(authority_path)
    restart_bindings = tuple(server._contract_routes.values())
    restarted_session = capture_production_dispatch(
        restart_active,
        bundle_root=packaged_profile_bundle_root(),
        ecosystem_root=RUNTIME_ROOT / "ecosystem",
        authority_store=restart_authority,
        http_contract_bindings=restart_bindings,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        capability_binding_snapshot_factory=defaultspack_capability_snapshot_mapping,
        capability_binding_selector=defaultspack_capability_binding,
    )
    assert (
        restarted_session.profile_id,
        restart_active.resolved.plan["profile_revision"],
        restarted_session.plan_digest,
    ) == (
        refreshed_identity[0],
        refreshed_identity[1],
        refreshed_identity[3],
    )
    restarted_session.close()

    # Re-open the HTTP boundary with the freshly captured session and verify
    # that the same identity is exposed after restart.
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(authority_user_data))
    server.stop()
    restarted_authority = AuthorityStore(authority_path)
    restarted_session = capture_production_dispatch(
        restart_active,
        bundle_root=packaged_profile_bundle_root(),
        ecosystem_root=RUNTIME_ROOT / "ecosystem",
        authority_store=restarted_authority,
        http_contract_bindings=restart_bindings,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        capability_binding_snapshot_factory=defaultspack_capability_snapshot_mapping,
        capability_binding_selector=defaultspack_capability_binding,
    )
    restarted_server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="desktop-bootstrap"),
        dispatch_session=restarted_session,
        contract_bindings=restart_bindings,
        runtime_capture_factory=defaultspack_runtime_capture_inputs,
        capability_snapshot_factory=defaultspack_capability_snapshot,
        application_presentation=DefaultspackHTTPPresentation(),
    )
    try:
        restarted_server.start()
        restart_cookie, _restart_csrf, _restart_origin = _authenticate(restarted_server)
        status, restarted_profile, _ = _request(
            restarted_server,
            "GET",
            _contract("GET", "/api/runtime-surface/profile"),
            headers={
                "Cookie": restart_cookie,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status == 200, restarted_profile
        restarted = restarted_profile["data"]
        assert (
            restarted["profile_id"],
            restarted["profile_revision"],
            restarted["data"]["activation_record"]["activation_id"],
            restarted["plan_digest"],
        ) == refreshed_identity
    finally:
        restarted_server.stop()
        restarted_session.close()


def test_runtime_surface_operation_identity_invokes_exact_capability_binding(
    production_server,
) -> None:
    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    status, payload, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/topology/operations"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, payload
    envelope = payload["data"]
    status_operations = [
        item for item in envelope["data"]["operations"] if item["operation_id"] == "pack.status"
    ]
    assert any(item["invokable"] is True for item in status_operations), json.dumps(
        status_operations, indent=2
    )
    operation = next(item for item in status_operations if item["invokable"] is True)
    base = {
        "request_id": str(uuid.uuid4()),
        "expires_at": time.time() + 30,
        "profile_id": envelope["profile_id"],
        "profile_revision": envelope["profile_revision"],
        "activation_id": operation["activation_id"],
        "plan_hash": envelope["plan_digest"],
        "catalog_hash": operation["invocation_catalog_hash"],
        "contribution_id": operation["invocation_contribution_id"],
        "owner_pack_id": operation["invocation_owner_pack_id"],
        "contract_id": operation["contract_id"],
        "payload": {"pack_id": "defaultspack"},
    }
    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }

    def invoke(body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
        code, response, _ = _request(
            server,
            "POST",
            _contract("POST", "/api/ui/capability/invoke"),
            body=body,
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        return code, response

    code, response = invoke(base)
    assert code == 200, response
    assert response["data"]["pack_id"] == "defaultspack"

    denied_requests = (
        {**base, "request_id": str(uuid.uuid4()), "catalog_hash": "sha256:" + "0" * 64},
        {**base, "request_id": str(uuid.uuid4()), "contribution_id": "pack.forged.operation"},
        {**base, "request_id": str(uuid.uuid4()), "expires_at": time.time() - 1},
        {**base, "request_id": str(uuid.uuid4()), "owner_pack_id": "forged-pack"},
    )
    for denied in denied_requests:
        denied_code, denied_response = invoke(denied)
        assert denied_code == 404
        assert denied_response["success"] is False


def test_profile_ceremony_uses_four_canonical_broker_operations(
    production_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }

    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, profile
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]

    def post(
        target: str,
        body: Mapping[str, object],
        *,
        request_id: str | None = None,
    ):
        return _request(
            server,
            "POST",
            _contract("POST", target),
            body=body,
            headers={
                **headers,
                "X-Tobkiri-Request-ID": request_id or str(uuid.uuid4()),
            },
        )

    status, resolved, _ = post(
        "/api/runtime-surface/profile-change/resolve",
        {
            "profile_id": "defaults",
            "expected_profile_revision": envelope["profile_revision"],
            "expected_plan_digest": envelope["plan_digest"],
            "desired_pack_ids": desired,
        },
    )
    assert status == 200, resolved
    status, reviewed, _ = post(
        "/api/runtime-surface/profile-change/review",
        {
            "candidate_id": resolved["data"]["candidate_id"],
            "candidate_digest": resolved["data"]["candidate_digest"],
        },
    )
    assert status == 200, reviewed
    status, approved, _ = post(
        "/api/runtime-surface/profile-change/approve",
        {
            "candidate_id": reviewed["data"]["candidate_id"],
            "candidate_digest": reviewed["data"]["candidate_digest"],
        },
    )
    assert status == 200, approved
    receipt = approved["data"]["authority_approval"]
    assert approved["data"]["approval_id"] == receipt["approval_id"]
    assert authority.get_approval(receipt["approval_id"]) is not None
    approval_audit = next(
        event
        for event in reversed(authority.audit_events())
        if event["event_type"] == "authority_records_committed"
    )
    assert approval_audit["payload"]["records"] == [
        {
            "record_type": "approval",
            "record_id": approved["data"]["approval_id"],
            "record_digest": approved["data"]["approval_digest"],
        }
    ]
    read_worker_capture_loads: list[int] = []
    original_load = ActivationStore.load_active_snapshot

    def counted_load(store):
        if threading.current_thread().name.startswith("tobkiri-runtime-read"):
            read_worker_capture_loads.append(threading.get_ident())
        return original_load(store)

    monkeypatch.setattr(
        ActivationStore,
        "load_active_snapshot",
        counted_load,
    )
    assert server.handler_class is not None
    monkeypatch.setattr(
        server.handler_class,
        "_runtime_refresh",
        staticmethod(lambda _session: None),
    )
    activation_request_id = str(uuid.uuid4())
    activation_body = {
        "approval_id": approved["data"]["approval_id"],
        "approval_digest": approved["data"]["approval_digest"],
    }
    status, activated, _ = post(
        "/api/runtime-surface/profile-change/activate",
        activation_body,
        request_id=activation_request_id,
    )
    assert status == 200, activated
    assert activated["data"]["state"] == "active"
    assert activated["data"]["authoritative_snapshot"]["state"] == "ready"
    # The session's direct active loader still performs its independent
    # authority check.  No additional capture_default_profile store read is
    # allowed in the worker after the mutation recapture populated the scope.
    assert read_worker_capture_loads == []
    journal = server._operation_journal
    assert journal is not None
    replay_mutating_calls: list[str] = []

    def unexpected_replay_renew(*_args, **_kwargs) -> None:
        replay_mutating_calls.append("renew_session")

    def unexpected_replay_begin(*_args, **_kwargs):
        replay_mutating_calls.append("begin_operation")
        return {}, False

    monkeypatch.setattr(journal, "renew_session", unexpected_replay_renew)
    monkeypatch.setattr(journal, "begin_operation", unexpected_replay_begin)
    status, replayed, _ = post(
        "/api/runtime-surface/profile-change/activate",
        activation_body,
        request_id=activation_request_id,
    )
    assert status == 401, replayed
    assert replayed["error"] == "Unauthorized"
    assert replay_mutating_calls == []


def test_mutation_status_reconciles_lost_response_and_exact_approval_retry(
    production_server,
) -> None:
    server, _session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, profile
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]

    def post(target: str, body: Mapping[str, object], request_id: str):
        return _request(
            server,
            "POST",
            _contract("POST", target),
            body=body,
            headers={**headers, "X-Tobkiri-Request-ID": request_id},
        )

    status, resolved, _ = post(
        "/api/runtime-surface/profile-change/resolve",
        {
            "profile_id": "defaults",
            "expected_profile_revision": envelope["profile_revision"],
            "expected_plan_digest": envelope["plan_digest"],
            "desired_pack_ids": desired,
        },
        str(uuid.uuid4()),
    )
    assert status == 200, resolved
    status, reviewed, _ = post(
        "/api/runtime-surface/profile-change/review",
        {
            "candidate_id": resolved["data"]["candidate_id"],
            "candidate_digest": resolved["data"]["candidate_digest"],
        },
        str(uuid.uuid4()),
    )
    assert status == 200, reviewed
    approve_body = {
        "candidate_id": reviewed["data"]["candidate_id"],
        "candidate_digest": reviewed["data"]["candidate_digest"],
    }
    request_id = str(uuid.uuid4())
    lost_response = http.client.HTTPConnection(
        "127.0.0.1",
        server.port,
        timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS,
    )
    lost_response.request(
        "POST",
        _contract("POST", "/api/runtime-surface/profile-change/approve"),
        body=json.dumps(approve_body).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json",
            "X-Tobkiri-Request-ID": request_id,
        },
    )
    lost_response.close()

    status_path = _contract("GET", "/api/runtime-surface/operation-status")
    deadline = time.monotonic() + EVENTUAL_RECONCILIATION_TIMEOUT_SECONDS
    while True:
        status, reconciled, _ = _request(
            server,
            "GET",
            f"{status_path}?request_id={request_id}",
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        if status == 200:
            reconciliation_state = reconciled["data"]["state"]
            assert reconciliation_state in {"pending", "succeeded"}, reconciled
            if reconciliation_state == "succeeded":
                break
        else:
            assert status == 409, reconciled
        assert time.monotonic() < deadline, reconciled
        time.sleep(0.02)
    assert status == 200, reconciled
    assert reconciled["data"]["state"] == "succeeded"
    assert reconciled["data"]["request_id"] == request_id
    assert reconciled["data"]["result_digest"].startswith("sha256:")
    approved = {"data": reconciled["data"]["result"]}
    assert reconciled["data"]["record_refs"] == [
        {
            "kind": "approval",
            "id": approved["data"]["approval_id"],
            "digest": approved["data"]["approval_digest"],
        }
    ]

    server.stop()
    server.start()
    status, after_restart, _ = _request(
        server,
        "GET",
        f"{status_path}?request_id={request_id}",
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, after_restart
    assert after_restart["data"] == reconciled["data"]

    other_cookie, _other_csrf, _other_origin = _authenticate(server)
    status, cross_session, _ = _request(
        server,
        "GET",
        f"{status_path}?request_id={request_id}",
        headers={
            "Cookie": other_cookie,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert status == 409, cross_session

    status, same_request, _ = post(
        "/api/runtime-surface/profile-change/approve",
        approve_body,
        request_id,
    )
    assert status == 200, same_request
    assert same_request["data"] == approved["data"]
    status, different_request, _ = post(
        "/api/runtime-surface/profile-change/approve",
        approve_body,
        str(uuid.uuid4()),
    )
    assert status == 200, different_request
    assert different_request["data"]["approval_id"] == approved["data"]["approval_id"]
    assert different_request["data"]["approval_digest"] == approved["data"]["approval_digest"]
    assert different_request["data"]["authority_approval"] == approved["data"]["authority_approval"]

    commits = [
        event
        for event in authority.audit_events()
        if event["event_type"] == "authority_records_committed"
        and any(
            item.get("record_id") == approved["data"]["approval_id"]
            for item in event["payload"].get("records", [])
        )
    ]
    assert len(commits) == 1

    for unknown_id in (
        "00000000-0000-4000-8000-000000000000",
        request_id + "-tampered",
    ):
        status, rejected, _ = _request(
            server,
            "GET",
            f"{status_path}?request_id={unknown_id}",
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert status == 409, rejected


def test_contract_replay_unknown_and_stale_capture_fail_closed(
    production_server,
) -> None:
    server, session, authority = production_server
    cookie, _csrf, _origin = _authenticate(server)
    request_id = str(uuid.uuid4())
    path = _contract("GET", "/api/home/dashboard")
    headers = {"Cookie": cookie, "X-Tobkiri-Request-ID": request_id}
    first = _request(server, "GET", path, headers=headers)
    assert first[0] == 200, first[1]
    audit_after_first = len(authority.audit_events())
    assert _request(server, "GET", path, headers=headers)[0] == 409
    assert len(authority.audit_events()) == audit_after_first

    traversal = "/api/contracts/defaultspack/GET%20%2Fapi%2F..%2Fsecrets"
    assert (
        _request(
            server,
            "GET",
            traversal,
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )[0]
        == 400
    )
    assert len(authority.audit_events()) == audit_after_first

    assert (
        _request(
            server,
            "GET",
            "/api/ui/catalog",
            headers={"Cookie": cookie},
        )[0]
        == 404
    )
    assert len(authority.audit_events()) == audit_after_first

    unknown = _contract("GET", "/api/pack-control/not-selected")
    assert (
        _request(
            server,
            "GET",
            unknown,
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )[0]
        == 404
    )
    assert len(authority.audit_events()) == audit_after_first

    authority.advance_security_epoch("test stale frontend capture")
    stale_server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="other"),
        dispatch_session=session,
        contract_bindings=tuple(server._contract_routes.values()),
    )
    with pytest.raises(Exception, match="stale|epoch"):
        stale_server.start()
    assert stale_server.server is None


def test_stale_fresh_mutation_has_no_journal_admission_side_effects(
    production_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, profile
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]
    journal = server._operation_journal
    assert journal is not None
    assert not journal.path.exists()
    mutating_calls: list[str] = []
    lookup_calls: list[str] = []
    original_lookup = journal.lookup_operation

    def counted_lookup(**kwargs):
        lookup_calls.append(str(kwargs["request_id"]))
        return original_lookup(**kwargs)

    def unexpected_renew(*_args, **_kwargs) -> None:
        mutating_calls.append("renew_session")

    def unexpected_begin(*_args, **_kwargs):
        mutating_calls.append("begin_operation")
        return {}, False

    monkeypatch.setattr(journal, "lookup_operation", counted_lookup)
    monkeypatch.setattr(journal, "renew_session", unexpected_renew)
    monkeypatch.setattr(journal, "begin_operation", unexpected_begin)
    authority.advance_security_epoch("reject stale fresh mutation")

    status, rejected, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/runtime-surface/profile-change/resolve"),
        body={
            "profile_id": "defaults",
            "expected_profile_revision": envelope["profile_revision"],
            "expected_plan_digest": envelope["plan_digest"],
            "desired_pack_ids": desired,
        },
        headers={
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )

    assert status == 401, rejected
    assert rejected["error"] == "Unauthorized"
    assert lookup_calls == []
    assert mutating_calls == []
    assert not journal.path.exists()


def test_replayed_mutation_without_record_is_filesystem_immutable(
    production_server,
) -> None:
    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    request_id = str(uuid.uuid4())
    journal = server._operation_journal.path
    assert not journal.exists()
    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": request_id},
    )
    assert status == 200
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]

    status, rejected, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/runtime-surface/profile-change/resolve"),
        body={
            "profile_id": "defaults",
            "expected_profile_revision": envelope["profile_revision"],
            "expected_plan_digest": envelope["plan_digest"],
            "desired_pack_ids": desired,
        },
        headers={
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
            "X-Tobkiri-Request-ID": request_id,
        },
    )

    assert status == 409, rejected
    assert not journal.exists()


def test_corrupt_reconciliation_journal_maps_to_typed_503_without_detail(
    production_server,
) -> None:
    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    journal = server._operation_journal.path
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b"not a sqlite database")
    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]

    status, rejected, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/runtime-surface/profile-change/resolve"),
        body={
            "profile_id": "defaults",
            "expected_profile_revision": envelope["profile_revision"],
            "expected_plan_digest": envelope["plan_digest"],
            "desired_pack_ids": desired,
        },
        headers={
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )

    assert status == 503
    assert rejected["data"]["code"] == "operation_reconciliation_unavailable"
    assert rejected["error"] == "Control operation reconciliation is unavailable"
    assert "sqlite" not in json.dumps(rejected).lower()


def test_reconciliation_binding_conflict_maps_to_typed_409_without_detail(
    production_server,
) -> None:
    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]
    assert len(desired) > 1
    request_id = str(uuid.uuid4())
    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
        "X-Tobkiri-Request-ID": request_id,
    }
    path = _contract("POST", "/api/runtime-surface/profile-change/resolve")
    body = {
        "profile_id": "defaults",
        "expected_profile_revision": envelope["profile_revision"],
        "expected_plan_digest": envelope["plan_digest"],
        "desired_pack_ids": desired,
    }
    assert _request(server, "POST", path, body=body, headers=headers)[0] == 200
    tampered = {**body, "desired_pack_ids": list(reversed(desired))}

    status, rejected, _ = _request(
        server,
        "POST",
        path,
        body=tampered,
        headers=headers,
    )

    assert status == 409
    assert rejected["data"]["code"] == "operation_reconciliation_mismatch"
    assert rejected["error"] == "Control operation conflicts with durable state"
    assert "digest" not in json.dumps(rejected).lower()


def test_contract_server_rejects_missing_or_wrong_capture_before_bind(
    production_server,
) -> None:
    server, session, _authority = production_server
    bindings = tuple(server._contract_routes.values())
    missing = PackAPIServer(port=0, contract_bindings=bindings)
    with pytest.raises(RuntimeError, match="captured v4 session"):
        missing.start()
    assert missing.server is None

    route = bindings[0]
    target = route.targets[0]
    wrong_target = FrontendContractTarget(
        contribution_id=target.contribution_id,
        contract_id=target.contract_id,
        operation_id=target.operation_id,
        provider_id="unselected.provider",
        function_id="unselected.provider",
        allowed_payload_keys=target.allowed_payload_keys,
    )
    wrong_binding = FrontendContractBinding(
        method=route.method,
        path=route.path,
        presentation=route.presentation,
        targets=(wrong_target,),
    )
    wrong = PackAPIServer(
        port=0,
        dispatch_session=session,
        contract_bindings=(wrong_binding,),
    )
    with pytest.raises(RuntimeError, match="Provider identity"):
        wrong.start()
    assert wrong.server is None


def test_contract_server_rejects_empty_and_wrong_backend_registry_before_bind(
    production_server,
) -> None:
    server, session, _authority = production_server
    bindings = tuple(server._contract_routes.values())
    selected_backends = session.broker._backends

    session.broker._backends = BackendRegistry(())
    empty = PackAPIServer(
        port=0,
        dispatch_session=session,
        contract_bindings=bindings,
    )
    with pytest.raises(BackendUnavailableError, match="not installed"):
        empty.start()
    assert empty.server is None

    original_statuses = [
        (backend, backend.status) for backend in selected_backends.registered
    ]
    try:
        for backend, original_status in original_statuses:
            backend.status = type(original_status)(
                backend_id=original_status.backend_id,
                execution_kind=original_status.execution_kind,
                platform=original_status.platform,
                backend_digest="sha256:" + "0" * 64,
                production_enabled=True,
                conformance_only=False,
                satisfied_gates=original_status.satisfied_gates,
            )
        session.broker._backends = BackendRegistry(
            backend for backend, _status in original_statuses
        )
        wrong = PackAPIServer(
            port=0,
            dispatch_session=session,
            contract_bindings=bindings,
        )
        with pytest.raises(RuntimeError, match="metadata is stale or wrong"):
            wrong.start()
        assert wrong.server is None
    finally:
        for backend, original_status in original_statuses:
            backend.status = original_status
        session.broker._backends = selected_backends

    exact = PackAPIServer(
        port=0,
        dispatch_session=session,
        contract_bindings=bindings,
    )
    with pytest.raises(
        BackendUnavailableError,
        match="authenticated PackVM supervisor",
    ):
        exact._validate_contract_runtime()
    assert exact.server is None


def test_selected_desktop_entrypoint_has_no_compatibility_server_authority() -> None:
    desktop = (
        RUNTIME_ROOT / "ecosystem" / "defaultspack" / "defaultspack" / "desktop_app.py"
    ).read_text(encoding="utf-8")
    assert "DefaultsHttpServer" not in desktop
    assert "transport.http" not in desktop
    assert "build_fallback_http_routes" not in desktop
