"""Compose current Defaults with an explicitly authenticated development Shell.

The normal release generator remains unchanged. This development-only wrapper
uses its complete transaction/validation path in private staging, preserves
three authenticated prebuilt documents verbatim, then publishes both roots.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from tobkiri_protocol.canonical import strict_loads

from . import generate_packaged_defaultspack_v4_bundle as generator

if TYPE_CHECKING:
    from core_runtime.debug_prebuilt_shell_v4 import VerifiedPrebuiltShellInput


def stage_debug_prebuilt_bundle(
    *,
    prebuilt: VerifiedPrebuiltShellInput,
    bundle_root: Path,
    artifact_root: Path,
    source_provenance_file: Path,
) -> None:
    """Replace existing prepared outputs with current Packs and an exact Shell.

    The bundle output must already exist, matching the normal generator's
    owned-output contract. No authority or bootstrap fallback is created for
    a fresh owner or an absent prepared bundle.
    """
    from core_runtime.debug_prebuilt_shell_v4 import assert_prebuilt_shell_current
    from tobkiri_protocol.secure_persistence import SecureDirectory

    assert_prebuilt_shell_current(prebuilt)
    generator._preverified_source_provenance(source_provenance_file)
    expected_documents = {
        "shell.tauri.default.shell.v1.json",
        "packs/shell.tauri.default.pack.v4.json",
        "packs/runtime.tauri.application.default.pack.v4.json",
    }
    if {path for path, _raw in prebuilt.documents} != expected_documents:
        raise ValueError("prebuilt input does not select the supported Defaults Shell")
    current_host = SecureDirectory(
        generator.ROOT / "ecosystem/defaultspack", create=False
    )

    def validate_input() -> None:
        assert_prebuilt_shell_current(prebuilt)
        for path, _digest in prebuilt.host_assets:
            current = current_host.read_bytes_bounded(path, max_bytes=8 * 1024 * 1024)
            retained = (prebuilt.root / "host-assets" / path).read_bytes()
            if current != retained:
                raise ValueError(
                    "current source Host asset differs from the active prebuilt Shell"
                )

    validate_input()
    bundle_root = bundle_root.absolute()
    artifact_root = artifact_root.absolute()
    transaction = generator._new_transaction(bundle_root, artifact_root)
    try:
        staged_bundle = transaction / "bundle"
        staged_artifacts = transaction / "artifacts"
        generator._snapshot_tree(
            generator.ROOT / "ecosystem/defaultspack/v4",
            staged_bundle,
        )
        variant = strict_loads(prebuilt.variant_json)
        # Run the complete normal generator against current source first. Its
        # intermediate revision remains private to this enclosing transaction.
        generator.stage_packaged_bundle(
            source_artifact=prebuilt.root
            / "platform-artifacts"
            / variant["relative_path"],
            bundle_root=staged_bundle,
            artifact_root=staged_artifacts,
            relative_path=variant["relative_path"],
            entrypoint=variant["entrypoint"],
            platform=variant["platform"],
            architecture=variant["architecture"],
            bundle_identity=variant["bundle_identity"],
            source_provenance_file=source_provenance_file,
        )
        for path, raw in prebuilt.documents:
            (staged_bundle / path).write_bytes(raw)
        shell = strict_loads(
            dict(prebuilt.documents)["shell.tauri.default.shell.v1.json"]
        )
        generator.refresh_packaged_profile_and_lock(
            staged_bundle,
            variant=variant,
            definition_revision=shell["definition_revision"],
            generator_path=Path(__file__),
            generator_name="defaultspack-v4-debug-prebuilt-packager",
        )
        generator._validate_staged_bundle(
            staged_bundle,
            staged_artifacts,
            variant["relative_path"],
            variant["entrypoint"],
            variant["platform"],
            variant["architecture"],
            variant["bundle_identity"],
            variant["artifact_digest"],
            variant["entrypoint_digest"],
        )
        assert_prebuilt_shell_current(prebuilt)
        generator._publish_directories(
            staged_bundle,
            bundle_root,
            staged_artifacts,
            artifact_root,
            validate_publication=validate_input,
        )
    finally:
        generator._remove_owned(transaction)


def main() -> int:
    """Prepare explicit development inputs through normal full active capture."""
    from core_runtime.bundled_artifact_retention_v4 import capture_retention_origin
    from core_runtime.debug_prebuilt_shell_v4 import verified_active_prebuilt_shell
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "owner-root",
        "old-bundle-root",
        "old-artifact-root",
        "old-host-root",
        "bundle-root",
        "artifact-root",
        "source-provenance-file",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--architecture", required=True)
    args = parser.parse_args()
    install_defaultspack_profile_runtime()
    origin = capture_retention_origin(args.owner_root)
    with verified_active_prebuilt_shell(
        origin,
        bundle_root=args.old_bundle_root,
        artifact_root=args.old_artifact_root,
        host_asset_root=args.old_host_root,
    ) as prebuilt:
        variant = strict_loads(prebuilt.variant_json)
        if (variant["platform"], variant["architecture"]) != (
            args.platform,
            args.architecture,
        ):
            raise ValueError(
                "requested development target differs from the active Shell"
            )
        stage_debug_prebuilt_bundle(
            prebuilt=prebuilt,
            bundle_root=args.bundle_root,
            artifact_root=args.artifact_root,
            source_provenance_file=args.source_provenance_file,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
