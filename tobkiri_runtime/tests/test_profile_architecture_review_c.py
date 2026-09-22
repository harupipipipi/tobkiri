"""Review-C regressions for versioning, artifacts, scopes, and provenance."""

from __future__ import annotations

import copy
import hashlib
import json
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.profile_capture import (
    _bundle_root,
    _verify_installed_bundle_binding,
)
from ecosystem.defaultspack.domain.runtime_v4 import (
    ActivationStore,
    BundledCatalog,
    ProfileReconfirmationRequired,
    ProfileResolutionDenied,
    resolve_default_profile,
)
from tests.conformance_support.packaged_profile import (
    build_packaged_profile_bundle,
    create_test_source_provenance,
)
from tobkiri_protocol.canonical import canonical_digest, canonical_json
from tobkiri_protocol.profile_scope import normalize_requested_scope_template
from tobkiri_protocol.platform_artifact import artifact_digest, verify_platform_artifact
from tobkiri_protocol import platform_artifact
from tobkiri_protocol.provenance import (
    informational_source_commit,
    normative_generated_provenance,
    trusted_source_commit,
)
from scripts import generate_defaultspack_v4_bundle
from scripts.generate_packaged_defaultspack_v4_bundle import stage_packaged_bundle

ROOT = Path(__file__).resolve().parents[1]
SOURCE_BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"
FROZEN = Path(__file__).parent / "fixtures/profile_v4/pre-e853-activation.json"
SOURCE_COMMIT = subprocess.run(
    ["git", "rev-parse", "--verify", "HEAD^{commit}"],
    cwd=ROOT.parent,
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
assert len(SOURCE_COMMIT) == 40 and all(
    character in "0123456789abcdef" for character in SOURCE_COMMIT
)
SOURCE_TREE = subprocess.run(
    ["git", "rev-parse", "--verify", "HEAD^{tree}"],
    cwd=ROOT.parent,
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
assert len(SOURCE_TREE) == 40 and all(
    character in "0123456789abcdef" for character in SOURCE_TREE
)


def _edge_key(edge: dict[str, object]) -> str:
    return "|".join(
        str(edge[key])
        for key in (
            "caller_function_id",
            "target_provider_id",
            "contract_id",
            "operation_id",
        )
    )


def _packaged_catalog(tmp_path: Path) -> BundledCatalog:
    provenance = create_test_source_provenance(
        ROOT,
        tmp_path,
        provenance_record={
            "source_commit": SOURCE_COMMIT,
            "source_tree": SOURCE_TREE,
            "source_clean": True,
        },
    )
    bundle = build_packaged_profile_bundle(
        SOURCE_BUNDLE,
        tmp_path,
        source_provenance_file=provenance,
    )
    return BundledCatalog.load(bundle)


def _packaged_catalog_revision(tmp_path: Path, marker: bytes) -> BundledCatalog:
    """Build one valid packaged catalog with a distinct Shell identity."""

    bundle = tmp_path / "defaultspack" / "v4"
    artifacts = tmp_path / "defaultspack" / "platform-artifacts"
    executable = tmp_path / "verified-release" / "Tobkiri.AppImage"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 10 + b">\x00fixture-" + marker)
    executable.chmod(0o755)
    shutil.copytree(SOURCE_BUNDLE, bundle)
    provenance = create_test_source_provenance(
        ROOT,
        tmp_path,
        provenance_record={
            "source_commit": SOURCE_COMMIT,
            "source_tree": SOURCE_TREE,
            "source_clean": True,
        },
    )
    stage_packaged_bundle(
        source_artifact=executable,
        bundle_root=bundle,
        artifact_root=artifacts,
        relative_path="Tobkiri.AppImage",
        entrypoint="Tobkiri.AppImage",
        platform="linux",
        architecture="x86_64",
        bundle_identity="io.tobkiri.shell.tauri",
        source_provenance_file=provenance,
    )
    return BundledCatalog.load(bundle)


def _resolve(catalog: BundledCatalog):
    profile = catalog.profiles["defaults"]
    return resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests={
            str(manifest["pack"]["artifact_digest"]) for manifest in catalog.packs.values()
        },
        authority_snapshot_digest="sha256:" + "9" * 64,
        authority_bindings={
            _edge_key(edge): "authority-ref:test."
            + canonical_digest(_edge_key(edge)).removeprefix("sha256:")
            for edge in profile["requested_edges"]
        },
        security_epoch=1,
    )


def _macos_artifact(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    artifact_root = tmp_path / "artifacts"
    application = artifact_root / "Tobkiri.app"
    executable = application / "Contents" / "MacOS" / "tobkiri-shell"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01fixture")
    executable.chmod(0o755)
    (application / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "io.tobkiri.shell.tauri"})
    )
    return artifact_root, {
        "relative_path": "Tobkiri.app",
        "entrypoint": "Tobkiri.app/Contents/MacOS/tobkiri-shell",
        "platform": "macos",
        "architecture": "arm64",
        "bundle_identity": "io.tobkiri.shell.tauri",
        "artifact_digest": artifact_digest(application),
        "entrypoint_digest": "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest(),
    }


def test_source_checkout_profile_is_explicitly_unavailable() -> None:
    catalog = BundledCatalog.load(SOURCE_BUNDLE)
    assert catalog.shells["shell.tauri.default"]["availability"] == "build_required"
    assert catalog.shells["shell.tauri.default"]["launch"]["variants"] == []
    with pytest.raises(ProfileResolutionDenied, match="Shell artifact is unavailable"):
        _resolve(catalog)


def test_packaged_artifact_resolves_activates_and_restarts(tmp_path: Path) -> None:
    catalog = _packaged_catalog(tmp_path)
    resolved = _resolve(catalog)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    store = ActivationStore(
        tmp_path / "state",
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=catalog,
    )
    activation = store.activate(
        resolved,
        activation_id="activation:defaults-packaged",
        created_at="2026-08-10T00:00:00Z",
    )
    assert activation["activation_api_version"] == "io.tobkiri.activation-record.v2"
    assert store.load_active_snapshot().resolved.plan == resolved.plan


def test_activation_and_restart_reverify_selected_executable(tmp_path: Path) -> None:
    catalog = _packaged_catalog(tmp_path)
    resolved = _resolve(catalog)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    store = ActivationStore(
        tmp_path / "state",
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=catalog,
    )
    variant = catalog.shells["shell.tauri.default"]["launch"]["variants"][0]
    executable = catalog.artifact_root / str(variant["entrypoint"])
    executable.write_bytes(executable.read_bytes() + b"pre-activation-tamper")
    with pytest.raises(ProfileResolutionDenied, match="artifact rejected"):
        store.activate(
            resolved,
            activation_id="activation:defaults-final-tamper",
            created_at="2026-08-10T00:00:00Z",
        )

    catalog = _packaged_catalog(tmp_path / "restart")
    resolved = _resolve(catalog)
    restart_workspace = tmp_path / "restart-workspace"
    restart_workspace.mkdir()
    restart_authority = AuthorityStore(tmp_path / "restart-authority.sqlite3")
    restart_store = ActivationStore(
        tmp_path / "restart-state",
        restart_workspace,
        profile_id="defaults",
        authority=restart_authority,
        catalog=catalog,
    )
    restart_store.activate(
        resolved,
        activation_id="activation:defaults-restart-tamper",
        created_at="2026-08-10T00:00:00Z",
    )
    variant = catalog.shells["shell.tauri.default"]["launch"]["variants"][0]
    executable = catalog.artifact_root / str(variant["entrypoint"])
    executable.write_bytes(executable.read_bytes() + b"restart-tamper")
    with pytest.raises(ProfileResolutionDenied, match="artifact rejected"):
        restart_store.load_active_snapshot()


def test_relocated_packaged_artifact_preserves_exact_active_identity(
    tmp_path: Path,
) -> None:
    catalog = _packaged_catalog_revision(tmp_path / "installed", b"same-release")
    resolved = _resolve(catalog)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=catalog,
    ).activate(
        resolved,
        activation_id="activation:defaults-before-relocation",
        created_at="2026-08-12T00:00:00Z",
    )

    relocated = tmp_path / "relocated" / "defaultspack"
    shutil.copytree(catalog.root.parent, relocated)
    relocated_catalog = BundledCatalog.load(relocated / "v4")
    restarted = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=relocated_catalog,
    ).load_active_snapshot()

    assert restarted.resolved == resolved
    assert relocated_catalog.artifact_root == (relocated / "platform-artifacts").resolve()


def test_valid_active_artifact_revision_requires_explicit_reconfirmation(
    tmp_path: Path,
) -> None:
    predecessor_catalog = _packaged_catalog_revision(tmp_path / "predecessor", b"predecessor")
    successor_catalog = _packaged_catalog_revision(tmp_path / "successor", b"successor")
    predecessor = _resolve(predecessor_catalog)
    successor = _resolve(successor_catalog)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=predecessor_catalog,
    ).activate(
        predecessor,
        activation_id="activation:defaults-predecessor-artifact",
        created_at="2026-08-12T00:00:00Z",
    )
    pointer_before = (state / "active.json").read_bytes()
    predecessor_envelope = next((state / "activations").iterdir())
    predecessor_bytes = predecessor_envelope.read_bytes()
    store = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=successor_catalog,
    )

    with pytest.raises(
        ProfileReconfirmationRequired,
        match="artifact identity was superseded",
    ):
        store.load_active_snapshot()
    assert (state / "active.json").read_bytes() == pointer_before
    assert predecessor_envelope.read_bytes() == predecessor_bytes
    assert len(tuple((state / "activations").iterdir())) == 1

    store.reconcile_active(
        successor,
        activation_id="activation:defaults-confirmed-artifact",
        created_at="2026-08-12T00:01:00Z",
    )
    restarted = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=successor_catalog,
    ).load_active_snapshot()
    assert restarted.resolved == successor
    assert restarted.activation["activation_id"] == ("activation:defaults-confirmed-artifact")
    assert predecessor_envelope.read_bytes() == predecessor_bytes


def test_capture_flow_reconfirms_valid_artifact_successor_and_persists_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_runtime.bootstrap import profile_capture

    predecessor_catalog = _packaged_catalog_revision(tmp_path / "predecessor", b"predecessor")
    successor_catalog = _packaged_catalog_revision(tmp_path / "successor", b"successor")
    predecessor = _resolve(predecessor_catalog)
    successor = _resolve(successor_catalog)
    user_data = tmp_path / "user-data"
    workspace = user_data / "workspaces" / "defaults"
    workspace.mkdir(parents=True)
    state = workspace / "activation"
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        ActivationStore(
            state,
            workspace,
            profile_id="defaults",
            authority=authority,
            catalog=predecessor_catalog,
        ).activate(
            predecessor,
            activation_id="activation:defaults-capture-predecessor",
            created_at="2026-08-12T00:00:00Z",
        )
    confirmation = {
        "operation_id": "defaults.activate",
        "confirmation_digest": "sha256:" + "a" * 64,
    }
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setattr(
        profile_capture,
        "_bundle_root",
        lambda _base=None: successor_catalog.root,
    )
    monkeypatch.setattr(
        profile_capture,
        "_resolve_candidate",
        lambda **_kwargs: (successor, confirmation),
    )

    with pytest.raises(ProfileReconfirmationRequired, match="superseded"):
        profile_capture.capture_default_profile()
    with pytest.raises(ProfileResolutionDenied, match="stale or tampered"):
        profile_capture.capture_default_profile(
            confirmation={**confirmation, "operation_id": "attacker.activate"}
        )
    active = profile_capture.capture_default_profile(confirmation=confirmation)
    restarted = profile_capture.capture_default_profile()
    assert restarted == active
    assert active.resolved == successor


def test_confirmed_artifact_reconfirmation_recovers_interrupted_commit(
    tmp_path: Path,
) -> None:
    predecessor_catalog = _packaged_catalog_revision(tmp_path / "predecessor", b"predecessor")
    successor_catalog = _packaged_catalog_revision(tmp_path / "successor", b"successor")
    successor = _resolve(successor_catalog)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=predecessor_catalog,
    ).activate(
        _resolve(predecessor_catalog),
        activation_id="activation:defaults-interrupted-predecessor",
        created_at="2026-08-12T00:00:00Z",
    )
    predecessor_envelope = next((state / "activations").iterdir())
    predecessor_bytes = predecessor_envelope.read_bytes()

    def interrupt(stage: str) -> None:
        if stage == "after_authority_commit":
            raise OSError("simulated restart after Authority commit")

    with pytest.raises(OSError, match="simulated restart"):
        ActivationStore(
            state,
            workspace,
            profile_id="defaults",
            authority=authority,
            catalog=successor_catalog,
            fault=interrupt,
        ).reconcile_active(
            successor,
            activation_id="activation:defaults-interrupted-successor",
            created_at="2026-08-12T00:01:00Z",
        )

    recovered = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=successor_catalog,
    ).load_active_snapshot()
    assert recovered.resolved == successor
    assert recovered.activation["activation_id"] == ("activation:defaults-interrupted-successor")
    assert predecessor_envelope.read_bytes() == predecessor_bytes


@pytest.mark.parametrize(
    "case",
    ("stale_revision", "missing_successor", "tampered_successor", "unauthorized"),
)
def test_artifact_reconfirmation_keeps_hard_denials_fail_closed(
    tmp_path: Path,
    case: str,
) -> None:
    predecessor_catalog = _packaged_catalog_revision(tmp_path / "predecessor", b"predecessor")
    successor_catalog = _packaged_catalog_revision(tmp_path / "successor", b"successor")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=predecessor_catalog,
    ).activate(
        _resolve(predecessor_catalog),
        activation_id="activation:defaults-hard-denial",
        created_at="2026-08-12T00:00:00Z",
    )
    pointer = json.loads((state / "active.json").read_text(encoding="utf-8"))
    envelope_path = state / "activations" / pointer["envelope_path"]
    expected = ""
    selected_authority = authority
    if case == "stale_revision":
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        envelope["lock"]["profile_revision"] = "sha256:" + "f" * 64
        envelope_path.write_bytes(canonical_json(envelope) + b"\n")
        pointer["envelope_digest"] = canonical_digest(envelope)
        (state / "active.json").write_bytes(canonical_json(pointer) + b"\n")
        expected = "ProfileLock or ResolvedPlan is stale"
    elif case in {"missing_successor", "tampered_successor"}:
        variant = successor_catalog.shells["shell.tauri.default"]["launch"]["variants"][0]
        artifact = successor_catalog.artifact_root / str(variant["relative_path"])
        if case == "missing_successor":
            artifact.unlink()
        else:
            artifact.write_bytes(artifact.read_bytes() + b"tamper")
        expected = "verified successor Shell artifact rejected"
    else:
        selected_authority = AuthorityStore(tmp_path / "foreign-authority.sqlite3")
        expected = "active activation authority"
    pointer_before = (state / "active.json").read_bytes()
    envelope_before = envelope_path.read_bytes()

    with pytest.raises(ProfileResolutionDenied, match=expected) as denied:
        ActivationStore(
            state,
            workspace,
            profile_id="defaults",
            authority=selected_authority,
            catalog=successor_catalog,
        ).load_active_snapshot()
    assert not isinstance(denied.value, ProfileReconfirmationRequired)
    assert (state / "active.json").read_bytes() == pointer_before
    assert envelope_path.read_bytes() == envelope_before
    assert len(tuple((state / "activations").iterdir())) == 1


def test_production_macos_artifact_requires_valid_code_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root, variant = _macos_artifact(tmp_path)
    monkeypatch.setattr(
        platform_artifact.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "invalid"),
    )
    with pytest.raises(Exception, match="signature is invalid"):
        verify_platform_artifact(
            artifact_root,
            variant,
            require_macos_code_signature=True,
        )


def test_macos_bundle_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    artifact_root, variant = _macos_artifact(tmp_path)
    variant["bundle_identity"] = "io.tobkiri.attacker"
    with pytest.raises(Exception, match="bundle identity does not match"):
        verify_platform_artifact(artifact_root, variant)


def test_packaged_generator_binds_macos_tree_and_entrypoint_digests(
    tmp_path: Path,
) -> None:
    source_root, _ = _macos_artifact(tmp_path / "source")
    source_app = source_root / "Tobkiri.app"
    resource = source_app / "Contents" / "Resources" / "presentation.json"
    resource.parent.mkdir(parents=True)
    resource.write_text("sealed presentation", encoding="utf-8")
    bundle = tmp_path / "staged" / "defaultspack" / "v4"
    artifacts = tmp_path / "staged" / "defaultspack" / "platform-artifacts"
    shutil.copytree(SOURCE_BUNDLE, bundle)
    provenance = create_test_source_provenance(
        ROOT,
        tmp_path,
        provenance_record={
            "source_commit": SOURCE_COMMIT,
            "source_tree": SOURCE_TREE,
            "source_clean": True,
        },
    )
    stage_packaged_bundle(
        source_artifact=source_app,
        bundle_root=bundle,
        artifact_root=artifacts,
        relative_path="Tobkiri.app",
        entrypoint="Tobkiri.app/Contents/MacOS/tobkiri-shell",
        platform="macos",
        architecture="arm64",
        bundle_identity="io.tobkiri.shell.tauri",
        source_provenance_file=provenance,
    )
    shell = json.loads((bundle / "shell.tauri.default.shell.v1.json").read_text())
    variant = shell["launch"]["variants"][0]
    assert variant["artifact_digest"] != variant["entrypoint_digest"]
    verify_platform_artifact(artifacts, variant)

    staged_resource = artifacts / "Tobkiri.app/Contents/Resources/presentation.json"
    staged_resource.write_text("tampered presentation", encoding="utf-8")
    with pytest.raises(Exception, match="selected bytes"):
        verify_platform_artifact(artifacts, variant)
    staged_resource.write_text("sealed presentation", encoding="utf-8")

    staged_entrypoint = artifacts / str(variant["entrypoint"])
    staged_entrypoint.write_bytes(staged_entrypoint.read_bytes() + b"tamper")
    with pytest.raises(Exception, match="selected bytes|entrypoint digest"):
        verify_platform_artifact(artifacts, variant)


def test_production_bundle_root_ignores_environment_attack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_runtime import pack_control_v4
    from core_runtime.app_lifecycle_manager import AppLifecycleManager
    from core_runtime.bootstrap import profile_capture
    from core_runtime.pack_api_server import _load_production_capture_inputs
    from core_runtime.runtime_surface_v4 import RuntimeSurfaceService

    attacker = tmp_path / "attacker-bundle"
    attacker.mkdir()
    monkeypatch.setenv("TOBKIRI_DEFAULTS_BUNDLE_ROOT", str(attacker))
    monkeypatch.setenv("TOBKIRI_RUNTIME_MODE", "test")
    monkeypatch.setenv("TOBKIRI_TEST_DEFAULTS_BUNDLE_ROOT", str(attacker))
    monkeypatch.setattr(profile_capture, "_bundle_root", _bundle_root)
    assert _bundle_root() == SOURCE_BUNDLE

    observed: list[Path] = []

    class CatalogProbe(RuntimeError):
        pass

    def probe(_cls: Any, root: Path, **_kwargs: Any) -> None:
        observed.append(Path(root))
        raise CatalogProbe

    monkeypatch.setattr(BundledCatalog, "load", classmethod(probe))
    monkeypatch.setattr(profile_capture, "capture_default_profile", lambda **_kwargs: object())
    entrypoints = (
        lambda: RuntimeSurfaceService._load_catalog(),
        lambda: pack_control_v4._required_profile_pack_ids("defaults"),
        _load_production_capture_inputs,
        lambda: AppLifecycleManager(base_dir=tmp_path).activate_default_profile({}),
    )
    for entrypoint in entrypoints:
        with pytest.raises(CatalogProbe):
            entrypoint()
    assert observed == [SOURCE_BUNDLE] * len(entrypoints)


def test_installed_bundle_is_exactly_bound_to_resource_manifest(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    bundle_root = build_packaged_profile_bundle(
        SOURCE_BUNDLE,
        runtime_root / "ecosystem",
        source_provenance_file=create_test_source_provenance(
            ROOT,
            runtime_root / "ecosystem",
            provenance_record={
                "source_commit": SOURCE_COMMIT,
                "source_tree": SOURCE_TREE,
                "source_clean": True,
            },
        ),
    )
    artifact_root = bundle_root.parent / "platform-artifacts"
    entries = []
    for path in sorted((*bundle_root.rglob("*"), *artifact_root.rglob("*"))):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(runtime_root).as_posix(),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    (runtime_root / "runtime-resource-manifest.v1.json").write_text(
        json.dumps(
            {
                "schema": "io.tobkiri.runtime-resource-manifest.v1",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    _verify_installed_bundle_binding(runtime_root, bundle_root)
    executable = artifact_root / "Tobkiri.AppImage"
    executable.write_bytes(executable.read_bytes() + b"tamper")
    with pytest.raises(ProfileResolutionDenied, match="not launcher-bound"):
        _verify_installed_bundle_binding(runtime_root, bundle_root)

    executable.write_bytes(executable.read_bytes().removesuffix(b"tamper"))
    unlisted = bundle_root / "unlisted.json"
    unlisted.write_text("{}", encoding="utf-8")
    with pytest.raises(ProfileResolutionDenied, match="resource is unlisted"):
        _verify_installed_bundle_binding(runtime_root, bundle_root)


@pytest.mark.parametrize("field", ("platform", "architecture"))
def test_packaged_artifact_metadata_mismatch_is_rejected(tmp_path: Path, field: str) -> None:
    catalog = _packaged_catalog(tmp_path)
    shell = copy.deepcopy(catalog.shells["shell.tauri.default"])
    replacement = {
        "platform": "macos",
        "architecture": "arm64",
    }[field]
    shell["launch"]["variants"][0][field] = replacement
    tampered = type(catalog)(
        root=catalog.root,
        packs=catalog.packs,
        bases=catalog.bases,
        shells={**catalog.shells, "shell.tauri.default": shell},
        profiles=catalog.profiles,
        artifact_root=catalog.artifact_root,
    )
    with pytest.raises(ProfileResolutionDenied):
        _resolve(tampered)


@pytest.mark.parametrize("case", ("missing", "digest", "sentinel", "symlink"))
def test_packaged_artifact_path_digest_and_symlink_rejection(tmp_path: Path, case: str) -> None:
    catalog = _packaged_catalog(tmp_path)
    variant = copy.deepcopy(catalog.shells["shell.tauri.default"]["launch"]["variants"][0])
    executable = catalog.artifact_root / str(variant["entrypoint"])
    if case == "missing":
        variant["relative_path"] = "Missing.app"
    elif case == "digest":
        variant["artifact_digest"] = "sha256:" + "1" * 64
    elif case == "sentinel":
        variant["artifact_digest"] = "sha256:" + "d" * 64
    else:
        outside = tmp_path / "outside"
        outside.write_bytes(executable.read_bytes())
        executable.unlink()
        executable.symlink_to(outside)
    with pytest.raises(Exception):
        verify_platform_artifact(catalog.artifact_root, variant)


def test_requested_scope_normalization_denies_expansion_and_is_canonical() -> None:
    semantics = "sha256:" + "1" * 64
    normalized = normalize_requested_scope_template(
        {},
        contract_id="example.echo.v1",
        operation_id="echo",
        semantics_digest=semantics,
    )
    assert normalized["dimensions"] == {
        "contract": ["example.echo.v1"],
        "operation": ["echo"],
    }
    with pytest.raises(Exception, match="wildcards|does not match"):
        normalize_requested_scope_template(
            {"dimensions": {"operation": ["*"]}},
            contract_id="example.echo.v1",
            operation_id="echo",
            semantics_digest=semantics,
        )


def _write_frozen_activation(
    state: Path,
    workspace: Path,
    authority: AuthorityStore,
    fixture: dict[str, Any] | None = None,
) -> None:
    fixture = fixture or json.loads(FROZEN.read_text(encoding="utf-8"))
    activation = fixture["activation"]
    reservation_id, fencing_token = authority.reserve_activation(
        activation_id=activation["activation_id"],
        profile_id=activation["profile_id"],
        plan_digest=activation["plan_digest"],
        profile_authority_digest=activation["profile_authority_snapshot_digest"],
        security_epoch=activation["security_epoch"],
    )
    assert fencing_token == activation["fencing_token"]
    for before, after in (
        ("prepared", "ready_without_authority"),
        ("ready_without_authority", "committing"),
        ("committing", "active"),
    ):
        authority.transition_activation(reservation_id, expected_state=before, new_state=after)
    workspace_digest = canonical_digest({"workspace_root": str(workspace.resolve())})
    envelope = {
        "schema": "io.tobkiri.defaultspack-activation-envelope.v1",
        "workspace_digest": workspace_digest,
        **{key: fixture[key] for key in ("profile", "lock", "plan", "activation")},
    }
    envelope_path = state / "activations/defaults-pre-e853.json"
    envelope_path.parent.mkdir(parents=True)
    envelope_path.write_bytes(canonical_json(envelope) + b"\n")
    pointer = {
        "schema": "io.tobkiri.defaultspack-active-pointer.v1",
        "activation_id": activation["activation_id"],
        "envelope_path": envelope_path.name,
        "envelope_digest": canonical_digest(envelope),
        "workspace_digest": workspace_digest,
    }
    (state / "active.json").write_bytes(canonical_json(pointer) + b"\n")


def _compatible_legacy_fixture(resolved: Any) -> dict[str, Any]:
    profile = copy.deepcopy(resolved.profile)
    plan = {
        "plan_api_version": "io.tobkiri.resolved-plan.v1",
        "profile_id": profile["profile_id"],
        "profile_revision": canonical_digest(profile),
        "security_epoch": resolved.plan["security_epoch"],
        "base": {key: resolved.plan["base"][key] for key in ("pack_id", "artifact_digest")},
        "shell": {
            key: resolved.plan["shell"][key]
            for key in ("provider_id", "pack_id", "artifact_digest", "contract_id")
        },
        "bindings": [
            {
                key: binding[key]
                for key in (
                    "pack_id",
                    "artifact_digest",
                    "function_principal",
                    "contract_id",
                    "operation_id",
                    "domain_kind",
                )
            }
            for binding in resolved.plan["bindings"]
        ],
        "plan_digest": "sha256:" + "0" * 64,
    }
    plan["plan_digest"] = canonical_digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    lock = {
        "lock_api_version": "io.tobkiri.profile-lock.v4",
        "profile_id": profile["profile_id"],
        "profile_revision": canonical_digest(profile),
        "catalog_revision": profile["catalog_revision"],
        "security_epoch": resolved.plan["security_epoch"],
        "base": copy.deepcopy(resolved.lock["base"]),
        "shell": {
            key: resolved.lock["shell"][key]
            for key in resolved.lock["shell"]
            if key != "executable_artifact_digest"
        },
        "effective_set": copy.deepcopy(resolved.plan["effective_set"]),
        "plan_digest": plan["plan_digest"],
        "profile_authority_snapshot_digest": profile["profile_authority_snapshot_digest"],
        "lock_digest": "sha256:" + "0" * 64,
    }
    lock["lock_digest"] = canonical_digest(
        {key: value for key, value in lock.items() if key != "lock_digest"}
    )
    activation = {
        "activation_api_version": "io.tobkiri.activation-record.v1",
        "profile_id": profile["profile_id"],
        "activation_id": "activation:defaults-compatible-legacy",
        "state": "active",
        "state_generation": 4,
        "plan_digest": plan["plan_digest"],
        "profile_authority_snapshot_digest": profile["profile_authority_snapshot_digest"],
        "security_epoch": resolved.plan["security_epoch"],
        "fencing_token": 1,
        "created_at": "2026-08-10T00:00:00Z",
        "committed_at": "2026-08-10T00:00:00Z",
    }
    return {"profile": profile, "lock": lock, "plan": plan, "activation": activation}


def _redigest_legacy_fixture(fixture: dict[str, Any]) -> None:
    profile = fixture["profile"]
    plan = fixture["plan"]
    lock = fixture["lock"]
    activation = fixture["activation"]
    revision = canonical_digest(profile)
    plan["profile_revision"] = revision
    lock["profile_revision"] = revision
    plan["plan_digest"] = canonical_digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    lock["plan_digest"] = plan["plan_digest"]
    lock["lock_digest"] = canonical_digest(
        {key: value for key, value in lock.items() if key != "lock_digest"}
    )
    activation["plan_digest"] = plan["plan_digest"]


def _retarget_legacy_artifact(fixture: dict[str, Any], role: str) -> None:
    replacement = "sha256:" + {"base": "a", "shell": "b", "pack": "c"}[role] * 64
    profile = fixture["profile"]
    lock = fixture["lock"]
    plan = fixture["plan"]
    if role in {"base", "shell"}:
        pack_id = profile[role]["pack_id"]
        profile[role]["artifact_digest"] = replacement
        lock[role]["artifact_digest"] = replacement
        plan[role]["artifact_digest"] = replacement
    else:
        selected = profile["packs"][0]
        pack_id = selected["pack_id"]
        selected["artifact_digest"] = replacement
    for item in lock["effective_set"]:
        if item["identity"] == pack_id:
            item["artifact_digest"] = replacement
    for binding in plan["bindings"]:
        if binding["pack_id"] == pack_id:
            binding["artifact_digest"] = replacement
            binding["function_principal"]["parent_artifact_digest"] = replacement
    _redigest_legacy_fixture(fixture)


def _remove_legacy_authority_edge(fixture: dict[str, Any]) -> None:
    """Model an older, narrower activation without inventing authority."""

    profile = fixture["profile"]
    plan = fixture["plan"]
    removed = profile["requested_edges"].pop()
    removed_reference = removed["authority_reference"]
    profile["authority_references"] = [
        reference for reference in profile["authority_references"] if reference != removed_reference
    ]
    plan["bindings"] = [
        binding
        for binding in plan["bindings"]
        if not (
            binding["function_principal"]["function_id"] == removed["caller_function_id"]
            and binding["contract_id"] == removed["contract_id"]
            and binding["operation_id"] == removed["operation_id"]
        )
    ]
    _redigest_legacy_fixture(fixture)


def test_exact_legacy_activation_migrates_once_without_drift(tmp_path: Path) -> None:
    catalog = _packaged_catalog(tmp_path)
    resolved = _resolve(catalog)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    _write_frozen_activation(
        state,
        workspace,
        authority,
        _compatible_legacy_fixture(resolved),
    )
    store = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=catalog,
    )
    migrated = store.load_active_snapshot()
    assert migrated.resolved.plan == resolved.plan
    assert migrated.activation["activation_api_version"] == ("io.tobkiri.activation-record.v2")
    assert store.load_active_snapshot().activation == migrated.activation


def test_valid_narrower_activation_requires_confirmed_reconciliation(
    tmp_path: Path,
) -> None:
    catalog = _packaged_catalog(tmp_path)
    resolved = _resolve(catalog)
    fixture = _compatible_legacy_fixture(resolved)
    _remove_legacy_authority_edge(fixture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    _write_frozen_activation(state, workspace, authority, fixture)
    old_pointer = (state / "active.json").read_bytes()
    old_envelope = next((state / "activations").iterdir())
    old_envelope_bytes = old_envelope.read_bytes()
    store = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=catalog,
    )

    with pytest.raises(
        ProfileReconfirmationRequired,
        match="Authority Kernel reference is missing",
    ):
        store.load_active_snapshot()
    assert (state / "active.json").read_bytes() == old_pointer
    assert old_envelope.read_bytes() == old_envelope_bytes

    store.reconcile_active(
        resolved,
        activation_id="activation:defaults-confirmed-reconcile",
        created_at="2026-08-11T00:00:00Z",
    )
    restarted = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=catalog,
    ).load_active_snapshot()
    assert restarted.resolved == resolved
    assert restarted.activation["activation_api_version"] == ("io.tobkiri.activation-record.v2")
    assert old_envelope.read_bytes() == old_envelope_bytes


def test_capture_flow_reconciles_only_with_exact_explicit_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_runtime.bootstrap import profile_capture

    catalog = _packaged_catalog(tmp_path / "catalog")
    resolved = _resolve(catalog)
    fixture = _compatible_legacy_fixture(resolved)
    _remove_legacy_authority_edge(fixture)
    user_data = tmp_path / "user-data"
    workspace = user_data / "workspaces" / "defaults"
    workspace.mkdir(parents=True)
    state = workspace / "activation"
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        _write_frozen_activation(state, workspace, authority, fixture)
    confirmation = {
        "operation_id": "defaults.activate",
        "confirmation_digest": "sha256:" + "a" * 64,
    }
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setattr(profile_capture, "_bundle_root", lambda _base=None: catalog.root)
    monkeypatch.setattr(
        profile_capture,
        "_resolve_candidate",
        lambda **_kwargs: (resolved, confirmation),
    )

    with pytest.raises(ProfileResolutionDenied, match="stale or tampered"):
        profile_capture.capture_default_profile(
            confirmation={**confirmation, "operation_id": "attacker.activate"}
        )
    active = profile_capture.capture_default_profile(confirmation=confirmation)
    assert active.resolved == resolved
    assert active.activation["activation_id"].startswith("activation:defaults-reconcile-")
    assert profile_capture.capture_default_profile().activation == active.activation


def test_reconfirmation_hard_denials_do_not_replace_predecessor(
    tmp_path: Path,
) -> None:
    catalog = _packaged_catalog(tmp_path)
    resolved = _resolve(catalog)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    for case in ("stale_revision", "envelope_digest", "unauthorized"):
        case_root = tmp_path / case
        state = case_root / "state"
        authority = AuthorityStore(case_root / "authority.sqlite3")
        fixture = _compatible_legacy_fixture(resolved)
        _remove_legacy_authority_edge(fixture)
        _write_frozen_activation(state, workspace, authority, fixture)
        pointer = json.loads((state / "active.json").read_text(encoding="utf-8"))
        envelope_path = state / "activations" / pointer["envelope_path"]
        if case == "stale_revision":
            envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            envelope["lock"]["profile_revision"] = "sha256:" + "f" * 64
            envelope_path.write_bytes(canonical_json(envelope) + b"\n")
            pointer["envelope_digest"] = canonical_digest(envelope)
            (state / "active.json").write_bytes(canonical_json(pointer) + b"\n")
            expected = "predecessor is stale"
            candidate_authority = authority
        elif case == "envelope_digest":
            envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            envelope["plan"]["security_epoch"] = 2
            envelope_path.write_bytes(canonical_json(envelope) + b"\n")
            expected = "activation envelope digest changed"
            candidate_authority = authority
        else:
            candidate_authority = AuthorityStore(case_root / "foreign-authority.sqlite3")
            expected = "Authority record is unavailable"
        pointer_before = (state / "active.json").read_bytes()
        store = ActivationStore(
            state,
            workspace,
            profile_id="defaults",
            authority=candidate_authority,
            catalog=catalog,
        )
        with pytest.raises(ProfileResolutionDenied, match=expected) as denied:
            store.reconcile_active(
                resolved,
                activation_id=f"activation:defaults-{case}-denied",
                created_at="2026-08-11T00:00:00Z",
            )
        assert not isinstance(denied.value, ProfileReconfirmationRequired)
        assert (state / "active.json").read_bytes() == pointer_before


def test_confirmed_reconciliation_recovers_commit_across_restart(
    tmp_path: Path,
) -> None:
    catalog = _packaged_catalog(tmp_path)
    resolved = _resolve(catalog)
    fixture = _compatible_legacy_fixture(resolved)
    _remove_legacy_authority_edge(fixture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    _write_frozen_activation(state, workspace, authority, fixture)
    predecessor = next((state / "activations").iterdir())
    predecessor_bytes = predecessor.read_bytes()

    def interrupt(stage: str) -> None:
        if stage == "after_authority_commit":
            raise OSError("simulated restart after Authority commit")

    with pytest.raises(OSError, match="simulated restart"):
        ActivationStore(
            state,
            workspace,
            profile_id="defaults",
            authority=authority,
            catalog=catalog,
            fault=interrupt,
        ).reconcile_active(
            resolved,
            activation_id="activation:defaults-restart-reconcile",
            created_at="2026-08-11T00:00:00Z",
        )

    recovered = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=catalog,
    ).load_active_snapshot()
    assert recovered.resolved == resolved
    assert recovered.activation["activation_id"] == ("activation:defaults-restart-reconcile")
    assert predecessor.read_bytes() == predecessor_bytes


@pytest.mark.parametrize("role", ("base", "shell", "pack"))
def test_legacy_migration_rejects_self_consistent_artifact_drift(tmp_path: Path, role: str) -> None:
    catalog = _packaged_catalog(tmp_path)
    fixture = _compatible_legacy_fixture(_resolve(catalog))
    _retarget_legacy_artifact(fixture, role)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    state = tmp_path / "state"
    _write_frozen_activation(state, workspace, authority, fixture)
    with pytest.raises(ProfileResolutionDenied, match="artifact closure changed"):
        ActivationStore(
            state,
            workspace,
            profile_id="defaults",
            authority=authority,
            catalog=catalog,
        ).load_active_snapshot()


def test_legacy_migration_rejects_principal_drift(tmp_path: Path) -> None:
    catalog = _packaged_catalog(tmp_path)
    fixture = _compatible_legacy_fixture(_resolve(catalog))
    fixture["plan"]["bindings"][0]["function_principal"]["function_implementation_digest"] = (
        "sha256:" + "e" * 64
    )
    _redigest_legacy_fixture(fixture)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    state = tmp_path / "state"
    _write_frozen_activation(state, workspace, authority, fixture)
    with pytest.raises(ProfileResolutionDenied, match="principal binding changed"):
        ActivationStore(
            state,
            workspace,
            profile_id="defaults",
            authority=authority,
            catalog=catalog,
        ).load_active_snapshot()


def test_frozen_pre_e853_restart_rejects_implicit_artifact_upgrade(
    tmp_path: Path,
) -> None:
    catalog = _packaged_catalog(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    _write_frozen_activation(state, workspace, authority)
    store = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=catalog,
    )
    with pytest.raises(
        ProfileResolutionDenied,
        match="artifact closure changed|Authority Kernel reference is missing",
    ):
        store.load_active_snapshot()
    with pytest.raises(
        ProfileResolutionDenied,
        match="artifact closure changed|Authority Kernel reference is missing",
    ):
        store.load_active_snapshot()


def test_frozen_pre_e853_tamper_and_migration_crash_fail_closed(
    tmp_path: Path,
) -> None:
    catalog = _packaged_catalog(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    authority = AuthorityStore(tmp_path / "authority.sqlite3")
    _write_frozen_activation(state, workspace, authority)
    pointer = json.loads((state / "active.json").read_text(encoding="utf-8"))
    envelope_path = state / "activations" / pointer["envelope_path"]
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["plan"]["security_epoch"] = 2
    envelope_path.write_bytes(canonical_json(envelope) + b"\n")
    pointer["envelope_digest"] = canonical_digest(envelope)
    (state / "active.json").write_bytes(canonical_json(pointer) + b"\n")
    store = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        catalog=catalog,
    )
    with pytest.raises(ProfileResolutionDenied, match="predecessor is stale"):
        store.load_active_snapshot()

    clean_state = tmp_path / "clean-state"
    clean_authority = AuthorityStore(tmp_path / "clean-authority.sqlite3")
    _write_frozen_activation(clean_state, workspace, clean_authority)

    def crash(stage: str) -> None:
        if stage == "after_authority_commit":
            raise RuntimeError("migration crash")

    crashing = ActivationStore(
        clean_state,
        workspace,
        profile_id="defaults",
        authority=clean_authority,
        catalog=catalog,
        fault=crash,
    )
    with pytest.raises(
        ProfileResolutionDenied,
        match="artifact closure changed|Authority Kernel reference is missing",
    ):
        crashing.load_active_snapshot()
    with pytest.raises(
        ProfileResolutionDenied,
        match="artifact closure changed|Authority Kernel reference is missing",
    ):
        ActivationStore(
            clean_state,
            workspace,
            profile_id="defaults",
            authority=clean_authority,
            catalog=catalog,
        ).load_active_snapshot()


def test_normative_generator_rejects_dirty_implicit_commit(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    source = repository / "source.json"
    source.write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "source.json"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
    source.write_text('{"dirty":true}', encoding="utf-8")
    with pytest.raises(Exception, match="dirty working tree"):
        trusted_source_commit(repository)


def test_official_bundle_render_is_byte_identical_across_two_passes() -> None:
    first = generate_defaultspack_v4_bundle._render()
    second = generate_defaultspack_v4_bundle._render()
    assert first == second


def test_informational_commit_is_stable_across_child_and_shallow_checkout(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    source = repository / "source.json"
    source.write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "source.json"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "source"], cwd=repository, check=True)
    first = informational_source_commit(repository)
    marker = repository / "unrelated.txt"
    marker.write_text("unrelated", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "child"], cwd=repository, check=True)
    second = informational_source_commit(repository)
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", repository.as_uri(), str(shallow)],
        check=True,
    )
    assert first == second == informational_source_commit(shallow) == "working-tree"


def test_normative_provenance_is_non_self_referential_and_source_bound() -> None:
    first = normative_generated_provenance(
        source_path="source.json",
        payload={"value": 1},
        repository_commit_value=SOURCE_COMMIT,
        generator="test",
        generator_version="1.0.0",
        generator_path="generator.py",
        generator_payload=b"generator-v1",
    )
    second = normative_generated_provenance(
        source_path="source.json",
        payload={"value": 2},
        repository_commit_value=SOURCE_COMMIT,
        generator="test",
        generator_version="1.0.0",
        generator_path="generator.py",
        generator_payload=b"generator-v1",
    )
    assert first["source_digest"] != second["source_digest"]
    assert first["content_root_digest"] != second["content_root_digest"]
    assert first["generator_digest"] == second["generator_digest"]
    assert first["repository_commit_trusted"] is False
    generator_changed = normative_generated_provenance(
        source_path="source.json",
        payload={"value": 1},
        repository_commit_value=SOURCE_COMMIT,
        generator="test",
        generator_version="1.0.0",
        generator_path="generator.py",
        generator_payload=b"generator-v2",
    )
    assert generator_changed["content_root_digest"] != first["content_root_digest"]
    assert "provenance" not in json.dumps({"value": 1})
    shallow = normative_generated_provenance(
        source_path="source.json",
        payload={"value": 1},
        repository_commit_value="working-tree",
        generator="test",
        generator_version="1.0.0",
        generator_path="generator.py",
        generator_payload=b"generator-v1",
    )
    assert shallow["content_root_digest"] == first["content_root_digest"]
    mismatched_commit = normative_generated_provenance(
        source_path="source.json",
        payload={"value": 1},
        repository_commit_value="b" * 39 + "c",
        generator="test",
        generator_version="1.0.0",
        generator_path="generator.py",
        generator_payload=b"generator-v1",
    )
    assert mismatched_commit["content_root_digest"] == first["content_root_digest"]
    with pytest.raises(Exception, match="informational repository commit"):
        normative_generated_provenance(
            source_path="source.json",
            payload={"value": 1},
            repository_commit_value="0" * 40,
            generator="test",
            generator_version="1.0.0",
            generator_path="generator.py",
            generator_payload=b"generator-v1",
        )
