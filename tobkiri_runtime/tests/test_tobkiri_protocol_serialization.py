from __future__ import annotations

import json
from pathlib import Path

import pytest

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.errors import CanonicalizationError, SchemaValidationError
from tobkiri_protocol.migration import (
    load_and_migrate_legacy_profile,
    migrate_legacy_profile,
)
from tobkiri_protocol.scanners import scan_v4_scope
from tobkiri_protocol.serialization import load_json_document
from tobkiri_protocol.validation import load_schema, validate_document


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tobkiri_protocol" / "fixtures"
DIGEST = "sha256:" + "0" * 64


def test_all_v4_schemas_are_valid_json_schema_documents() -> None:
    schema_paths = sorted((ROOT / "tobkiri_protocol" / "schemas").glob("*.schema.json"))
    assert schema_paths
    for path in schema_paths:
        assert load_schema(path.name)["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_valid_pack_manifest_and_request_round_trip_canonically() -> None:
    manifest = load_json_document(FIXTURES / "pack_manifest.v4.json", "pack")
    request = load_json_document(FIXTURES / "request_frame.v1.json", "request")
    assert strict_loads(canonical_json(manifest)) == manifest
    assert strict_loads(canonical_json(request)) == request
    assert canonical_digest(manifest).startswith("sha256:")


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        0,
        -(2**53 - 1),
        {"unicode": "Tobkiri\u00a0\u3042", "items": [1, "two", None]},
        {"nested": [{"alpha": [False, 7]}, {"beta": "value"}]},
    ],
)
def test_canonical_json_round_trip_property(value: object) -> None:
    """Every value in the supported I-JSON subset has one stable encoding."""
    encoded = canonical_json(value)
    assert strict_loads(encoded) == value
    assert canonical_json(strict_loads(encoded)) == encoded


@pytest.mark.parametrize(
    "value",
    [
        '{"a":1,"a":2}',
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":1.0}',
        json.dumps({"a": 2**53}),
    ],
)
def test_adversarial_json_is_rejected_before_schema_validation(value: str) -> None:
    with pytest.raises(CanonicalizationError):
        strict_loads(value)


def test_deep_json_is_rejected() -> None:
    value = "[" * 65 + "0" + "]" * 65
    with pytest.raises(CanonicalizationError, match="depth"):
        strict_loads(value)


def test_request_payload_cannot_smuggle_authority_fields() -> None:
    request = json.loads((FIXTURES / "request_frame.v1.json").read_text(encoding="utf-8"))
    request["payload"] = {"approved": True}
    with pytest.raises(SchemaValidationError, match="forbidden authority"):
        validate_document(request, "request")


def test_legacy_profile_migration_is_review_only_and_does_not_copy_authority() -> None:
    legacy = json.loads((FIXTURES / "legacy_profile.v3.json").read_text(encoding="utf-8"))
    result = migrate_legacy_profile(legacy, repository_root=ROOT)

    assert result["status"] == "review_required"
    assert result["activation_eligible"] is False
    assert result["authority_minted"] is False
    profile = result["profile"]
    assert profile is not None
    assert profile["state"] == "needs_resolution"
    assert profile["base"]["pack_id"] == "defaults-basepack"
    assert profile["base"]["artifact_digest"] is None
    assert profile["authority_references"] == []
    assert profile["profile_authority_snapshot_digest"] is None
    assert profile["legacy_migration"]["command_digest"].startswith("sha256:")
    assert "permissions" in " ".join(result["dropped_authority_fields"])
    serialized = json.dumps(profile, sort_keys=True)
    for forbidden in ("secret_material", "approval", "grant", "lease", "token"):
        assert forbidden not in serialized


def test_yaml_migration_rejects_duplicate_keys(tmp_path: Path) -> None:
    source = tmp_path / "duplicate.profile.yaml"
    source.write_text(
        "version: 3\nprofile_id: duplicate\nprofile_id: other\nbase_pack: defaultspack\n",
        encoding="utf-8",
    )
    result = load_and_migrate_legacy_profile(source, repository_root=ROOT)
    assert result["status"] == "blocked"
    assert result["activation_eligible"] is False


def test_malformed_legacy_json_is_blocked_without_activation_candidate(tmp_path: Path) -> None:
    source = tmp_path / "malformed.profile.json"
    source.write_text('{"profile_id": "broken",', encoding="utf-8")
    result = load_and_migrate_legacy_profile(source, repository_root=ROOT)
    assert result["status"] == "blocked"
    assert result["profile"] is None
    assert result["authority_minted"] is False


def test_ambiguous_legacy_profile_fails_closed() -> None:
    legacy = json.loads((FIXTURES / "ambiguous_profile.v3.json").read_text(encoding="utf-8"))
    result = migrate_legacy_profile(legacy, repository_root=ROOT)
    assert result["status"] == "blocked"
    assert result["profile"] is None
    assert result["authority_minted"] is False


def test_incompatible_legacy_ids_are_not_guessed() -> None:
    result = migrate_legacy_profile(
        {
            "version": 3,
            "profile_id": "safe-profile",
            "base_pack": "rumi:unknown-pack",
            "packs": ["rumi:unknown-pack"],
        },
        repository_root=ROOT,
    )
    assert result["status"] == "blocked"
    assert result["profile"] is None


def test_v4_scope_has_no_legacy_or_fallback_findings() -> None:
    findings = scan_v4_scope(ROOT)
    assert findings == [], [finding.to_dict() for finding in findings]
