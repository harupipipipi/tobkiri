"""Tests for one-way compatibility projections from canonical Pack v3 data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.offline_legacy_projection import (
    ManifestProjectionError,
    generate_legacy_ecosystem_projection,
    project_legacy_ecosystem,
    source_manifest_identity,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "pack_v3" / "minimal_service.json"


def test_projection_is_one_way_and_records_canonical_provenance() -> None:
    manifest = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    projection = project_legacy_ecosystem(manifest)

    assert projection["pack_id"] == "example_echo_service_pack"
    assert projection["connectivity"] == {
        "provides": ["rumi.service.example.echo.v1"],
        "requires": [],
    }
    assert projection["host_execution"] is False
    assert projection["metadata"]["read_only_projection"] is True
    assert projection["metadata"]["generated_from"]["source_content_hash"] == (
        source_manifest_identity(manifest)
    )


def test_projection_check_rejects_stale_or_hand_edited_output(tmp_path: Path) -> None:
    canonical = tmp_path / "rumi.pack.v3.json"
    output = tmp_path / "ecosystem.json"
    canonical.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    generate_legacy_ecosystem_projection(canonical, output)
    source_identity = generate_legacy_ecosystem_projection(
        canonical,
        output,
        check=True,
    )
    assert source_identity.startswith("sha256:")

    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ManifestProjectionError, match="drift"):
        generate_legacy_ecosystem_projection(canonical, output, check=True)


def test_projection_never_guesses_contract_requirements_as_pack_dependencies() -> None:
    manifest = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    manifest["contracts"]["requires"] = [
        {
            "id": "rumi.resource.example.echo.v1",
            "version_range": ">=1.0,<2.0",
            "cardinality": "one",
            "optional": False,
        }
    ]

    projection = project_legacy_ecosystem(manifest)

    assert projection["connectivity"]["requires"] == [
        "rumi.resource.example.echo.v1"
    ]
    assert projection["dependencies"] == {}


def test_projection_preserves_explicit_legacy_activation_contract() -> None:
    manifest = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    manifest["extensions"] = {
        "rumi.legacy_projection": {
            "pack_id": "rumi_example_pack",
            "dependencies": {
                "rumi_workspace_mount_pack": ">=1.0.0,<2.0.0"
            },
            "host_execution": True,
        }
    }

    projection = project_legacy_ecosystem(manifest)

    assert projection["pack_id"] == "rumi_example_pack"
    assert projection["dependencies"] == {
        "rumi_workspace_mount_pack": ">=1.0.0,<2.0.0"
    }
    assert projection["host_execution"] is True


def test_projection_rejects_a_noncanonical_manifest(tmp_path: Path) -> None:
    manifest = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    manifest["unknown"] = True
    canonical = tmp_path / "rumi.pack.v3.json"
    canonical.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ManifestProjectionError, match="canonical manifest is invalid"):
        generate_legacy_ecosystem_projection(canonical, tmp_path / "ecosystem.json")
