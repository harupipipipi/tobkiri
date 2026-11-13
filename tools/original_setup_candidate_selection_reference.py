"""Observe the pinned original Setup caller through candidate selection in CI."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import FrameType
from typing import Any
from unittest.mock import patch

from original_active_profile_fixtures import original_activation_writer
from original_authority_reference import ORIGINAL_HEAD, error_graph, write_json
from original_catalog_prefix_reference import IGNORED, bind_resources, code_hashes
from original_publication_reference import filesystem
from original_setup_review_reference import check_original

FORMAT = "io.tobkiri.original-setup-candidate-selection-reference.v2"
NATIVE_INPUT = "io.tobkiri.original-setup-candidate-selection-native-input.v1"
NATIVE_OBSERVATIONS = "io.tobkiri.original-setup-candidate-selection-native-observations.v1"
COMPARISON = "io.tobkiri.original-setup-candidate-selection-comparison.v1"
DENIED_CLASS = "ecosystem.defaultspack.domain.runtime_v4.service.ProfileResolutionDenied"
LIMIT = 2 * 1024 * 1024
PROFILE_ID = "defaults"
BUNDLE = "ecosystem/defaultspack/v4"
SOURCES = (
    "core_runtime/bootstrap/profile_registry.py",
    "core_runtime/bootstrap/profile_publication.py",
    "core_runtime/active_profile_store_v4.py",
    "core_runtime/profile_definition_store_v4.py",
    "tobkiri_protocol/canonical.py",
    "tobkiri_protocol/secure_persistence.py",
)
DENIALS = {
    "no-pointer": "source update requires an active Profile",
    "other-profile": "source update requires the active Profile",
    "unregistered-profile": "source update requires a registered Profile",
}
POINTER_ERRORS = {
    "dangling-pointer": "active Profile pointer is unavailable",
    "missing-snapshot": "active Profile activation snapshot is unavailable",
    "other-profile-snapshot-mismatch": (
        "activation snapshot identity does not match active Profile pointer"
    ),
}
CASES = (*DENIALS, *POINTER_ERRORS, "registered-profile")
DEFINITION_LOCK = "profiles/.profile-definition.lock"
PUBLICATION_LOCK = "profiles/.bootstrap-publication.lock"
ACTIVE_LOCK = "profiles/.active-profile.lock"


class ActiveAuthorityBoundary(BaseException):
    """Stop at the active-candidate Authority call before opening the database."""


class ResolveCandidateBoundary(BaseException):
    """Stop before using the returned bundled candidate for resolution."""


def _source_hashes(runtime: Path) -> dict[str, str]:
    return {
        **code_hashes(runtime),
        **{
            name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
            for name in SOURCES
        },
    }


def _seed(case: str, user_data: Path, source: Path) -> None:
    from core_runtime.active_profile_store_v4 import ActiveProfileStore
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
    from tobkiri_protocol import canonical as protocol

    if case == "no-pointer":
        return
    if case == "dangling-pointer":
        user_data.mkdir(mode=0o700, parents=True)
        profiles = user_data / "profiles"
        profiles.mkdir(mode=0o700)
        (profiles / "active.json").symlink_to("missing-active.json")
        return

    pointer_id = "other-profile" if case.startswith("other-profile") else PROFILE_ID
    writer = original_activation_writer(source, protocol)
    activation, snapshot, relative = writer(user_data, pointer_id, "selection")
    store = ActiveProfileStore(user_data, clock=lambda: 1000.0)
    pointer = store.commit_activation(
        activation, activation_snapshot=snapshot, activation_snapshot_path=relative
    )
    if store.load(verify_snapshot=True) != pointer:
        raise AssertionError("original active Profile fixture did not verify")

    snapshot_path = user_data / relative
    if case == "missing-snapshot":
        snapshot_path.unlink()
    elif case == "other-profile-snapshot-mismatch":
        # Bind the new bytes so the original verifier reaches the identity check.
        snapshot["profile"]["profile_id"] = PROFILE_ID
        snapshot_path.write_bytes(protocol.canonical_json(snapshot) + b"\n")
        payload = pointer.payload()
        payload["activation_snapshot_digest"] = protocol.canonical_digest(snapshot)
        payload["pointer_digest"] = protocol.canonical_digest(payload)
        store.path.write_bytes(protocol.canonical_json(payload) + b"\n")
    elif case == "registered-profile":
        definition = protocol.strict_loads(
            (source / BUNDLE / "defaults.profile.v5.json").read_bytes()
        )
        ProfileDefinitionStore(user_data, clock=lambda: 1000.0).create_profile(
            definition
        )


def _observe(case: str, additions: bool, runtime: Path, user_data: Path) -> dict:
    from core_runtime.active_profile_store_v4 import (
        ActiveProfileStore,
        ActiveProfileStoreIntegrityError,
    )
    from core_runtime.bootstrap import profile_capture, profile_registry
    from core_runtime.bootstrap.profile_publication import ProfilePublicationJournal
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
    from core_runtime.profile_runtime_port import require_profile_runtime
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )
    from ecosystem.defaultspack.domain.profile_resolution import ProfileResolutionDenied
    from tobkiri_protocol.bundle_catalog import BundledCatalog
    from tobkiri_protocol.canonical import canonical_digest

    port = install_defaultspack_profile_runtime()
    catalog_code = type(port).load_catalog.__code__
    selection_code = profile_registry.bootstrap_review_catalog.__code__
    recovery_code = profile_capture._recover_bootstrap_publication.__code__
    journal_exists_code = ProfilePublicationJournal.exists.__code__
    pointer_code = ActiveProfileStore.load.__code__
    definition_code = ProfileDefinitionStore.get_profile.__code__
    for code, name in (
        (
            catalog_code,
            "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
        ),
        (selection_code, "core_runtime/bootstrap/profile_registry.py"),
        (recovery_code, "core_runtime/bootstrap/profile_capture.py"),
        (journal_exists_code, "core_runtime/bootstrap/profile_publication.py"),
        (pointer_code, "core_runtime/active_profile_store_v4.py"),
        (definition_code, "core_runtime/profile_definition_store_v4.py"),
        (
            profile_capture._bootstrap_review_candidate.__code__,
            "core_runtime/bootstrap/profile_capture.py",
        ),
        (
            profile_capture.prepare_bootstrap_profile_review.__code__,
            "core_runtime/bootstrap/profile_capture.py",
        ),
    ):
        if Path(code.co_filename).resolve() != runtime / name:
            raise AssertionError("Setup observation imported another runtime source")
    observed_catalogs: list[Any] = []
    calls: list[str] = []
    recoveries: list[bool] = []
    recovery_results: list[str] = []
    journal_results: list[bool] = []

    def observe(frame: FrameType, event: str, result: Any) -> None:
        code = frame.f_code
        if code is journal_exists_code and event == "return":
            journal = frame.f_locals["self"]
            if (
                calls
                or frame.f_back is None
                or frame.f_back.f_code is not recovery_code
                or journal.path != user_data / "profiles/bootstrap-publication.json"
                or result is not False
                or len(journal_results) + 1 != len(recoveries)
            ):
                raise AssertionError("original recovery did not use an absent owned journal")
            journal_results.append(result)
        if code is recovery_code and event == "return":
            if (
                calls
                or result is not None
                or len(recoveries) != len(recovery_results) + 1
                or len(journal_results) != len(recoveries)
            ):
                raise AssertionError("original recovery did not return before selection")
            recovery_results.append("no-journal")
        if code is catalog_code and event == "return":
            if (
                type(result) is not BundledCatalog
                or frame.f_locals["self"] is not port
                or result.root != runtime / BUNDLE
                or result.artifact_root
                != (runtime / BUNDLE).parent / "platform-artifacts"
            ):
                raise AssertionError("original Pack did not load its packaged catalog")
            observed_catalogs.append(result)
            calls.append("catalog-load")
        if event != "call":
            return
        if code is recovery_code:
            args = frame.f_locals
            if (
                calls
                or args["confirmation"] is not None
                or profile_capture.runtime_user_data_root() != user_data
            ):
                raise AssertionError("original recovery order or scope changed")
            recoveries.append(args["discard_superseded"])
        elif code is selection_code:
            args = frame.f_locals
            if (
                len(observed_catalogs) != 1
                or args["catalog"] is not observed_catalogs[0]
                or args["runtime"] is not port
                or args["profile_id"] != PROFILE_ID
                or args["user_data"] != user_data
                or args["include_source_additions"] is not additions
                or profile_capture.runtime_user_data_root() != user_data
            ):
                raise AssertionError("Setup candidate owner, input or root changed")
            calls.append("candidate-selection")
        elif code is pointer_code:
            args = frame.f_locals
            if (
                args["self"].user_data_root != user_data
                or args["verify_snapshot"] is not True
            ):
                raise AssertionError(
                    "candidate did not require the owned verified pointer"
                )
            calls.append("verified-active-pointer-load")
        elif code is definition_code:
            args = frame.f_locals
            if (
                args["self"].user_data_root != user_data
                or args["profile_id"] != PROFILE_ID
                or args["include_tombstone"] is not False
            ):
                raise AssertionError("candidate registry lookup changed")
            calls.append("registered-profile-lookup")

    def stop_authority(path: Path) -> None:
        if path != user_data / "authority/v4.sqlite3":
            raise AssertionError("candidate requested Authority from another Host root")
        calls.append("active-authority-entry")
        raise ActiveAuthorityBoundary

    def stop_resolve(*, base_dir: Path | None, review_candidate: Any) -> None:
        if (
            base_dir is not None
            or type(review_candidate) is not tuple
            or len(review_candidate) != 2
            or len(observed_catalogs) != 1
            or review_candidate[0] is not observed_catalogs[0]
            or type(review_candidate[1]) is not tuple
            or review_candidate[1]
        ):
            raise AssertionError("candidate did not preserve the exact bundled catalog")
        raise ResolveCandidateBoundary

    authority = profile_registry.AuthorityStore
    resolve_candidate = profile_capture._resolve_bootstrap_candidate
    before = filesystem(user_data)
    previous = sys.getprofile()
    with (
        patch.dict(
            os.environ,
            {
                "TOBKIRI_USER_DATA": str(user_data),
                "RUMI_USER_DATA": str(user_data),
                "RUMI_ENVIRONMENT": "production",
            },
        ),
        patch.object(profile_registry, "AuthorityStore", stop_authority),
        patch.object(profile_capture, "_resolve_bootstrap_candidate", stop_resolve),
    ):
        sys.setprofile(observe)
        try:
            profile_capture.prepare_bootstrap_profile_review(
                include_source_additions=additions
            )
        except ActiveAuthorityBoundary:
            result = {
                "outcome": "integration-boundary",
                "stage": "active-authority-entry",
            }
        except ResolveCandidateBoundary:
            result = {
                "outcome": "bundled-catalog",
                "same_catalog": True,
                "additional_packs": [],
            }
        except (ProfileResolutionDenied, ActiveProfileStoreIntegrityError) as error:
            result = {"outcome": "error", "error": error_graph(error, user_data)}
        else:
            raise AssertionError("original Setup passed the candidate selection boundary")
        finally:
            sys.setprofile(previous)

    if (
        profile_registry.AuthorityStore is not authority
        or profile_capture.AuthorityStore is not authority
        or profile_capture._resolve_bootstrap_candidate is not resolve_candidate
        or require_profile_runtime() is not port
        or len(observed_catalogs) != 1
        or recoveries != [True, False]
        or recovery_results != ["no-journal", "no-journal"]
    ):
        raise AssertionError("original candidate callable or registered owner changed")
    _check_outcome(case, additions, calls, result, user_data)
    after = filesystem(user_data)
    definition_lock_free = _check_effects(case, before, after, user_data)
    return {
        "case": case,
        "include_source_additions": additions,
        "profile_id": PROFILE_ID,
        "bundled_profile_digest": canonical_digest(
            observed_catalogs[0].profiles[PROFILE_ID]
        ),
        "calls": calls,
        "discard_superseded_calls": recoveries,
        "recovery_results": recovery_results,
        "expected": result,
        "user_data_before": before,
        "user_data_after": after,
        "definition_lock_free_after": definition_lock_free,
        "publication_lock_free_after": _lock_free(user_data / PUBLICATION_LOCK),
        "active_lock_free_after": _lock_free(user_data / ACTIVE_LOCK),
    }


def _check_outcome(
    case: str, additions: bool, calls: list[str], result: dict, user_data: Path
) -> None:
    expected_calls = ["catalog-load", "candidate-selection"]
    if case != "no-pointer":
        expected_calls.append("verified-active-pointer-load")
    if case in ("unregistered-profile", "registered-profile"):
        expected_calls.append("registered-profile-lookup")
    if case == "registered-profile":
        expected_calls.append("active-authority-entry")
        expected = "integration-boundary"
    elif case in POINTER_ERRORS or additions:
        expected = "error"
    else:
        expected = "bundled-catalog"
    if calls != expected_calls or result["outcome"] != expected:
        raise AssertionError((case, additions, calls, result))
    if expected != "error":
        return
    error = result["error"]
    if case in POINTER_ERRORS:
        error_type = (
            "core_runtime.active_profile_store_v4.ActiveProfileStoreIntegrityError"
        )
        message = POINTER_ERRORS[case]
    else:
        # The original definition preserves its legacy public module name.
        error_type = DENIED_CLASS
        message = DENIALS[case]
    if (error["type"], error["message"]) != (error_type, message):
        raise AssertionError((case, additions, error))
    if error_type == DENIED_CLASS and (
        error["suppressed"] or error["cause"] is not None or error["context"] is not None
    ):
        raise AssertionError("original plain denial unexpectedly has an exception chain")
    if case == "dangling-pointer":
        cause = error["cause"] or {}
        if (
            not (user_data / "profiles/active.json").is_symlink()
            or cause.get("type")
            != "tobkiri_protocol.secure_persistence.SecurePersistenceError"
        ):
            raise AssertionError(
                "dangling pointer did not fail through secure persistence"
            )


def _check_effects(
    case: str, before: dict, after: dict, user_data: Path
) -> bool | None:
    lock = next((row for row in after["files"] if row["path"] == DEFINITION_LOCK), None)
    comparable = after
    if case == "unregistered-profile":
        if lock != {
            "path": DEFINITION_LOCK,
            "kind": "file",
            "mode": 0o600,
            "bytes": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
        }:
            raise AssertionError("original empty registry did not create its read lock")
        comparable = {**after, "files": [row for row in after["files"] if row != lock]}
    if before != comparable:
        raise AssertionError("candidate changed existing user state")
    free = _lock_free(user_data / DEFINITION_LOCK)
    if free is not (True if lock is not None else None):
        raise AssertionError("original definition lock was not released")
    return free


def _lock_free(path: Path) -> bool | None:
    try:
        stream = path.open("r+b")
    except FileNotFoundError:
        return None
    with stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(stream, fcntl.LOCK_UN)
    return True


def prepare(original: Path, output: Path, native_inputs: bool) -> None:
    """Observe original cases and independently seed native inputs on request."""
    check_original(original)
    if output.is_relative_to(original) or not sys.dont_write_bytecode:
        raise ValueError("independent fixture directory and disabled bytecode required")
    source = original / "tobkiri_runtime"
    expected = _source_hashes(source)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    runtime = output / "runtime"
    shutil.copytree(source, runtime, ignore=IGNORED)
    bind_resources(runtime)
    if _source_hashes(runtime) != expected:
        raise ValueError("staged original sources differ")
    sys.path.insert(0, str(runtime))

    cases, inputs = [], []
    for case in CASES:
        for additions in (False, True):
            case_root = output / f"{case}-{int(additions)}"
            user_data = case_root / "user-data"
            _seed(case, user_data, source)
            observed = _observe(case, additions, runtime, user_data)
            cases.append(observed)
            if native_inputs:
                native_root = case_root / "native"
                native_root.mkdir(mode=0o700, parents=True)
                native_user_data = native_root / "user-data"
                _seed(case, native_user_data, source)
                if filesystem(native_user_data) != observed["user_data_before"]:
                    raise AssertionError("independent native seed differs from original input")
                inputs.append({
                    "case": case,
                    "include_source_additions": additions,
                    "profile_id": PROFILE_ID,
                })
    if _source_hashes(runtime) != expected or _source_hashes(source) != expected:
        raise AssertionError("original sources changed during candidate selection")
    check_original(original)
    write_json(
        output / "summary.json",
        {
            "format": FORMAT,
            "original_head": ORIGINAL_HEAD,
            "python": "3.13.16",
            "source_hashes": expected,
            "fixture_writer_sha256": hashlib.sha256(
                (source / "tests/test_named_profile_store_v4.py").read_bytes()
            ).hexdigest(),
            "cases": cases,
            "case_count": len(cases),
            "active_authority_body_executed": False,
            "native_comparison": False,
            "qualification": False,
            "production_enabled": False,
            "scope": "original packaged-resource fixture and post-load Setup candidate selection",
        },
    )
    if native_inputs:
        write_json(
            output / "native-input.json",
            {"format": NATIVE_INPUT, "original_head": ORIGINAL_HEAD, "cases": inputs},
        )


def _read_bounded(path: Path) -> dict:
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("candidate comparison exceeds bounded input")
    result = json.loads(data)
    if type(result) is not dict:
        raise ValueError("candidate comparison requires an object")
    return result


def _case_map(summary: dict, format_name: str) -> dict[tuple[str, bool], dict]:
    rows = summary["cases"]
    wanted = {(name, additions) for name in CASES for additions in (False, True)}
    if (
        summary.get("format") != format_name
        or summary.get("original_head") != ORIGINAL_HEAD
        or summary.get("case_count") != len(wanted)
        or type(rows) is not list
        or len(rows) != len(wanted)
    ):
        raise ValueError("candidate comparison identity differs")
    result = {}
    for row in rows:
        name, additions = row["case"], row["include_source_additions"]
        if type(name) is not str or type(additions) is not bool:
            raise ValueError("invalid candidate case key")
        result[(name, additions)] = row
    if result.keys() != wanted:
        raise ValueError("candidate comparison case set differs")
    return result


def _expected_native(original: dict) -> dict:
    if original["outcome"] == "bundled-catalog":
        if original["same_catalog"] is not True or original["additional_packs"] != []:
            raise ValueError("original bundled return contract changed")
        return {"outcome": "bundled-catalog"}
    if original["outcome"] == "error" and original["error"]["type"] == DENIED_CLASS:
        return {"outcome": "denial-request", "message": original["error"]["message"]}
    return original


def compare(original: Path, output: Path) -> None:
    """Compare actual native outcomes, descriptor receipts and independent disk state."""
    check_original(original)
    reference = _read_bounded(output / "summary.json")
    source = original / "tobkiri_runtime"
    if (
        reference.get("python") != "3.13.16"
        or reference.get("source_hashes") != _source_hashes(source)
        or reference.get("fixture_writer_sha256")
        != hashlib.sha256((source / "tests/test_named_profile_store_v4.py").read_bytes()).hexdigest()
    ):
        raise ValueError("candidate comparison requires the pinned original sources")
    expected = _case_map(reference, FORMAT)
    actual = _case_map(
        _read_bounded(output / "native-observations.json"), NATIVE_OBSERVATIONS
    )
    results = []
    for (case, additions), original_row in expected.items():
        observed = actual[(case, additions)]
        root = output / f"{case}-{int(additions)}" / "native/user-data"
        after = filesystem(root)
        definition_lock = _lock_free(root / DEFINITION_LOCK)
        publication_lock = _lock_free(root / PUBLICATION_LOCK)
        active_lock = _lock_free(root / ACTIVE_LOCK)
        profile_matches = observed["profile_id"] == original_row["profile_id"]
        outcome_matches = observed["selection"] == _expected_native(
            original_row["expected"]
        )
        filesystem_matches = (
            observed["user_data_before"] == original_row["user_data_before"]
            and observed["user_data_after"] == after == original_row["user_data_after"]
        )
        locks_match = (
            observed["definition_lock_free_after"] is definition_lock
            and definition_lock is original_row["definition_lock_free_after"]
            and observed["publication_lock_free_after"] is publication_lock
            and publication_lock is original_row["publication_lock_free_after"]
            and observed["active_lock_free_after"] is active_lock
            and active_lock is original_row["active_lock_free_after"]
            and definition_lock is not False
            and publication_lock is None
            and active_lock is not False
        )
        capacity = observed["capacity"]
        cleanup = (
            observed["close_succeeded"] is True
            and observed["close_receipt"]
            == {"open_files": 0, "open_connections": 0, "uncertain": False}
            and observed["retained_cleanup"] is False
            and capacity["charged_bytes"] == capacity["reservations"] == 0
            and 0 < capacity["peak_bytes"] <= capacity["limit_bytes"] == 64 * 1024 * 1024
            and observed["physical_owner_reopened"] is True
            and locks_match
        )
        execution = (
            observed["execution_error"] is None
            and observed["discard_superseded_calls"]
            == original_row["discard_superseded_calls"]
            and observed["recovery_results"] == original_row["recovery_results"]
            and observed["same_original_deadline"] is True
            and observed["initial_fuel"] == observed["final_fuel"] == 2_000_000_000
        )
        results.append({
            "case": case,
            "include_source_additions": additions,
            "input_matches": profile_matches,
            "outcome_matches": outcome_matches,
            "filesystem_matches": filesystem_matches,
            "locks_match": locks_match,
            "execution_verified": execution,
            "cleanup_verified": cleanup,
            "matched": (
                profile_matches and outcome_matches and filesystem_matches
                and execution and cleanup
            ),
        })
    passed = sum(row["matched"] for row in results)
    write_json(
        output / "comparison.json",
        {
            "format": COMPARISON,
            "original_head": ORIGINAL_HEAD,
            "cases": results,
            "passed": passed,
            "failed": len(results) - passed,
            "qualification": False,
            "production_enabled": False,
            "scope": "direct Host selector and typed requests under one real Store; "
            "catalog identity only in original lane; no native catalog, getter, Guest denial "
            "factory, candidate resolution, active Authority, AppHandle, or GUI",
        },
    )
    if passed != len(results):
        raise AssertionError("native candidate selection differs; see comparison.json")


def main() -> None:
    """Prepare or compare the fixed original Setup candidate fixtures in CI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "compare"), default="prepare", nargs="?")
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-inputs", action="store_true")
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    output = args.output.resolve()
    if args.command == "compare":
        if args.native_inputs:
            parser.error("--native-inputs is only valid during prepare")
        compare(original, output)
    else:
        prepare(original, output, args.native_inputs)


if __name__ == "__main__":
    main()
