"""Observe the pinned original Setup caller through candidate selection in CI."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
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

FORMAT = "io.tobkiri.original-setup-candidate-selection-reference.v1"
PROFILE_ID = "defaults"
BUNDLE = "ecosystem/defaultspack/v4"
SOURCES = (
    "core_runtime/bootstrap/profile_registry.py",
    "core_runtime/active_profile_store_v4.py",
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


class ActiveAuthorityBoundary(BaseException):
    """Stop at the active-candidate Authority call before opening the database."""


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
    pointer_code = ActiveProfileStore.load.__code__
    definition_code = ProfileDefinitionStore.get_profile.__code__
    for code, name in (
        (
            catalog_code,
            "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
        ),
        (selection_code, "core_runtime/bootstrap/profile_registry.py"),
        (pointer_code, "core_runtime/active_profile_store_v4.py"),
        (definition_code, "core_runtime/profile_definition_store_v4.py"),
        (
            profile_capture._bootstrap_review_candidate.__code__,
            "core_runtime/bootstrap/profile_capture.py",
        ),
    ):
        if Path(code.co_filename).resolve() != runtime / name:
            raise AssertionError("Setup observation imported another runtime source")
    observed_catalogs: list[Any] = []
    calls: list[str] = []

    def observe(frame: FrameType, event: str, result: Any) -> None:
        code = frame.f_code
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
        if code is selection_code:
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

    authority = profile_registry.AuthorityStore
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
    ):
        sys.setprofile(observe)
        try:
            candidate = profile_capture._bootstrap_review_candidate(
                include_source_additions=additions
            )
        except ActiveAuthorityBoundary:
            result = {
                "outcome": "integration-boundary",
                "stage": "active-authority-entry",
            }
        except (ProfileResolutionDenied, ActiveProfileStoreIntegrityError) as error:
            result = {"outcome": "error", "error": error_graph(error, user_data)}
        else:
            if (
                type(candidate) is not tuple
                or len(candidate) != 2
                or len(observed_catalogs) != 1
                or candidate[0] is not observed_catalogs[0]
                or type(candidate[1]) is not tuple
                or candidate[1]
            ):
                raise AssertionError(
                    "candidate did not preserve the exact bundled catalog"
                )
            result = {
                "outcome": "bundled-catalog",
                "same_catalog": True,
                "additional_packs": [],
            }
        finally:
            sys.setprofile(previous)

    if (
        profile_registry.AuthorityStore is not authority
        or profile_capture.AuthorityStore is not authority
        or require_profile_runtime() is not port
        or len(observed_catalogs) != 1
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
        "expected": result,
        "user_data_before": before,
        "user_data_after": after,
        "definition_lock_free_after": definition_lock_free,
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
        error_type = (
            "ecosystem.defaultspack.domain.profile_resolution.ProfileResolutionDenied"
        )
        message = DENIALS[case]
    if (error["type"], error["message"]) != (error_type, message):
        raise AssertionError((case, additions, error))
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
    if lock is None:
        return None
    with (user_data / DEFINITION_LOCK).open("r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(stream, fcntl.LOCK_UN)
    return True


def main() -> None:
    """Run independent fixture cases and write only bounded reference metadata."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    check_original(original)
    output = args.output.resolve()
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

    cases = []
    for case in CASES:
        for additions in (False, True):
            user_data = output / f"{case}-{int(additions)}" / "user-data"
            _seed(case, user_data, source)
            cases.append(_observe(case, additions, runtime, user_data))
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


if __name__ == "__main__":
    main()
