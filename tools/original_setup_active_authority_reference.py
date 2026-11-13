"""Observe the original registered Setup path up to the activation factory in CI."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import FrameType
from typing import Any
from unittest.mock import patch

from original_authority_reference import (
    CLOCK,
    ORIGINAL_HEAD,
    SOURCES as AUTHORITY_SOURCES,
    database_state,
    error_graph,
    logical_state,
    mutate,
    write_json,
)
from original_catalog_prefix_reference import IGNORED, bind_resources
from original_publication_reference import filesystem
from original_setup_candidate_selection_reference import (
    ACTIVE_LOCK,
    BUNDLE,
    DEFINITION_LOCK,
    PROFILE_ID,
    PUBLICATION_LOCK,
    _lock_free,
    _seed,
    _source_hashes as candidate_source_hashes,
)
from original_setup_review_reference import check_original

FORMAT = "io.tobkiri.original-setup-active-authority-reference.v1"
DB = Path("authority/v4.sqlite3")
KEY = DB.with_suffix(".key")
GUARD = DB.with_name(".v4.sqlite3.lifecycle.lock")
COLLISION = "database-created-after-key"
CASES = (
    "absent",
    "existing-invalid-reason",
    "historical-v1",
    "database-directory",
    "dangling-database",
    "missing-key",
    COLLISION,
)
PATH_ERRORS = {"database-directory", "dangling-database"}
ERRORS = {*PATH_ERRORS, "missing-key", COLLISION}
HISTORICAL_SOURCES = (
    "tests/__init__.py",
    "tests/test_authority_v4_lifecycle.py",
    "tests/test_authority_v4_schema_migration.py",
)
FIXTURE_WRITER = "tests/test_named_profile_store_v4.py"


class _ActivationFactoryBoundary(BaseException):
    """Stop at the exact activation factory invocation during the real context."""


def _source_hashes(runtime: Path) -> dict[str, str]:
    return {
        **candidate_source_hashes(runtime),
        **{
            name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
            for name in AUTHORITY_SOURCES
        },
    }


def _seed_authority(case: str, user_data: Path, fixture_root: Path) -> None:
    from core_runtime.authority.v4 import AuthorityStore

    path = user_data / DB
    if case in {"absent", COLLISION}:
        return
    path.parent.mkdir(mode=0o700)
    if case == "database-directory":
        path.mkdir(mode=0o700)
    elif case == "dangling-database":
        path.symlink_to("missing.sqlite3")
    elif case == "historical-v1":
        from tests.test_authority_v4_schema_migration import historical_authority_v1

        historical = fixture_root / "historical"
        historical.mkdir(mode=0o700)
        fixture = historical_authority_v1.__wrapped__(historical)
        fixture.harness.store.close()
        # The original fixture can leave committed WAL pages. SQLite backup
        # captures them without opening the historical database as Authority.
        with closing(sqlite3.connect(fixture.path)) as live:
            with closing(sqlite3.connect(path)) as captured:
                live.backup(captured)
        path.chmod(0o600)
        shutil.copy2(fixture.key_path, user_data / KEY)
        shutil.copy2(
            fixture.path.with_name(f".{fixture.path.name}.lifecycle.lock"),
            user_data / GUARD,
        )
    else:
        with AuthorityStore(path, clock=lambda: CLOCK) as store:
            store.advance_security_epoch("registered-setup-reference-fixture")
        if case == "existing-invalid-reason":
            mutate(path, {"security_epoch_reason_digest": "invalid"})
        elif case == "missing-key":
            (user_data / KEY).unlink()


def _observe(
    runtime: Path,
    user_data: Path,
    additions: bool,
    *,
    competing_database: bool,
) -> tuple[dict, list[str]]:
    from core_runtime.active_profile_store_v4 import ActiveProfileStore
    from core_runtime.authority.v4_store import AuthorityStore, AuthorityStoreError
    from core_runtime.bootstrap import profile_capture, profile_registry
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
    from core_runtime.profile_runtime_port import require_profile_runtime
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )
    from tobkiri_protocol.bundle_catalog import BundledCatalog
    from tobkiri_protocol.canonical import canonical_digest

    port = install_defaultspack_profile_runtime()
    port_type = type(port)
    catalog_code = port_type.load_catalog.__code__
    selection_code = profile_registry.bootstrap_review_catalog.__code__
    recovery_code = profile_capture._recover_bootstrap_publication.__code__
    caller_code = profile_capture.prepare_bootstrap_profile_review.__code__
    init_code = AuthorityStore.__init__.__code__
    database_code = AuthorityStore._ensure_database_file.__code__
    enter_code = AuthorityStore.__enter__.__code__
    exit_code = AuthorityStore.__exit__.__code__
    close_code = AuthorityStore.close.__code__
    active_code = ActiveProfileStore.load.__code__
    definition_code = ProfileDefinitionStore.get_profile.__code__
    calls = {
        selection_code,
        recovery_code,
        init_code,
        exit_code,
        active_code,
        definition_code,
    }
    later = {
        port_type.is_reconfirmation_required.__code__,
        profile_capture._resolve_bootstrap_candidate.__code__,
        AuthorityStore.security_epoch.fget.__wrapped__.__code__,
        AuthorityStore.security_epoch_record.fget.__wrapped__.__code__,
    }
    if (
        profile_registry.AuthorityStore is not AuthorityStore
        or profile_capture.AuthorityStore is not AuthorityStore
        or Path(init_code.co_filename).resolve()
        != runtime / "core_runtime/authority/v4_store.py"
        or Path(selection_code.co_filename).resolve()
        != runtime / "core_runtime/bootstrap/profile_registry.py"
        or Path(catalog_code.co_filename).resolve()
        != runtime
        / "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py"
        or Path(caller_code.co_filename).resolve()
        != runtime / "core_runtime/bootstrap/profile_capture.py"
    ):
        raise AssertionError("original Setup or Authority callable was substituted")
    trace: list[str] = []
    packaged_catalog = None
    constructed = None
    entered = None
    factory = None
    stop = _ActivationFactoryBoundary()

    def observe(frame: FrameType, event: str, value: Any) -> None:
        nonlocal packaged_catalog, constructed, entered
        code = frame.f_code
        if event == "return":
            if code is recovery_code:
                if value is not None:
                    raise AssertionError(
                        "Setup recovery unexpectedly returned a publication"
                    )
                trace.append("recover-no-journal")
            elif code is catalog_code:
                if (
                    packaged_catalog is not None
                    or type(value) is not BundledCatalog
                    or frame.f_locals["self"] is not port
                    or value.root != runtime / BUNDLE
                    or value.artifact_root
                    != (runtime / BUNDLE).parent / "platform-artifacts"
                ):
                    raise AssertionError("original packaged catalog identity differs")
                packaged_catalog = value
                trace.append("catalog")
            elif code is enter_code:
                if (
                    value is not constructed
                    or frame.f_locals["self"] is not constructed
                ):
                    raise AssertionError(
                        "original context did not return its Authority owner"
                    )
                entered = value
                trace.append("authority-enter")
            elif code is close_code:
                if frame.f_locals["self"] is not entered or not entered._closed:
                    raise AssertionError("original Authority owner did not close")
                trace.append("authority-close")
            return
        if event != "call":
            return
        if code in later:
            raise AssertionError("Setup ran beyond the expected Authority boundary")
        if code is database_code and competing_database:
            store = frame.f_locals["self"]
            if (
                store is not constructed
                or store._database_identity is not None
                or not (user_data / KEY).is_file()
                or (user_data / GUARD).exists()
            ):
                raise AssertionError("expected a prepared DB path and a newly published key")
            fd = os.open(user_data / DB, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            trace.append("competing-database-created")
            return
        if code not in calls:
            return
        args = frame.f_locals
        parent = frame.f_back.f_code if frame.f_back is not None else None
        if code is recovery_code:
            if args["base_dir"] is not None or args["confirmation"] is not None:
                raise AssertionError(
                    "original Setup recovery received different arguments"
                )
            trace.append(
                "recover-discard" if args["discard_superseded"] else "recover-keep"
            )
        elif code is selection_code:
            if (
                args["catalog"] is not packaged_catalog
                or args["runtime"] is not port
                or args["user_data"] != user_data
                or args["profile_id"] != PROFILE_ID
                or args["include_source_additions"] is not additions
            ):
                raise AssertionError(
                    "registered Setup selection received different arguments"
                )
            trace.append("select")
        elif code is active_code:
            if (
                parent is not selection_code
                or args["self"].user_data_root != user_data
                or args["verify_snapshot"] is not True
            ):
                raise AssertionError(
                    "Setup did not request the owned verified active pointer"
                )
            trace.append("verified-pointer")
        elif code is definition_code:
            if (
                parent is not selection_code
                or args["self"].user_data_root != user_data
                or args["profile_id"] != PROFILE_ID
                or args["include_tombstone"] is not False
            ):
                raise AssertionError(
                    "Setup did not look up the registered live Profile"
                )
            trace.append("registered-profile")
        elif code is init_code:
            if (
                parent is not selection_code
                or constructed is not None
                or type(args["self"]) is not AuthorityStore
                or args["path"] != user_data / DB
                or args["key_path"] is not None
            ):
                raise AssertionError("Setup opened a different Authority")
            constructed = args["self"]
            trace.append("authority-construct")
        elif code is exit_code:
            if (
                parent is not selection_code
                or args["self"] is not entered
                or args["exc_type"] is not type(stop)
                or args["exc_value"] is not stop
                or args["traceback"] is None
            ):
                raise AssertionError(
                    "original Authority did not exit for the factory stop"
                )
            trace.append("authority-exit")

    def stop_factory(
        *,
        root: Path,
        workspace: Path,
        profile_id: str,
        authority: Any,
        catalog: Any,
    ) -> None:
        nonlocal factory
        expected_workspace = user_data / "workspaces" / PROFILE_ID
        if (
            factory is not None
            or sys._getframe(1).f_code is not selection_code
            or entered is None
            or authority is not entered
            or authority is not constructed
            or authority._closed
            or authority.path != user_data / DB
            or authority.key_path != user_data / KEY
            or authority._guard_path != user_data / GUARD
            or catalog is not packaged_catalog
            or workspace != expected_workspace
            or root != expected_workspace / "activation"
            or profile_id != PROFILE_ID
            or require_profile_runtime() is not port
            or profile_capture.runtime_user_data_root() != user_data
        ):
            raise AssertionError("activation factory arguments changed")
        factory = {
            "workspace": workspace.relative_to(user_data).as_posix(),
            "root": root.relative_to(user_data).as_posix(),
            "authority": authority.path.relative_to(user_data).as_posix(),
            "same_authority": True,
            "same_catalog": True,
        }
        trace.append("activation-factory")
        raise stop

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
        patch.object(port, "activation_store", stop_factory),
    ):
        sys.setprofile(observe)
        try:
            profile_capture.prepare_bootstrap_profile_review(
                include_source_additions=additions
            )
        except _ActivationFactoryBoundary as error:
            if error is not stop or factory is None:
                raise AssertionError("unexpected activation factory stop") from error
            result = {"outcome": "activation-factory", "factory": factory}
        except AuthorityStoreError as error:
            result = {"outcome": "error", "error": error_graph(error, user_data)}
        else:
            raise AssertionError("original Setup returned before the expected stop")
        finally:
            sys.setprofile(previous)
    if packaged_catalog is None:
        raise AssertionError("original Setup did not load a packaged catalog")
    result["bundled_profile_digest"] = canonical_digest(
        packaged_catalog.profiles[PROFILE_ID]
    )
    return result, trace


def _non_authority_state(user_data: Path) -> dict:
    return {
        "root_mode": user_data.stat().st_mode,
        "children": {
            path.name: filesystem(path)
            for path in user_data.iterdir()
            if path.name != DB.parent.name
        },
    }


def _sqlite_summary(state: dict) -> dict | None:
    tables = state.get("tables")
    if tables is None:
        return None
    # Compare full logical tables in memory. The report contains a digest
    # and fixed metadata rather than decrypted records or any key material.
    logical = json.dumps(tables, ensure_ascii=True, allow_nan=False, sort_keys=True)
    return {
        "logical_sha256": hashlib.sha256(logical.encode()).hexdigest(),
        "meta": {row["key"]: row["value"] for row in tables["authority_meta"]["rows"]},
        "row_counts": {name: len(table["rows"]) for name, table in tables.items()},
        "lease_states": sorted(
            row["state"] for row in tables["invocation_leases"]["rows"]
        ),
    }


def _check_outcome(case: str, result: dict, trace: list[str]) -> None:
    expected_trace = [
        "recover-discard",
        "recover-no-journal",
        "recover-keep",
        "recover-no-journal",
        "catalog",
        "select",
        "verified-pointer",
        "registered-profile",
        "authority-construct",
    ]
    if case not in ERRORS:
        expected_trace.extend(
            (
                "authority-enter",
                "activation-factory",
                "authority-exit",
                "authority-close",
            )
        )
    elif case == COLLISION:
        expected_trace.append("competing-database-created")
    if trace != expected_trace:
        raise AssertionError((case, trace))
    if case not in ERRORS:
        if result["outcome"] != "activation-factory":
            raise AssertionError("matched registered Setup did not reach the factory")
        return
    if result["outcome"] != "error":
        raise AssertionError("invalid Authority did not fail during construction")
    graph = result["error"]
    expected_message = {
        **dict.fromkeys(PATH_ERRORS, "authority state path is unsafe"),
        "missing-key": "authority encryption key is missing for an existing database",
        COLLISION: "authority database path is unsafe",
    }[case]
    if (
        graph["type"] != "core_runtime.authority.store_contracts.AuthorityStoreError"
        or graph["message"] != expected_message
    ):
        raise AssertionError((case, graph))
    if case in PATH_ERRORS:
        cause = graph["cause"]
        if (
            cause is None
            or cause["type"] != "core_runtime.secure_sqlite_path.SecurePathError"
            or cause["message"] != "file is not regular"
            or cause["cause"] is not None
            or cause["context"] is not None
            or graph["context"] != {"ref": cause["id"]}
            or not graph["suppressed"]
        ):
            raise AssertionError((case, graph))
    elif case == COLLISION:
        cause = graph["cause"]
        if (
            cause is None
            or cause["type"] != "core_runtime.secure_sqlite_path.SecurePathError"
        ):
            raise AssertionError((case, graph))
        exists = cause["cause"]
        if (
            cause["message"] != "ancestor is unsafe"
            or not cause["suppressed"]
            or exists is None
            or exists["type"] != "builtins.FileExistsError"
            or exists["errno"] != errno.EEXIST
            or exists["filename"] != DB.name
            or exists["cause"] is not None
            or exists["context"] is not None
            or exists["suppressed"]
            or cause["context"] != {"ref": exists["id"]}
            or graph["context"] != {"ref": cause["id"]}
            or not graph["suppressed"]
        ):
            raise AssertionError((case, graph))
    elif (
        graph["cause"] is not None
        or graph["context"] is not None
        or graph["suppressed"]
    ):
        raise AssertionError((case, graph))


def _check_effects(case: str, before: dict, after: dict) -> None:
    if "inspection_error" in before or "inspection_error" in after:
        raise AssertionError("failed to read the independent SQLite inspection copy")
    if case == COLLISION:
        actual = {Path(row["path"]): row["mode"] for row in after["files"]}
        if before["files"] or actual != {DB.parent: 0o700, DB: 0o600, KEY: 0o600}:
            raise AssertionError((case, before, after))
        return
    if case in ERRORS or case == "existing-invalid-reason":
        if logical_state(before) != logical_state(after):
            raise AssertionError(
                "Authority changed the existing filesystem or database"
            )
    if case in ERRORS:
        return
    expected_modes = {DB.parent: 0o700, DB: 0o600, KEY: 0o600, GUARD: 0o600}
    actual_modes = {Path(row["path"]): row["mode"] for row in after["files"]}
    if actual_modes != expected_modes:
        raise AssertionError((case, actual_modes))
    tables = after["tables"]
    meta = {row["key"]: row["value"] for row in tables["authority_meta"]["rows"]}
    if meta["schema_version"] != "4" or "host_extension_registrations" not in tables:
        raise AssertionError("original Authority did not finish schema v4")
    if case == "absent":
        from core_runtime.authority.v4 import authority_digest

        if (
            before["files"]
            or meta["security_epoch"] != "1"
            or meta["activation_fencing_token"] != "0"
            or not math.isfinite(float(meta["security_epoch_advanced_at"]))
            or meta["security_epoch_reason_digest"]
            != authority_digest({"reason": "genesis"})
            or any(
                table["rows"]
                for name, table in tables.items()
                if name != "authority_meta"
            )
        ):
            raise AssertionError("absent Authority did not create real empty genesis")
    elif case == "existing-invalid-reason":
        if (
            meta["security_epoch"] != "2"
            or meta["security_epoch_reason_digest"] != "invalid"
        ):
            raise AssertionError(
                "Authority opening read or changed the malformed epoch reason"
            )
    elif case == "historical-v1":
        previous_tables = before["tables"]
        previous_meta = {
            row["key"]: row["value"]
            for row in previous_tables["authority_meta"]["rows"]
        }
        if (
            previous_meta["schema_version"] != "1"
            or "host_extension_registrations" in previous_tables
            or {row["state"] for row in previous_tables["invocation_leases"]["rows"]}
            != {"issued", "dispatched"}
            or {row["state"] for row in tables["invocation_leases"]["rows"]}
            != {"revoked", "ambiguous"}
            or len(tables["authority_audit"]["rows"])
            != len(previous_tables["authority_audit"]["rows"]) + 2
        ):
            raise AssertionError("real historical v1 lease migration did not run")


def _case(case: str, additions: bool, source: Path, runtime: Path, root: Path) -> dict:
    from cryptography.fernet import Fernet

    user_data = root / "user-data"
    _seed("registered-profile", user_data, source)
    _seed_authority(case, user_data, root)
    profile_state = _non_authority_state(user_data)
    before = database_state(user_data, root / "inspect-before", Fernet)
    key_path = user_data / KEY
    prior_key = key_path.read_bytes() if key_path.is_file() else None
    result, trace = _observe(runtime, user_data, additions, competing_database=case == COLLISION)
    _check_outcome(case, result, trace)
    if case == COLLISION:
        # Inspect the actual empty file without opening it as SQLite.
        if (user_data / DB).stat().st_size != 0:
            raise AssertionError("original Setup changed the competing file")
        paths = [user_data / DB.parent, *sorted((user_data / DB.parent).iterdir())]
        after = {
            "files": [
                {
                    "path": path.relative_to(user_data).as_posix(),
                    "mode": path.lstat().st_mode & 0o777,
                }
                for path in paths
            ],
        }
    else:
        after = database_state(user_data, root / "inspect-after", Fernet)
    _check_effects(case, before, after)
    if profile_state != _non_authority_state(user_data) or (
        prior_key is not None and key_path.read_bytes() != prior_key
    ):
        raise AssertionError(
            "Setup changed the seeded Profile or replaced its Authority key"
        )
    locks = {
        "authority": _lock_free(user_data / GUARD),
        "active_profile": _lock_free(user_data / ACTIVE_LOCK),
        "definition": _lock_free(user_data / DEFINITION_LOCK),
        "publication": _lock_free(user_data / PUBLICATION_LOCK),
    }
    if locks != {
        "authority": True if case not in ERRORS or case == "missing-key" else None,
        "active_profile": True,
        "definition": True,
        "publication": None,
    }:
        raise AssertionError((case, locks))
    return {
        "case": case,
        "include_source_additions": additions,
        "profile_id": PROFILE_ID,
        "trace": trace,
        "expected": result,
        "before": {"files": before["files"], "sqlite": _sqlite_summary(before)},
        "after": {"files": after["files"], "sqlite": _sqlite_summary(after)},
        "profile_files_unchanged": True,
        "lock_free_after": locks,
    }


def prepare(original: Path, output: Path) -> None:
    """Run the real pinned Setup caller and retain only bounded observations."""
    check_original(original)
    if output.is_relative_to(original) or not sys.dont_write_bytecode:
        raise ValueError("independent fixture directory and disabled bytecode required")
    source = original / "tobkiri_runtime"
    expected = _source_hashes(source)
    fixture_hashes = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in (*HISTORICAL_SOURCES, FIXTURE_WRITER)
    }
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    runtime = output / "runtime"
    shutil.copytree(source, runtime, ignore=IGNORED)
    (runtime / "tests").mkdir()
    for name in HISTORICAL_SOURCES:
        shutil.copy2(source / name, runtime / name)
    bind_resources(runtime)
    if _source_hashes(runtime) != expected or any(
        hashlib.sha256((runtime / name).read_bytes()).hexdigest()
        != fixture_hashes[name]
        for name in HISTORICAL_SOURCES
    ):
        raise ValueError("staged original sources differ")
    sys.path.insert(0, str(runtime))
    rows = [
        _case(case, additions, source, runtime, output / f"{case}-{int(additions)}")
        for case in CASES
        for additions in (False, True)
    ]
    if _source_hashes(runtime) != expected or _source_hashes(source) != expected:
        raise AssertionError("pinned source changed during registered Setup")
    check_original(original)
    write_json(
        output / "summary.json",
        {
            "format": FORMAT,
            "original_head": ORIGINAL_HEAD,
            "python": "3.13.16",
            "source_hashes": expected,
            "fixture_source_hashes": fixture_hashes,
            "cases": rows,
            "case_count": len(rows),
            "reference_checks_passed": True,
            "activation_factory_body_executed": False,
            "native_comparison": False,
            "qualification": False,
            "production_enabled": False,
        },
        compact=True,
    )


def main() -> None:
    """Run the original-only registered Setup Authority observation in CI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.original_root.resolve(strict=True), args.output.resolve())


if __name__ == "__main__":
    main()
