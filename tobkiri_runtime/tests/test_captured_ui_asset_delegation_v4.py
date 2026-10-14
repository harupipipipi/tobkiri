"""Application UI delegation requires exact Native-scoped signed CAS bytes."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from core_runtime.captured_pack_ui_assets_v4 import (
    CapturedPackUIAssetDenied,
    CapturedPackUIAssets,
    UIAssetDelegation,
    bind_captured_pack_web_mounts,
)
from core_runtime.captured_ui_asset_delegation_v4 import CapturedAssetOnlyUIPacks
from core_runtime.pack_artifact_integrity import _policy_digest, read_host_policy_snapshot
from core_runtime.pack_signature import (
    SIGNED_MANIFEST_RELATIVE,
    build_signed_manifest,
    sign_manifest,
)
from core_runtime.publisher_key_versions import (
    prepare_publisher_key_version,
    write_publisher_key_version,
)
from tests.test_captured_pack_ui_assets_v4 import admit_ui_source, signed_ui_source
from tests.test_host_extension_update_v4 import _refresh
from tests.test_publisher_key_versions import CONTEXT, _public
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.canonical import canonical_digest


PACK_ID = "defaultspack_ui_assets_pack"
PUBLISHER = "tobkiri.repository"


def _asset_only_source(tmp_path: Path) -> tuple[Path, Path]:
    source, public = signed_ui_source(tmp_path)
    (source / SIGNED_MANIFEST_RELATIVE).unlink()
    manifest = json.loads((source / "pack.v4.json").read_bytes())
    manifest["pack"].update(id=PACK_ID, publisher_id=PUBLISHER)
    for field in ("functions", "contracts", "operation_catalog", "provider_catalog"):
        manifest[field] = []
    removed = {item["path"] for item in manifest["artifacts"] if item["kind"] == "executable"}
    for name in removed:
        (source / name).unlink()
    manifest["artifacts"] = [item for item in manifest["artifacts"] if item["path"] not in removed]
    (source / "pack.v4.json").write_text(json.dumps(manifest))
    for filename, field in (
        ("contracts.v4.json", "contracts"),
        ("executables.v4.json", "variants"),
    ):
        document = json.loads((source / filename).read_bytes())
        document["pack_id"] = PACK_ID
        document[field] = []
        (source / filename).write_text(json.dumps(document))
    index = json.loads((source / "artifact-index.v4.json").read_bytes())
    index["pack_id"] = PACK_ID
    index["artifacts"] = [item for item in index["artifacts"] if item["path"] not in removed]
    (source / "artifact-index.v4.json").write_text(json.dumps(index))
    _refresh(source)
    private = Ed25519PrivateKey.generate()
    signed = sign_manifest(
        build_signed_manifest(
            source,
            pack_id=PACK_ID,
            publisher_id=PUBLISHER,
            version=manifest["pack"]["version"],
            core_compatibility=">=0",
            contract_versions={},
            requested_capabilities=[],
        ),
        private,
    )
    (source / SIGNED_MANIFEST_RELATIVE).write_text(json.dumps(signed))
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return source, public


@pytest.fixture
def delegated_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CapturedAssetOnlyUIPacks, Path, Path]:
    """Real admission with a test-only exact scope authorized by a fixture verifier."""
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(owner))
    monkeypatch.setenv("RUMI_USER_DATA", str(owner))
    source, public = _asset_only_source(tmp_path)
    compiled = compile_pack_root(source)
    assert not compiled.artifact.functions and not compiled.artifact.variants
    signed = json.loads((source / SIGNED_MANIFEST_RELATIVE).read_bytes())
    policy_path = owner / "pack_control/publisher-trust.json"
    policy_path.parent.mkdir(mode=0o700)
    policy: dict[str, Any] = {
        "publishers": {
            PUBLISHER: {
                "public_key_pem": _public("legacy-ui-anchor")[0],
                "allowed_pack_namespaces": ["legacy.ui"],
                "revoked_key_ids": [],
            }
        },
        "install_records": {},
        "policy_generation": 1,
    }
    policy["policy_digest"] = _policy_digest(policy)
    policy_path.write_text(json.dumps(policy))
    policy_path.chmod(0o600)
    plan = prepare_publisher_key_version(
        policy,
        publisher_id=PUBLISHER,
        public_key_pem=public.read_text(),
        pack_id=PACK_ID,
        artifact_digest=compiled.artifact.digest,
        signed_manifest_digest=canonical_digest(signed),
        approval_context=CONTEXT,
    )
    write_publisher_key_version(
        policy_path,
        plan=plan,
        approval_verifier=lambda *_: None,
        approval_proof={
            "domain": "tobkiri.publisher-key-version.v1",
            "request_id": "ui_fixture_request_000001",
            "issued_at_ms": 1000,
            "expires_at_ms": 2000,
            "plan_digest": plan.digest,
            "context": CONTEXT,
            "evidence": {"native": "test-fixture"},
        },
        clock_ms=lambda: 1500,
    )
    monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(policy_path))
    root, digest = admit_ui_source(source, public, owner)
    return (
        CapturedAssetOnlyUIPacks(
            artifact_pins={PACK_ID: digest},
            owner_root=owner,
        ),
        root,
        policy_path,
    )


def _mount(root: Path, **overrides: Any) -> tuple[dict[str, Any], ...]:
    return (
        {
            "path_prefix": "/p",
            "web_root": root / "ui",
            "spa_fallback": True,
            "index_file": "shell.html",
            "auth_required": True,
            "auth_bootstrap": True,
            "ui_asset_pack_id": PACK_ID,
            "ui_asset_publisher_id": PUBLISHER,
            **overrides,
        },
    )


def test_asset_only_signed_capture_mounts_without_function_bindings(
    delegated_ui: tuple[CapturedAssetOnlyUIPacks, Path, Path],
    tmp_path: Path,
) -> None:
    capture, root, _policy = delegated_ui
    mounts = bind_captured_pack_web_mounts(
        _mount(tmp_path / "defaultspack"),
        lambda pack, index: capture.capture(pack, index, assert_current=lambda: None),
    )
    assert mounts is not None
    assert mounts[0]["web_root"] == root / "ui"
    assert mounts[0]["auth_bootstrap"] is True
    assert mounts[0]["byte_reader"]("/shell-app.js", False)[0].startswith(b"console")


def test_absent_slot_alone_uses_verified_original_builtin(
    tmp_path: Path,
) -> None:
    root = tmp_path / "defaultspack"
    legacy = CapturedPackUIAssets(
        "defaultspack",
        "sha256:" + "a" * 64,
        root,
        (1, 1),
        tmp_path,
        (1, 2),
        {},
        "shell.html",
        lambda: None,
        legacy_builtin=True,
    )
    calls = []

    def capture(pack: str, index: str) -> CapturedPackUIAssets | None:
        calls.append((pack, index))
        return legacy if pack == "defaultspack" else None

    mounts = bind_captured_pack_web_mounts(_mount(root), capture)
    assert calls == [(PACK_ID, "shell.html"), ("defaultspack", "shell.html")]
    assert mounts is not None and "byte_reader" not in mounts[0]


@pytest.mark.parametrize("mutation", ["publisher", "owner", "legacy", "unscoped"])
def test_delegated_capture_identity_cannot_be_promoted_or_mixed(
    delegated_ui: tuple[CapturedAssetOnlyUIPacks, Path, Path],
    tmp_path: Path,
    mutation: str,
) -> None:
    capture, _root, _policy = delegated_ui
    assets = capture.capture(PACK_ID, "shell.html", assert_current=lambda: None)
    assert assets is not None and assets.delegation is not None
    if mutation == "publisher":
        assets = replace(
            assets,
            delegation=UIAssetDelegation(
                "other.publisher",
                assets.delegation.key_id,
                assets.delegation.signed_manifest_digest,
            ),
        )
    elif mutation == "owner":
        assets = replace(assets, pack_id="other.pack")
    elif mutation == "legacy":
        assets = replace(assets, legacy_builtin=True)
    else:
        assets = replace(assets, delegation=None)
    with pytest.raises(CapturedPackUIAssetDenied):
        bind_captured_pack_web_mounts(_mount(tmp_path / "defaultspack"), lambda *_: assets)


def test_selected_missing_or_unknown_pin_never_falls_back(
    delegated_ui: tuple[CapturedAssetOnlyUIPacks, Path, Path],
    tmp_path: Path,
) -> None:
    capture, root, _policy = delegated_ui
    wrong = CapturedAssetOnlyUIPacks(
        artifact_pins={PACK_ID: "sha256:" + "f" * 64},
        owner_root=capture._owner_root,
    )
    with pytest.raises(RuntimeError):
        bind_captured_pack_web_mounts(
            _mount(tmp_path / "defaultspack"),
            lambda pack, index: wrong.capture(pack, index, assert_current=lambda: None),
        )
    target = root / "ui/shell.html"
    target.parent.chmod(0o700)
    target.unlink()
    with pytest.raises((RuntimeError, OSError, ValueError)):
        capture.capture(PACK_ID, "shell.html", assert_current=lambda: None)


@pytest.mark.parametrize("mutation", ["legacy", "scope", "revoked", "publisher"])
def test_current_trust_must_authorize_nonlegacy_exact_ui_tuple(
    delegated_ui: tuple[CapturedAssetOnlyUIPacks, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    capture, _root, policy_path = delegated_ui
    policy = read_host_policy_snapshot(policy_path)
    record = policy["publisher_key_versions"]["publishers"][PUBLISHER]
    key_id, key = next(iter(record.items()))
    if mutation == "legacy":
        policy["publishers"][PUBLISHER]["public_key_pem"] = key["public_key_pem"]
    elif mutation == "scope":
        key["scopes"][0]["signed_manifest_digest"] = "sha256:" + "d" * 64
    elif mutation == "revoked":
        policy["publishers"][PUBLISHER]["revoked_key_ids"].append(key_id)
    else:
        policy["install_records"][PACK_ID]["publisher_id"] = "other.publisher"
    from core_runtime import captured_ui_asset_delegation_v4 as module

    monkeypatch.setattr(module, "read_host_policy_snapshot", lambda *_: policy)
    with pytest.raises((CapturedPackUIAssetDenied, ValueError)):
        capture.capture(PACK_ID, "shell.html", assert_current=lambda: None)


def test_reader_fences_policy_signed_manifest_and_closed_capture(
    delegated_ui: tuple[CapturedAssetOnlyUIPacks, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture, root, policy_path = delegated_ui
    closed = False

    def current() -> None:
        if closed:
            raise RuntimeError("closed capture")

    assets = capture.capture(PACK_ID, "shell.html", assert_current=current)
    assert assets is not None
    closed = True
    with pytest.raises(RuntimeError, match="closed"):
        assets.read("/shell-app.js", False)
    closed = False
    policy = read_host_policy_snapshot(policy_path)
    policy["policy_generation"] += 1
    from core_runtime import captured_ui_asset_delegation_v4 as module

    with monkeypatch.context() as patched:
        patched.setattr(module, "read_host_policy_snapshot", lambda *_: policy)
        with pytest.raises(CapturedPackUIAssetDenied, match="authorization changed"):
            assets.read("/shell-app.js", False)
    signed_path = root / SIGNED_MANIFEST_RELATIVE
    signed_path.chmod(0o600)
    signed = json.loads(signed_path.read_bytes())
    signed["signature"]["value"] = "invalid"
    signed_path.write_text(json.dumps(signed))
    with pytest.raises((CapturedPackUIAssetDenied, ValueError)):
        assets.read("/shell-app.js", False)


def test_delegation_cannot_bind_without_production_capture(tmp_path: Path) -> None:
    with pytest.raises(CapturedPackUIAssetDenied, match="capture is unavailable"):
        bind_captured_pack_web_mounts(_mount(tmp_path / "defaultspack"), None)
