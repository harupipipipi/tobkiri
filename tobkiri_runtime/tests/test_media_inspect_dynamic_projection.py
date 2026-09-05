"""Real loopback coverage for the selected Media Inspect Pack contribution."""

from __future__ import annotations

import hashlib
import http.client
import json
import time
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import pytest

from core_runtime.authority.v4 import AuthorityDenied, AuthorityStore
from core_runtime.bootstrap.production_v4 import (
    _pack_root_identities,
    capture_production_dispatch,
)
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
    load_frontend_contract_bindings,
)
from ecosystem.defaultspack.defaultspack.http_contract_composition import (
    defaultspack_capability_snapshot,
)
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.pack_control_v4 import (
    PACK_CONTROL_CONTRACT,
    capture_pack_catalog_reader,
    capture_pack_control_session,
)
from core_runtime.panel_auth import PanelAuthManager
from ecosystem.defaultspack.domain.runtime_v4 import (
    BundledCatalog,
    ProfileResolutionDenied,
    dynamic_profile_edges,
)
from ecosystem.rumi_file_inspect_pack.runtime.inspect import FileInspectService
from ecosystem.rumi_media_inspect_service_pack.runtime.inspect import (
    MediaInspectService,
)
from ecosystem.rumi_workspace_mount_pack.runtime.mounts import (
    WorkspaceMountStore,
    capture_selected_workspace_binding,
)
from tobkiri_host.backends import (
    REQUIRED_PRODUCTION_GATES,
    BackendRegistry,
    BackendStatus,
)
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.errors import AuthorizationError
from tobkiri_host.models import (
    ExecutionKind,
    InvocationFrame,
    OpaqueAuthorityRef,
    RuntimeEvidence,
)
from tobkiri_protocol.canonical import canonical_digest
from tests.conformance_support.host_contract import host_contract_for_session


def _bundle_root() -> Path:
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    return packaged_profile_bundle_root()


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = (
    RUNTIME_ROOT / "ecosystem" / "defaultspack" / "defaultspack" / "frontend_contract_map.v4.json"
)
MEDIA_PACK = "rumi_media_inspect_service_pack"
MEDIA_CONTRACT = "tobkiri.service.media.inspect.v1"
MEDIA_OPERATION = "rumi_media_inspect_service_pack.media-inspect"
FILE_CONTRACT = "tobkiri.service.file.inspect.v1"
FILE_OPERATION = "rumi_file_inspect_pack.file-inspect.for-media"
GENERAL_FILE_OPERATION = "rumi_file_inspect_pack.file-inspect"


def _capture_control_session(**kwargs):
    """Compose the Defaultspack runtime surface explicitly for direct tests."""

    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        create_runtime_surface_services,
    )

    return capture_pack_control_session(
        runtime_surface_factory=create_runtime_surface_services,
        **kwargs,
    )


def _capture_defaultspack_dispatch(active: object, **kwargs: object):
    """Compose production dispatch with Defaultspack-owned dependencies."""

    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        create_runtime_surface_services,
    )

    return capture_production_dispatch(
        active,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        **kwargs,
    )


CONVERSATION_CALLER = "defaultspack.conversation"
MEDIA_CALLER = "rumi_media_inspect_service_pack.media-inspect.service"
WORKSPACE_CONTRACT = "tobkiri.resource.workspace.v1"


def _contract(method: str, path: str) -> str:
    return "/api/contracts/defaultspack/" + quote(f"{method.upper()} {path}", safe="")


class _WorkspaceClient:
    def __init__(self, store: WorkspaceMountStore) -> None:
        self.store = store

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        assert contract_id == WORKSPACE_CONTRACT
        if operation_id == "get":
            mount = self.store.get(str(payload["workspace_id"]))
            if mount is None:
                raise KeyError("workspace mount is unknown")
            return mount
        if operation_id == "list":
            return self.store.snapshot()
        raise AssertionError(operation_id)


def _workspace_binding(store: WorkspaceMountStore) -> dict[str, Any]:
    mount = store.get("defaults")
    assert mount is not None
    root = Path(str(mount["root_path"])).resolve(strict=True)
    root_stat = root.stat()
    binding: dict[str, Any] = {
        "workspace_id": "defaults",
        "access": "read_only",
        "mount_revision": str(
            mount.get("revision") or mount.get("updated_at_ms") or mount.get("updated_at") or ""
        ),
        "canonical_root": str(root),
        "root_st_dev": int(root_stat.st_dev),
        "root_st_ino": int(root_stat.st_ino),
    }
    binding["root_identity"] = hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return binding


class _BrokerMediaClient:
    def __init__(
        self,
        backend: "_MediaBackend",
        session_id: str,
        binding: Mapping[str, Any],
    ) -> None:
        self.backend = backend
        self.session_id = session_id
        self.binding = dict(binding)

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if contract_id != FILE_CONTRACT:
            raise AssertionError(contract_id)
        return self.backend.session.invoke(
            FILE_CONTRACT,
            FILE_OPERATION,
            {
                **dict(payload),
                "_workspace_binding": self.binding,
                "_session_id": self.session_id,
            },
        )


class _MediaBackend:
    """Production-gated PackVM harness executing the real read-only services."""

    def __init__(
        self,
        store: WorkspaceMountStore,
        binding: Mapping[str, Any],
    ) -> None:
        self.status = BackendStatus(
            backend_id="tobkiri.python-pack-v4",
            execution_kind=ExecutionKind.PACK_VM,
            platform="any",
            backend_digest=canonical_digest({"backend": "media-test-v4"}),
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )
        self.store = store
        self.binding = dict(binding)
        self.session: Any = None
        self.target_domains: dict[str, str] = {}
        self.artifact_resolver: Any = None
        self.target_domain_resolver: Any = None
        self.calls: list[tuple[str, str]] = []

    def bind_artifact_resolver(self, resolver: Any) -> None:
        """Accept the activation-bound artifact resolver used by Production."""

        self.artifact_resolver = resolver

    def bind_target_domain_resolver(self, resolver: Any) -> None:
        """Accept the Authority-owned target-domain resolver."""

        self.target_domain_resolver = resolver

    def materialize(self, binding: Any, reservation_id: str) -> RuntimeEvidence:
        del reservation_id
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(self.target_domains[binding.principal_ref.value]),
            executable_digest=binding.function.implementation_digest,
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
        )

    def invoke(self, request: Any) -> ProviderOutcome:
        self.calls.append((request.contract_id, request.operation_id))
        if request.contract_id == MEDIA_CONTRACT:
            workspace_binding = request.payload.get("_workspace_binding")
            if not isinstance(workspace_binding, Mapping):
                raise PermissionError("Host workspace binding is missing")
            client = _BrokerMediaClient(
                self,
                request.context.caller_session_id,
                workspace_binding,
            )
            result = MediaInspectService(client).invoke(
                str(request.payload.get("name") or ""),
                request.payload,
            )
            return ProviderOutcome(result)
        if request.contract_id == FILE_CONTRACT:
            payload = dict(request.payload)
            operation = str(payload.pop("name", ""))
            result = FileInspectService(_WorkspaceClient(self.store)).invoke(
                operation,
                payload,
            )
            return ProviderOutcome(result)
        raise AssertionError((request.contract_id, request.operation_id))

    def cancel(self, request_id: str) -> None:
        del request_id

    def terminate(self, domain_id: str) -> None:
        del domain_id


class _PackRouteValidationBackend:
    """Test-only backend for one Pack's captured route availability."""

    def __init__(self, pack_id: str, *, ready: bool) -> None:
        self._pack_id = pack_id
        self.status = BackendStatus(
            backend_id="tobkiri.python-pack-v4",
            execution_kind=ExecutionKind.PACK_VM,
            platform="any",
            backend_digest=canonical_digest(
                {"backend": "route-validation-v4", "pack_id": pack_id, "ready": ready}
            ),
            production_enabled=ready,
            conformance_only=not ready,
            satisfied_gates=REQUIRED_PRODUCTION_GATES if ready else frozenset(),
            unavailable_reason=(
                None
                if ready
                else "authenticated PackVM supervisor is not registered for the selected backend"
            ),
        )

    def supports(self, binding: Any) -> bool:
        return binding.artifact.pack_id == self._pack_id

    def bind_artifact_resolver(self, resolver: Any) -> None:
        del resolver

    def materialize(self, binding: Any, reservation_id: str) -> RuntimeEvidence:
        del binding, reservation_id
        raise AssertionError("route validation backend must not materialize")

    def invoke(self, request: Any) -> ProviderOutcome:
        del request
        raise AssertionError("route validation backend must not invoke")

    def cancel(self, request_id: str) -> None:
        del request_id

    def terminate(self, domain_id: str) -> None:
        del domain_id


@pytest.fixture
def media_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.png").write_bytes(b"\x89PNG\r\n")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    try:
        (workspace / "outside-link.png").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    store = WorkspaceMountStore("defaults", user_data_root=user_data)
    mounted = store.mount("defaults", str(workspace), expected_revision=0)
    store.select("defaults", expected_revision=int(mounted["revision"]))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    control = _capture_control_session()
    control.invoke(
        PACK_CONTROL_CONTRACT,
        "pack.install",
        {"pack_id": MEDIA_PACK, "_session_id": "setup"},
    )
    candidate = control.invoke(
        PACK_CONTROL_CONTRACT,
        "approval.candidate",
        {"pack_id": MEDIA_PACK, "_session_id": "setup"},
    )
    control.invoke(
        PACK_CONTROL_CONTRACT,
        "approval.approve",
        {
            "pack_id": MEDIA_PACK,
            "candidate_id": candidate["candidate_id"],
            "_session_id": "setup",
        },
    )
    control.invoke(
        PACK_CONTROL_CONTRACT,
        "pack.enable",
        {"pack_id": MEDIA_PACK, "_session_id": "setup"},
    )
    active = capture_default_profile()

    authority_path = user_data / "authority" / "v4.sqlite3"
    authority_setup = AuthorityStore(authority_path)
    binding = _workspace_binding(store)
    backend = _MediaBackend(store, binding)
    authority_session = _capture_defaultspack_dispatch(
        active,
        bundle_root=_bundle_root(),
        ecosystem_root=RUNTIME_ROOT / "ecosystem",
        authority_store=authority_setup,
        backends=BackendRegistry((backend,)),
    )
    for contract_id, operation_id in (
        (MEDIA_CONTRACT, MEDIA_OPERATION),
        (FILE_CONTRACT, FILE_OPERATION),
    ):
        context = authority_session.context_for(
            contract_id,
            operation_id,
            "preflight",
        )
        resolved = authority_session.broker._catalog.resolve(
            contract_id,
            operation_id,
            ">=1,<2",
        )
        backend.target_domains[resolved.principal_ref.value] = context.target_domain_id
    backend.session = authority_session
    authority_session.close()

    authority = AuthorityStore(authority_path)
    session = _capture_defaultspack_dispatch(
        active,
        bundle_root=_bundle_root(),
        ecosystem_root=RUNTIME_ROOT / "ecosystem",
        authority_store=authority,
        backends=BackendRegistry(
            (
                _PackRouteValidationBackend("defaultspack", ready=True),
                _PackRouteValidationBackend(MEDIA_PACK, ready=False),
            )
        ),
    )

    catalog = BundledCatalog.load(_bundle_root())
    bindings = load_frontend_contract_bindings(
        MAP_PATH,
        catalog.packs["runtime.tauri.application.default"],
    )
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="media-test-secret"),
        dispatch_session=session,
        contract_bindings=bindings,
        capability_snapshot_factory=defaultspack_capability_snapshot,
        application_presentation=DefaultspackHTTPPresentation(),
        host_contract=host_contract_for_session(session),
        workspace_binding_resolver=lambda profile_id: capture_selected_workspace_binding(
            profile_id,
            user_data_root=user_data,
        ),
    )
    server.start()
    try:
        yield server, session, control, authority, user_data
    finally:
        server.stop()
        session.close()


def _request(
    server: PackAPIServer,
    method: str,
    path: str,
    *,
    body: object | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any], list[tuple[str, str]]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
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


def _authenticated(server: PackAPIServer) -> dict[str, str]:
    origin = f"http://127.0.0.1:{server.port}"
    status, bootstrap, _ = _request(
        server,
        "POST",
        "/api/panel/auth/bootstrap",
        body={},
        headers={"X-Rumi-Desktop-Bootstrap": "media-test-secret"},
    )
    assert status == 200, bootstrap
    status, exchange, response_headers = _request(
        server,
        "POST",
        "/api/panel/auth/exchange",
        body={"code": bootstrap["data"]["code"]},
        headers={"Origin": origin},
    )
    assert status == 200, exchange
    cookie = next(value for key, value in response_headers if key.lower() == "set-cookie").split(
        ";", 1
    )[0]
    return {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": str(exchange["data"]["csrf_token"]),
    }


def _dynamic_request(
    server: PackAPIServer,
    headers: Mapping[str, str],
    host: Mapping[str, Any],
    target: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    request_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    request_id = request_id or str(uuid.uuid4())
    body = {
        "request_id": str(uuid.uuid4()),
        "expires_at": time.time() + 30,
        "profile_id": host["profile_id"],
        "profile_revision": host["profile_revision"],
        "activation_id": host["activation_id"],
        "plan_hash": host["plan_hash"],
        "catalog_hash": host["catalog_hash"],
        "contribution_id": target["contribution_id"],
        "owner_pack_id": target["owner_pack_id"],
        "contract_id": target["action_contract"],
        "payload": dict(payload),
    }
    status, result, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/ui/capability/invoke"),
        body=body,
        headers={**headers, "X-Tobkiri-Request-ID": request_id},
    )
    return status, result


def test_media_catalog_reports_missing_authenticated_supervisor(media_server) -> None:
    """The selected Pack stays non-invokable without a real PackVM supervisor."""

    server, _session, _control, _authority, _user_data = media_server
    row = next(
        item
        for item in capture_pack_catalog_reader().read()["packs"]
        if item["pack_id"] == MEDIA_PACK
    )
    assert row["enabled"] is True
    assert row["approved"] is True
    assert row["operations_api_version"] == "io.tobkiri.pack-operations.v1"
    assert "declared_operations" not in row
    assert "invokable_operations" not in row
    assert {item["name"] for item in row["capabilities"]} == {
        "file.inspect",
        "media.inspect",
    }
    media_operations = [
        item
        for item in row["operations"]
        if item["contract_id"] == MEDIA_CONTRACT and item["invokable"] is True
    ]
    assert len(media_operations) == 1, row
    operation = media_operations[0]
    assert operation["operation_id"] == MEDIA_OPERATION
    assert operation["capabilities"] == operation["required_capabilities"]
    assert isinstance(operation["input_schema"], dict)

    headers = _authenticated(server)
    status, catalog, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/ui/catalog"),
        headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, catalog
    host = catalog["data"]["dynamic_host"]
    assert all(item["owner_pack_id"] != MEDIA_PACK for item in host["contributions"])
    diagnostic = next(item for item in host["diagnostics"] if item["owner_pack_id"] == MEDIA_PACK)
    assert diagnostic["code"] == "production_backend_unavailable"
    assert diagnostic["severity"] == "error"
    assert diagnostic["contribution_id"] == f"pack.{MEDIA_PACK}.{MEDIA_OPERATION}"
    assert "authenticated PackVM supervisor" in diagnostic["message"]


def test_deleted_approval_removes_descriptor_and_fences_capture(media_server) -> None:
    """Approval deletion immediately removes projection and denies the old session."""

    server, session, control, _authority, user_data = media_server
    headers = _authenticated(server)
    status, catalog, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/ui/catalog"),
        headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200
    host = catalog["data"]["dynamic_host"]
    assert all(item["owner_pack_id"] != MEDIA_PACK for item in host["contributions"])
    approval = user_data / "pack_control" / "approvals" / "defaults" / f"{MEDIA_PACK}.json"
    approval.unlink()
    row = control.invoke(
        PACK_CONTROL_CONTRACT,
        "pack.status",
        {"pack_id": MEDIA_PACK, "_session_id": "lifecycle"},
    )
    assert row["approved"] is False
    assert row["enabled"] is False
    with pytest.raises(Exception, match="approval"):
        session.assert_current()


def test_corrupt_approval_fences_captured_dependency_authority(media_server) -> None:
    """Corrupt approval bytes revoke the Media edge and its File dependency edge."""

    _server, session, control, _authority, user_data = media_server
    approval = user_data / "pack_control" / "approvals" / "defaults" / f"{MEDIA_PACK}.json"
    approval.write_text("{", encoding="utf-8")
    row = control.invoke(
        PACK_CONTROL_CONTRACT,
        "pack.status",
        {"pack_id": MEDIA_PACK, "_session_id": "lifecycle"},
    )
    assert row["approved"] is False
    assert row["enabled"] is False
    with pytest.raises(Exception, match="approval"):
        session.assert_current()


def test_file_operations_have_exact_distinct_callers(media_server) -> None:
    """Conversation and Media cannot cross the two signed File edges."""

    _server, session, _control, _authority, _user_data = media_server
    conversation_context = session.context_for(
        FILE_CONTRACT,
        GENERAL_FILE_OPERATION,
        "conversation-negative",
    )
    media_context = session.context_for(
        FILE_CONTRACT,
        FILE_OPERATION,
        "media-negative",
    )
    general_binding = session.broker._catalog.resolve(
        FILE_CONTRACT,
        GENERAL_FILE_OPERATION,
        ">=1,<2",
    )
    media_binding = session.broker._catalog.resolve(
        FILE_CONTRACT,
        FILE_OPERATION,
        ">=1,<2",
    )
    resolver = session.broker._authority._principals

    assert (
        resolver.resolve_principal(conversation_context.caller_principal).function_id
        == CONVERSATION_CALLER
    )
    assert resolver.resolve_principal(media_context.caller_principal).function_id == (MEDIA_CALLER)
    assert general_binding.operation.operation_id == GENERAL_FILE_OPERATION
    assert media_binding.operation.operation_id == FILE_OPERATION

    payload = {
        "name": "stat",
        "path": "sample.png",
        "profile_id": "defaults",
        "workspace_id": "defaults",
        "_workspace_binding": {},
    }
    with pytest.raises(AuthorizationError, match="static authorization failed"):
        session.broker.invoke(
            InvocationFrame(
                contract_id=FILE_CONTRACT,
                version_range=">=1,<2",
                operation_id=FILE_OPERATION,
                payload=payload,
            ),
            conversation_context,
            effect_scope=session.effect_scope_for(
                FILE_CONTRACT,
                FILE_OPERATION,
                payload,
            ),
        )
    with pytest.raises(AuthorizationError, match="static authorization failed"):
        session.broker.invoke(
            InvocationFrame(
                contract_id=FILE_CONTRACT,
                version_range=">=1,<2",
                operation_id=GENERAL_FILE_OPERATION,
                payload=payload,
            ),
            media_context,
            effect_scope=session.effect_scope_for(
                FILE_CONTRACT,
                GENERAL_FILE_OPERATION,
                payload,
            ),
        )


def test_pack_root_identity_rejects_root_symlink_and_detects_swap(tmp_path: Path) -> None:
    """Root binding ignores unrelated content but rejects root replacement links."""

    pack_root = tmp_path / "pack"
    pack_root.mkdir()
    (pack_root / "runtime.py").write_text("pass\n", encoding="utf-8")
    captured = _pack_root_identities({MEDIA_PACK: pack_root})

    moved_root = tmp_path / "pack-old"
    pack_root.rename(moved_root)
    pack_root.mkdir()
    (pack_root / "runtime.py").write_text("pass\n", encoding="utf-8")
    assert _pack_root_identities({MEDIA_PACK: pack_root}) != captured

    bin_directory = pack_root / "webapp" / "node_modules" / ".bin"
    bin_directory.mkdir(parents=True)
    (bin_directory / "tool").symlink_to(moved_root / "runtime.py")
    replacement_identity = _pack_root_identities({MEDIA_PACK: pack_root})

    linked_root = tmp_path / "pack-link"
    linked_root.symlink_to(pack_root, target_is_directory=True)
    with pytest.raises(AuthorityDenied, match="root is unavailable"):
        _pack_root_identities({MEDIA_PACK: linked_root})
    assert replacement_identity == _pack_root_identities({MEDIA_PACK: pack_root})


def test_media_dynamic_projection_stops_at_direct_signed_dependency() -> None:
    """Transitive implementation closure must not acquire inferred callers."""

    catalog = BundledCatalog.load(_bundle_root())
    edges = dynamic_profile_edges(catalog, "defaults", (MEDIA_PACK,))

    assert {
        (
            str(edge["caller_function_id"]),
            str(edge["target_provider_id"]),
            str(edge["contract_id"]),
            str(edge["operation_id"]),
        )
        for edge in edges
    } == {
        (
            "shell.tauri.default",
            "rumi_media_inspect_service_pack.media-inspect.service",
            MEDIA_CONTRACT,
            MEDIA_OPERATION,
        ),
        (
            "rumi_media_inspect_service_pack.media-inspect.service",
            "rumi_file_inspect_pack.file-inspect.service",
            FILE_CONTRACT,
            FILE_OPERATION,
        ),
    }


@pytest.mark.parametrize(
    ("dependency_id", "message"),
    (
        ("missing.file.pack", "not in the exact inventory"),
        (
            "rumi_host_authority_bridge_pack",
            "does not provide a signed required Contract",
        ),
    ),
)
def test_media_dependency_graph_rejects_missing_or_forged_pack(
    dependency_id: str,
    message: str,
) -> None:
    """A Pack ID alone cannot forge the signed Media-to-File dependency edge."""

    bundled = BundledCatalog.load(_bundle_root())
    media_manifest = json.loads(
        (RUNTIME_ROOT / "ecosystem" / MEDIA_PACK / "pack.v4.json").read_text(encoding="utf-8")
    )
    media_manifest["requirements"]["pack_dependencies"] = {dependency_id: ">=1.0.0,<2.0.0"}
    catalog = BundledCatalog(
        root=bundled.root,
        packs={**bundled.packs, MEDIA_PACK: media_manifest},
        bases=bundled.bases,
        shells=bundled.shells,
        profiles=bundled.profiles,
    )
    with pytest.raises(ProfileResolutionDenied, match=message):
        dynamic_profile_edges(catalog, "defaults", (MEDIA_PACK,))
