from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest


SCRIPT = Path(__file__).with_name("scan_pack_boundaries.py")
BOOTSTRAP_VERIFIER = Path(__file__).with_name("verify_pack_boundary_bootstrap.py")


def _load_scanner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_pack_boundaries_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_bootstrap_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_pack_boundary_bootstrap_test", BOOTSTRAP_VERIFIER
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scanner() -> ModuleType:
    return _load_scanner()


@pytest.fixture
def bootstrap_verifier() -> ModuleType:
    return _load_bootstrap_verifier()


def _write_json(path: Path, value: object, *, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=indent) + "\n", encoding="utf-8")


def _bootstrap_baseline(fingerprints: list[str]) -> dict[str, object]:
    return {
        "baseline_api_version": "io.tobkiri.pack-boundary-baseline.v1",
        "policy": "exact-current-shrink-only-from-reference",
        "summary": {"by_rule": {}, "total": len(fingerprints)},
        "violations": [{"fingerprint": fingerprint} for fingerprint in fingerprints],
    }


def _trust_reference(
    bootstrap_verifier: ModuleType, reference: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bootstrap_verifier,
        "EXPECTED_REFERENCE_SHA256",
        hashlib.sha256(reference.read_bytes()).hexdigest(),
    )
    reference_document = json.loads(reference.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        bootstrap_verifier,
        "EXPECTED_REFERENCE_VIOLATION_COUNT",
        len(reference_document["violations"]),
    )


def _pack(
    pack_id: str,
    *,
    kind: str = "normal_sandbox",
    isolation: str = "pack_vm",
    effect_ceiling: list[str] | None = None,
    sunset_at: str = "2027-12-31",
    normative: bool = False,
    repository_commit: str = "a" * 40,
) -> dict[str, object]:
    ceiling = ["capability:test.read"] if effect_ceiling is None else effect_ceiling
    return {
        "contracts": [{"contract_id": "test.contract.v1"}],
        "functions": [
            {
                "id": f"{pack_id}.function",
                "isolation": isolation,
                "operations": [f"{pack_id}.operation"],
            }
        ],
        "migration": {"sunset_at": sunset_at},
        "operation_catalog": [
            {
                "effect_ceiling": ceiling,
                "operation_id": f"{pack_id}.operation",
            }
        ],
        "pack": {"id": pack_id, "kind": kind},
        "pack_api_version": "io.tobkiri.pack.v4",
        "provenance": {
            "normative": normative,
            "repository_commit": repository_commit,
        },
        "requirements": {
            "contract_dependencies": [{"contract_id": "test.dependency.v1"}],
            "pack_dependencies": {},
        },
    }


def _write_pack(
    root: Path,
    pack_id: str,
    value: dict[str, object] | None = None,
    *,
    effect_class: str = "read",
) -> Path:
    pack = value or _pack(pack_id)
    pack_path = root / "tobkiri_runtime" / "ecosystem" / pack_id / "pack.v4.json"
    _write_json(pack_path, pack)
    functions = pack.get("functions")
    operations = pack.get("operation_catalog")
    function = functions[0] if isinstance(functions, list) and functions else None
    operation = operations[0] if isinstance(operations, list) and operations else None
    if isinstance(function, dict) and isinstance(operation, dict):
        executable = {
            "catalog_api_version": "io.tobkiri.executable-catalog.v4",
            "variants": [
                {
                    "function_id": function.get("id"),
                    "operations": [
                        {
                            "effect_class": effect_class,
                            "operation_id": operation.get("operation_id"),
                        }
                    ],
                }
            ],
        }
        _write_json(pack_path.with_name("executables.v4.json"), executable)
    return pack_path


def _rules(scanner: ModuleType, root: Path) -> list[str]:
    return [item["rule_id"] for item in scanner.scan_repository(root)]


def test_detects_requested_pack_boundary_violations(
    scanner: ModuleType, tmp_path: Path
) -> None:
    empty = _pack("empty")
    empty["functions"] = []
    empty["contracts"] = []
    empty["requirements"] = {
        "contract_dependencies": [],
        "pack_dependencies": {},
    }
    _write_pack(tmp_path, "empty", empty)

    sunset = _pack("sunset", sunset_at="2099-01-01")
    _write_pack(tmp_path, "sunset", sunset)

    host = _pack("host", kind="host_extension")
    _write_pack(tmp_path, "host", host, effect_class="privileged")

    pure = _pack("pure", isolation="dedicated_process", effect_ceiling=[])
    _write_pack(tmp_path, "pure", pure, effect_class="pure")

    mirror_pack = _pack("mirrored")
    _write_pack(tmp_path, "mirrored", mirror_pack)
    mirror = (
        tmp_path
        / "tobkiri_runtime"
        / "ecosystem"
        / "defaultspack"
        / "v4"
        / "packs"
        / "mirrored.pack.v4.json"
    )
    _write_json(mirror, mirror_pack)

    working = _pack(
        "working",
        normative=True,
        repository_commit="working-tree",
    )
    _write_pack(tmp_path, "working", working)

    profile = {
        "profile_api_version": "io.tobkiri.profile.v5",
        "description": "a v5 API in a v4-named document",
    }
    _write_json(
        tmp_path
        / "tobkiri_runtime"
        / "ecosystem"
        / "profiles"
        / "test.profile.v4.json",
        profile,
    )

    rules = set(_rules(scanner, tmp_path))

    assert {
        "pack-boundary.empty-pack-boundary",
        "pack-boundary.host-extension-without-external-effect",
        "pack-boundary.identical-projection-mirror",
        "pack-boundary.indefinite-sunset",
        "pack-boundary.normative-working-tree",
        "pack-boundary.pure-operation-dedicated-process",
        "pack-boundary.v4-file-uses-v5-api",
    } <= rules


def test_committed_baseline_does_not_excuse_the_non_authoritative_profile(
    scanner: ModuleType,
) -> None:
    repo_root = SCRIPT.parents[2]
    profile_path = "tobkiri_runtime/ecosystem/defaultspack/v4/defaults.profile.v4.json"
    baseline_path = SCRIPT.with_name("pack_boundary_baseline.json")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    profile_exceptions = [
        item
        for item in baseline["violations"]
        if item["rule_id"] == "pack-boundary.normative-working-tree"
        and item["path"] == profile_path
    ]
    assert profile_exceptions == []
    assert baseline["summary"]["by_rule"]["pack-boundary.normative-working-tree"] == 8

    violations = scanner.scan_repository(repo_root)
    assert not any(
        item["rule_id"] == "pack-boundary.normative-working-tree"
        and item["path"] == profile_path
        for item in violations
    )


def test_committed_baseline_exactly_matches_current_violations(
    scanner: ModuleType,
) -> None:
    """The reviewed baseline must neither hide new edges nor retain stale ones."""

    repo_root = SCRIPT.parents[2]
    baseline_path = SCRIPT.with_name("pack_boundary_baseline.json")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    actual = scanner.scan_repository(repo_root)

    baseline_ids = {item["fingerprint"] for item in baseline["violations"]}
    actual_ids = {item["fingerprint"] for item in actual}
    assert actual_ids - baseline_ids == set()
    assert baseline_ids - actual_ids == set()
    assert baseline["violations"] == actual


def test_false_positive_guards(scanner: ModuleType, tmp_path: Path) -> None:
    host = _pack(
        "host",
        kind="host_extension",
        effect_ceiling=["capability:test.read", "host:brokered-execution"],
    )
    _write_pack(tmp_path, "host", host, effect_class="read")
    dedicated = _pack("dedicated", isolation="dedicated_process")
    _write_pack(tmp_path, "dedicated", dedicated, effect_class="privileged")
    normal = _pack("normal")
    normal["description"] = "Planning for a v5 API does not declare one."
    _write_pack(tmp_path, "normal", normal)
    distinct = dict(normal)
    distinct["description"] = "A deliberately distinct bundled projection."
    _write_json(
        tmp_path
        / "tobkiri_runtime"
        / "ecosystem"
        / "defaultspack"
        / "v4"
        / "packs"
        / "normal.pack.v4.json",
        distinct,
    )

    assert scanner.scan_repository(tmp_path) == []


def test_fingerprint_is_stable_when_json_line_changes(
    scanner: ModuleType, tmp_path: Path
) -> None:
    pack = _pack("sunset", sunset_at="2099-01-01")
    path = _write_pack(tmp_path, "sunset", pack)
    first = next(
        item
        for item in scanner.scan_repository(tmp_path)
        if item["rule_id"] == "pack-boundary.indefinite-sunset"
    )
    path.write_text(
        "\n\n" + json.dumps(pack, indent=4) + "\n",
        encoding="utf-8",
    )
    second = next(
        item
        for item in scanner.scan_repository(tmp_path)
        if item["rule_id"] == "pack-boundary.indefinite-sunset"
    )

    assert first["fingerprint"] == second["fingerprint"]
    assert first["line"] != second["line"]


@pytest.mark.parametrize("failure", ["parse", "unknown", "symlink"])
def test_scan_failures_are_diagnosed_and_cannot_be_baselined(
    scanner: ModuleType,
    tmp_path: Path,
    failure: str,
) -> None:
    _write_pack(tmp_path, "valid")
    ecosystem = tmp_path / "tobkiri_runtime" / "ecosystem"
    if failure == "parse":
        broken = ecosystem / "broken" / "broken.v4.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("{broken", encoding="utf-8")
        expected = "pack-boundary.scan.parse-error"
    elif failure == "unknown":
        _write_json(ecosystem / "unknown" / "unknown.v4.json", {"value": 1})
        expected = "pack-boundary.scan.unknown-schema"
    else:
        target = ecosystem / "target.json"
        _write_json(target, {"value": 1})
        link = ecosystem / "linked.v4.json"
        try:
            link.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
        expected = "pack-boundary.scan.symlink"

    assert expected in _rules(scanner, tmp_path)
    assert scanner.main(["--root", str(tmp_path), "--update-baseline"]) == 1


def test_check_does_not_update_baseline_and_new_violation_fails(
    scanner: ModuleType, tmp_path: Path
) -> None:
    _write_pack(tmp_path, "valid")
    baseline = tmp_path / "baseline.json"
    args = ["--root", str(tmp_path), "--baseline", str(baseline)]
    assert scanner.main([*args, "--update-baseline"]) == 0
    original = baseline.read_bytes()
    assert scanner.main(args) == 0

    violating = _pack("late", sunset_at="2099-01-01")
    _write_pack(tmp_path, "late", violating)

    assert scanner.main(args) == 1
    assert baseline.read_bytes() == original


def test_reference_baseline_rejects_simultaneous_manual_expansion(
    scanner: ModuleType, tmp_path: Path
) -> None:
    _write_pack(tmp_path, "valid")
    baseline = tmp_path / "baseline.json"
    reference = tmp_path / "reference.json"
    args = ["--root", str(tmp_path), "--baseline", str(baseline)]
    assert scanner.main([*args, "--update-baseline"]) == 0
    shutil.copyfile(baseline, reference)

    violating = _pack("late", sunset_at="2099-01-01")
    _write_pack(tmp_path, "late", violating)
    assert scanner.main([*args, "--update-baseline"]) == 0

    assert scanner.main([*args, "--reference-baseline", str(reference)]) == 1


def test_bootstrap_reference_allows_only_shrinking_candidate(
    bootstrap_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.json"
    candidate = tmp_path / "candidate.json"
    _write_json(reference, _bootstrap_baseline(["sha256:one", "sha256:two"]))
    _write_json(candidate, _bootstrap_baseline(["sha256:one"]))
    _trust_reference(bootstrap_verifier, reference, monkeypatch)

    bootstrap_verifier.verify_bootstrap(candidate, reference)


def test_bootstrap_reference_rejects_candidate_expansion(
    bootstrap_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.json"
    candidate = tmp_path / "candidate.json"
    _write_json(reference, _bootstrap_baseline(["sha256:one"]))
    _write_json(candidate, _bootstrap_baseline(["sha256:one", "sha256:two"]))
    _trust_reference(bootstrap_verifier, reference, monkeypatch)

    with pytest.raises(ValueError, match="expands the reviewed reference"):
        bootstrap_verifier.verify_bootstrap(candidate, reference)


def test_bootstrap_reference_rejects_candidate_as_its_own_authority(
    bootstrap_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.json"
    _write_json(baseline, _bootstrap_baseline(["sha256:one"]))
    _trust_reference(bootstrap_verifier, baseline, monkeypatch)

    with pytest.raises(ValueError, match="cannot authorize its own"):
        bootstrap_verifier.verify_bootstrap(baseline, baseline)


def test_bootstrap_reference_rejects_digest_mismatch(
    bootstrap_verifier: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.json"
    candidate = tmp_path / "candidate.json"
    _write_json(reference, _bootstrap_baseline(["sha256:one"]))
    _write_json(candidate, _bootstrap_baseline(["sha256:one"]))
    monkeypatch.setattr(bootstrap_verifier, "EXPECTED_REFERENCE_SHA256", "0" * 64)
    monkeypatch.setattr(bootstrap_verifier, "EXPECTED_REFERENCE_VIOLATION_COUNT", 1)

    with pytest.raises(ValueError, match="reference digest mismatch"):
        bootstrap_verifier.verify_bootstrap(candidate, reference)


@pytest.mark.parametrize(
    "invalid",
    [
        {"violations": []},
        {
            "baseline_api_version": "io.tobkiri.pack-boundary-baseline.v1",
            "policy": "exact-current-shrink-only-from-reference",
            "summary": {"by_rule": {}, "total": 1},
            "violations": [None],
        },
        {
            "baseline_api_version": "io.tobkiri.pack-boundary-baseline.v1",
            "policy": "exact-current-shrink-only-from-reference",
            "summary": {"by_rule": {}, "total": 1},
            "violations": [
                {
                    "evidence": {},
                    "fingerprint": "invalid",
                    "line": 1,
                    "path": "pack.v4.json",
                    "rule_id": [],
                }
            ],
        },
    ],
)
def test_invalid_baseline_schema_fails_closed(
    scanner: ModuleType,
    tmp_path: Path,
    invalid: object,
) -> None:
    _write_pack(tmp_path, "valid")
    baseline = tmp_path / "baseline.json"
    _write_json(baseline, invalid)

    assert scanner.main(["--root", str(tmp_path), "--baseline", str(baseline)]) == 1


def test_report_only_is_deterministic_and_does_not_create_baseline(
    scanner: ModuleType, tmp_path: Path
) -> None:
    violating = _pack("sunset", sunset_at="2099-01-01")
    _write_pack(tmp_path, "sunset", violating)
    baseline = tmp_path / "baseline.json"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    common = ["--root", str(tmp_path), "--baseline", str(baseline), "--report"]

    assert scanner.main([*common, "--report-output", str(first)]) == 0
    assert scanner.main([*common, "--report-output", str(second)]) == 0

    assert first.read_bytes() == second.read_bytes()
    assert not baseline.exists()
