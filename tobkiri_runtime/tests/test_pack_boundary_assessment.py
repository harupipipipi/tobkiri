from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.quality.check_pack_boundary_assessment import (
    ASSESSMENT_PATH,
    BOUNDARY_CRITERIA,
    DOCUMENT_ROLE,
    SCHEMA_VERSION,
    discover_pack_locations,
    find_runtime_references,
    render_assessment,
    validate_assessment,
)

pytestmark = pytest.mark.contract
REPO_ROOT = Path(__file__).resolve().parents[2]


def _payload() -> dict[str, object]:
    return json.loads(ASSESSMENT_PATH.read_text(encoding="utf-8"))


def _write_manifest(path: Path, contents: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _accepted_row(
    payload: dict[str, object],
    repo_root: Path = REPO_ROOT,
    support_path: Path | None = None,
) -> dict[str, object]:
    row = payload["records"][0]
    assert isinstance(row, dict)
    row.update(
        {
            "review_status": "accepted",
            "lifecycle_owner": "runtime-team",
            "state_owner": "runtime-team",
            "external_effects": [],
            "trust_domain": "first-party",
            "execution_mode": "in-process",
            "canonical_owner": "core-runtime",
            "disposition": "keep",
            "boundary_criteria": ["independent_lifecycle"],
            "assessment_justification": "A reviewed lifecycle requires this boundary.",
        }
    )
    evidence = row["evidence"]
    assert isinstance(evidence, list)
    support_path = support_path or (
        repo_root / "tobkiri_runtime/docs/macos-unsigned-distribution.md"
    )
    evidence.append(
        {
            "path": support_path.relative_to(repo_root).as_posix(),
            "sha256": hashlib.sha256(support_path.read_bytes()).hexdigest(),
        }
    )
    return row


def test_assessment_is_non_runtime_and_matches_discovered_manifests() -> None:
    payload = _payload()

    assert payload["runtime_authority"] is False
    assert payload["activation_input"] is False
    assert validate_assessment(payload, REPO_ROOT) == []
    assert len(payload["records"]) == len(discover_pack_locations(REPO_ROOT))
    assert {row["review_status"] for row in payload["records"]} == {"unreviewed"}


def test_discovery_matches_direct_nested_legacy_and_core_runtime_shapes(
    tmp_path: Path,
) -> None:
    root = tmp_path
    _write_manifest(root / "tobkiri_runtime/ecosystem/direct/ecosystem.json")
    _write_manifest(root / "tobkiri_runtime/ecosystem/direct/backend/ecosystem.json")
    _write_manifest(root / "tobkiri_runtime/ecosystem/nested/zeta/ecosystem.json")
    _write_manifest(root / "tobkiri_runtime/ecosystem/nested/alpha/ecosystem.json")
    _write_manifest(
        root / "tobkiri_runtime/ecosystem/packs/legacy/backend/ecosystem.json"
    )
    _write_manifest(root / "tobkiri_runtime/ecosystem/dupe/ecosystem.json")
    _write_manifest(
        root / "tobkiri_runtime/ecosystem/packs/dupe/backend/ecosystem.json"
    )
    _write_manifest(
        root / "tobkiri_runtime/core_runtime/core_pack/core_direct/ecosystem.json"
    )
    _write_manifest(
        root
        / "tobkiri_runtime/core_runtime/core_pack/core_nested/backend/ecosystem.json"
    )

    locations = {
        location.pack_id: location for location in discover_pack_locations(root)
    }

    assert locations["direct"].ecosystem_json_path.name == "ecosystem.json"
    assert locations["direct"].pack_subdir.name == "direct"
    assert locations["nested"].pack_subdir.name == "alpha"
    assert locations["legacy"].is_legacy is True
    assert locations["legacy"].pack_subdir.name == "backend"
    assert locations["dupe"].is_legacy is False
    assert locations["dupe"].ecosystem_json_path.parent.name == "dupe"
    assert locations["core_direct"].is_core is True
    assert locations["core_nested"].is_core is True
    assert locations["core_nested"].pack_subdir.name == "backend"


def test_regeneration_resets_reviewed_rows_when_manifest_evidence_changes(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "tobkiri_runtime/ecosystem/demo/ecosystem.json"
    _write_manifest(manifest, '{"pack_id": "demo"}\n')
    _write_manifest(tmp_path / "tobkiri_runtime/docs/adr/review.md", "ADR evidence\n")
    reviewed = render_assessment(tmp_path)
    row = _accepted_row(reviewed)
    support = tmp_path / "tobkiri_runtime/review/demo.md"
    _write_manifest(support, "review evidence\n")
    row["evidence"][1] = {
        "path": support.relative_to(tmp_path).as_posix(),
        "sha256": hashlib.sha256(support.read_bytes()).hexdigest(),
    }

    assert (
        render_assessment(tmp_path, reviewed)["records"][0]["review_status"]
        == "accepted"
    )

    _write_manifest(manifest, '{"pack_id": "demo", "changed": true}\n')
    regenerated = render_assessment(tmp_path, reviewed)

    assert regenerated["records"][0]["review_status"] == "unreviewed"
    assert regenerated["records"][0]["assessment_justification"] == ""


@pytest.mark.parametrize("field", ["runtime_authority", "activation_input"])
def test_assessment_rejects_runtime_authority(field: str) -> None:
    payload = _payload()
    payload[field] = True

    assert any(field in error for error in validate_assessment(payload, REPO_ROOT))


def test_accepted_rows_require_adr_criteria_and_justification() -> None:
    payload = copy.deepcopy(_payload())
    row = _accepted_row(payload)
    row["boundary_criteria"] = []
    row["assessment_justification"] = ""

    errors = validate_assessment(payload, REPO_ROOT)

    assert any("accepted row requires boundary_criteria" in error for error in errors)
    assert any(
        "accepted row requires assessment_justification" in error for error in errors
    )


def test_accepted_rows_reject_unknown_criteria() -> None:
    payload = copy.deepcopy(_payload())
    row = _accepted_row(payload)
    row["boundary_criteria"] = ["not-an-adr-criterion"]

    errors = validate_assessment(payload, REPO_ROOT)

    assert BOUNDARY_CRITERIA
    assert any("unsupported boundary_criteria" in error for error in errors)


def test_accepted_rows_require_supporting_evidence_beyond_manifest() -> None:
    payload = copy.deepcopy(_payload())
    row = _accepted_row(payload)
    row["evidence"] = row["evidence"][:1]

    errors = validate_assessment(payload, REPO_ROOT)

    assert any(
        "requires supporting evidence beyond manifest" in error for error in errors
    )


def test_accepted_rows_reject_adr_as_the_only_supporting_evidence() -> None:
    payload = copy.deepcopy(_payload())
    row = _accepted_row(payload)
    evidence = row["evidence"]
    assert isinstance(evidence, list)
    adr_path = REPO_ROOT / "tobkiri_runtime/docs/adr/0001-pack-boundary-criteria.md"
    row["evidence"] = [
        evidence[0],
        {
            "path": adr_path.relative_to(REPO_ROOT).as_posix(),
            "sha256": hashlib.sha256(adr_path.read_bytes()).hexdigest(),
        },
    ]

    errors = validate_assessment(payload, REPO_ROOT)

    assert any("requires non-ADR supporting evidence" in error for error in errors)


def test_reviewed_evidence_rejects_absolute_alias_of_manifest() -> None:
    payload = copy.deepcopy(_payload())
    row = _accepted_row(payload)
    manifest_path = row["manifest_path"]
    assert isinstance(manifest_path, str)
    manifest_file = REPO_ROOT / manifest_path
    evidence = row["evidence"]
    assert isinstance(evidence, list)
    evidence.insert(
        1,
        {
            "path": manifest_file.resolve().as_posix(),
            "sha256": hashlib.sha256(manifest_file.read_bytes()).hexdigest(),
        },
    )

    errors = validate_assessment(payload, REPO_ROOT)

    assert any("repository-relative, not absolute" in error for error in errors)
    assert not any("requires one manifest evidence item" in error for error in errors)


def test_reviewed_evidence_rejects_case_alias_of_manifest_on_casefolding_fs() -> None:
    payload = copy.deepcopy(_payload())
    row = _accepted_row(payload)
    manifest_path = row["manifest_path"]
    assert isinstance(manifest_path, str)
    parts = Path(manifest_path).parts
    case_alias = Path(parts[0].upper(), *parts[1:])
    manifest_file = REPO_ROOT / manifest_path
    alias_file = REPO_ROOT / case_alias
    try:
        aliases_manifest = alias_file.is_file() and alias_file.samefile(manifest_file)
    except OSError:
        aliases_manifest = False
    if not aliases_manifest:
        pytest.skip("requires a case-insensitive filesystem alias")

    evidence = row["evidence"]
    assert isinstance(evidence, list)
    evidence[1] = {
        "path": case_alias.as_posix(),
        "sha256": hashlib.sha256(alias_file.read_bytes()).hexdigest(),
    }

    errors = validate_assessment(payload, REPO_ROOT)

    assert any("duplicates filesystem evidence" in error for error in errors)
    assert any("requires one manifest evidence item" in error for error in errors)
    assert any(
        "requires supporting evidence beyond manifest" in error for error in errors
    )


def test_casefolding_adr_alias_is_not_non_adr_support() -> None:
    payload = copy.deepcopy(_payload())
    row = _accepted_row(payload)
    adr_path = REPO_ROOT / "tobkiri_runtime/docs/adr/0001-pack-boundary-criteria.md"
    alias_parts = adr_path.relative_to(REPO_ROOT).parts
    case_alias = Path(alias_parts[0].upper(), *alias_parts[1:])
    alias_file = REPO_ROOT / case_alias
    try:
        aliases_adr = alias_file.is_file() and alias_file.samefile(adr_path)
    except OSError:
        aliases_adr = False
    if not aliases_adr:
        pytest.skip("requires a case-insensitive filesystem alias")

    evidence = row["evidence"]
    assert isinstance(evidence, list)
    evidence[1] = {
        "path": case_alias.as_posix(),
        "sha256": hashlib.sha256(alias_file.read_bytes()).hexdigest(),
    }

    errors = validate_assessment(payload, REPO_ROOT)

    assert any("requires non-ADR supporting evidence" in error for error in errors)


def test_reviewed_evidence_rejects_hardlink_alias_of_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "tobkiri_runtime/ecosystem/demo/ecosystem.json"
    _write_manifest(manifest, '{"pack_id": "demo"}\n')
    _write_manifest(tmp_path / "tobkiri_runtime/docs/adr/review.md", "ADR evidence\n")
    reviewed = render_assessment(tmp_path)
    row = _accepted_row(reviewed)
    support = tmp_path / "tobkiri_runtime/review/demo-hardlink.json"
    support.parent.mkdir(parents=True, exist_ok=True)
    try:
        support.hardlink_to(manifest)
    except OSError:
        pytest.skip("filesystem does not support hard links in the test directory")
    evidence = row["evidence"]
    assert isinstance(evidence, list)
    evidence[1] = {
        "path": support.relative_to(tmp_path).as_posix(),
        "sha256": hashlib.sha256(support.read_bytes()).hexdigest(),
    }

    errors = validate_assessment(reviewed, tmp_path)

    assert any("duplicates filesystem evidence" in error for error in errors)
    assert any("requires one manifest evidence item" in error for error in errors)
    assert any(
        "requires supporting evidence beyond manifest" in error for error in errors
    )


def test_reviewed_evidence_rejects_hardlink_alias_of_adr_support(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "tobkiri_runtime/ecosystem/demo/ecosystem.json"
    adr_evidence = tmp_path / "tobkiri_runtime/docs/adr/review.md"
    support = tmp_path / "tobkiri_runtime/review/adr-hardlink.md"
    _write_manifest(manifest, '{"pack_id": "demo"}\n')
    _write_manifest(adr_evidence, "ADR evidence\n")
    support.parent.mkdir(parents=True, exist_ok=True)
    try:
        support.hardlink_to(adr_evidence)
    except OSError:
        pytest.skip("filesystem does not support hard links in the test directory")
    reviewed = render_assessment(tmp_path)
    _accepted_row(reviewed, tmp_path, support)

    errors = validate_assessment(reviewed, tmp_path)

    assert any("requires non-ADR supporting evidence" in error for error in errors)


def test_reviewed_evidence_fails_closed_for_symlinked_adr_root(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "tobkiri_runtime/ecosystem/demo/ecosystem.json"
    support = tmp_path / "tobkiri_runtime/review/support.md"
    adr_target = tmp_path / "adr-target"
    adr_root = tmp_path / "tobkiri_runtime/docs/adr"
    _write_manifest(manifest, '{"pack_id": "demo"}\n')
    _write_manifest(support, "Independent support\n")
    _write_manifest(adr_target / "review.md", "ADR evidence\n")
    adr_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        adr_root.symlink_to(adr_target, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not support symlinks in the test directory")
    reviewed = render_assessment(tmp_path)
    _accepted_row(reviewed, tmp_path, support)

    errors = validate_assessment(reviewed, tmp_path)

    assert any("ADR evidence identities are unavailable" in error for error in errors)


@pytest.mark.parametrize(
    "path, expected_error",
    [
        ("../outside.md", "escapes repository"),
        (
            "tobkiri_runtime/docs/../docs/macos-unsigned-distribution.md",
            "canonical repository-relative path",
        ),
    ],
)
def test_evidence_paths_reject_outside_and_noncanonical_aliases(
    path: str, expected_error: str
) -> None:
    payload = copy.deepcopy(_payload())
    row = _accepted_row(payload)
    evidence = row["evidence"]
    assert isinstance(evidence, list)
    evidence.append({"path": path, "sha256": "0" * 64})

    errors = validate_assessment(payload, REPO_ROOT)

    assert any(expected_error in error for error in errors)


def test_evidence_digest_detects_tampering() -> None:
    payload = copy.deepcopy(_payload())
    row = payload["records"][0]
    assert isinstance(row, dict)
    evidence = row["evidence"]
    assert isinstance(evidence, list)
    assert isinstance(evidence[0], dict)
    evidence[0]["sha256"] = "0" * 64

    errors = validate_assessment(payload, REPO_ROOT)

    assert any("sha256 does not match current file" in error for error in errors)


def test_removal_phase_requires_aliases_and_removal_disposition() -> None:
    payload = copy.deepcopy(_payload())
    row = payload["records"][0]
    assert isinstance(row, dict)
    row["removal_phase"] = "phase-2"

    errors = validate_assessment(payload, REPO_ROOT)

    assert any("removal_phase requires deprecated_ids" in error for error in errors)
    assert any(
        "removal_phase requires a removal disposition" in error for error in errors
    )


@pytest.mark.parametrize(
    "path, token",
    [
        (
            "tobkiri_runtime/core_runtime/consumer.py",
            "pack-boundary-assessment.v1.json",
        ),
        ("tobkiri_launcher/src-tauri/tauri.conf.json", SCHEMA_VERSION),
        (".github/workflows/consumer.yml", DOCUMENT_ROLE),
    ],
)
def test_static_non_consumption_guard_detects_production_mutations(
    tmp_path: Path, path: str, token: str
) -> None:
    candidate = tmp_path / path
    _write_manifest(candidate, token)
    _write_manifest(tmp_path / "tobkiri_runtime/docs/ignored.py", token)

    references = find_runtime_references(tmp_path)

    assert references == [f"{path} ({token})"]


def test_static_non_consumption_guard_excludes_only_tauri_generated_prefix(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "tobkiri_launcher/src-tauri/gen/schema.rs"
    similarly_named_source = tmp_path / "tobkiri_runtime/core_runtime/gen/check.py"
    _write_manifest(generated, SCHEMA_VERSION)
    _write_manifest(similarly_named_source, SCHEMA_VERSION)

    references = find_runtime_references(tmp_path)

    assert references == [
        "tobkiri_runtime/core_runtime/gen/check.py " f"({SCHEMA_VERSION})"
    ]


def test_static_guard_excludes_swift_build_aliases_but_checks_helper_source(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "tobkiri_launcher/packvm-vz-helper"
    output = helper / ".build/arm64-apple-macosx/debug"
    output.mkdir(parents=True)
    try:
        (helper / ".build/debug").symlink_to(output, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not support symlinks in the test directory")
    _write_manifest(output / "generated.json", SCHEMA_VERSION)
    _write_manifest(helper / "Sources/check.rs", SCHEMA_VERSION)

    assert find_runtime_references(tmp_path) == [
        "tobkiri_launcher/packvm-vz-helper/Sources/check.rs "
        f"({SCHEMA_VERSION})"
    ]


@pytest.mark.parametrize(
    "target_path",
    [
        "tobkiri_runtime/docs/hidden.py",
        "tobkiri_launcher/src-tauri/gen/hidden.rs",
    ],
)
def test_static_guard_scans_production_symlink_to_lexically_excluded_target(
    tmp_path: Path,
    target_path: str,
) -> None:
    target = tmp_path / target_path
    source = tmp_path / "tobkiri_runtime/core_runtime/linked_consumer.py"
    _write_manifest(target, SCHEMA_VERSION)
    source.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not support symlinks in the test directory")

    references = find_runtime_references(tmp_path)

    assert references == [
        "tobkiri_runtime/core_runtime/linked_consumer.py " f"({SCHEMA_VERSION})"
    ]


@pytest.mark.parametrize(
    "target_path",
    [
        "tobkiri_runtime/docs/hidden-package",
        "tobkiri_launcher/src-tauri/gen/hidden-package",
    ],
)
def test_static_guard_fails_closed_for_production_directory_symlink(
    tmp_path: Path,
    target_path: str,
) -> None:
    target = tmp_path / target_path
    source = tmp_path / "tobkiri_runtime/core_runtime/linked_package"
    _write_manifest(target / "consumer.py", SCHEMA_VERSION)
    source.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not support symlinks in the test directory")

    references = find_runtime_references(tmp_path)

    assert references == [
        "tobkiri_runtime/core_runtime/linked_package "
        "(symlinked production directory)"
    ]


def test_static_guard_keeps_lexically_excluded_tauri_prefix_out_of_scope(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tobkiri_runtime/docs/consumer.rs"
    excluded_link = tmp_path / "tobkiri_launcher/src-tauri/gen/linked.rs"
    _write_manifest(target, SCHEMA_VERSION)
    excluded_link.parent.mkdir(parents=True, exist_ok=True)
    try:
        excluded_link.symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not support symlinks in the test directory")

    assert find_runtime_references(tmp_path) == []


def test_static_guard_fails_closed_for_production_symlink_outside_repository(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tobkiri_runtime/core_runtime/external_consumer.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.symlink_to("/dev/null")
    except OSError:
        pytest.skip("filesystem does not support symlinks in the test directory")

    references = find_runtime_references(tmp_path)

    assert references == [
        "tobkiri_runtime/core_runtime/external_consumer.py "
        "(target escapes repository)"
    ]


def test_production_runtime_does_not_consume_assessment() -> None:
    assert find_runtime_references(REPO_ROOT) == []
