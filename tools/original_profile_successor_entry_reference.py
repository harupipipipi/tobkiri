"""Observe the pinned original successor migration up to its next call boundary.

The original recovery, catalog, definition store, and successor loop run in CI.
The two sentinels stop at the successor builder or display-name repair entry.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import FrameType
from typing import TYPE_CHECKING, Any

from original_authority_reference import ORIGINAL_HEAD, load_json, write_json
from original_catalog_prefix_reference import code_hashes
from original_profile_definition_prefix_reference import (
    CASES as DEFINITION_CASES,
    FORMAT as DEFINITION_FORMAT,
    _lock_free,
)
from original_publication_reference import filesystem
from original_setup_review_reference import check_original

if TYPE_CHECKING:
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore

FORMAT = "io.tobkiri.original-profile-successor-entry-reference.v1"
CASES = (
    "index-absent",
    "index-empty",
    "tombstone",
    "no-legacy-id",
    "existing-v4",
    "existing-v5",
    "eligible-source",
    "eligible-fallback",
)
ELIGIBLE = {"eligible-source", "eligible-fallback"}
SOURCE_FILE = "settings/legacy-profiles.json"
FALLBACK_PATH = "legacy/startup_profiles.json"
SOURCE_ID = "Legacy Fixture"
PROFILE_ID = "legacy-fixture"
DEFINITION_LOCK = ".profile-definition.lock"
JOURNAL_LOCK = ".bootstrap-publication.lock"


class SuccessorBoundary(BaseException):
    """Stop outside the original recovery exception conversion."""


def _source_hashes(runtime: Path) -> dict[str, str]:
    return code_hashes(runtime) | {
        name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
        for name in (
            "core_runtime/active_profile_store_v4.py",
            "core_runtime/legacy_profile_successor_v4.py",
            "tobkiri_protocol/secure_persistence.py",
        )
    }


def _file_identity(path: Path) -> tuple[int, int, int, int, bytes] | None:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not path.is_file() or path.is_symlink() or info.st_size > 4 * 1024 * 1024:
        raise ValueError("unexpected successor fixture file")
    return (info.st_dev, info.st_ino, info.st_nlink, info.st_mode, path.read_bytes())


def probe(original: Path, runtime: Path, user_data: Path, output: Path) -> None:
    """Run recovery through the original locked migration with entry sentinels."""
    check_original(original)
    hashes = _source_hashes(original / "tobkiri_runtime")
    if _source_hashes(runtime) != hashes:
        raise ValueError("staged original code differs")
    sys.path.insert(0, str(runtime))
    from core_runtime import profile_definition_store_v4 as definitions
    from core_runtime.bootstrap import profile_capture as capture
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )
    from tobkiri_protocol.bundle_catalog import BundledCatalog

    for module, relative in (
        (capture, "core_runtime/bootstrap/profile_capture.py"),
        (definitions, "core_runtime/profile_definition_store_v4.py"),
    ):
        if Path(module.__file__).resolve() != runtime / relative:
            raise ValueError("probe imported another original source tree")
    install_defaultspack_profile_runtime()
    port = capture.require_profile_runtime()
    store = definitions.ProfileDefinitionStore
    if capture.ProfileDefinitionStore is not store:
        raise ValueError("recovery uses a different original definition store")
    migration = store.migrate_legacy_successors.__code__
    read = store._read_state.__code__
    snapshot = store.snapshot.__code__
    source_lookup = definitions._legacy_source_profile.__code__
    load_catalog = type(port).load_catalog.__code__
    forbidden = {
        store.import_legacy_collection.__code__,
        store._write_state.__code__,
        definitions._append_profile_successor.__code__,
    }
    sentinels = {
        name: vars(store)[name]
        for name in ("_legacy_v4_successor", "repair_legacy_display_names")
    }
    trace = []
    loaded_catalog: BundledCatalog | None = None
    instance: ProfileDefinitionStore | None = None
    registry: dict[str, Any] | None = None
    source_item: Mapping[str, Any] | None = None
    migration_return: int | None = None
    helper = None
    stage = None
    profiles = user_data / "profiles"

    def reached(event: str, definition_free: bool | None = None) -> None:
        row: dict[str, Any] = {"event": event}
        if definition_free is not None:
            row["definition_lock_free"] = _lock_free(profiles / DEFINITION_LOCK)
            row["journal_lock_free"] = _lock_free(profiles / JOURNAL_LOCK)
            if row["definition_lock_free"] is not definition_free:
                raise AssertionError(f"unexpected definition lock state at {event}")
            if row["journal_lock_free"] is not False:
                raise AssertionError(f"publication journal was unlocked at {event}")
        trace.append(row)

    def observe(frame: FrameType, event: str, arg: Any) -> None:
        nonlocal loaded_catalog, instance, registry, source_item, migration_return
        code = frame.f_code
        if event == "call" and code in forbidden:
            raise AssertionError("successor probe passed a read-only boundary")
        if event == "return" and code is load_catalog:
            if loaded_catalog is not None or not isinstance(arg, BundledCatalog):
                raise AssertionError("expected one real original loaded catalog")
            loaded_catalog = arg
            reached("catalog-loaded")
        elif event == "return" and code is snapshot:
            instance = frame.f_locals["self"]
            reached("list-snapshot-exit", True)
        elif event == "call" and code is migration:
            if (
                instance is None
                or frame.f_locals["self"] is not instance
                or instance.store_root != profiles
                or loaded_catalog is None
                or frame.f_locals["catalog"] is not loaded_catalog
            ):
                raise AssertionError("successor migration changed its store or catalog")
            reached("migration-enter", True)
        elif (
            event == "return"
            and code is read
            and frame.f_back is not None
            and frame.f_back.f_code is migration
        ):
            if frame.f_locals["self"] is not instance or not isinstance(arg, dict):
                raise AssertionError(
                    "successor migration did not read its own registry"
                )
            registry = arg
            reached("migration-registry-read", False)
        elif event == "return" and code is source_lookup:
            source_item = arg
            reached("legacy-source-lookup", False)
        elif event == "return" and code is migration:
            migration_return = arg
            # CPython reports a return event with None during exception unwind.
            reached("migration-unwind" if arg is None else "migration-return", True)

    def stop_successor(
        *,
        item: Mapping[str, Any],
        profile_id: str,
        catalog: object,
        source_path: str,
    ) -> None:
        nonlocal helper, stage
        if catalog is not loaded_catalog or registry is None:
            raise AssertionError("builder entry lost its catalog or registry")
        current = registry["profiles"][0]["revisions"][-1]["profile"]
        if item is not source_item and item is not current:
            raise AssertionError("builder entry received a different source object")
        helper = {
            "item": dict(item),
            "item_origin": "legacy-source"
            if item is source_item
            else "current-revision",
            "profile_id": profile_id,
            "source_path": source_path,
            "same_loaded_catalog": True,
        }
        stage = "_legacy_v4_successor"
        reached("successor-builder-entry", False)
        raise SuccessorBoundary

    def stop_repair(repair_store: ProfileDefinitionStore) -> None:
        nonlocal stage
        if repair_store is not instance or type(migration_return) is not int:
            raise AssertionError("repair entry lost its store or migration result")
        stage = "repair_legacy_display_names"
        reached("display-name-repair-entry", True)
        raise SuccessorBoundary

    paths = (profiles / "index.json", profiles / ".publication-key")
    before_files = filesystem(user_data)
    before_identity = [_file_identity(path) for path in paths]
    if before_identity[1] is None:
        raise AssertionError("original publication key fixture is absent")
    previous_profile = sys.getprofile()
    try:
        store._legacy_v4_successor = staticmethod(stop_successor)
        store.repair_legacy_display_names = stop_repair
        sys.setprofile(observe)
        try:
            capture._recover_bootstrap_publication(discard_superseded=True)
        except SuccessorBoundary:
            pass
        else:
            raise AssertionError("recovery passed both successor entry sentinels")
    finally:
        sys.setprofile(previous_profile)
        for name, method in sentinels.items():
            setattr(store, name, method)
    if (
        _source_hashes(runtime) != hashes
        or capture.require_profile_runtime() is not port
        or any(vars(store)[name] is not method for name, method in sentinels.items())
    ):
        raise AssertionError("original source or runtime changed")
    if loaded_catalog is None or registry is None or stage is None:
        raise AssertionError("original successor observation is incomplete")
    bundle = runtime / "ecosystem/defaultspack/v4"
    if (
        loaded_catalog.root != bundle
        or loaded_catalog.artifact_root != bundle.parent / "platform-artifacts"
    ):
        raise AssertionError(
            "successor catalog is outside the original resource fixture"
        )
    after_files = filesystem(user_data)
    locks = {f"profiles/{name}" for name in (DEFINITION_LOCK, JOURNAL_LOCK)}
    for before, after in zip(before_identity, map(_file_identity, paths), strict=True):
        if before != after:
            raise AssertionError(
                "successor interval changed a persisted file or its identity"
            )
    if [row for row in before_files["files"] if row["path"] not in locks] != [
        row for row in after_files["files"] if row["path"] not in locks
    ]:
        raise AssertionError("successor interval changed the input filesystem")
    lock_free_after = {
        "definition_lock_free": _lock_free(profiles / DEFINITION_LOCK),
        "journal_lock_free": _lock_free(profiles / JOURNAL_LOCK),
    }
    if any(value is not True for value in lock_free_after.values()):
        raise AssertionError("successor interval did not release both locks")
    catalog_type = type(loaded_catalog)
    write_json(
        output,
        {
            "expected": {"outcome": "integration-boundary", "stage": stage},
            "migration_return": migration_return,
            "helper": helper,
            "trace": trace,
            "registry": {
                "index_present": before_identity[0] is not None,
                "generation": registry["generation"],
                "profiles": [
                    {
                        "profile_id": row["profile_id"],
                        "tombstone": row["tombstone"],
                        "legacy_ids": row["legacy_ids"],
                        "profile_api_version": (
                            row["revisions"][-1]["profile"] or {}
                        ).get("profile_api_version"),
                    }
                    for row in registry["profiles"]
                ],
            },
            "catalog": {
                "type": f"{catalog_type.__module__}.{catalog_type.__qualname__}",
                "bundle_lock_sha256": hashlib.sha256(
                    (bundle / "bundle.lock.json").read_bytes()
                ).hexdigest(),
                "profile_ids": sorted(loaded_catalog.profiles),
            },
            "user_data_before": before_files,
            "user_data_after": after_files,
            "index_bytes_and_identity_unchanged": True,
            "key_bytes_and_identity_unchanged": True,
            **lock_free_after,
            "source_hashes": hashes,
        },
    )


def _seed_registry(
    store_type: type[ProfileDefinitionStore], user_data: Path, name: str
) -> None:
    index = user_data / "profiles/index.json"
    if index.exists():
        raise AssertionError("absent-index definition seed was changed")
    if name == "index-absent":
        return
    store = store_type(user_data, clock=lambda: 1000.0)
    if name == "index-empty":
        with store._locked():
            store._write_state(store._read_state())
        return
    if name == "no-legacy-id":
        store.create_profile({"profile_id": PROFILE_ID, "display_name": "Fixture"})
        return
    # Import strips the index's legacy ID. The raw source lookup compares the
    # original spelling, so surrounding spaces exercise the current revision.
    item = {
        "profile_id": f" {SOURCE_ID} " if name == "eligible-fallback" else SOURCE_ID,
        "name": "Fixture",
        "created_at": 100,
        "updated_at": 200,
    }
    if name in ("existing-v4", "existing-v5"):
        item["profile_api_version"] = (
            f"io.tobkiri.profile.{name.removeprefix('existing-')}"
        )
    collection = {"profiles": [item]}
    import_source: dict | Path = collection
    if name == "eligible-source":
        import_source = user_data / SOURCE_FILE
        import_source.parent.mkdir(mode=0o700)
        write_json(import_source, collection)
    result = store.import_legacy_collection(import_source, copy_workspaces=False)
    if result.profile_ids != (PROFILE_ID,):
        raise AssertionError("legacy fixture produced a different canonical ID")
    if name == "tombstone":
        store.delete_profile(PROFILE_ID)


def _check_observation(name: str, observed: dict, seed: Path) -> None:
    profiles = (
        []
        if name in ("index-absent", "index-empty")
        else [
            {
                "profile_id": PROFILE_ID,
                "tombstone": name == "tombstone",
                "legacy_ids": [] if name == "no-legacy-id" else [SOURCE_ID],
                "profile_api_version": {
                    "existing-v4": "io.tobkiri.profile.v4",
                    "existing-v5": "io.tobkiri.profile.v5",
                }.get(name),
            }
        ]
    )
    if observed["registry"] != {
        "index_present": name != "index-absent",
        "generation": len(profiles) + int(name == "tombstone"),
        "profiles": profiles,
    }:
        raise AssertionError((name, "successor registry fixture differs"))
    common = [
        "catalog-loaded",
        "list-snapshot-exit",
        "migration-enter",
        "migration-registry-read",
    ]
    events = [row["event"] for row in observed["trace"]]
    if name not in ELIGIBLE:
        if (
            observed["expected"]["stage"] != "repair_legacy_display_names"
            or type(observed["migration_return"]) is not int
            or observed["migration_return"] != 0
            or observed["helper"] is not None
            or events != common + ["migration-return", "display-name-repair-entry"]
        ):
            raise AssertionError((name, "original successor skip branch changed"))
        return
    if (
        observed["expected"]["stage"] != "_legacy_v4_successor"
        or observed["migration_return"] is not None
        or events
        != common
        + ["legacy-source-lookup", "successor-builder-entry", "migration-unwind"]
    ):
        raise AssertionError((name, "original successor builder entry changed"))
    index = load_json(seed / "profiles/index.json")
    if name == "eligible-source":
        expected_item = load_json(seed / SOURCE_FILE)["profiles"][0]
        origin, source_path = "legacy-source", str(seed / SOURCE_FILE)
    else:
        expected_item = index["profiles"][0]["revisions"][-1]["profile"]
        origin, source_path = "current-revision", FALLBACK_PATH
    if observed["helper"] != {
        "item": expected_item,
        "item_origin": origin,
        "profile_id": PROFILE_ID,
        "source_path": source_path,
        "same_loaded_catalog": True,
    }:
        raise AssertionError((name, "original successor builder arguments changed"))


def prepare(original: Path, fixtures: Path, output: Path) -> None:
    """Extend the independently prepared catalog and definition fixture."""
    check_original(original)
    prefix = load_json(fixtures)
    if (
        prefix.get("format") != DEFINITION_FORMAT
        or prefix.get("original_head") != ORIGINAL_HEAD
        or prefix.get("case_count") != len(DEFINITION_CASES)
        or [row["name"] for row in prefix["cases"]] != list(DEFINITION_CASES)
        or prefix["cases"][0]["expected"]
        != {"outcome": "integration-boundary", "stage": "migrate_legacy_successors"}
    ):
        raise ValueError("fresh independent definition-prefix fixtures required")
    base = fixtures.parent / "index-absent"
    base_input = load_json(base / "native/input.json")
    runtime = Path(base_input["runtime"]).resolve(strict=True)
    source = original / "tobkiri_runtime"
    if _source_hashes(runtime) != _source_hashes(source):
        raise ValueError("catalog resource source differs")
    sys.path.insert(0, str(source))
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore

    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    reports = []
    for name in CASES:
        directory = output / name
        directory.mkdir(mode=0o700)
        seed = directory / "seed-user-data"
        shutil.copytree(base / "seed-user-data", seed)
        _seed_registry(ProfileDefinitionStore, seed, name)
        for role in ("reference", "native"):
            target = directory / role
            target.mkdir(mode=0o700)
            shutil.copytree(seed, target / "user-data")
            write_json(
                target / "input.json",
                {
                    "case": name,
                    "runtime": str(runtime),
                    "user_data": str(target / "user-data"),
                    "resource_fixture_only": True,
                },
            )
        reference = directory / "reference"
        user_data = reference / "user-data"
        env = dict(
            os.environ,
            PYTHONDONTWRITEBYTECODE="1",
            RUMI_ENVIRONMENT="production",
            TOBKIRI_USER_DATA=str(user_data),
            RUMI_USER_DATA=str(user_data),
        )
        env.pop("RUMI_APP_DIR", None)
        observation = reference / "observation.json"
        subprocess.run(
            [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "probe",
                "--original-root",
                str(original),
                "--runtime",
                str(runtime),
                "--user-data",
                str(user_data),
                "--output",
                str(observation),
            ],
            env=env,
            check=True,
            timeout=180,
        )
        observed = load_json(observation)
        _check_observation(name, observed, seed)
        reports.append(
            {
                "name": name,
                "native_input": str(directory / "native/input.json"),
                **observed,
            }
        )
    check_original(original)
    write_json(
        output / "summary.json",
        {
            "format": FORMAT,
            "original_head": ORIGINAL_HEAD,
            "python": "3.13.16",
            "case_count": len(reports),
            "cases": reports,
            "native_comparison": False,
            "qualification": False,
            "production_enabled": False,
            "scope": (
                "Original recovery with controlled resource and registry fixtures "
                "through the locked successor loop to display-name repair or "
                "successor builder entry."
            ),
            "successor_builder_body_executed": False,
            "successor_written": False,
        },
    )


def main() -> None:
    """Prepare the bounded fixture or execute one original probe in CI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "probe"))
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--user-data", type=Path)
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    if args.command == "prepare":
        if args.fixtures is None:
            parser.error("prepare requires --fixtures")
        prepare(original, args.fixtures.resolve(strict=True), args.output.resolve())
    else:
        if args.runtime is None or args.user_data is None:
            parser.error("probe requires --runtime and --user-data")
        probe(
            original,
            args.runtime.resolve(strict=True),
            args.user_data.resolve(strict=True),
            args.output,
        )


if __name__ == "__main__":
    main()
