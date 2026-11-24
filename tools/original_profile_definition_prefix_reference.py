"""CI-only original constructor/list reference before the first migration.

Use the existing independently prepared packaged catalog resource fixture.
Constructor, snapshot, validation, list and filesystem bodies run unchanged.
Only the two first-migration method entries are replaced by test sentinels.
No migration, registration, approval or Authority body is claimed here.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from original_authority_reference import ORIGINAL_HEAD, error_graph, write_json
from original_catalog_prefix_reference import code_hashes
from original_publication_reference import filesystem
from original_setup_review_reference import check_original

FORMAT = "io.tobkiri.original-profile-definition-prefix-reference.v1"
CASES = (
    "index-absent", "legacy-file", "existing-profile", "legacy-bootstrap-missing",
    "invalid-json", "invalid-utf8", "duplicate-key", "wrong-digest",
    "wrong-schema", "generation-bool", "duplicate-profile-id", "index-symlink",
    "index-hardlink",
)


class MigrationBoundary(BaseException):
    """A test reachpoint outside the original recovery catch."""


def _lock_free(path: Path) -> bool | None:
    import fcntl

    if not path.exists():
        return None
    with path.open("r+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(stream, fcntl.LOCK_UN)
        return True


def probe(original: Path, runtime: Path, user_data: Path, output: Path) -> None:
    """Run the original recovery, stopping only at its first migration entry."""
    check_original(original)
    expected = code_hashes(original / "tobkiri_runtime")
    if code_hashes(runtime) != expected:
        raise ValueError("staged original code differs")
    sys.path.insert(0, str(runtime))
    from core_runtime.bootstrap import profile_capture as capture
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )

    if Path(capture.__file__).resolve() != runtime / "core_runtime/bootstrap/profile_capture.py":
        raise ValueError("another original runtime was imported")
    install_defaultspack_profile_runtime()
    store = capture.ProfileDefinitionStore
    methods = {name: getattr(store, name) for name in (
        "__init__", "snapshot", "list_profiles", "_read_state", "_validate_state",
        "_stored_from_entry", "import_legacy_collection", "migrate_legacy_successors",
    )}
    reached = []

    def boundary(name):
        def stop(instance, *args, **kwargs):
            if instance.store_root != user_data / "profiles":
                raise AssertionError("constructor used another user-data source")
            reached.append({"method": name,
                            "definition_lock_free": _lock_free(instance.store_root / ".profile-definition.lock"),
                            "journal_lock_free": _lock_free(instance.store_root / ".bootstrap-publication.lock")})
            raise MigrationBoundary
        return stop

    before = filesystem(user_data)
    try:
        for name in ("import_legacy_collection", "migrate_legacy_successors"):
            setattr(store, name, boundary(name))
        try:
            capture._recover_bootstrap_publication(discard_superseded=True)
        except MigrationBoundary:
            if len(reached) != 1:
                raise AssertionError("first migration boundary repeated")
            result = {"outcome": "integration-boundary", "stage": reached[0]["method"]}
        except BaseException as error:
            result = {"outcome": "error", "error": error_graph(error, user_data.parent)}
        else:
            raise AssertionError("original recovery passed the migration boundary")
    finally:
        for name in ("import_legacy_collection", "migrate_legacy_successors"):
            setattr(store, name, methods[name])
    if any(getattr(store, name) is not method for name, method in methods.items()):
        raise AssertionError("an original definition callable changed")
    if code_hashes(runtime) != expected:
        raise AssertionError("original code bytes changed")
    write_json(output, {"expected": result, "reached": reached,
               "constructor_and_list_body_executed": bool(reached),
               "migration_body_executed": False, "source_hashes": expected,
               "user_data_before": before, "user_data_after": filesystem(user_data),
               "journal_lock_free_after": _lock_free(user_data / "profiles/.bootstrap-publication.lock"),
               "definition_lock_free_after": _lock_free(user_data / "profiles/.profile-definition.lock")})


def prepare(original: Path, catalog: Path, output: Path) -> None:
    """Seed controlled old registry inputs using actual original writer bodies."""
    check_original(original)
    if catalog.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("catalog fixture exceeds its bound")
    catalog_data = json.loads(catalog.read_bytes())
    if (catalog_data.get("original_head") != ORIGINAL_HEAD
            or catalog_data.get("case_count") != 9):
        raise ValueError("independent original catalog fixture required")
    base = catalog.parent / "packaged-valid"
    runtime = (base / "native/runtime").resolve(strict=True)
    source = original / "tobkiri_runtime"
    if code_hashes(runtime) != code_hashes(source):
        raise ValueError("catalog source tree differs")
    sys.path.insert(0, str(source))
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
    from tobkiri_protocol.canonical import canonical_digest, canonical_json

    template = json.loads((source / "ecosystem/defaultspack/v4/defaults.profile.v5.json").read_bytes())
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    reports = []
    for name in CASES:
        directory = output / name
        directory.mkdir(mode=0o700)
        seed = directory / "seed-user-data"
        shutil.copytree(base / "seed-user-data", seed)
        index = seed / "profiles/index.json"
        if name not in ("index-absent", "legacy-file"):
            store = ProfileDefinitionStore(seed)
            store.create_profile(template)
        if name == "legacy-file":
            (seed / "settings").mkdir(exist_ok=True)
            (seed / "settings/startup_profiles.json").write_text("{}\n")
        elif name in ("legacy-bootstrap-missing", "wrong-schema", "generation-bool", "duplicate-profile-id"):
            state = json.loads(index.read_bytes())
            if name == "legacy-bootstrap-missing":
                state.pop("bootstrap")
            elif name == "wrong-schema":
                state["schema"] = "unsupported.fixture"
            elif name == "generation-bool":
                state["generation"] = True
            else:
                state["profiles"].append(copy.deepcopy(state["profiles"][0]))
            state["store_digest"] = canonical_digest({k: v for k, v in state.items() if k != "store_digest"})
            index.write_bytes(canonical_json(state) + b"\n")
        elif name == "invalid-json":
            index.write_bytes(b"{")
        elif name == "invalid-utf8":
            index.write_bytes(b"\xff")
        elif name == "duplicate-key":
            index.write_bytes(b'{"schema":0,"schema":1}')
        elif name == "wrong-digest":
            state = json.loads(index.read_bytes())
            state["store_digest"] = "sha256:" + "0" * 64
            index.write_bytes(canonical_json(state) + b"\n")
        elif name in ("index-symlink", "index-hardlink"):
            backing = index.with_name("fixture-index.json")
            index.rename(backing)
            if name == "index-symlink":
                index.symlink_to(backing.name)
            else:
                os.link(backing, index)
        for role in ("reference", "native"):
            target = directory / role
            target.mkdir(mode=0o700)
            shutil.copytree(seed, target / "user-data", symlinks=True)
            if name == "index-hardlink":
                copied = target / "user-data/profiles/index.json"
                copied.unlink()
                os.link(copied.with_name("fixture-index.json"), copied)
            write_json(target / "input.json", {"case": name, "runtime": str(runtime),
                       "user_data": str(target / "user-data"), "resource_fixture_only": True})
        reference = directory / "reference"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", RUMI_ENVIRONMENT="production",
                   TOBKIRI_USER_DATA=str(reference / "user-data"), RUMI_USER_DATA=str(reference / "user-data"))
        env.pop("RUMI_APP_DIR", None)
        result = reference / "observation.json"
        subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "probe",
                        "--original-root", str(original), "--runtime", str(runtime),
                        "--user-data", str(reference / "user-data"), "--output", str(result)],
                       env=env, check=True, timeout=180)
        observed = json.loads(result.read_bytes())
        reports.append({"name": name, "native_input": str(directory / "native/input.json"), **observed})
    positives = {"index-absent", "legacy-file", "existing-profile", "legacy-bootstrap-missing"}
    for row in reports:
        if (row["name"] in positives) != (row["expected"]["outcome"] == "integration-boundary"):
            raise AssertionError((row["name"], "unexpected original branch"))
        if row["name"] in positives:
            method = ("import_legacy_collection" if row["name"] == "legacy-file"
                      else "migrate_legacy_successors")
            if row["expected"]["stage"] != method:
                raise AssertionError((row["name"], "wrong original migration entry"))
        if row["reached"] and (row["reached"][0]["definition_lock_free"] is not True
                               or row["reached"][0]["journal_lock_free"] is not False):
            raise AssertionError("original definition/journal lock lifetimes differ")
    write_json(output / "summary.json", {"format": FORMAT, "original_head": ORIGINAL_HEAD,
               "case_count": len(reports), "cases": reports, "native_comparison": False,
               "qualification": False, "production_enabled": False,
               "scope": "original constructor/list and cleanup up to first migration; controlled resource fixtures; "
                        "no migration body, actual native Store, factory qualification, Authority or GUI"})


def compare(original: Path, fixtures: Path, observations: Path, output: Path) -> None:
    """Independently compare direct native observations and actual fixture files."""
    check_original(original)
    for path in (fixtures, observations):
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("definition comparison exceeds bounded input")
    expected = json.loads(fixtures.read_bytes())
    native = json.loads(observations.read_bytes())
    if (expected.get("format") != FORMAT or expected.get("original_head") != ORIGINAL_HEAD
            or expected.get("case_count") != 13
            or native.get("format") != "io.tobkiri.original-profile-definition-prefix-native-observations.v1"
            or native.get("original_head") != ORIGINAL_HEAD or native.get("case_count") != 13):
        raise ValueError("definition comparison identity differs")
    references = {row["name"]: row for row in expected["cases"]}
    actual = {row["name"]: row for row in native["cases"]}
    if (len(expected["cases"]) != 13 or len(native["cases"]) != 13
            or set(references) != set(CASES) or set(actual) != set(CASES)):
        raise ValueError("definition comparison case set differs")
    flags = (
        "key_bytes_and_identity_unchanged", "index_bytes_and_identity_unchanged",
        "execution_matched_interval", "physical_owner_reopened",
    )
    # The driver records the exact close receipt rather than a new success
    # token; its data is compared with independently inspected OS state below.
    results = []
    for name in CASES:
        reference, observed = references[name], actual[name]
        user_data = fixtures.parent / name / "native/user-data"
        state = filesystem(user_data)
        receipt = observed.get("close_receipt", {})
        after = observed.get("capacity", {}).get("after_reap", {})
        key = user_data / "profiles/.publication-key"
        seed_key = fixtures.parent / name / "seed-user-data/profiles/.publication-key"
        key_equal = key.read_bytes() == seed_key.read_bytes()
        # cleanup_verified is derived here; it is not an input success flag.
        cleanup = (observed.get("close_succeeded") is True
                   and receipt == {"open_files": 0, "open_connections": 0, "uncertain": False}
                   and after == {"bytes": 0, "reservations": 0}
                   and _lock_free(user_data / "profiles/.profile-definition.lock") is True
                   and _lock_free(user_data / "profiles/.bootstrap-publication.lock") is True)
        matched = (observed.get("observation") == reference["expected"]
                   and state == reference["user_data_after"]
                   and observed.get("user_data_after") == state
                   and all(observed.get(flag) is True for flag in flags)
                   and observed.get("journal_lock_held_after_store_retirement") is True
                   and observed.get("definition_lock_free_before_finish") is True
                   and observed.get("journal_lock_free_before_finish") is False
                   and observed.get("single_store") is True
                   and observed.get("budget_resets") == 0
                   and observed.get("same_original_30s_deadline") is True
                   and key_equal and cleanup)
        results.append({"name": name, "matched": matched,
                        "typed_outcome_matches": observed.get("observation") == reference["expected"],
                        "independent_files_match": state == reference["user_data_after"],
                        "existing_key_unchanged": key_equal, "cleanup_verified": cleanup})
    passed = sum(row["matched"] for row in results)
    write_json(output, {"format": "io.tobkiri.original-profile-definition-prefix-comparison.v1",
               "original_head": ORIGINAL_HEAD, "cases": results,
               "passed": passed, "failed": len(results) - passed,
               "qualification": False, "production_enabled": False,
               "scope": "direct controlled-root native interval and typed projection only; "
                        "no actual AppHandle, Wasm catalog, Guest factory, migration or Authority qualification"})
    if passed != 13:
        raise AssertionError("native definitions interval differs; see bounded comparison")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "probe", "compare"))
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog-fixtures", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--user-data", type=Path)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    if args.command == "prepare":
        if args.catalog_fixtures is None:
            parser.error("prepare requires --catalog-fixtures")
        prepare(original, args.catalog_fixtures.resolve(strict=True), args.output.resolve())
    elif args.command == "probe":
        if args.runtime is None or args.user_data is None:
            parser.error("probe requires --runtime and --user-data")
        probe(original, args.runtime.resolve(strict=True), args.user_data.resolve(strict=True), args.output)
    else:
        if args.fixtures is None or args.observations is None:
            parser.error("compare requires --fixtures and --observations")
        compare(original, args.fixtures.resolve(strict=True),
                args.observations.resolve(strict=True), args.output)


if __name__ == "__main__":
    main()
