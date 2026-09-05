"""Focused no-download tests for direct macOS VZ allocation facts."""

from __future__ import annotations

import ast
import hashlib
import hmac
import importlib.util
import io
import json
import os
from dataclasses import replace
from pathlib import Path
import shutil
import threading
import sys
import textwrap
from types import SimpleNamespace

import pytest

import ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner as macos_vz_provisioner
from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
    MacOSVZAssetManifest,
    MacOSVZProvisioner,
    VZ_ASSET_MANIFEST_SCHEMA,
    VZ_BUNDLE_MANIFEST_SCHEMA,
    VZ_RAW_EFI_IMAGE_DECLARED_BYTES,
    _MacOSVZHelperProcess,
    _file_digest,
    _parse_image_descriptor,
)
from core_runtime.packvm_lifecycle_v4 import (
    PackVMLifecycleV4,
    _cleanup_binding_is_retryable,
)
from ecosystem.defaultspack.backend.sandbox.isolation.lima_runtime import (
    PackVMLimaProvisioner,
)
from ecosystem.defaultspack.backend.sandbox.isolation.packvm_image_cache import (
    PackVMPinnedImage,
    PackVMVerifiedImage,
)
from core_runtime.bootstrap.production_v4 import _authenticated_packvm_backend
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.artifact_materialization import (
    MaterializedArtifactFile,
    MaterializedPackArtifact,
    _materialization_digest,
)
from tobkiri_host.macos_vz_supervisor import (
    MacOSVZAgentIdentity,
    MacOSVZHelperIdentity,
    MacOSVZLaunchAssets,
    MacOSVZSupervisorDriver,
)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _private_file(path: Path, payload: bytes, mode: int = 0o600) -> Path:
    path.write_bytes(payload)
    path.chmod(mode)
    return path


def test_prelaunch_canonical_json_matches_cross_language_hmac_vector() -> None:
    """Keep Python prelaunch HMAC bytes aligned with the Swift helper."""

    payload = {"path": "/Users/é"}
    encoded = macos_vz_provisioner._canonical_bytes(payload)

    assert encoded == b'{"path":"/Users/\xc3\xa9"}'
    assert hmac.new(
        b"01234567890123456789012345678901", encoded, hashlib.sha256
    ).hexdigest() == "fd5fa037f203e451dd633ca6810db1125a6760b57851c0518ff963368d5d36cc"


def test_nocloud_network_seed_is_local_only_and_has_no_egress_configuration() -> None:
    """A local dummy link satisfies cloud-final without attaching a VZ NIC."""

    network_seed = macos_vz_provisioner._NOCLOUD_LOCAL_ONLY_NETWORK_CONFIG

    assert network_seed == (
        b"version: 2\n"
        b"renderer: networkd\n"
        b"dummy-devices:\n"
        b"  tobkiri0:\n"
        b"    addresses: [192.0.2.1/32]\n"
    )
    for forbidden in (b"gateway", b"routes", b"nameservers", b"dhcp", b"ethernet"):
        assert forbidden not in network_seed


def test_cloud_bootstrap_registers_current_dataclass_runner_before_execution() -> None:
    """Cloud-init must use importlib's module-registration contract."""

    workspace = Path(__file__).resolve().parents[2]
    runner_path = (
        workspace
        / "tobkiri_runtime"
        / "ecosystem"
        / "defaultspack"
        / "backend"
        / "sandbox"
        / "isolation"
        / "resources"
        / "packvm_guest_runner.py"
    )
    template_path = (
        workspace
        / "tobkiri_launcher"
        / "packvm-vz-helper"
        / "Provisioning"
        / "cloud_init_template.yaml"
    )
    template = template_path.read_text(encoding="utf-8")
    assert "sys.modules[runner_spec.name] = runner_module" in template
    assert "sys.modules.pop(runner_spec.name, None)" in template

    module_name = "_tobkiri_packvm_seed_runner_bootstrap_test"
    spec = importlib.util.spec_from_file_location(module_name, runner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        assert callable(module.materialize_seed_artifact)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def test_cloud_bootstrap_emits_only_bounded_nonsecret_materialization_markers() -> None:
    """Debug console milestones identify bootstrap failure without seed/key data."""

    workspace = Path(__file__).resolve().parents[2]
    template = (
        workspace
        / "tobkiri_launcher"
        / "packvm-vz-helper"
        / "Provisioning"
        / "cloud_init_template.yaml"
    ).read_text(encoding="utf-8")

    assert 'phase "bootstrap-exit-${status}"' in template
    for marker in (
        "install-dir-ready",
        "runner-import-begin",
        "runner-imported",
        "seed-materialize-begin",
        "seed-materialize-oserror",
        "seed-materialize-capacity-rejected",
        "seed-materialize-validation-rejected",
        "seed-materialize-memory-rejected",
        "seed-materialize-unexpected-rejected",
        "seed-materialized",
        "service-written",
    ):
        assert f'phase("{marker}")' in template
    assert "os.open(\"/dev/hvc0\", os.O_WRONLY | os.O_CLOEXEC)" in template
    assert "agent-ed25519.pem" not in template.split("def phase(code: str)", 1)[1].split(
        "def regular", 1
    )[0]


def test_cloud_bootstrap_python_phase_writes_a_real_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendered CIDATA code emits line-delimited, non-secret hvc0 milestones."""

    workspace = Path(__file__).resolve().parents[2]
    template = (
        workspace
        / "tobkiri_launcher"
        / "packvm-vz-helper"
        / "Provisioning"
        / "cloud_init_template.yaml"
    ).read_text(encoding="utf-8")
    start = template.index("      python3 - ")
    start = template.index("\n", start) + 1
    end = template.index("      PY\n", start)
    script = textwrap.dedent(template[start:end])
    parsed = ast.parse(script)
    phase = next(
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name == "phase"
    )
    namespace: dict[str, object] = {}
    exec("import os", namespace)
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[phase], type_ignores=[])),
            "rendered-cloud-init-phase.py",
            "exec",
        ),
        namespace,
    )
    opened: list[int] = []
    written: list[bytes] = []
    module_os = namespace["os"]
    monkeypatch.setattr(module_os, "open", lambda *_args: 41)
    monkeypatch.setattr(module_os, "write", lambda descriptor, value: written.append(value))
    monkeypatch.setattr(module_os, "close", lambda descriptor: opened.append(descriptor))

    phase_function = namespace["phase"]
    assert callable(phase_function)
    phase_function("seed-materialize-begin")

    assert written == [b"TOBKIRI_BOOTSTRAP:seed-materialize-begin\n"]
    assert opened == [41]


def test_cloud_bootstrap_starts_guest_service_without_multi_user_cycle() -> None:
    """Cloud-final must not wait on the target that waits on cloud-final."""

    workspace = Path(__file__).resolve().parents[2]
    template = (
        workspace
        / "tobkiri_launcher"
        / "packvm-vz-helper"
        / "Provisioning"
        / "cloud_init_template.yaml"
    ).read_text(encoding="utf-8")
    service = json.loads(
        (
            workspace
            / "tobkiri_launcher"
            / "packvm-vz-helper"
            / "Provisioning"
            / "guest_service_template.v1.json"
        ).read_text(encoding="utf-8")
    )

    unit = service["service_unit"]
    assert "After=local-fs.target" in unit
    assert "After=multi-user.target" not in unit
    assert "WantedBy=multi-user.target" in unit
    assert "systemctl enable --now tobkiri-packvm-guest.service" not in template
    assert "systemctl enable tobkiri-packvm-guest.service" in template
    assert "systemctl start tobkiri-packvm-guest.service" in template


def test_cloud_bootstrap_runner_is_readable_to_the_unprivileged_pack_child() -> None:
    """The bwrap child must read its separately bound runner after dropping UID."""

    workspace = Path(__file__).resolve().parents[2]
    template = (
        workspace
        / "tobkiri_launcher"
        / "packvm-vz-helper"
        / "Provisioning"
        / "cloud_init_template.yaml"
    ).read_text(encoding="utf-8")

    assert 'install_dir.mkdir(mode=0o700, exist_ok=False)' in template
    assert (
        'copy_private(runner_path, install_dir / "packvm_guest_runner.py", 0o755)'
        in template
    )
    assert 'copy_private(agent_config, runtime_dir / "agent-config.json", 0o600)' in template
    assert 'copy_private(agent_key, runtime_dir / "agent-ed25519.pem", 0o600)' in template


def test_cloud_bootstrap_completes_dpkg_triggers_before_auditing() -> None:
    """The offline package install must not manufacture a pending trigger."""

    workspace = Path(__file__).resolve().parents[2]
    template = (
        workspace
        / "tobkiri_launcher"
        / "packvm-vz-helper"
        / "Provisioning"
        / "cloud_init_template.yaml"
    ).read_text(encoding="utf-8")

    install = 'dpkg -i "$agent_mount/bubblewrap_arm64.deb"'
    audit = 'if [ -n "$(dpkg --audit)" ]; then'
    assert install in template
    assert "dpkg --no-triggers" not in template
    assert template.index(install) < template.index(audit)


def _materialized_artifact(seed: str = "fixture") -> MaterializedPackArtifact:
    """Build one small, fully self-consistent Host-captured artifact."""

    content = f"def tobkiri_packvm_invoke(*_): return {{'seed': '{seed}'}}\n".encode()
    implementation_digest = _digest(content)
    artifact_digest = _digest(f"artifact:{seed}".encode())
    files = (
        MaterializedArtifactFile(
            path="runtime/entry.py",
            digest=implementation_digest,
            executable=False,
            content=content,
        ),
    )
    materialization_digest = _materialization_digest(
        "fixture-pack",
        artifact_digest,
        "fixture-pack.entry",
        implementation_digest,
        "runtime/entry.py",
        files,
    )
    return MaterializedPackArtifact(
        pack_id="fixture-pack",
        artifact_digest=artifact_digest,
        function_id="fixture-pack.entry",
        implementation_digest=implementation_digest,
        implementation_path="runtime/entry.py",
        materialization_digest=materialization_digest,
        root_device=1,
        root_inode=1,
        files=files,
    )


def test_packvm_lifecycle_exposes_only_its_verified_backend_registration(
    tmp_path: Path,
) -> None:
    """Production capture reuses the lifecycle-owned provisioner facts."""

    facts = object()

    class Provisioner:
        state_path = tmp_path / "packvm-vz-attestation.json"

        def prepare_direct_vz(self) -> object:
            return facts

    lifecycle = PackVMLifecycleV4(provisioner=Provisioner())  # type: ignore[arg-type]

    assert lifecycle.production_backend_registration() is facts


def test_failed_cleanup_binding_can_be_retried() -> None:
    cleanup_id = "11111111-1111-1111-1111-111111111111"
    operations = {
        cleanup_id: {"operation_kind": "cleanup", "state": "failed"},
    }

    assert _cleanup_binding_is_retryable(
        operations, cleanup_id, "22222222-2222-2222-2222-222222222222"
    )
    operations[cleanup_id]["state"] = "running"
    assert not _cleanup_binding_is_retryable(
        operations, cleanup_id, "22222222-2222-2222-2222-222222222222"
    )


@pytest.fixture
def provisioner_fixture(tmp_path: Path) -> tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path]:
    """Build tiny verified inputs without a VM image download or VZ helper."""

    assets = tmp_path / "assets"
    assets.mkdir(mode=0o700)
    helper = _private_file(assets / "tobkiri-packvm-vz-helper", b"test-helper", 0o700)
    runner = _private_file(assets / "runner.py", b"print('runner')\n", 0o444)
    service = _private_file(
        assets / "guest_service_template.v1.json", b'{"service":"test"}\n', 0o444
    )
    bubblewrap = _private_file(assets / "bubblewrap_arm64.deb", b"deb", 0o444)
    descriptor = _private_file(
        assets / "bubblewrap_descriptor.v1.json", b'{"descriptor":"test"}\n', 0o444
    )
    cloud = _private_file(assets / "cloud-init.yaml", b"#cloud-config\n", 0o444)
    base = _private_file(tmp_path / "verified-base.raw", b"raw-efi-image")
    manifest = MacOSVZAssetManifest(
        helper_path=helper,
        helper_digest=_digest(helper.read_bytes()),
        helper_bundle_id="dev.tobkiri.launcher.packvm-vz-helper",
        helper_team_id="ABCDEFGHIJ",
        helper_signing_identity="Developer ID Application: Test (ABCDEFGHIJ)",
        agent_path=runner,
        agent_digest=_digest(runner.read_bytes()),
        guest_service_path=service,
        guest_service_digest=_digest(service.read_bytes()),
        bubblewrap_path=bubblewrap,
        bubblewrap_digest=_digest(bubblewrap.read_bytes()),
        bubblewrap_descriptor_path=descriptor,
        bubblewrap_descriptor_digest=_digest(descriptor.read_bytes()),
        config_path=cloud,
        config_digest=_digest(cloud.read_bytes()),
        image_source="https://example.invalid/direct.raw",
        image_digest=_file_digest(base),
        image_sha512=None,
        image_size_bytes=VZ_RAW_EFI_IMAGE_DECLARED_BYTES,
        architecture="aarch64-apple-darwin",
        manifest_digest=_digest(b"fixture"),
    )

    def prepare_efi(_root: Path, domain: str, path: Path, _key: bytes) -> dict[str, object]:
        _private_file(path, b"efi-store")
        metadata = path.stat()
        return {
            "domain_id": domain,
            "state": "prepared",
            "path": str(path),
            "digest": _file_digest(path),
            "device": str(metadata.st_dev),
            "inode": str(metadata.st_ino),
        }

    provisioner = MacOSVZProvisioner(
        state_dir=(tmp_path / "state").resolve(),
        platform_system="darwin",
        machine="arm64",
        clone_file=shutil.copyfile,
        efi_store_preparer=prepare_efi,
        helper_identity_verifier=lambda _manifest: (True, None),
    )
    state = {
        "base_image_path": str(base),
        "image_digest": manifest.image_digest,
        "attestation_digest": _digest(b"attestation"),
    }
    provisioner._require_manifest = lambda: manifest  # type: ignore[method-assign]
    provisioner._load_state = lambda: state  # type: ignore[method-assign]
    provisioner._verify_state_bindings = (  # type: ignore[method-assign]
        lambda _state, _manifest: None
    )
    return provisioner, manifest, base


def test_operation_gate_adopts_only_an_exact_stale_owner_claim(
    provisioner_fixture: tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioner, _manifest, _base = provisioner_fixture
    binding = {
        "session_digest": _digest(b"session"),
        "plan_digest": _digest(b"plan"),
        "ceremony_nonce_digest": _digest(b"nonce"),
    }
    provisioner.mutation_claim_path.parent.mkdir(parents=True, mode=0o700)
    _private_file(
        provisioner.mutation_claim_path,
        json.dumps(
            {
                "version": 1,
                "operation": "provision",
                "instance": "tobkiri-packvm-v4",
                "owner_pid": 99_999_999,
                "binding": binding,
            }
        ).encode(),
    )
    monkeypatch.setattr(macos_vz_provisioner, "_process_is_alive", lambda _pid: False)

    with provisioner.operation_gate("provision", binding, recover_claim=True):
        claim = json.loads(provisioner.mutation_claim_path.read_text())
        assert claim["owner_pid"] == os.getpid()

    assert not provisioner.mutation_claim_path.exists()


def test_legacy_empty_recovery_root_is_bound_before_cleanup(
    provisioner_fixture: tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path],
) -> None:
    provisioner, _manifest, _base = provisioner_fixture
    root = provisioner.mutation_claim_path.parent / "instances" / "tobkiri-packvm-v4"
    root.mkdir(parents=True, mode=0o700)
    recovery = {"instance_root": str(root)}

    bound = provisioner._bind_legacy_empty_recovery_root(root, recovery)

    assert bound["instance_root_device"] == root.stat().st_dev
    assert bound["instance_root_inode"] == root.stat().st_ino


def test_allocate_creates_per_domain_cow_efi_and_seeds(
    provisioner_fixture: tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path],
) -> None:
    """Each allocation owns generated keys/seeds and cleans all mutable files."""

    provisioner, manifest, base = provisioner_fixture
    artifact = _materialized_artifact()
    allocation = provisioner.allocate(
        domain_id="domain.conversation",
        reservation_id="reservation-1",
        lease_id="lease-1",
        channel_key=b"k" * 32,
        artifact_digest=artifact.artifact_digest,
        executable_digest=artifact.implementation_digest,
        materialization_digest=artifact.materialization_digest,
        artifact=artifact,
    )

    root = Path(allocation.run_root)
    assert Path(allocation.cow_disk_path).read_bytes() == base.read_bytes()
    assert Path(allocation.cow_disk_path).stat().st_size == base.stat().st_size
    assert allocation.cow_disk_digest == manifest.image_digest
    assert Path(allocation.efi_store_path).is_file()
    assert Path(allocation.agent_seed_path).stat().st_size % 512 == 0
    assert Path(allocation.config_seed_path).stat().st_size % 512 == 0
    agent_seed = Path(allocation.agent_seed_path).read_bytes()
    config_seed = Path(allocation.config_seed_path).read_bytes()
    assert b"guest_service_template.v1.json;1" in agent_seed
    assert b"bubblewrap_descriptor.v1.json;1" in agent_seed
    assert b"bubblewrap_arm64.deb;1" in agent_seed
    assert b"agent-config.json;1" in config_seed
    assert b"agent-ed25519.pem;1" in config_seed
    assert b"artifact-seed.v1.bin;1" in config_seed
    assert b"tobkiri-packvm-artifact-seed.v1\0" in config_seed
    assert b"BEGIN PRIVATE KEY" in config_seed
    assert artifact.artifact_digest in config_seed.decode("latin1")
    assert allocation.guest_public_key_digest in config_seed.decode("latin1")
    assert not (root / "artifact-seed.v1.bin").exists()
    assert not (root / "agent-ed25519.pem").exists()
    assert not (root / "agent-config.json").exists()

    provisioner.release(allocation)
    assert not root.exists()


def test_prepare_declares_three_gib_download_without_downloading(
    provisioner_fixture: tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan asks for explicit consent and capacity before fetching 3 GiB."""

    provisioner, manifest, _base = provisioner_fixture
    monkeypatch.setattr(provisioner, "_load_manifest_for_plan", lambda: (manifest, None))
    monkeypatch.setattr(provisioner.image_cache, "status", lambda _authority: ("absent", None))
    monkeypatch.setattr(
        provisioner,
        "_disk_usage",
        lambda _path: SimpleNamespace(free=20 * 1024 * 1024 * 1024),
    )

    plan = provisioner.prepare()

    assert plan.image_download_required is True
    assert plan.image_download_bytes == VZ_RAW_EFI_IMAGE_DECLARED_BYTES
    assert plan.image_size_bytes == VZ_RAW_EFI_IMAGE_DECLARED_BYTES
    assert plan.disk_size_bytes == VZ_RAW_EFI_IMAGE_DECLARED_BYTES
    assert plan.host_free_space_required_bytes == (
        2 * VZ_RAW_EFI_IMAGE_DECLARED_BYTES
        + macos_vz_provisioner.VZ_HOST_STORAGE_RESERVE_BYTES
        + macos_vz_provisioner.VZ_ARTIFACT_SEED_PEAK_RESERVE_BYTES
    )


def test_prepare_reserves_exact_raw_cow_size_when_the_image_is_cached(
    provisioner_fixture: tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified 3 GiB raw image gets a 3 GiB clone, never a 4 GiB sparse disk."""

    provisioner, manifest, _base = provisioner_fixture
    required = (
        VZ_RAW_EFI_IMAGE_DECLARED_BYTES
        + macos_vz_provisioner.VZ_HOST_STORAGE_RESERVE_BYTES
        + macos_vz_provisioner.VZ_ARTIFACT_SEED_PEAK_RESERVE_BYTES
    )
    monkeypatch.setattr(provisioner, "_load_manifest_for_plan", lambda: (manifest, None))
    monkeypatch.setattr(
        provisioner.image_cache,
        "status",
        lambda _authority: ("verified_source", None),
    )
    monkeypatch.setattr(
        provisioner,
        "_disk_usage",
        lambda _path: SimpleNamespace(free=required),
    )

    plan = provisioner.prepare()

    assert plan.launcher_reason is None
    assert plan.image_download_required is False
    assert plan.disk_size_bytes == VZ_RAW_EFI_IMAGE_DECLARED_BYTES
    assert plan.host_free_space_required_bytes == required


def test_allocate_rechecks_exact_artifact_peak_capacity_before_mutation(
    provisioner_fixture: tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid large seed cannot fail midway after a stale generic preflight."""

    provisioner, _manifest, _base = provisioner_fixture
    artifact = _materialized_artifact("capacity")
    required = provisioner._required_allocation_space(artifact)
    monkeypatch.setattr(
        provisioner,
        "_disk_usage",
        lambda _path: SimpleNamespace(free=required - 1),
    )

    with pytest.raises(ValueError, match="provisioning requires at least"):
        provisioner.allocate(
            domain_id="domain.capacity",
            reservation_id="reservation-capacity",
            lease_id="lease-capacity",
            channel_key=b"k" * 32,
            artifact_digest=artifact.artifact_digest,
            executable_digest=artifact.implementation_digest,
            materialization_digest=artifact.materialization_digest,
            artifact=artifact,
        )

    domains = provisioner._state_dir / "domains"
    assert not domains.exists()


def test_direct_terminate_keeps_domain_owned_until_stop_and_diagnostics_close() -> None:
    """Binding failures and VZ stop failures must leave termination retryable."""

    workspace = Path(__file__).resolve().parents[2]
    source = (
        workspace
        / "tobkiri_launcher"
        / "packvm-vz-helper"
        / "Sources"
        / "PackVMVZCore"
        / "VZSupervisor.swift"
    ).read_text(encoding="utf-8")
    body = source.split("public func directTerminate(", 1)[1].split(
        "public func invoke(", 1
    )[0]

    assert body.index("activeDirectDomain(domainID)") < body.index(
        "DOMAIN_BINDING_MISMATCH"
    )
    assert body.index("DOMAIN_BINDING_MISMATCH") < body.index("try stop(")
    assert body.index("try stop(") < body.index("domain.diagnostics.close()")
    assert body.index("domain.diagnostics.close()") < body.index(
        "removeDirectDomain(domainID)"
    )


def test_instance_creation_compares_descriptor_bare_sha512(
    provisioner_fixture: tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path],
) -> None:
    """The image descriptor's SHA-512 value has no ``sha512:`` prefix."""

    provisioner, manifest, base = provisioner_fixture
    metadata = base.stat()
    image = PackVMPinnedImage(
        verified=PackVMVerifiedImage(
            path=base,
            digest=manifest.image_digest,
            size_bytes=metadata.st_size,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            source_url=manifest.image_source,
        ),
        descriptor=-1,
    )
    with_sha512 = replace(
        manifest,
        image_sha512=hashlib.sha512(base.read_bytes()).hexdigest(),
    )

    root = provisioner._state_dir / "sha512-instance"
    provisioner._ensure_state_root()
    recovery = {
        "version": macos_vz_provisioner.VZ_STATE_VERSION,
        "backend_id": macos_vz_provisioner.PACKVM_BACKEND_ID,
        "instance": macos_vz_provisioner.VZ_INSTANCE,
        "instance_root": str(root),
        **provisioner.recovery_identity(),
    }
    macos_vz_provisioner._atomic_private_json(
        provisioner.recovery_path,
        provisioner._signed_recovery(recovery),
    )

    provisioner._create_instance(
        root,
        image,
        with_sha512,
        None,
    )


def test_doctor_reports_bounded_reason_before_provisioning(tmp_path: Path) -> None:
    """A fresh install reports not-provisioned without exposing its host path."""

    state_dir = (tmp_path / "private-user-state" / "packvm-vz").resolve()
    provisioner = MacOSVZProvisioner(
        state_dir=state_dir,
        platform_system="darwin",
        machine="arm64",
    )

    doctor = provisioner.doctor()

    assert doctor.ready is False
    assert doctor.reason == "PackVM VZ has not completed authenticated provisioning"
    assert str(tmp_path) not in str(doctor.reason)


def test_seed_and_template_tampering_are_rejected_separately(
    provisioner_fixture: tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path],
) -> None:
    """Generated CIDATA and immutable cloud-init are independently measured."""

    provisioner, manifest, base = provisioner_fixture
    artifact = _materialized_artifact("first")
    allocation = provisioner.allocate(
        domain_id="domain.first",
        reservation_id="reservation-1",
        lease_id="lease-1",
        channel_key=b"k" * 32,
        artifact_digest=artifact.artifact_digest,
        executable_digest=artifact.implementation_digest,
        materialization_digest=artifact.materialization_digest,
        artifact=artifact,
    )
    driver = MacOSVZSupervisorDriver(
        transport_factory=lambda _allocation: None,
        helper_path=manifest.helper_path,
        helper_identity=MacOSVZHelperIdentity(
            binary_digest=manifest.helper_digest,
            code_digest=manifest.helper_digest,
            bundle_id=manifest.helper_bundle_id,
            team_id=manifest.helper_team_id,
            signing_identity=manifest.helper_signing_identity,
        ),
        launch_assets=MacOSVZLaunchAssets(
            base_image_digest=_file_digest(base),
            base_image_path=str(base),
            agent_template_digest=manifest.agent_digest,
            config_template_digest=manifest.config_digest,
            base_image_read_only=True,
        ),
        agent_identity=MacOSVZAgentIdentity(agent_digest=manifest.agent_digest),
        domain_allocator=None,
    )
    driver._verify_launch_assets(allocation)
    config_seed = Path(allocation.config_seed_path)
    _private_file(config_seed, config_seed.read_bytes() + b"tampered")
    with pytest.raises(BackendUnavailableError, match="config seed digest mismatch"):
        driver._verify_launch_assets(allocation)

    provisioner.release(allocation)
    manifest.config_path.chmod(0o600)
    _private_file(manifest.config_path, b"#cloud-config\ntampered\n", 0o444)
    with pytest.raises(ValueError, match="asset changed"):
        second = _materialized_artifact("second")
        provisioner.allocate(
            domain_id="domain.second",
            reservation_id="reservation-2",
            lease_id="lease-2",
            channel_key=b"l" * 32,
            artifact_digest=second.artifact_digest,
            executable_digest=second.implementation_digest,
            materialization_digest=second.materialization_digest,
            artifact=second,
        )


def test_image_descriptor_rejects_redirecting_debian_origin(tmp_path: Path) -> None:
    """The downloader only accepts the fixed direct-200 source URL."""

    descriptor = _private_file(
        tmp_path / "image.json",
        (
            b'{"schema":"io.tobkiri.packvm-vz-image-descriptor.v1",'
            b'"boot_mode":"efi","architecture":"arm64","format":"raw",'
            b'"source":{"url":"https://cloud.debian.org/redirect.raw",'
            b'"size_bytes":3221225472,'
            b'"sha256":"sha256:9440bc19285b9e0ccb217fd5ac818a253a3c0bfd46c9ac83241959c78f90ad71",'
            b'"sha512":"f21843e29eade9747b1b7bb7d9622c30613eb3d875fbb6a7f9bd76acaadfdbfe0ef68137da4eb7520e440a6cd3bbb248db41aa322f58d11e71fea667eb569a2c"},'
            b'"license":{"spdx_id":"LicenseRef-Debian-Distribution",'
            b'"url":"https://www.debian.org/legal/licenses/"}}'
        ),
        0o444,
    )

    with pytest.raises(ValueError, match="image descriptor"):
        _parse_image_descriptor(descriptor)


def test_authenticated_bundle_binding_rehashes_both_resource_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each manifest load rejects a post-launch resource-manifest replacement."""

    provisioner, provisioning_path, _resources, binding = _bound_bundle_fixture(tmp_path)
    sentinel = object()
    monkeypatch.setattr(provisioner, "_parse_provisioning_manifest", lambda *_args: sentinel)
    monkeypatch.setattr(provisioner, "_verify_helper_identity", lambda _manifest: (True, None))

    assert provisioner._require_manifest() is sentinel
    provisioning_path.chmod(0o600)
    _private_file(provisioning_path, b'{"replaced":true}', 0o444)

    with pytest.raises(ValueError, match="bundle binding changed"):
        provisioner._require_manifest()

    assert binding.provisioning_sha256 != _file_digest(provisioning_path)


def test_authenticated_bundle_binding_is_the_expected_helper_team_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mutable helper manifest cannot select a different Developer ID team."""

    provisioner, provisioning_path, resources, binding = _bound_bundle_fixture(tmp_path)
    monkeypatch.setattr(
        macos_vz_provisioner,
        "_macho_code_digest",
        lambda _path: _digest(b"helper-code"),
    )

    parsed = provisioner._parse_bundle_helper_manifest(resources, provisioning_path)
    assert parsed["helper_team_id"] == binding.helper_team_id

    mismatched_binding = SimpleNamespace(
        root=binding.root,
        provisioning_sha256=binding.provisioning_sha256,
        helper_manifest_sha256=binding.helper_manifest_sha256,
        helper_team_id="KLMNOPQRST",
    )
    mismatched = MacOSVZProvisioner(
        state_dir=(tmp_path / "mismatched-state").resolve(),
        bundle_binding=mismatched_binding,
        platform_system="darwin",
        machine="arm64",
    )
    with pytest.raises(ValueError, match="Team ID binding changed"):
        mismatched._parse_bundle_helper_manifest(resources, provisioning_path)


def test_authenticated_bundle_accepts_exact_ad_hoc_helper_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSS release helpers carry no Team ID or certificate authority."""

    provisioner, provisioning_path, resources, binding = _bound_bundle_fixture(
        tmp_path, signing_mode="ad-hoc"
    )
    monkeypatch.setattr(
        macos_vz_provisioner,
        "_macho_code_digest",
        lambda _path: _digest(b"helper-code"),
    )

    parsed = provisioner._parse_bundle_helper_manifest(resources, provisioning_path)
    assert parsed["helper_team_id"] == ""
    assert parsed["helper_signing_identity"] == ""
    assert binding.helper_team_id == ""


def _bound_bundle_fixture(
    tmp_path: Path,
    *,
    signing_mode: str = "developer-id",
) -> tuple[MacOSVZProvisioner, Path, Path, SimpleNamespace]:
    """Build only the two Launcher-attested resource manifests for a unit test."""

    root = tmp_path / "Tobkiri Launcher.app"
    resources = root / "Contents" / "Resources"
    helper_path = root / "Contents" / "MacOS" / "tobkiri-packvm-vz-helper"
    resources.mkdir(parents=True)
    helper_path.parent.mkdir()
    _private_file(helper_path, b"helper", 0o700)
    provisioning_path = _private_file(
        resources / "packvm-vz-provisioning.v1.json",
        json.dumps(
            {
                "schema": VZ_ASSET_MANIFEST_SCHEMA,
                "target": "aarch64-apple-darwin",
                "boot_mode": "efi",
                "inputs": [],
            }
        ).encode("utf-8"),
        0o444,
    )
    team_id = "ABCDEFGHIJ" if signing_mode == "developer-id" else ""
    signing = (
        {
            "signing_mode": "developer-id",
            "team_id": team_id,
            "authority": f"Developer ID Application: Tobkiri ({team_id})",
        }
        if signing_mode == "developer-id"
        else {"signing_mode": "ad-hoc", "team_id": None, "authority": None}
    )
    helper_manifest = {
        "schema": VZ_BUNDLE_MANIFEST_SCHEMA,
        "helper": {
            "path": "Contents/MacOS/tobkiri-packvm-vz-helper",
            "code_sha256": _digest(b"helper-code"),
            "identifier": "dev.tobkiri.launcher.packvm-vz-helper",
            "entitlements": ["com.apple.security.virtualization"],
            "signing": signing,
        },
        "provisioning": {
            "path": "Contents/Resources/packvm-vz-provisioning.v1.json",
            "sha256": _file_digest(provisioning_path),
        },
    }
    helper_manifest_path = _private_file(
        resources / "packvm-vz-helper.manifest.v1.json",
        json.dumps(helper_manifest, sort_keys=True).encode("utf-8"),
        0o444,
    )
    binding = SimpleNamespace(
        root=root,
        provisioning_sha256=_file_digest(provisioning_path),
        helper_manifest_sha256=_file_digest(helper_manifest_path),
        helper_team_id=team_id,
    )
    return (
        MacOSVZProvisioner(
            state_dir=(tmp_path / "state").resolve(),
            bundle_binding=binding,
            platform_system="darwin",
            machine="arm64",
        ),
        provisioning_path,
        resources,
        binding,
    )


def test_transport_requires_explicit_fd_key_binding_before_exchange() -> None:
    """A domain helper cannot accept an outer request before enrollment."""

    process = _helper_process_for_test(b'{"ok":true}\n')
    request = {
        "operation": "launch",
        "domain_id": "domain.test",
        "launch_binding_digest": _digest(b"launch"),
    }
    with pytest.raises(ValueError, match="transport binding"):
        process.exchange(request)

    process.enroll_launch_secret(
        domain_id="domain.test",
        host_nonce="a" * 64,
        launch_binding_digest=_digest(b"launch"),
        secret=b"k" * 32,
    )
    assert process.exchange(request) == {"ok": True}


def test_transport_accepts_one_mebibyte_protocol_lines_not_state_limit() -> None:
    """Helper protocol frames may carry bounded bridge payloads above 128 KiB."""

    valid = json.dumps({"payload": "x" * (200 * 1024)}).encode() + b"\n"
    process = _helper_process_for_test(valid)
    assert len(process._exchange_line({"request": "ok"})["payload"]) == 200 * 1024

    oversized = b"x" * (1024 * 1024 + 1)
    with pytest.raises(ValueError, match="response exceeds"):
        _helper_process_for_test(oversized)._exchange_line({"request": "ok"})


def test_lifecycle_ignores_ambient_limactl_and_reports_direct_vz_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PATH-visible Lima install never changes production lifecycle selection."""

    limactl = _private_file(tmp_path / "limactl", b"#!/bin/sh\nexit 99\n", 0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(
        macos_vz_provisioner,
        "_default_state_dir",
        lambda: tmp_path / "direct-vz-state",
    )
    monkeypatch.setattr(
        macos_vz_provisioner,
        "_packaged_packvm_bundle_binding",
        lambda: None,
    )

    lifecycle = PackVMLifecycleV4(macos_vz_provisioner.default_packvm_provisioner())

    assert shutil.which("limactl") == str(limactl)
    assert isinstance(lifecycle._provisioner, MacOSVZProvisioner)
    plan = lifecycle.prepare()
    assert plan["limactl"] is None
    assert "macOS VZ" in str(plan["launcher_reason"])
    readiness = lifecycle.readiness_snapshot()
    assert readiness["ready"] is False
    assert readiness["platform"] == "macos-arm64"
    assert "PackVM VZ" in str(readiness["reason"])
    assert _authenticated_packvm_backend(lifecycle) is None


def test_explicit_lima_injection_stays_out_of_production_composition(
    tmp_path: Path,
) -> None:
    """Lima remains injectable for tests but cannot become a production backend."""

    invocation_marker = tmp_path / "limactl-invoked"
    limactl = _private_file(
        tmp_path / "limactl",
        f"#!/bin/sh\ntouch {invocation_marker}\nexit 0\n".encode(),
        0o700,
    )
    lima = PackVMLimaProvisioner(
        command_path=str(limactl),
        state_dir=tmp_path / "lima-state",
        machine="arm64",
    )
    lifecycle = PackVMLifecycleV4(provisioner=lima)

    assert lifecycle._provisioner is lima
    assert _authenticated_packvm_backend(lifecycle) is None
    assert not invocation_marker.exists()


def test_development_default_accepts_only_the_launcher_selected_app_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "Tobkiri Launcher Dev.app"
    manifest = bundle / "Contents/Resources/packvm-vz-provisioning.v1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("RUMI_ENVIRONMENT", "development")
    monkeypatch.setenv("TOBKIRI_DEVELOPMENT_PACKVM_BUNDLE_ROOT", str(bundle))

    lifecycle = PackVMLifecycleV4(
        provisioner=macos_vz_provisioner.default_packvm_provisioner()
    )

    assert lifecycle._provisioner._bundle_root == bundle.resolve()
    assert lifecycle._provisioner._asset_manifest_path == manifest.resolve()


def test_production_ignores_development_packvm_bundle_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RUMI_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "TOBKIRI_DEVELOPMENT_PACKVM_BUNDLE_ROOT",
        str(tmp_path / "Untrusted.app"),
    )

    lifecycle = PackVMLifecycleV4(
        provisioner=macos_vz_provisioner.default_packvm_provisioner()
    )

    assert lifecycle._provisioner._bundle_root is None


def _helper_process_for_test(response: bytes) -> _MacOSVZHelperProcess:
    """Construct a process-free transport shell for framing tests."""

    instance = object.__new__(_MacOSVZHelperProcess)
    instance._process = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=_Readline(response),
        poll=lambda: None,
    )
    instance._key = bytearray(b"k" * 32)
    instance._lock = threading.RLock()
    instance._closed = False
    instance._domain_id = None
    instance._launch_binding_digest = None
    return instance


class _Readline:
    """Bound-aware binary stdout fixture for a helper process."""

    def __init__(self, response: bytes) -> None:
        self._response = response

    def readline(self, _limit: int) -> bytes:
        return self._response
