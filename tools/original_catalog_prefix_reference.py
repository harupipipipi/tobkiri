"""Observe the original catalog path up to ProfileDefinitionStore construction.

All execution is CI-only, with the pinned original interpreter and checkout.
Each staged runtime keeps the original Python bytes and real root getters.
Only resource fixture data changes; only the constructor call-site alias is
replaced by a BaseException sentinel. No catalog, validation or getter body is
replaced, and no constructor body, migration, approval or model is executed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from original_authority_reference import ORIGINAL_HEAD, error_graph, first_difference, write_json
from original_publication_reference import filesystem, seed
from original_setup_review_reference import check_original


FORMAT = "io.tobkiri.original-catalog-prefix-reference.v1"
CASES = (
    "packaged-valid", "source-without-artifact-root", "manifest-missing",
    "manifest-invalid-json", "manifest-invalid-utf8", "manifest-wrong-schema",
    "manifest-crlf", "resource-unlisted", "development-bundled",
)
SOURCES = (
    "core_runtime/bootstrap/profile_capture.py",
    "core_runtime/bootstrap/profile_publication.py",
    "core_runtime/profile_runtime_port.py",
    "core_runtime/profile_definition_store_v4.py",
    "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
    "ecosystem/defaultspack/domain/profile_resolution.py",
    "tobkiri_protocol/bundle_catalog.py",
)
IGNORED = shutil.ignore_patterns(
    "__pycache__", "node_modules", ".venv", ".pytest_cache", "target", "webapp", "tests",
)


class ProfileDefinitionBoundary(BaseException):
    """Test-only reachpoint, never an original application error."""


def probe_recovery(original: Path, fixtures: Path, output: Path) -> None:
    """Observe the live Profile projection and scoped Authority initialization."""
    check_original(original)
    runtime = fixtures.parent / "packaged-valid/reference/runtime"
    user_data = fixtures.parent / "packaged-valid/reference/user-data"
    native_data = fixtures.parent / "packaged-valid/native/user-data"
    hashes = code_hashes(original / "tobkiri_runtime")
    if code_hashes(runtime) != hashes:
        raise ValueError("staged original code differs")
    sys.path.insert(0, str(runtime))
    os.environ.update(TOBKIRI_USER_DATA=str(user_data), RUMI_USER_DATA=str(user_data),
                      RUMI_ENVIRONMENT="production")
    os.environ.pop("RUMI_APP_DIR", None)
    from core_runtime.bootstrap import profile_capture as capture
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )
    from original_profile_definition_prefix_reference import _lock_free
    from tobkiri_protocol.canonical import canonical_digest

    profile = json.loads((runtime / "ecosystem/defaultspack/v4/defaults.profile.v5.json").read_bytes())
    profile["profile_id"] = "recovery-test-profile"
    for owner in (user_data, native_data):
        ProfileDefinitionStore(owner).create_profile(profile)

    port = install_defaultspack_profile_runtime()
    owner_type = type(port)
    catalog_with_profiles = owner_type.catalog_with_profiles
    authority = capture.AuthorityStore
    journal_type = capture.ProfilePublicationJournal
    original_clear = journal_type.clear
    observed = []
    authority_observations = []

    class AuthorityBoundary(BaseException):
        """The original has evaluated all arguments by this call."""

    def observe(self, catalog, profiles):
        projected = catalog_with_profiles(self, catalog, profiles)
        if self is not port or projected is catalog:
            raise AssertionError("recovery did not use the original runtime catalog")
        if list(projected.profiles) != ["recovery-test-profile"] or list(catalog.profiles) == list(projected.profiles):
            raise AssertionError("recovery did not replace the Profile table")
        observed.append(canonical_digest([[key, value] for key, value in projected.profiles.items()]))
        return projected

    def open_authority(path):
        if path != user_data / "authority/v4.sqlite3" or len(observed) != 1:
            raise AssertionError("original Authority call or catalog order changed")
        if (_lock_free(user_data / "profiles/.bootstrap-publication.lock") is not False
                or _lock_free(user_data / "profiles/.profile-definition.lock") is not True):
            raise AssertionError("original lock ownership changed before Authority")
        return authority(path)

    def stop_at_journal_clear(self):
        if (_lock_free(user_data / "profiles/.bootstrap-publication.lock") is not False
                or not (user_data / "authority/v4.sqlite3").is_file()):
            raise AssertionError("original empty active reservation read did not retain custody")
        authority_observations.append("journal-clear-entry")
        raise AuthorityBoundary

    def non_authority(root):
        return [item for item in filesystem(root)["files"]
                if item["path"] != "authority" and not item["path"].startswith("authority/")]

    before = non_authority(user_data)
    if (user_data / "authority").exists() or (native_data / "authority").exists():
        raise AssertionError("Authority fixture must start absent")
    try:
        owner_type.catalog_with_profiles = observe
        capture.AuthorityStore = open_authority
        journal_type.clear = stop_at_journal_clear
        try:
            capture._recover_bootstrap_publication(discard_superseded=True)
        except AuthorityBoundary:
            pass
        else:
            raise AssertionError("original did not reach Authority")
    finally:
        owner_type.catalog_with_profiles = catalog_with_profiles
        capture.AuthorityStore = authority
        journal_type.clear = original_clear
    if (code_hashes(runtime) != hashes or before != non_authority(user_data)
            or authority_observations != ["journal-clear-entry"]
            or _lock_free(user_data / "profiles/.bootstrap-publication.lock") is not True):
        raise AssertionError("original recovery altered the retained profile seed or source")
    write_json(output, {"profile_rows_digest": observed[0], "authority_constructor_entries": 1,
                        "next_stage": authority_observations[0]})


def code_hashes(runtime: Path) -> dict[str, str]:
    return {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
            for name in SOURCES}


def bind_resources(runtime: Path, *, packaged_artifacts: bool = False) -> None:
    """Seed actual fixture bytes, not an observation or permission grant."""
    bundle = runtime / "ecosystem/defaultspack/v4"
    artifacts = bundle.parent / "platform-artifacts"
    if packaged_artifacts:
        if artifacts.is_symlink() or not artifacts.is_dir():
            raise ValueError("physical packaged fixture artifact directory required")
    else:
        artifacts.mkdir(exist_ok=False)
    entries = []
    for directory in (bundle, artifacts):
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                raw = path.read_bytes()
                entries.append({"path": path.relative_to(runtime).as_posix(),
                                "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    write_json(runtime / "runtime-resource-manifest.v1.json", {
        "schema": "io.tobkiri.runtime-resource-manifest.v1", "entries": entries,
    })


def mutate(runtime: Path, name: str) -> dict[str, str | None]:
    manifest = runtime / "runtime-resource-manifest.v1.json"
    bundle = runtime / "ecosystem/defaultspack/v4"
    env: dict[str, str | None] = {"RUMI_ENVIRONMENT": "production", "RUMI_APP_DIR": None}
    if name == "source-without-artifact-root":
        (bundle.parent / "platform-artifacts").rmdir()
    elif name == "manifest-missing":
        manifest.unlink()
    elif name == "manifest-invalid-json":
        manifest.write_bytes(b"{")
    elif name == "manifest-invalid-utf8":
        manifest.write_bytes(b"\xff")
    elif name == "manifest-wrong-schema":
        data = json.loads(manifest.read_text())
        data["schema"] = "unsupported.fixture"
        write_json(manifest, data)
    elif name == "manifest-crlf":
        manifest.write_bytes(manifest.read_bytes().replace(b"\n", b"\r\n"))
    elif name == "resource-unlisted":
        (bundle / "unlisted-fixture.txt").write_bytes(b"unlisted fixture\n")
    elif name == "development-bundled":
        target = runtime / "bundled/dev-defaults"
        target.mkdir(parents=True, exist_ok=False)
        bundle.rename(target / "v4")
        (bundle.parent / "platform-artifacts").rename(target / "platform-artifacts")
        env = {"RUMI_ENVIRONMENT": "development", "RUMI_APP_DIR": str(runtime)}
    elif name != "packaged-valid":
        raise ValueError("unknown closed catalog fixture")
    return env


def intrinsic_parse_errors(error: BaseException) -> list[dict]:
    """Read real parser fields without reconstructing an exception from text."""
    records, visited, pending = [], set(), [error]
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if len(visited) > 16:
            raise ValueError("catalog exception graph exceeds finite fixture bound")
        if type(current) is json.JSONDecodeError:
            records.append({"kind": "json-decode", "msg": current.msg,
                            "doc": current.doc, "pos": current.pos,
                            "lineno": current.lineno, "colno": current.colno})
        elif type(current) is UnicodeDecodeError:
            records.append({"kind": "unicode-decode", "encoding": current.encoding,
                            "object_hex": current.object.hex(), "start": current.start,
                            "end": current.end, "reason": current.reason})
        for link in (BaseException.__cause__, BaseException.__context__):
            child = link.__get__(current)
            if child is not None:
                pending.append(child)
    return records


def probe(original: Path, runtime: Path, user_data: Path, output: Path) -> None:
    check_original(original)
    expected = code_hashes(original / "tobkiri_runtime")
    if code_hashes(runtime) != expected:
        raise ValueError("staged original code bytes differ")
    sys.path.insert(0, str(runtime))
    from core_runtime.bootstrap import profile_capture as capture
    from core_runtime.profile_runtime_port import require_profile_runtime
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )

    if Path(capture.__file__).resolve() != runtime / SOURCES[0]:
        raise ValueError("catalog probe imported another source tree")
    install_defaultspack_profile_runtime()
    port = require_profile_runtime()
    original_store = capture.ProfileDefinitionStore
    observed = []
    identities = (capture.host_profile_catalog, capture._bundle_root,
                  capture._verify_installed_bundle_binding, capture._bootstrap_profile_id,
                  type(port).host_resource_root, type(port).bundled_profile_root,
                  type(port).load_catalog)

    def stop_definition(root):
        if root != user_data.resolve():
            raise AssertionError("constructor received a different user-data root")
        observed.append("profile-definition-store-entry")
        raise ProfileDefinitionBoundary

    before = filesystem(user_data)
    try:
        capture.ProfileDefinitionStore = stop_definition
        try:
            capture._recover_bootstrap_publication(discard_superseded=True)
        except ProfileDefinitionBoundary:
            result = {"outcome": "integration-boundary", "stage": "profile-definition-store-entry"}
        except BaseException as error:
            result = {"outcome": "error", "error": error_graph(error, runtime.parent),
                      "intrinsic_parse_errors": intrinsic_parse_errors(error)}
        else:
            raise AssertionError("recovery unexpectedly returned before the constructor stop")
    finally:
        capture.ProfileDefinitionStore = original_store
    if identities != (capture.host_profile_catalog, capture._bundle_root,
                      capture._verify_installed_bundle_binding, capture._bootstrap_profile_id,
                      type(port).host_resource_root, type(port).bundled_profile_root,
                      type(port).load_catalog):
        raise AssertionError("an original catalog/getter callable was replaced")
    if code_hashes(runtime) != expected or require_profile_runtime() is not port:
        raise AssertionError("original source or immutable runtime changed")
    if len(observed) > 1:
        raise AssertionError("constructor boundary repeated")
    write_json(output, {"expected": result, "constructor_entries": len(observed),
                        "constructor_body_executed": False, "source_hashes": expected,
                        "user_data_before": before, "user_data_after": filesystem(user_data)})


def prepare(original: Path, destination: Path) -> None:
    check_original(original)
    runtime = original / "tobkiri_runtime"
    expected = code_hashes(runtime)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    sys.path.insert(0, str(runtime))
    from core_runtime.bootstrap.profile_publication import ProfilePublicationJournal
    from core_runtime.active_profile_store_v4 import ActiveProfilePointer
    from core_runtime import hmac_key_manager as signing
    from tobkiri_protocol import canonical as protocol

    reports = []
    for name in CASES:
        directory = destination / name
        directory.mkdir(mode=0o700)
        seed_data = directory / "seed-user-data"
        seed("valid", seed_data, ProfilePublicationJournal, ActiveProfilePointer, protocol, signing)
        template = directory / "seed-runtime"
        shutil.copytree(runtime, template, ignore=IGNORED)
        bind_resources(template)
        for role in ("reference", "native"):
            target = directory / role
            target.mkdir(mode=0o700)
            shutil.copytree(template, target / "runtime")
            shutil.copytree(seed_data, target / "user-data")
            environment = mutate(target / "runtime", name)
            write_json(target / "input.json", {"case": name, "runtime": str(target / "runtime"),
                       "user_data": str(target / "user-data"), "environment": environment,
                       "resource_fixture_only": True, "platform_artifact_directory_only": True})
        # The staging template is solely owned CI fixture data. Keep only the
        # two independent input trees used by the original/native comparison.
        shutil.rmtree(template)
        reference = directory / "reference"
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                           TOBKIRI_USER_DATA=str(reference / "user-data"),
                           RUMI_USER_DATA=str(reference / "user-data"))
        for key, value in json.loads((reference / "input.json").read_text())["environment"].items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        output = reference / "observation.json"
        subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "probe",
                        "--original-root", str(original), "--runtime", str(reference / "runtime"),
                        "--user-data", str(reference / "user-data"), "--output", str(output)],
                       check=True, env=environment, timeout=180)
        observation = json.loads(output.read_text())
        reports.append({"name": name, "native_input": str(directory / "native/input.json"),
                        **observation})
    if code_hashes(runtime) != expected:
        raise AssertionError("independent original checkout code changed")
    by_name = {row["name"]: row for row in reports}
    boundary = {"outcome": "integration-boundary", "stage": "profile-definition-store-entry"}
    for name in ("packaged-valid", "manifest-crlf", "development-bundled"):
        if by_name[name]["expected"] != boundary or by_name[name]["constructor_entries"] != 1:
            raise AssertionError((name, "original loader did not reach the natural boundary",
                                  by_name[name]["expected"]))
    for name in ("manifest-missing", "manifest-invalid-json", "manifest-invalid-utf8",
                 "manifest-wrong-schema", "resource-unlisted"):
        if by_name[name]["expected"]["outcome"] != "error" or by_name[name]["constructor_entries"]:
            raise AssertionError((name, "invalid fixture passed the actual original loader"))
    check_original(original)
    write_json(destination / "summary.json", {"format": FORMAT, "original_head": ORIGINAL_HEAD,
               "python": "3.13.16", "cases": reports, "case_count": len(reports),
               "native_comparison": False, "qualification": False, "production_enabled": False,
               "scope": "staged original Python bytes and real getters/catalog; resource fixture inputs; "
                        "constructor call-site sentinel only; no constructor body, migrations, Authority, "
                        "actual installed executable, approval, GUI, native comparison or full goal"})


def compare_actual(original: Path, fixtures: Path, observations: Path, output: Path) -> None:
    """Compare actual reachpoint/effects, never inject expected DATA into Guest."""
    check_original(original)
    if fixtures.stat().st_size > 2 * 1024 * 1024 or observations.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("catalog comparison input exceeds bounded observations")
    reference = json.loads(fixtures.read_text())
    native = json.loads(observations.read_text())
    if (reference.get("format") != FORMAT or reference.get("original_head") != ORIGINAL_HEAD
            or reference.get("case_count") != 9
            or native.get("format") != "io.tobkiri.actual-catalog-prefix-observations.v1"
            or native.get("name") != "packaged-valid"):
        raise ValueError("catalog observation identity differs")
    rows = [row for row in reference["cases"] if row["name"] == "packaged-valid"]
    if len(rows) != 1:
        raise ValueError("closed normal catalog reference required")
    row = rows[0]
    input_path = Path(row["native_input"])
    if input_path != fixtures.parent / "packaged-valid/native/input.json":
        raise ValueError("catalog fixture directory was substituted")
    user_data = input_path.parent / "user-data"
    state = filesystem(user_data)
    observed = native.get("observation") or {}
    boundary = {"outcome": observed.get("outcome"), "stage": observed.get("stage")}
    capacities = native.get("capacity", {})
    after = capacities.get("after_reap", {})
    flags = ("execution_matched_branch", "cleanup_verified", "journal_lock_released_after_finish",
             "source_destructor_reached")
    measured = ("same_original_deadline", "journal_lock_held_before_finish",
                "actual_host_root_retained", "actual_bundle_root_retained", "actual_catalog_retained")
    seed_key = fixtures.parent / "packaged-valid/seed-user-data/profiles/.publication-key"
    actual_key = user_data / "profiles/.publication-key"
    key_unchanged = seed_key.read_bytes() == actual_key.read_bytes()
    matched = (row["constructor_body_executed"] is False and row["constructor_entries"] == 1
               and boundary == row["expected"] and state == row["user_data_after"]
               and all(native.get(key) is True for key in flags)
               and all(observed.get(key) is True for key in measured)
               and observed.get("budget_resets") == 0
               and type(observed.get("fuel_before")) is int
               and type(observed.get("fuel_after")) is int
               and 0 <= observed["fuel_after"] < observed["fuel_before"]
               and after == {"bytes": 0, "reservations": 0} and key_unchanged)
    write_json(output, {"format": "io.tobkiri.actual-catalog-prefix-comparison.v1",
               "original_head": ORIGINAL_HEAD, "name": "packaged-valid", "matched": matched,
               "reachpoint_matches": boundary == row["expected"],
               "user_data_state_matches": state == row["user_data_after"],
               "existing_key_unchanged": key_unchanged, "native_comparison": True,
               "qualification": False, "production_enabled": False,
               "scope": "one actual same-guard catalog operation against original resource fixture; "
                        "no all9 native, constructor, migrations, Authority, installed executable or GUI qualification"})
    if not matched:
        raise AssertionError("actual catalog comparison differs; see bounded receipt")


def compare_nine_actual(
    original: Path, fixtures: Path, observations: Path, output: Path,
) -> None:
    """Check each actual operation against fresh original outcomes and files."""
    check_original(original)
    for path in (fixtures, observations):
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("nine-case catalog input exceeds observation bound")
    reference = json.loads(fixtures.read_bytes())
    native = json.loads(observations.read_bytes())
    if (reference.get("format") != FORMAT or reference.get("original_head") != ORIGINAL_HEAD
            or reference.get("case_count") != 9
            or native.get("format") != "io.tobkiri.actual-catalog-prefix-nine-observations.v1"
            or native.get("case_count") != 9):
        raise ValueError("fresh nine-case catalog reference and actual observations required")
    originals = {row["name"]: row for row in reference["cases"]}
    actuals = {row["name"]: row for row in native["cases"]}
    if (len(reference["cases"]) != 9 or len(native["cases"]) != 9
            or set(originals) != set(CASES) or set(actuals) != set(CASES)):
        raise ValueError("nine-case catalog inventory differs")
    def journal_is_free(user_data):
        import fcntl

        try:
            with (user_data / "profiles/.bootstrap-publication.lock").open("r+b") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream, fcntl.LOCK_UN)
                return True
        except OSError:
            # Missing/unavailable locks cannot qualify a failed preparation.
            return False

    results = []
    for name in CASES:
        original_case, actual = originals[name], actuals[name]
        path = fixtures.parent / name / "native/input.json"
        if Path(original_case["native_input"]) != path:
            raise ValueError("catalog native fixture directory was substituted")
        user_data = path.parent / "user-data"
        key = user_data / "profiles/.publication-key"
        seed_key = fixtures.parent / name / "seed-user-data/profiles/.publication-key"
        key_matches = key.read_bytes() == seed_key.read_bytes()
        files_match = filesystem(user_data) == original_case["user_data_after"]
        difference = first_difference(original_case["expected"], actual.get("observation"))
        boundary = original_case["expected"]["outcome"] == "integration-boundary"
        original_scope = (original_case["constructor_body_executed"] is False
                          and original_case["constructor_entries"] == int(boundary))
        flags = (
            "cleanup_verified", "source_destructor_reached",
            "physical_owner_reopened", "journal_lock_held_before_finish",
            "journal_lock_released_after_finish",
        )
        capacities = actual.get("capacity")
        capacities = capacities if isinstance(capacities, dict) else {}
        measured = actual.get("measurement")
        measured = measured if isinstance(measured, dict) else {}
        independent_lock_free = journal_is_free(user_data)
        execution_matches = actual.get("execution_matched_branch") is True
        cleanup = (all(actual.get(key) is True for key in flags)
                   and independent_lock_free
                   and first_difference({"bytes": 0, "reservations": 0},
                                        capacities.get("after_reap")) is None)
        budget = (measured.get("same_original_deadline") is True
                  and type(measured.get("budget_resets")) is int
                  and measured["budget_resets"] == 0
                  and type(measured.get("fuel_before")) is int
                  and type(measured.get("fuel_after")) is int
                  and 0 <= measured["fuel_after"] < measured["fuel_before"] <= 2_000_000_000
                  and capacities.get("limit_bytes") == 64 * 1024 * 1024
                  and type(capacities.get("peak_bytes")) is int
                  and 0 < capacities["peak_bytes"] <= capacities["limit_bytes"])
        roots = True
        if boundary:
            roots = (measured.get("actual_host_root_retained") is True
                     and measured.get("actual_catalog_retained") is True
                     and measured.get("actual_bundle_root_retained")
                     is (name != "development-bundled"))
        matched = (difference is None and files_match and key_matches
                   and original_scope and execution_matches and cleanup and budget and roots)
        results.append({"name": name, "matched": matched, "difference": difference,
                        "independent_files_match": files_match, "existing_key_unchanged": key_matches,
                        "original_scope_matches": original_scope, "cleanup_verified": cleanup,
                        "execution_matched_branch": execution_matches,
                        "independent_journal_lock_free": independent_lock_free,
                        "original_budget_preserved": budget, "actual_roots_match": roots})
    passed = sum(row["matched"] for row in results)
    report = {"format": "io.tobkiri.actual-catalog-prefix-nine-comparison.v1",
              "original_head": ORIGINAL_HEAD, "cases": results, "passed": passed,
              "failed": len(results) - passed, "native_comparison": True,
              "qualification": False, "production_enabled": False,
              "scope": "nine actual same-guard catalog operations against controlled original "
                       "resource fixtures, including leaf manifest errors; no constructor body, "
                       "migration, Authority, arbitrary deep exception, installed executable or GUI"}
    write_json(output, report)
    if passed != 9:
        raise AssertionError([row for row in results if not row["matched"]])
    print(json.dumps({"actual_catalog_cases": passed, "qualification": False}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "probe", "probe-recovery",
                                            "compare-actual", "compare-nine-actual"))
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--user-data", type=Path)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    if args.command == "prepare":
        prepare(original, args.output.resolve())
    elif args.command == "probe":
        if args.runtime is None or args.user_data is None:
            parser.error("probe requires --runtime and --user-data")
        probe(original, args.runtime.resolve(strict=True), args.user_data.resolve(strict=True), args.output)
    elif args.command == "probe-recovery":
        if args.fixtures is None:
            parser.error("probe-recovery requires --fixtures")
        probe_recovery(original, args.fixtures.resolve(strict=True), args.output)
    else:
        if args.fixtures is None or args.observations is None:
            parser.error("actual comparison requires --fixtures and --observations")
        comparator = compare_actual if args.command == "compare-actual" else compare_nine_actual
        comparator(original, args.fixtures.resolve(strict=True), args.observations.resolve(strict=True), args.output)


if __name__ == "__main__":
    main()
