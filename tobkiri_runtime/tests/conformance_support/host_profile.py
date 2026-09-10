"""Isolated, officially packaged Profile for production Host/Broker tests."""

from contextlib import contextmanager
import json
from pathlib import Path
import shutil

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture, runtime
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from scripts.generate_profile_artifacts import render
from scripts.generate_packaged_defaultspack_v4_bundle import package_bundle
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
from tests.conformance_support.host_contract import host_contract
from tobkiri_host.backends import BackendRegistry

_RUNTIME_ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def captured_host_profile(
    tmp_path, monkeypatch, *, packs, edges, backends, exclude_packs=(),
    exclude_callers=(),
):
    """Add explicit test edges without touching live activation or user data."""
    source_bundle = packaged_profile_bundle_root()
    destination = tmp_path / "packaged-defaultspack"
    shutil.copytree(source_bundle.parent, destination)
    bundle = destination / "v4"
    intent_path = bundle / "defaults.profile.intent.v1.json"
    # The packaged bundle intentionally omits source-only intent files.
    source_intent = (
        _RUNTIME_ROOT / "ecosystem/defaultspack/v4/defaults.profile.intent.v1.json"
    )
    intent = json.loads(source_intent.read_text())
    intent["packs"] = [
        item for item in intent["packs"] if item["pack_id"] not in exclude_packs
    ]
    for pack_id in packs:
        intent["packs"].append(
            {"artifact_digest": None, "pack_id": pack_id, "role": "provider"}
        )
    intent["requested_edges"] = [
        edge for edge in intent["requested_edges"]
        if edge["caller_function_id"] not in exclude_callers
    ] + list(edges)
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
    shell = json.loads(
        (source_bundle / "shell.tauri.default.shell.v1.json").read_text()
    )
    variant = shell["launch"]["variants"][0]
    package_bundle(
        bundle_root=bundle,
        artifact_root=destination / "platform-artifacts",
        **{
            key: variant[key]
            for key in (
                "relative_path",
                "entrypoint",
                "platform",
                "architecture",
                "bundle_identity",
            )
        },
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
    contract_path = user_data / "host_contract.json"
    contract_path.write_text(
        json.dumps(
            host_contract(
                profile_id=str(active.resolved.profile["profile_id"]),
                profile_revision=str(active.resolved.plan["profile_revision"]),
                activation_id=str(active.activation["activation_id"]),
                plan_digest=str(active.resolved.plan["plan_digest"]),
                values={"panel_bootstrap_secret": "host-fixture-bootstrap"},
            )
        )
    )
    contract_path.chmod(0o600)
    monkeypatch.setenv("TOBKIRI_HOST_CONTRACT_PATH", str(contract_path))
    store = AuthorityStore(user_data / "authority/v4.sqlite3")
    session = None
    try:
        session = capture_production_dispatch(
            active,
            bundle_root=bundle,
            ecosystem_root=_RUNTIME_ROOT / "ecosystem",
            authority_store=store,
            activation_snapshot_loader=defaultspack_activation_snapshot_loader,
            runtime_surface_factory=create_runtime_surface_services,
            backends=BackendRegistry(backends),
        )
        yield session, store
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            store.close()
