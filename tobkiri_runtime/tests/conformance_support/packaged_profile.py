"""Faithful packaged Profile bundle fixtures for conformance tests."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Mapping

from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
from scripts import generate_packaged_defaultspack_v4_bundle as packaged_generator
from scripts.generator_source_manifest import materialize_source_snapshot
from tobkiri_protocol.platform_artifact import verify_platform_artifact


_INJECTED_BUNDLE_ROOT: Path | None = None


def inject_packaged_profile_bundle(root: Path | None) -> None:
    """Install the session fixture dependency for test code only."""

    global _INJECTED_BUNDLE_ROOT
    _INJECTED_BUNDLE_ROOT = root


def packaged_profile_bundle_root() -> Path:
    """Return the explicitly injected packaged fixture root."""

    if _INJECTED_BUNDLE_ROOT is None:
        raise RuntimeError("packaged Profile test dependency was not injected")
    return _INJECTED_BUNDLE_ROOT


def load_packaged_profile_catalog() -> BundledCatalog:
    """Load the session's canonical packaged Profile fixture."""

    return BundledCatalog.load(packaged_profile_bundle_root())


def create_test_source_provenance(
    source_root: Path,
    destination: Path,
    *,
    provenance_record: Mapping[str, object],
) -> Path:
    """Create a private fixture snapshot and its core-shaped provenance file."""
    owner = destination / "sealed-source-owner"
    owner.mkdir(mode=0o700, parents=True, exist_ok=False)
    owner.chmod(0o700)
    snapshot = owner / "source"
    materialize_source_snapshot(source_root, snapshot)
    snapshot.chmod(0o700)
    manifest = snapshot / "packaged_defaultspack_source_manifest.v1.json"
    provenance_path = snapshot / "packaging-source-provenance.v1.json"
    provenance_path.write_bytes(
        json.dumps(
            {
                "schema": "io.tobkiri.packaging-source-provenance.v1",
                "source_commit": provenance_record["source_commit"],
                "source_tree": provenance_record["source_tree"],
                "source_clean": provenance_record["source_clean"],
                "source_manifest_sha256": hashlib.sha256(
                    manifest.read_bytes()
                ).hexdigest(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    provenance_path.chmod(0o400)
    snapshot.chmod(0o500)
    return provenance_path


def build_packaged_profile_bundle(
    source_bundle: Path,
    destination: Path,
    *,
    source_provenance_file: Path,
) -> Path:
    """Build a verified Linux/x86_64 Profile bundle around fixture bytes.

    The generator is intentionally a preverified-snapshot consumer.  The test
    fixture creates the core-shaped provenance file; the generator receives
    only that one file and cannot silently rediscover Git.
    """

    bundle = destination / "defaultspack" / "v4"
    artifacts = destination / "defaultspack" / "platform-artifacts"
    executable = destination / "verified-release" / "Tobkiri.AppImage"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 10 + b">\x00fixture")
    executable.chmod(0o755)
    shutil.copytree(source_bundle, bundle)
    packaged_generator.stage_packaged_bundle(
        source_artifact=executable,
        bundle_root=bundle,
        artifact_root=artifacts,
        relative_path="Tobkiri.AppImage",
        entrypoint="Tobkiri.AppImage",
        platform="linux",
        architecture="x86_64",
        bundle_identity="io.tobkiri.shell.tauri",
        source_provenance_file=source_provenance_file,
    )
    catalog = BundledCatalog.load(bundle)
    shell = catalog.shells["shell.tauri.default"]
    variants = shell["launch"]["variants"]
    if len(variants) != 1 or catalog.artifact_root is None:
        raise AssertionError("packaged Profile fixture has no exact Shell artifact")
    verify_platform_artifact(catalog.artifact_root, variants[0])
    return bundle


__all__ = [
    "build_packaged_profile_bundle",
    "create_test_source_provenance",
    "inject_packaged_profile_bundle",
    "load_packaged_profile_catalog",
    "packaged_profile_bundle_root",
]
