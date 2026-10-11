"""Contract tests for the hermetic packaged Profile fixture."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest

from tests.conformance_support.packaged_profile import load_packaged_profile_catalog
from tobkiri_protocol.errors import ProtocolError
from tobkiri_protocol.platform_artifact import verify_platform_artifact


def _selected_variant() -> tuple[Path, dict[str, object]]:
    catalog = load_packaged_profile_catalog()
    assert catalog.artifact_root is not None
    shell = catalog.shells["shell.tauri.default"]
    variants = shell["launch"]["variants"]
    assert len(variants) == 1
    return catalog.artifact_root, copy.deepcopy(variants[0])


def test_packaged_profile_fixture_uses_exact_production_shell_contract() -> None:
    """The shared fixture is a real packaged catalog verified by production code."""

    catalog = load_packaged_profile_catalog()
    artifact_root, variant = _selected_variant()
    shell = catalog.shells["shell.tauri.default"]
    profile = catalog.profiles["defaults"]

    assert shell["availability"] == "verified"
    assert variant["platform"] == "linux"
    assert variant["architecture"] == "x86_64"
    assert variant["relative_path"] == "Tobkiri.AppImage"
    assert variant["entrypoint"] == "Tobkiri.AppImage"
    assert variant["bundle_identity"] == "io.tobkiri.shell.tauri"
    assert profile["shell"]["artifact_digest"] == variant["artifact_digest"]
    assert profile["shell"]["executable_artifact_digest"] == variant[
        "entrypoint_digest"
    ]
    assert verify_platform_artifact(artifact_root, variant) == (
        artifact_root / "Tobkiri.AppImage"
    )


@pytest.mark.parametrize("case", ["missing", "wrong-architecture", "tampered"])
def test_packaged_profile_fixture_shell_failures_are_closed(
    tmp_path: Path,
    case: str,
) -> None:
    """Missing, mismatched, and modified fixture artifacts remain untrusted."""

    artifact_root, variant = _selected_variant()
    copied = tmp_path / "platform-artifacts"
    shutil.copytree(artifact_root, copied)
    executable = copied / "Tobkiri.AppImage"
    if case == "missing":
        executable.unlink()
    elif case == "wrong-architecture":
        variant["architecture"] = "arm64"
    else:
        executable.write_bytes(executable.read_bytes() + b"tampered")

    with pytest.raises((OSError, ProtocolError)):
        verify_platform_artifact(copied, variant)
