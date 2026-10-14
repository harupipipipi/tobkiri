"""Signed CAS UI assets remain bound to their captured bytes and owner."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from core_runtime.captured_pack_ui_assets_v4 import (
    CapturedPackUIAssetDenied,
    CapturedPackUIAssets,
    bind_captured_pack_web_mounts,
)
from core_runtime.external_pack_catalog_v4 import resolve_admitted_pack_root
from core_runtime.native_pack_onboarding import commit_signed_pack, preview_signed_pack
from core_runtime.pack_signature import (
    SIGNED_MANIFEST_RELATIVE,
    build_signed_manifest,
    sign_manifest,
)
from tests.test_host_extension_update_v4 import _refresh
from tobkiri_host.artifact_compiler import compile_pack_root


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = RUNTIME_ROOT / "tests/fixtures/conformance_minimal_echo_pack"


def signed_ui_source(
    tmp_path: Path,
    *,
    baseline: Path = FIXTURE,
    with_ui: bool = True,
) -> tuple[Path, Path]:
    """Sign real canonical Pack documents and a finite compiled UI payload."""
    source = tmp_path / "source" / baseline.name
    source.mkdir(parents=True)
    index = json.loads((baseline / "artifact-index.v4.json").read_bytes())
    for relative in {
        "artifact-index.v4.json",
        *(
            item["path"]
            for item in index["artifacts"]
            if not item["path"].startswith("ui/")
        ),
    }:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(baseline / relative, target)
    manifest = json.loads((source / "pack.v4.json").read_bytes())
    manifest["artifacts"] = [
        item for item in manifest["artifacts"] if not item["path"].startswith("ui/")
    ]
    index["artifacts"] = [
        item for item in index["artifacts"] if not item["path"].startswith("ui/")
    ]
    manifest["pack"]["version"] = (
        "4.0.2" if baseline.name == "defaultspack" else "1.1.0"
    )
    if with_ui:
        payload = {
            "ui/shell.html": b"<!doctype html><p>signed-selected-ui</p>",
            "ui/shell-app.js": b"console.log('signed-selected-ui');",
            "ui/shell-app.css": b"body { color: green; }",
            "ui/fonts/local.woff2": b"signed-local-font",
        }
        for path, content in payload.items():
            target = source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            manifest["artifacts"].append(
                {"path": path, "digest": digest, "kind": "asset"}
            )
            index["artifacts"].append({"path": path, "digest": digest, "role": "asset"})
    (source / "pack.v4.json").write_text(json.dumps(manifest) + "\n")
    (source / "artifact-index.v4.json").write_text(json.dumps(index) + "\n")
    _refresh(source)
    compiled = compile_pack_root(source)
    key = Ed25519PrivateKey.generate()
    signed = sign_manifest(
        build_signed_manifest(
            source,
            pack_id=compiled.artifact.pack_id,
            version=compiled.artifact.version,
            publisher_id=compiled.artifact.publisher_lineage,
            core_compatibility=">=0",
            contract_versions={
                operation.contract_id: operation.contract_version
                for function in compiled.artifact.functions
                for operation in function.operations
            },
            requested_capabilities=manifest["requirements"]["capabilities"],
        ),
        key,
    )
    signed_path = source / SIGNED_MANIFEST_RELATIVE
    signed_path.parent.mkdir(parents=True, exist_ok=True)
    signed_path.write_text(json.dumps(signed) + "\n")
    public = tmp_path / "publisher.pem"
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return source, public


def admit_ui_source(source: Path, public: Path, owner: Path) -> tuple[Path, str]:
    """Use normal signature verification, Host admission and authenticated CAS."""
    preview = preview_signed_pack(source, public)
    admitted = commit_signed_pack(
        source,
        public,
        expected_preview_digest=preview["preview_digest"],
        trust_store_path=owner / "pack_control/publisher-trust.json",
    )
    digest = str(admitted["artifact_digest"])
    return resolve_admitted_pack_root(
        str(preview["pack_id"]), artifact_digest=digest
    ), digest


@pytest.fixture
def cas_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CapturedPackUIAssets:
    """Capture a genuinely signed and admitted CAS root in an isolated owner."""
    owner = tmp_path / "owner"
    owner.mkdir()
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(owner))
    monkeypatch.setenv("RUMI_USER_DATA", str(owner))
    source, public = signed_ui_source(tmp_path)
    root, digest = admit_ui_source(source, public, owner)
    assert root != source
    return CapturedPackUIAssets.capture(
        pack_id="conformance.minimal.echo",
        artifact_digest=digest,
        pack_root=root,
        owner_root=owner,
        index_file="shell.html",
        assert_current=lambda: None,
    )


def test_reader_serves_exact_signed_assets_and_bounded_spa_fallback(
    cas_assets: CapturedPackUIAssets,
) -> None:
    assert (
        cas_assets.read("/shell-app.js", False)[0]
        == b"console.log('signed-selected-ui');"
    )
    assert cas_assets.read("/fonts/local.woff2", False)[0] == b"signed-local-font"
    assert cas_assets.read("/conversation/one", True) == cas_assets.read("/", True)
    for path in (
        "/missing.js",
        "/missing",
        "/pack.v4.json",
        "/.shell-bundle-manifest.json",
    ):
        with pytest.raises((FileNotFoundError, CapturedPackUIAssetDenied)):
            cas_assets.read(path, False)


@pytest.mark.parametrize(
    "path",
    [
        "/../shell.html",
        "/%2e%2e/shell.html",
        "/%252e%252e/shell.html",
        "/fonts/../../shell.html",
        "/fonts\\local.woff2",
        "/.secret/route",
        "//shell.html",
    ],
)
def test_reader_rejects_unsafe_paths_even_with_spa_fallback(
    cas_assets: CapturedPackUIAssets,
    path: str,
) -> None:
    with pytest.raises(CapturedPackUIAssetDenied):
        cas_assets.read(path, True)


@pytest.mark.parametrize(
    "mutation", ["bytes", "missing", "symlink", "hardlink", "ancestor"]
)
def test_reader_rejects_changed_cas_bytes_and_file_boundaries(
    cas_assets: CapturedPackUIAssets,
    tmp_path: Path,
    mutation: str,
) -> None:
    root = cas_assets.pack_root
    target = root / "ui/fonts/local.woff2"
    target.parent.chmod(0o700)
    target.chmod(0o600)
    if mutation == "bytes":
        target.write_bytes(b"modified-after-capture")
    elif mutation == "missing":
        target.unlink()
    elif mutation == "symlink":
        outside = tmp_path / "outside.woff2"
        outside.write_bytes(b"signed-local-font")
        target.unlink()
        target.symlink_to(outside)
    elif mutation == "hardlink":
        os.link(target, tmp_path / "second-link.woff2")
    else:
        root.joinpath("ui").chmod(0o700)
        target.parent.rename(root / "ui/original-fonts")
        (root / "ui/fonts").symlink_to(
            root / "ui/original-fonts", target_is_directory=True
        )
    with pytest.raises((CapturedPackUIAssetDenied, RuntimeError, OSError)):
        cas_assets.read("/fonts/local.woff2", False)


def test_reader_rejects_owner_change(
    cas_assets: CapturedPackUIAssets,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = tmp_path / "other-owner"
    other.mkdir()
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(other))
    monkeypatch.setenv("RUMI_USER_DATA", str(other))
    with pytest.raises(CapturedPackUIAssetDenied, match="owner"):
        cas_assets.read("/shell-app.js", False)


def test_capture_requires_ui_for_external_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = tmp_path / "owner"
    owner.mkdir()
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(owner))
    monkeypatch.setenv("RUMI_USER_DATA", str(owner))
    source, public = signed_ui_source(tmp_path, with_ui=False)
    root, digest = admit_ui_source(source, public, owner)
    with pytest.raises(CapturedPackUIAssetDenied, match="incomplete"):
        CapturedPackUIAssets.capture(
            pack_id="conformance.minimal.echo",
            artifact_digest=digest,
            pack_root=root,
            owner_root=owner,
            index_file="shell.html",
            assert_current=lambda: None,
        )


def test_capture_checks_before_and_after_read(
    cas_assets: CapturedPackUIAssets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_runtime import captured_pack_ui_assets_v4 as assets_module

    checks = 0
    original = assets_module._read_regular_from_root_descriptor

    def raced_read(
        descriptor: int,
        relative: str,
        **limits: int,
    ) -> tuple[bytes, int]:
        nonlocal checks
        result = original(descriptor, relative, **limits)
        checks += 1
        return result

    def current() -> None:
        if checks:
            raise RuntimeError("activation changed during read")

    from dataclasses import replace

    raced = replace(cas_assets, assert_current=current)
    monkeypatch.setattr(assets_module, "_read_regular_from_root_descriptor", raced_read)
    with pytest.raises(RuntimeError, match="activation"):
        raced.read("/shell-app.js", False)


def test_regular_asset_replaced_with_fifo_is_rejected_without_blocking(
    cas_assets: CapturedPackUIAssets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real leaf replacement reaches fstat rather than waiting for a writer."""
    from core_runtime import external_pack_catalog_v4 as catalog_module

    target = cas_assets.pack_root / "ui/shell-app.js"
    target.parent.chmod(0o700)
    original = os.open
    swapped = False

    def racing_open(path: str | Path, flags: int, **options: int) -> int:
        nonlocal swapped
        if path == target.name and not swapped:
            assert flags & os.O_NONBLOCK
            swapped = True
            target.unlink()
            os.mkfifo(target)
        return original(path, flags, **options)

    monkeypatch.setattr(catalog_module.os, "open", racing_open)
    with pytest.raises(RuntimeError, match="unsafe"):
        cas_assets.read("/shell-app.js", False)
    assert swapped


def test_oversized_asset_is_rejected_before_any_content_allocation(
    cas_assets: CapturedPackUIAssets,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 32 MiB UI bound is enforced before the first read, not after hashing."""
    target = cas_assets.pack_root / "ui/shell-app.js"
    target.chmod(0o600)
    with target.open("r+b") as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    from core_runtime import external_pack_catalog_v4 as catalog_module

    def forbidden_read(_descriptor: int, _size: int) -> bytes:
        pytest.fail("oversized UI bytes must not be allocated")

    monkeypatch.setattr(catalog_module.os, "read", forbidden_read)
    with pytest.raises(RuntimeError, match="unsafe"):
        cas_assets.read("/shell-app.js", False)


@pytest.mark.parametrize("limit", [True, 1.5, None, "1", 0, 128 * 1024 * 1024 + 1])
def test_invalid_descriptor_size_bound_is_denied_before_any_filesystem_access(
    limit: object,
) -> None:
    from core_runtime.external_pack_catalog_v4 import _read_regular_from_root_descriptor

    with pytest.raises(RuntimeError, match="size bound"):
        _read_regular_from_root_descriptor(
            -1,
            "not-opened",
            max_file_bytes=limit,  # type: ignore[arg-type]
        )


def test_legacy_compatibility_mount_must_equal_captured_builtin_root(
    cas_assets: CapturedPackUIAssets,
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    legacy = replace(cas_assets, legacy_builtin=True)
    mounts = (
        {
            "path_prefix": "/p",
            "web_root": tmp_path / "wrong-owner" / legacy.pack_id / "ui",
            "spa_fallback": True,
            "index_file": "shell.html",
            "auth_required": True,
        },
    )
    with pytest.raises(CapturedPackUIAssetDenied, match="bundled root"):
        bind_captured_pack_web_mounts(mounts, lambda *_: legacy)
