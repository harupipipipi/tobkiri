"""Opt-in signed Normal Pack updates through authenticated macOS PackVM.

Run with TOBKIRI_RUN_NATIVE_PACKVM_ACCEPTANCE=1 on a provisioned macOS Host.
The registered base and allocator are used in place; unavailable native facts
fail the opted-in run, and no live VM owner is stopped or re-provisioned.
"""

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import socket
from typing import Any
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

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
from core_runtime.packvm_lifecycle_v4 import PackVMLifecycleV4
from core_runtime.profile_pack_versions import (
    profile_pack_versions,
    select_profile_pack_version,
)
from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
    default_packvm_provisioner,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
    defaultspack_packvm_backend_factory,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from tests.conformance_support.host_profile import captured_host_profile
from tests.test_host_extension_update_v4 import _refresh
from tests.test_normal_pack_production_update_v4 import _activate_revision
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.credential_store import host_credential_store_factory
from tobkiri_host.models import ExecutionKind
from tobkiri_host.platform_backends import MacOSVZBackend
from tobkiri_protocol.validation import validate_file

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
PACK_ID = "rumi_shell_policy_pack"
PACK_ROOT = RUNTIME_ROOT / "ecosystem" / PACK_ID
FUNCTION_ID = f"{PACK_ID}.shell-policy.inspect"
OPERATION_ID = f"{PACK_ID}.shell-inspect"
CONTRACT_ID = "tobkiri.service.shell.inspect.v1"
IMPLEMENTATION_PATH = Path("runtime/policy.py")
SUCCESSOR_MARKER = "signed-packvm-normal-successor-v2"
NATIVE_ACCEPTANCE_ENV = "TOBKIRI_RUN_NATIVE_PACKVM_ACCEPTANCE"


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
    """Sign a real Python PackVM update with a fixture-owned, temporary key."""

    source = tmp_path / "signed-successor" / PACK_ID
    shutil.copytree(PACK_ROOT, source, ignore=shutil.ignore_patterns("__pycache__"))
    manifest_path = source / "pack.v4.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pack"]["version"] = "1.1.0"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    implementation = source / IMPLEMENTATION_PATH
    previous = implementation.read_text(encoding="utf-8")
    needle = '        "executed": False,\n'
    assert previous.count(needle) == 1
    implementation.write_text(
        previous.replace(
            needle,
            needle + f'        "normal_pack_successor": "{SUCCESSOR_MARKER}",\n',
        ),
        encoding="utf-8",
    )
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


def _classify(session: Any) -> dict[str, Any]:
    """Inspect a finite command through the captured Shell's granted edge."""

    session.assert_operation_ready(CONTRACT_ID, OPERATION_ID)
    return dict(
        session.invoke(
            CONTRACT_ID,
            OPERATION_ID,
            {
                "operation": "classify",
                "command": "git status",
                "_session_id": "normal-packvm-update-invoke",
            },
        )
    )


@pytest.mark.skipif(
    platform.system() != "Darwin" or os.environ.get(NATIVE_ACCEPTANCE_ENV) != "1",
    reason="opt-in native macOS PackVM acceptance requires registered signed assets",
)
def test_signed_normal_successor_invokes_real_packvm_with_same_data_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run exact signed successor bytes without replacing Host state or pins."""

    # Bind the existing registered substrate before the fixture isolates the
    # Profile user-data environment. Never copy registration or base images.
    provisioner = default_packvm_provisioner()
    lifecycle = PackVMLifecycleV4(provisioner)
    backend_factory = defaultspack_packvm_backend_factory(lifecycle)
    backend = backend_factory()
    assert isinstance(backend, MacOSVZBackend), provisioner.readiness_snapshot()
    assert backend.status.ready_for_production, backend.status.unavailable_reason
    assert backend.status.execution_kind is ExecutionKind.PACK_VM
    assert backend.status.requires_platform_attestation
    edge = {
        "caller_function_id": "shell.tauri.default",
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
    bundled_implementation = (PACK_ROOT / IMPLEMENTATION_PATH).read_bytes()
    try:
        with captured_host_profile(
            tmp_path, monkeypatch, packs=(PACK_ID,), edges=(edge,), backends=(backend,)
        ) as (old_session, authority):
            initial = capture_active_profile()
            user_data = runtime_user_data_root()
            assert user_data == tmp_path / "user-data"
            profile_id = str(initial.resolved.profile["profile_id"])
            baseline = old_session.broker._catalog.resolve_pinned(
                CONTRACT_ID, OPERATION_ID
            )
            baseline_digest = baseline.artifact.digest
            assert baseline.variant.execution_kind is ExecutionKind.PACK_VM
            assert baseline.variant.runtime_abi == "python3.13"
            old_classification = _classify(old_session)
            assert old_classification["classification"] == "low"
            assert old_classification["executed"] is False
            assert "normal_pack_successor" not in old_classification
            old_attestations = tuple(backend._domains.values())
            assert len(old_attestations) == 1
            old_attestation = old_attestations[0]
            assert old_attestation.artifact_digest == baseline_digest
            assert (
                old_attestation.executable_digest
                == baseline.function.implementation_digest
            )
            assert old_attestation.authenticated_channel and old_attestation.nonce_fresh
            # Explicit fixture backends are caller-owned. Release the old domain
            # before allocating a successor so only one extra VM exists at a time.
            backend.close()
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
                expected_conversation_revision=created["conversation"][
                    "conversation_revision"
                ],
            )
            snapshots = providers.snapshot(), conversations.snapshot()
            retained_files = {
                path: path.read_bytes() for path in (providers.path, conversations.path)
            }

            def assert_retained() -> None:
                assert runtime_user_data_root() == user_data
                assert (providers.snapshot(), conversations.snapshot()) == snapshots
                assert {
                    path: path.read_bytes() for path in retained_files
                } == retained_files

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
            cas_root = resolve_admitted_pack_root(
                PACK_ID, artifact_digest=successor_digest
            )
            assert cas_root not in {PACK_ROOT, source}
            assert (cas_root / IMPLEMENTATION_PATH).read_bytes() == (
                source / IMPLEMENTATION_PATH
            ).read_bytes()
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
            assert (
                updated.activation["activation_id"]
                != initial.activation["activation_id"]
            )
            with pytest.raises(AuthorityDenied, match="stale"):
                old_session.assert_current()
            for field, pin in protected.items():
                assert updated.resolved.lock[field] == pin
            fresh_backend = backend_factory()
            assert isinstance(fresh_backend, MacOSVZBackend)
            assert fresh_backend.status.ready_for_production
            assert fresh_backend.status.execution_kind is ExecutionKind.PACK_VM
            with ExitStack() as cleanup:
                cleanup.callback(fresh_backend.close)
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
                    assert (
                        old_session.owned_authority_store
                        is session.owned_authority_store
                        is authority
                    )
                    binding = session.broker._catalog.resolve_pinned(
                        CONTRACT_ID, OPERATION_ID
                    )
                    assert binding.artifact.digest == successor_digest
                    assert (
                        binding.function.implementation_digest
                        != baseline.function.implementation_digest
                    )
                    assert binding.function.implementation_digest == (
                        "sha256:"
                        + hashlib.sha256(
                            (cas_root / IMPLEMENTATION_PATH).read_bytes()
                        ).hexdigest()
                    )
                    domain = next(
                        row
                        for row in authority.list_domains()
                        if row.activation_id == updated.activation["activation_id"]
                        and any(
                            principal.function_id == FUNCTION_ID
                            for principal in row.principals
                        )
                    )
                    assert domain.boundary is DomainBoundary.DEDICATED_PROCESS
                    new_classification = _classify(session)
                    assert (
                        new_classification.pop("normal_pack_successor")
                        == SUCCESSOR_MARKER
                    )
                    assert new_classification == old_classification
                    attestations = tuple(fresh_backend._domains.values())
                    assert len(attestations) == 1
                    attestation = attestations[0]
                    assert attestation.artifact_digest == successor_digest
                    assert (
                        attestation.executable_digest
                        == binding.function.implementation_digest
                    )
                    assert attestation.authenticated_channel and attestation.nonce_fresh
                    assert_retained()
                    assert public_key.read_bytes() == public_bytes
                finally:
                    session.close()
    finally:
        backend.close()
    assert (PACK_ROOT / IMPLEMENTATION_PATH).read_bytes() == bundled_implementation
