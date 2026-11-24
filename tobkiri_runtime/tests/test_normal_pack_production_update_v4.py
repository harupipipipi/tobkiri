"""Signed Normal Pack successors retain their owner and execute real Wasm."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import socket
from typing import Any
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from core_runtime.active_profile_store_v4 import ActiveProfileStore
from core_runtime.authority.v4 import AuthorityDenied, DomainBoundary
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.bootstrap.profile_capture import (
    capture_active_profile,
    host_profile_catalog,
    runtime_user_data_root,
)
from core_runtime.external_pack_catalog_v4 import resolve_admitted_pack_root
from core_runtime.native_pack_onboarding import commit_signed_pack, preview_signed_pack
from core_runtime.pack_signature import (
    SIGNED_MANIFEST_RELATIVE,
    build_signed_manifest,
    sign_manifest,
)
from core_runtime.profile_catalog_v4 import bundle_lock_digest, profile_catalog_digest
from core_runtime.profile_pack_versions import (
    profile_pack_versions,
    select_profile_pack_version,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    RuntimeProfileChangeService,
    RuntimeSurfaceService,
    RuntimeSurfaceError,
    create_runtime_surface_services,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from tests.conformance_support.host_profile import captured_host_profile
from tests.test_host_extension_update_v4 import _refresh
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.credential_store import host_credential_store_factory
from tobkiri_host.wasm_backend import production_wasm_backend
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_file

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
PACK_ID = "rumi_scheduler_tool_adapter_pack"
PACK_ROOT = RUNTIME_ROOT / "ecosystem" / PACK_ID
FUNCTION_ID = f"{PACK_ID}.tool-definitions.scheduler"
OPERATION_ID = f"{PACK_ID}.scheduler-tool-definitions"
CONTRACT_ID = "tobkiri.resource.tool.definition.contribution.v1"
COMPONENT_PATH = Path("runtime/definitions_component.wasm")


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail instead of making an external call from this local-only test."""

    def denied(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("network access is forbidden in the Normal Pack update test")

    monkeypatch.setattr(urllib.request, "urlopen", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    original = socket.socket.connect

    def guarded_connect(connection: socket.socket, address: Any) -> Any:
        if connection.family in {socket.AF_INET, socket.AF_INET6}:
            denied()
        return original(connection, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def _signed_successor(tmp_path: Path) -> tuple[Path, Path]:
    """Sign a real component update with a fixture-owned, temporary key."""

    source = tmp_path / "signed-successor" / PACK_ID
    shutil.copytree(PACK_ROOT, source, ignore=shutil.ignore_patterns("__pycache__"))
    manifest_path = source / "pack.v4.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pack"]["version"] = "1.1.0"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    # Wasm custom sections have id zero and length-prefixed name/payload bytes.
    # This changes the actual selected implementation while retaining semantics.
    name = b"tobkiri-update"
    section = bytes([len(name)]) + name + b"normal-successor-two"
    assert len(section) < 128
    component = source / COMPONENT_PATH
    component.write_bytes(component.read_bytes() + b"\x00" + bytes([len(section)]) + section)
    _refresh(source)
    compiled = compile_pack_root(source)
    manifest = validate_file(manifest_path, "pack")
    key = Ed25519PrivateKey.generate()
    signed = sign_manifest(
        build_signed_manifest(
            source,
            pack_id=PACK_ID,
            version=compiled.artifact.version,
            publisher_id=compiled.artifact.publisher_lineage,
            core_compatibility=">=0",
            contract_versions={
                operation.contract_id: operation.contract_version
                for function in compiled.artifact.functions
                for operation in function.operations
            },
            requested_capabilities=list(manifest["requirements"]["capabilities"]),
        ),
        key,
    )
    signed_path = source / SIGNED_MANIFEST_RELATIVE
    signed_path.parent.mkdir(mode=0o700, exist_ok=True)
    signed_path.write_text(json.dumps(signed) + "\n", encoding="utf-8")
    public_key = tmp_path / "normal-publisher.pem"
    public_key.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return source, public_key


def _activate_revision(user_data: Path, profile_id: str) -> None:
    """Resolve, review and approve the exact successor through public services."""

    catalog = host_profile_catalog()
    predecessor = ActiveProfileStore(user_data).require(verify_snapshot=True)
    definition = catalog.profiles[profile_id]
    surface = RuntimeSurfaceService(catalog_loader=host_profile_catalog)
    try:
        service = RuntimeProfileChangeService(surface_service=surface, user_data_root=user_data)
        candidate = service.resolve(
            {
                "profile_id": profile_id,
                "expected_profile_revision": predecessor.profile_revision,
                "expected_plan_digest": predecessor.plan_digest,
                "desired_pack_ids": [
                    row["pack_id"]
                    for row in definition["packs"]
                    if row.get("role") != "application"
                ],
                "profile_definition_digest": canonical_digest(definition),
                "profile_catalog_digest": profile_catalog_digest(catalog),
                "bundle_lock_digest": bundle_lock_digest(catalog),
            },
            session_id="normal-update-review",
        )
        with pytest.raises(RuntimeSurfaceError):
            service.activate(
                {"approval_id": "unapproved", "approval_digest": "sha256:" + "f" * 64},
                session_id="normal-update-review",
            )
        reviewed = service.review(
            {
                "candidate_id": candidate["candidate_id"],
                "candidate_digest": candidate["candidate_digest"],
            },
            session_id="normal-update-review",
        )
        approved = service.approve(
            {
                "candidate_id": reviewed["candidate_id"],
                "candidate_digest": reviewed["candidate_digest"],
            },
            session_id="normal-update-review",
        )
        service.activate(
            {
                "approval_id": approved["approval_id"],
                "approval_digest": approved["approval_digest"],
            },
            session_id="normal-update-review",
        )
    finally:
        surface.close()


def _definitions(session: Any) -> dict[str, Any]:
    """Invoke the real component through its captured tool-registry caller."""

    session.assert_operation_ready(CONTRACT_ID, OPERATION_ID)
    return dict(
        session.invoke(
            "tobkiri.resource.tool.definition.v1",
            "rumi_tool_registry_pack.tool-definition-resource",
            {"operation": "list", "_session_id": "normal-update-invoke"},
        )
    )


def test_signed_normal_successor_invokes_real_wasm_with_same_data_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Update signed CAS bytes without replacing Host state or launch authority."""

    backend = production_wasm_backend()
    if not backend.status.ready_for_production:
        pytest.skip(backend.status.unavailable_reason or "hard controller unavailable")
    edge = {
        "caller_function_id": "rumi_tool_registry_pack.tool-registry.definition",
        "target_provider_id": FUNCTION_ID,
        "contract_id": CONTRACT_ID,
        "operation_id": OPERATION_ID,
        "authority_mode": "profile_grant",
        "requested_scope_template": {
            "capability": "operation.invoke",
            "dimensions": {"contract": [CONTRACT_ID], "operation": [OPERATION_ID]},
            "quotas": {},
            "exact_request_digest": None,
            "opaque": False,
        },
    }
    bundled_component = (PACK_ROOT / COMPONENT_PATH).read_bytes()
    with captured_host_profile(
        tmp_path, monkeypatch, packs=(PACK_ID,), edges=(edge,), backends=(backend,)
    ) as (old_session, authority):
        initial = capture_active_profile()
        user_data = runtime_user_data_root()
        assert user_data == tmp_path / "user-data"
        profile_id = str(initial.resolved.profile["profile_id"])
        baseline = old_session.broker._catalog.resolve_pinned(CONTRACT_ID, OPERATION_ID)
        baseline_digest = baseline.artifact.digest
        old_definitions = _definitions(old_session)
        protected = {
            field: deepcopy(initial.resolved.lock[field])
            for field in ("base", "shell", "application")
        }
        providers = ProviderRegistry(profile_id, user_data_root=user_data)
        providers.save(
            {
                "provider_instance_id": "retained-normal-provider",
                "adapter_id": "openai-compatible",
                "endpoint": "https://provider.invalid/v1",
                "credential_handle": "opaque:retained-normal-credential",
                "enabled": False,
            },
            expected_revision=0,
        )
        conversations = ConversationStore(profile_id, user_data_root=user_data)
        created = conversations.create(
            {"id": "retained-normal-chat", "title": "Keep this chat"},
            expected_revision=0,
        )
        conversations.append_message(
            "retained-normal-chat",
            {"id": "retained-message", "role": "user", "content": "Preserve me."},
            expected_conversation_revision=created["conversation"]["conversation_revision"],
        )
        snapshots = providers.snapshot(), conversations.snapshot()
        retained_files = {path: path.read_bytes() for path in (providers.path, conversations.path)}

        def assert_retained() -> None:
            assert runtime_user_data_root() == user_data
            assert (providers.snapshot(), conversations.snapshot()) == snapshots
            assert {path: path.read_bytes() for path in retained_files} == retained_files

        source, public_key = _signed_successor(tmp_path)
        public_bytes = public_key.read_bytes()
        pointer_path = user_data / "profiles/active.json"
        old_pointer = pointer_path.read_bytes()
        preview = preview_signed_pack(source, public_key)
        assert preview["kind"] == "normal_sandbox"
        assert preview["execution_boundary"] == "sandbox"
        assert preview["predecessor_artifact_digest"] == baseline_digest
        admitted = commit_signed_pack(
            source,
            public_key,
            expected_preview_digest=preview["preview_digest"],
            trust_store_path=user_data / "pack_control/publisher-trust.json",
        )
        successor_digest = str(admitted["artifact_digest"])
        assert successor_digest != baseline_digest
        cas_root = resolve_admitted_pack_root(PACK_ID, artifact_digest=successor_digest)
        assert cas_root not in {PACK_ROOT, source}
        assert (cas_root / COMPONENT_PATH).read_bytes() == (source / COMPONENT_PATH).read_bytes()
        assert pointer_path.read_bytes() == old_pointer
        assert_retained()
        view = profile_pack_versions(profile_id)
        selected = next(row for row in view["packs"] if row["pack_id"] == PACK_ID)
        result = select_profile_pack_version(
            {
                "profile_id": profile_id,
                "pack_id": PACK_ID,
                "artifact_digest": successor_digest,
                "expected_selected_digest": selected["selected_digest"],
                "expected_profile_revision": view["profile_definition_revision"],
                "expected_store_generation": view["store_generation"],
                "expected_catalog_revision": view["catalog_revision"],
            }
        )
        assert result["activation_required"] is True
        assert pointer_path.read_bytes() == old_pointer
        assert capture_active_profile().resolved.plan == initial.resolved.plan
        assert_retained()
        _activate_revision(user_data, profile_id)
        updated = capture_active_profile()
        assert updated.activation["activation_id"] != initial.activation["activation_id"]
        with pytest.raises(AuthorityDenied, match="stale"):
            old_session.assert_current()
        for field, pin in protected.items():
            assert updated.resolved.lock[field] == pin
        fresh_backend = production_wasm_backend()
        assert fresh_backend.status.ready_for_production
        session = capture_production_dispatch(
            updated,
            bundle_root=host_profile_catalog().root,
            ecosystem_root=RUNTIME_ROOT / "ecosystem",
            authority_store=authority,
            activation_snapshot_loader=defaultspack_activation_snapshot_loader,
            runtime_surface_factory=create_runtime_surface_services,
            credential_store_factory=host_credential_store_factory,
            backends=BackendRegistry((fresh_backend,)),
        )
        try:
            assert old_session.owned_authority_store is session.owned_authority_store is authority
            binding = session.broker._catalog.resolve_pinned(CONTRACT_ID, OPERATION_ID)
            assert binding.artifact.digest == successor_digest
            assert binding.function.implementation_digest != baseline.function.implementation_digest
            assert binding.function.implementation_digest == (
                "sha256:" + hashlib.sha256((cas_root / COMPONENT_PATH).read_bytes()).hexdigest()
            )
            domain = next(
                row
                for row in authority.list_domains()
                if row.activation_id == updated.activation["activation_id"]
                and any(principal.function_id == FUNCTION_ID for principal in row.principals)
            )
            assert domain.boundary is DomainBoundary.WASM_COMPONENT
            new_definitions = _definitions(session)
            assert new_definitions["definitions"] == old_definitions["definitions"]
            assert new_definitions["aliases"] == old_definitions["aliases"]
            assert {row["provider_instance_id"] for row in new_definitions["contributions"]} == {
                "tool-definitions.scheduler"
            }
            assert_retained()
            assert public_key.read_bytes() == public_bytes
        finally:
            session.close()
    assert (PACK_ROOT / COMPONENT_PATH).read_bytes() == bundled_component
